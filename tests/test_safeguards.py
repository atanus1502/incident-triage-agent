import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import SUBMISSION, FakeClient, response, tool_use
from tests.test_intake import PD_EVENT, StubAgent, StubPoster, pd_post
from triage.agent import TriageAgent
from triage.intake import create_app
from triage.models import Alert, TriageReport, TriageResult
from triage.prompts import render_alert
from triage.redaction import redact
from triage.safeguards import AuditLog, TriageLimiter, estimate_cost_usd
from triage.slack import build_blocks
from triage.tools.base import Tool, ToolSet

ALERT = Alert(source="pagerduty", id="Q1", title="t", triggered_at=datetime(2026, 4, 2, tzinfo=timezone.utc))


@pytest.mark.parametrize("text,label", [
    ("card 4111 1111 1111 1111 declined", "CARD"),
    ("pan=5500-0000-0000-0004", "CARD"),
    ("from jane.doe@example.com", "EMAIL"),
    ("ssn 123-45-6789", "SSN"),
    ("iban GB82WEST12345698765432", "ACCOUNT"),
    ("account number: 12345678 failed", "ACCOUNT"),
    ("call +14155552671 now", "PHONE"),
    ("Authorization: Bearer abc123def456ghi", "SECRET"),
    ("password=hunter2 rejected", "SECRET"),
    ("key sk-ant-api03-abcDEF", "SECRET"),
])
def test_redacts_sensitive_values(text, label):
    assert f"[REDACTED-{label}]" in redact(text)


@pytest.mark.parametrize("text", [
    "2026-04-02 14:05:00 QueuePool limit of size 40 overflow 10 reached",
    "commit 9f3c2ab41d0e deployed by deploy-bot",
    "host=10.42.18.21:8443 ConnectTimeout after 3000ms",
    "timestamp 1794017421000 duration_ms=5012",
    "order 4111111111111112 is not a valid card number",
    "account locked after three attempts",
    "CHG0052190 INC0031544 Q0K3JFRDZMA77J",
])
def test_leaves_diagnostic_text_alone(text):
    assert redact(text) == text


def test_tool_output_and_errors_are_redacted():
    def boom():
        raise RuntimeError("failed for jane@example.com")

    tools = ToolSet([Tool("logs", "", {}, (), lambda: "card 4111111111111111"), Tool("boom", "", {}, (), boom)])
    assert "4111" not in tools.call("logs", {})[0]
    assert "jane@" not in tools.call("boom", {})[0]


def test_redaction_runs_before_truncation():
    tools = ToolSet([Tool("logs", "", {}, (), lambda: "x" * 5 + " jane@example.com")], max_output_chars=12)
    assert "jane" not in tools.call("logs", {})[0]


def test_alert_is_redacted_before_reaching_the_model():
    alert = ALERT.model_copy(update={"title": "Refund failed for jane@example.com", "details": {"note": "card 4111111111111111"}})
    rendered = render_alert(alert)
    assert "jane@" not in rendered and "4111" not in rendered


def test_slack_post_is_redacted():
    report = TriageReport.model_validate({**SUBMISSION, "runbook_steps": ["Email jane@example.com"]})
    alert = ALERT.model_copy(update={"title": "Failure for bob@example.com"})
    blocks = json.dumps(build_blocks(TriageResult(alert=alert, report=report, model="m", tool_calls=1, elapsed_s=1)))
    assert "jane@" not in blocks and "bob@" not in blocks


@pytest.mark.parametrize("target,expected", [
    ("https://wiki.example.com/runbook", "<https://wiki.example.com/runbook|Service runbook>"),
    ("KB0010004", "Service runbook (KB0010004)"),
    ("https://evil.example.com|click here", "Service runbook (https://evil.example.com|click here)"),
])
def test_runbook_links_only_for_web_addresses(target, expected):
    report = TriageReport.model_validate({**SUBMISSION, "runbook_url": target})
    blocks = build_blocks(TriageResult(alert=ALERT, report=report, model="m", tool_calls=1, elapsed_s=1))
    assert expected in json.dumps(blocks, ensure_ascii=False)


def test_limiter_enforces_in_flight_and_daily_caps():
    limiter = TriageLimiter(max_concurrent=1, max_in_flight=2, max_per_day=3)
    assert limiter.admit() is None and limiter.admit() is None
    assert "in progress" in limiter.admit()
    with limiter.slot():
        pass
    assert limiter.admit() is None
    with limiter.slot():
        pass
    with limiter.slot():
        pass
    assert "daily limit" in limiter.admit()


