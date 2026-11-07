from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Alert(BaseModel):
    """A new incident, normalised from PagerDuty or ServiceNow."""

    source: Literal["pagerduty", "servicenow"]
    id: str
    title: str
    service: str | None = None
    severity: str | None = None
    triggered_at: datetime
    url: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Hypothesis(BaseModel):
    summary: str
    confidence: Literal["low", "medium", "high"]
    evidence: list[str]
    suspected_change: str | None = None


class TriageReport(BaseModel):
    """What the model submits via the submit_triage tool."""

    root_cause: Hypothesis
    alternatives: list[Hypothesis]
    runbook_steps: list[str]
    runbook_url: str | None = None
    similar_incidents: list[str]
    blast_radius: str


class TriageResult(BaseModel):
    alert: Alert
    report: TriageReport
    model: str
    tool_calls: int
    elapsed_s: float
    hit_time_budget: bool = False
    tool_log: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, int] = Field(default_factory=dict)
