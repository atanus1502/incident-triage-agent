from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from triage.config import Settings


def tool_use(id: str, name: str, input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def text(t: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=t)


def response(*content, stop_reason: str = "tool_use") -> SimpleNamespace:
    return SimpleNamespace(
        content=list(content),
        stop_reason=stop_reason,
        stop_details=None,
        model="claude-sonnet-5-5",
        usage=SimpleNamespace(input_tokens=100, cache_creation_input_tokens=0, cache_read_input_tokens=0, output_tokens=50),
    )


SUBMISSION = {
    "root_cause": {
        "summary": "Deploy 9f3c2ab exhausted the DB connection pool",
        "confidence": "high",
        "evidence": ["QueuePool limit reached at 14:04:51Z"],
        "suspected_change": "9f3c2ab",
    },
    "alternatives": [],
    "runbook_steps": ["Confirm pool saturation on the payments DB dashboard"],
    "runbook_url": None,
    "similar_incidents": ["INC0039120 - rolled back"],
    "blast_radius": "Transfers in online and mobile banking",
}


class FakeClient:
    """Stands in for anthropic.Anthropic; returns scripted responses and records requests."""

    def __init__(self, responses: list[SimpleNamespace]):
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **_: Any) -> "FakeClient":
        return self

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        # Snapshot messages: the agent keeps appending to the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        service_catalog=tmp_path / "missing.yaml",
        pagerduty_webhook_secret="pd-secret",
        snow_webhook_token="snow-token",
        slack_signing_secret="slack-secret",
        feedback_log=tmp_path / "feedback.jsonl",
        audit_log=tmp_path / "audit.jsonl",
        kill_switch_file=tmp_path / "TRIAGE_DISABLED",
    )