def test_limiter_runs_one_at_a_time_when_configured():
    limiter = TriageLimiter(max_concurrent=1, max_in_flight=5, max_per_day=5)
    running, peak, lock = [0], [0], threading.Lock()

    def work():
        limiter.admit()
        with limiter.slot():
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            threading.Event().wait(0.02)
            with lock:
                running[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert peak[0] == 1


def test_refusal_notices_are_rate_limited():
    limiter = TriageLimiter(1, 1, 1, notice_interval_s=600)
    assert limiter.should_notify() is True and limiter.should_notify() is False


def test_cost_estimate():
    usage = {"input_tokens": 10, "cache_write_tokens": 7629, "cache_read_tokens": 16937, "output_tokens": 2128}
    assert estimate_cost_usd("claude-sonnet-5-5", usage) == 0.0438
    assert estimate_cost_usd("some-other-model", usage) is None


def _audit(settings):
    return [json.loads(line) for line in settings.audit_log.read_text().splitlines()]


def test_kill_switch_stops_triage_without_restart(settings):
    agent, poster = StubAgent(), StubPoster()
    client = TestClient(create_app(settings, agent, poster))
    settings.kill_switch_file.touch()
    assert pd_post(client, PD_EVENT).json()["status"] == "disabled"
    assert agent.alerts == [] and poster.results == [] and poster.failures == []
    settings.kill_switch_file.unlink()
    assert pd_post(client, PD_EVENT).json()["status"] == "accepted"  # the disabled delivery didn't count as a duplicate
    assert len(agent.alerts) == 1
    assert [r["status"] for r in _audit(settings)] == ["disabled", "ok"]


def test_over_limit_incident_is_refused_and_notified_once(settings):
    from dataclasses import replace

    settings = replace(settings, max_triages_per_day=1)
    agent, poster = StubAgent(), StubPoster()
    client = TestClient(create_app(settings, agent, poster))
    assert pd_post(client, PD_EVENT).json()["status"] == "accepted"
    for incident in ("Q2", "Q3"):
        event = {"event": {**PD_EVENT["event"], "data": {**PD_EVENT["event"]["data"], "id": incident}}}
        assert pd_post(client, event).json()["status"] == "refused"
    assert len(agent.alerts) == 1 and len(poster.failures) == 1 and "daily limit" in poster.failures[0][1]


def test_audit_records_successful_run_with_trace(settings):
    def lookup(name):
        return "owner jane@example.com"

    client_fake = FakeClient([
        response(tool_use("t1", "lookup_service", {"name": "bob@example.com"})),
        response(tool_use("t2", "submit_triage", SUBMISSION)),
    ])
    agent = TriageAgent(client_fake, ToolSet([Tool("lookup_service", "", {"name": {"type": "string"}}, ("name",), lookup)]), settings)
    client = TestClient(create_app(settings, agent, StubPoster()))
    pd_post(client, PD_EVENT)
    record = _audit(settings)[0]
    assert record["status"] == "ok" and record["posted"] is True and record["prompt_version"]
    assert record["tool_log"][0]["tool"] == "lookup_service" and "bob@" not in record["tool_log"][0]["args"]
    assert record["estimated_cost_usd"] == 0.0014  # two turns: 200 input and 100 output tokens at Sonnet prices
    assert record["usage"]["output_tokens"] == 100 and record["report"]["root_cause"]["suspected_change"] == "9f3c2ab"


def test_audit_records_failed_run_with_partial_trace(settings):
    def lookup(name):
        return "ok"

    fake = FakeClient([response(tool_use("t1", "lookup_service", {"name": "a"})), response(stop_reason="refusal")])
    agent = TriageAgent(fake, ToolSet([Tool("lookup_service", "", {"name": {"type": "string"}}, ("name",), lookup)]), settings)
    poster = StubPoster()
    pd_post(TestClient(create_app(settings, agent, poster)), PD_EVENT)
    record = _audit(settings)[0]
    assert record["status"] == "failed" and "declined" in record["error"] and len(record["tool_log"]) == 1
    assert poster.failures


def test_audit_failure_does_not_stop_triage(settings, tmp_path):
    from dataclasses import replace

    blocker = tmp_path / "a_file"
    blocker.write_text("")
    settings = replace(settings, audit_log=blocker / "audit.jsonl")
    poster = StubPoster()
    pd_post(TestClient(create_app(settings, StubAgent(), poster)), PD_EVENT)
    assert len(poster.results) == 1
