import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from tests.conftest import SUBMISSION
from triage.agent import TriageFailed
from triage.intake import create_app
from triage.models import Alert, TriageReport, TriageResult
from triage.slack import build_blocks


class StubAgent:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.alerts = []

    def run(self, alert):
        self.alerts.append(alert)
        if self.fail:
            raise TriageFailed("boom")
        return TriageResult(alert=alert, report=TriageReport.model_validate(SUBMISSION), model="m", tool_calls=3, elapsed_s=40)


class StubPoster:
    def __init__(self):
        self.results, self.failures = [], []

    def post_result(self, result):
        self.results.append(result)

    def post_failure(self, alert, reason):
        self.failures.append((alert.id, reason))


PD_EVENT = {"event": {"event_type": "incident.triggered", "occurred_at": "2026-04-02T14:05:00Z", "data": {
    "id": "Q1ABC", "title": "payments-api 5xx", "service": {"id": "PXXXXXX", "summary": "Payments API"},
    "urgency": "high", "html_url": "https://yourbank.pagerduty.com/incidents/Q1ABC", "created_at": "2026-04-02T14:05:00Z"}}}


def pd_post(client, payload, secret="pd-secret"):
    body = json.dumps(payload).encode()
    sig = "v1=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/pagerduty", content=body, headers={"X-PagerDuty-Signature": sig})


def test_pagerduty_trigger_is_triaged_once(settings):
    agent, poster = StubAgent(), StubPoster()
    client = TestClient(create_app(settings, agent, poster))
    assert pd_post(client, PD_EVENT).json()["status"] == "accepted"
    assert pd_post(client, PD_EVENT).json()["status"] == "duplicate"
    assert len(agent.alerts) == 1 and agent.alerts[0].details["pagerduty_service_id"] == "PXXXXXX"
    assert len(poster.results) == 1


def test_pagerduty_bad_signature_rejected(settings):
    client = TestClient(create_app(settings, StubAgent(), StubPoster()))
    assert pd_post(client, PD_EVENT, secret="wrong").status_code == 401


def test_non_trigger_events_ignored(settings):
    client = TestClient(create_app(settings, StubAgent(), StubPoster()))
    event = {"event": {**PD_EVENT["event"], "event_type": "incident.acknowledged"}}
    assert pd_post(client, event).json()["status"] == "ignored"


def test_servicenow_token_and_failure_path(settings):
    agent, poster = StubAgent(fail=True), StubPoster()
    client = TestClient(create_app(settings, agent, poster))
    payload = {"number": "INC0052210", "short_description": "Card auths timing out", "cmdb_ci": "payments-api-prod",
               "opened_at": "2026-04-09 02:17:00"}
    assert client.post("/webhooks/servicenow", json=payload, headers={"X-Triage-Token": "nope"}).status_code == 401
    assert client.post("/webhooks/servicenow", json=payload, headers={"X-Triage-Token": "snow-token"}).status_code == 202
    assert agent.alerts[0].triggered_at == datetime(2026, 4, 9, 2, 17, tzinfo=timezone.utc)
    assert poster.failures == [("INC0052210", "boom")]


def test_failed_slack_post_falls_back_to_failure_notice(settings):
    class FlakyPoster(StubPoster):
        def post_result(self, result):
            raise RuntimeError("slack down")

    poster = FlakyPoster()
    assert pd_post(TestClient(create_app(settings, StubAgent(), poster)), PD_EVENT).status_code == 202
    assert poster.failures == [("Q1ABC", "internal error")]


def test_malformed_webhooks_get_400(settings):
    client = TestClient(create_app(settings, StubAgent(), StubPoster()))
    token = {"X-Triage-Token": "snow-token"}
    for body in (b"nope", b"{}", b"[]", b'{"number": "INC1", "opened_at": "yesterday"}'):
        assert client.post("/webhooks/servicenow", headers=token, content=body).status_code == 400
    for body in (b"nope", b'{"event": {"event_type": "incident.triggered", "data": {}}}', b"[]"):
        sig = "v1=" + hmac.new(b"pd-secret", body, hashlib.sha256).hexdigest()
        assert client.post("/webhooks/pagerduty", content=body, headers={"X-PagerDuty-Signature": sig}).status_code == 400


def test_slack_feedback_is_recorded(settings):
    client = TestClient(create_app(settings, StubAgent(), StubPoster()))
    payload = {"user": {"id": "U123"}, "actions": [{"action_id": "triage_feedback_accept", "value": "Q1ABC"}]}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(b"slack-secret", b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
    headers = {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig, "Content-Type": "application/x-www-form-urlencoded"}
    assert client.post("/slack/actions", content=body, headers=headers).status_code == 200
    record = json.loads(settings.feedback_log.read_text())
    assert record["alert_id"] == "Q1ABC" and record["verdict"] == "accept" and record["user"] == "U123"


def test_slack_blocks_escape_mentions_from_model_text():
    report = TriageReport.model_validate({**SUBMISSION, "runbook_steps": ["Page <!channel> now"]})
    alert = Alert(source="pagerduty", id="Q1", title="t", triggered_at=datetime.now(timezone.utc))
    blocks = json.dumps(build_blocks(TriageResult(alert=alert, report=report, model="m", tool_calls=1, elapsed_s=1)))
    assert "<!channel>" not in blocks and "&lt;!channel&gt;" in blocks
