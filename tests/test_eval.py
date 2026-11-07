import csv
from pathlib import Path

import httpx
import pytest

from tests.conftest import SUBMISSION
from triage.config import Settings
from triage.eval.export_incidents import EXPORT_FIELDS, write_csv
from triage.eval.fixtures import EvalCase, fixture_toolset
from triage.eval.grader import culprit_hit
from triage.eval.run_eval import summarise
from triage.models import TriageReport
from triage.tools.servicenow import ServiceNowTools, _flatten

CASES = Path(__file__).parent.parent / "evals" / "cases"


def test_example_cases_load():
    cases = EvalCase.load_dir(CASES)
    assert {c.id for c in cases} >= {"EXAMPLE-001", "EXAMPLE-002", "EXAMPLE-003"}


def test_fixture_replay_matches_and_records():
    case = next(c for c in EvalCase.load_dir(CASES) if c.id == "EXAMPLE-001")
    tools = fixture_toolset(case)
    content, is_error = tools.call("github_compare", {"repo": "yourbank/payments-api", "base": "51be07c", "head": "9f3c2ab41d0e"})
    assert not is_error and "transfer limits" in content
    content, _ = tools.call("github_compare", {"repo": "yourbank/payments-api", "base": "a", "head": "b"})
    assert content == "[]"
    assert [c["tool"] for c in tools.recorded_calls] == ["github_compare", "github_compare"]


def test_fixture_toolset_exposes_production_schemas():
    case = EvalCase.load_dir(CASES)[0]
    names = fixture_toolset(case).names
    assert "datadog_search_logs" in names and "servicenow_changes" in names and "lookup_service" in names


def test_live_system_bypasses_only_its_own_recordings(monkeypatch):
    case = next(c for c in EvalCase.load_dir(CASES) if c.id == "EXAMPLE-001")
    monkeypatch.setattr(
        "triage.tools.servicenow.ServiceNowTools.search_knowledge",
        lambda self, text, limit=5: [{"number": "KB-LIVE"}],
    )
    tools = fixture_toolset(case, live={"servicenow"})
    assert "KB-LIVE" in tools.call("servicenow_search_knowledge", {"text": "x"})[0]
    content, _ = tools.call("github_compare", {"repo": "yourbank/payments-api", "base": "51be07c", "head": "9f3c2ab41d0e"})
    assert "transfer limits" in content


def test_live_github_is_limited_to_the_case_repo(monkeypatch):
    case = next(c for c in EvalCase.load_dir(CASES) if c.id == "EXAMPLE-001")
    tools = fixture_toolset(case, live={"github"})
    content, is_error = tools.call("github_commits", {"repo": "someone/else", "start": "2026-04-02T13:00:00Z", "end": "2026-04-02T14:00:00Z"})
    assert is_error and "not in the service catalog" in content


def test_unknown_live_system_is_rejected():
    with pytest.raises(ValueError, match="pagerduty"):
        fixture_toolset(EvalCase.load_dir(CASES)[0], live={"pagerduty"})


def test_culprit_hit():
    case = next(c for c in EvalCase.load_dir(CASES) if c.id == "EXAMPLE-001")
    assert culprit_hit(case.ground_truth, TriageReport.model_validate(SUBMISSION)) is True


def test_servicenow_dates_are_utc_and_references_readable():
    row = _flatten({
        "start_date": {"display_value": "2026-04-02 06:30:00", "value": "2026-04-02 13:30:00"},
        "cmdb_ci": {"display_value": "payments-api-prod", "value": "abc123"},
        "number": {"display_value": "CHG0051877", "value": "CHG0051877"},
    })
    assert row == {"start_date": "2026-04-02T13:30:00Z", "cmdb_ci": "payments-api-prod", "number": "CHG0051877"}


def _snow(handler):
    return ServiceNowTools(Settings(snow_instance="x.test"), transport=httpx.MockTransport(handler))


def test_incident_search_falls_back_to_any_keyword():
    queries = []

    def handler(request):
        q = request.url.params["sysparm_query"]
        queries.append(q)
        rows = [{"number": {"display_value": "INC1", "value": "INC1"}}] if " OR " in q else []
        return httpx.Response(200, json={"result": rows})

    rows = _snow(handler).search_incidents("expired certificate")
    assert len(queries) == 2 and "123TEXTQUERY321=expired certificate^" in queries[0]
    assert "123TEXTQUERY321=expired OR certificate^" in queries[1] and "stateIN6,7" in queries[1]
    assert "daysAgoStart(1095)" in queries[0]
    assert rows[0]["number"] == "INC1" and rows[0]["match_quality"] == "matches only some keywords"


def test_full_keyword_match_is_not_flagged():
    queries = []

    def handler(request):
        queries.append(request.url.params["sysparm_query"])
        return httpx.Response(200, json={"result": [{"number": {"display_value": "KB1", "value": "KB1"}}]})

    rows = _snow(handler).search_knowledge("connection pool")
    assert len(queries) == 1 and "match_quality" not in rows[0]


def test_export_pages_and_blanks_review_columns(tmp_path):
    def handler(request):
        offset = int(request.url.params["sysparm_offset"])
        rows = [{"number": {"display_value": f"INC{i}", "value": f"INC{i}"},
                 "close_notes": {"display_value": "=HYPERLINK()", "value": "=HYPERLINK()"}} for i in range(offset, offset + 100)]
        return httpx.Response(200, json={"result": rows[: int(request.url.params["sysparm_limit"])]})

    rows = _snow(handler).export_incidents(365, 150, None, EXPORT_FIELDS)
    assert len(rows) == 150 and rows[149]["number"] == "INC149"
    out = tmp_path / "export.csv"
    write_csv(rows, out)
    first = next(csv.DictReader(out.open()))
    assert first["close_notes"] == "'=HYPERLINK()" and first["confirmed_root_cause"] == ""


def test_summary_counts_failures_as_misses():
    rows = [
        {"id": "a", "category": "deploy", "status": "ok", "grade": "match", "confidence": "high", "culprit_hit": True,
         "runbook_useful": True, "elapsed_s": 50, "tool_calls": 8},
        {"id": "b", "category": "infra", "status": "ok", "grade": "miss", "confidence": "low", "culprit_hit": None,
         "runbook_useful": False, "elapsed_s": 130, "tool_calls": 12},
        {"id": "c", "category": "infra", "status": "failed", "error": "x"},
    ]
    s = summarise(rows)
    assert s["top1_accuracy"] == 0.333 and s["failed"] == 1
    assert s["high_confidence_precision"] == 1.0 and s["within_sla"] == 0.5
