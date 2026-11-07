from dataclasses import replace
from datetime import datetime, timezone

import pytest

from tests.conftest import SUBMISSION, FakeClient, response, text, tool_use
from triage.agent import TriageAgent, TriageFailed
from triage.models import Alert
from triage.prompts import NO_SUBMIT_MESSAGE, WRAPUP_MESSAGE
from triage.tools.base import Tool, ToolSet

ALERT = Alert(
    source="pagerduty", id="Q1ABC", title="payments-api 5xx above 5%", service="Payments API",
    triggered_at=datetime(2026, 4, 2, 14, 5, tzinfo=timezone.utc),
)


def toolset(calls: list):
    def lookup(name: str):
        calls.append(name)
        return {"service": "payments-api"}
    return ToolSet([Tool("lookup_service", "", {"name": {"type": "string"}}, ("name",), lookup)])


def test_investigates_then_submits(settings):
    calls: list = []
    client = FakeClient([
        response(tool_use("t1", "lookup_service", {"name": "Payments API"}), tool_use("t2", "lookup_service", {"name": "x"})),
        response(tool_use("t3", "submit_triage", SUBMISSION)),
    ])
    result = TriageAgent(client, toolset(calls), settings).run(ALERT)

    assert result.report.root_cause.suspected_change == "9f3c2ab"
    assert result.tool_calls == 2 and calls == ["Payments API", "x"]
    # Both tool results go back in a single user message, in order.
    second = client.requests[1]["messages"]
    assert [b["tool_use_id"] for b in second[-1]["content"]] == ["t1", "t2"]
    # Request shape: adaptive thinking, only submit_triage strict, fallbacks on.
    req = client.requests[0]
    assert req["thinking"] == {"type": "adaptive"} and req["fallbacks"] == "default"
    assert {t["name"] for t in req["tools"]} == {"lookup_service", "submit_triage"}
    assert [t["name"] for t in req["tools"] if t.get("strict")] == ["submit_triage"]


def test_nudges_when_model_stops_without_submitting(settings):
    client = FakeClient([response(text("I think it's the DB."), stop_reason="end_turn"),
                         response(tool_use("t1", "submit_triage", SUBMISSION))])
    TriageAgent(client, toolset([]), settings).run(ALERT)
    assert client.requests[1]["messages"][-1] == {"role": "user", "content": NO_SUBMIT_MESSAGE}


def test_wrapup_system_message_when_time_is_short(settings):
    settings = replace(settings, time_budget_s=10, wrapup_s=30)  # already inside the wrap-up window
    client = FakeClient([response(tool_use("t1", "lookup_service", {"name": "a"})),
                         response(tool_use("t2", "submit_triage", SUBMISSION))])
    result = TriageAgent(client, toolset([]), settings).run(ALERT)
    assert client.requests[1]["messages"][-1] == {"role": "system", "content": WRAPUP_MESSAGE}
    assert result.hit_time_budget


def test_invalid_submission_is_returned_to_model(settings):
    bad = {**SUBMISSION, "root_cause": {**SUBMISSION["root_cause"], "confidence": "certain"}}
    client = FakeClient([response(tool_use("t1", "submit_triage", bad)),
                         response(tool_use("t2", "submit_triage", SUBMISSION))])
    TriageAgent(client, toolset([]), settings).run(ALERT)
    err = client.requests[1]["messages"][-1]["content"][0]
    assert err["is_error"] and "Invalid submission" in err["content"]


def test_refusal_fails_cleanly(settings):
    client = FakeClient([response(stop_reason="refusal")])
    with pytest.raises(TriageFailed, match="declined"):
        TriageAgent(client, toolset([]), settings).run(ALERT)


def test_gives_up_after_max_turns(settings):
    settings = replace(settings, max_turns=3)
    client = FakeClient([response(tool_use(f"t{i}", "lookup_service", {"name": "a"})) for i in range(3)])
    with pytest.raises(TriageFailed, match="no submission"):
        TriageAgent(client, toolset([]), settings).run(ALERT)
