"""Replay recorded tool responses so historical incidents can be re-triaged offline.

Each case file records, per tool, a list of {"match": {...}, "response": ...} entries. The first entry
whose match values all appear (case-insensitively) in the corresponding call arguments is returned;
an empty match {} matches anything. Calls with no matching entry return an empty list, which is what
the live system returns for a query that finds nothing.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Collection

from pydantic import BaseModel, Field

from triage.config import Settings
from triage.models import Alert
from triage.tools import ToolSet
from triage.tools.catalog import ServiceCatalog
from triage.tools.datadog import DatadogTools
from triage.tools.github import GitHubTools
from triage.tools.pagerduty import PagerDutyTools
from triage.tools.servicenow import ServiceNowTools


class GroundTruth(BaseModel):
    root_cause: str
    culprit: str | None = None  # change number, deploy or SHA, when the cause was a change
    category: str = "unknown"


class EvalCase(BaseModel):
    id: str
    alert: Alert
    ground_truth: GroundTruth
    fixtures: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)

    @classmethod
    def load_dir(cls, path: Path) -> list["EvalCase"]:
        return [cls.model_validate(json.loads(p.read_text())) for p in sorted(path.glob("*.json"))]


def _matches(match: dict[str, Any], args: dict[str, Any]) -> bool:
    return all(str(v).lower() in str(args.get(k, "")).lower() for k, v in match.items())


def _catalog_repos(case: EvalCase) -> set[str]:
    return {e["response"]["repo"] for e in case.fixtures.get("lookup_service", []) if e["response"].get("repo")}


def fixture_toolset(case: EvalCase, settings: Settings | None = None, live: Collection[str] = ()) -> ToolSet:
    """Same tool names, descriptions and schemas as production, with handlers replaced by the case's recordings.

    Systems named in `live` ("servicenow", "github", "datadog") keep their real handlers and query the configured account.
    """
    settings = settings or Settings()
    unknown = set(live) - {"servicenow", "github", "datadog"}
    if unknown:
        raise ValueError(f"Cannot run live: {', '.join(sorted(unknown))}")
    snow = ServiceNowTools(settings).tools()
    github = GitHubTools(settings, _catalog_repos(case)).tools()
    datadog = DatadogTools(settings).tools()
    tools = [
        *ServiceCatalog({}).tools(),
        *datadog,
        *github,
        *snow,
        *PagerDutyTools(settings).tools(),
    ]
    live_tools = [
        *(snow if "servicenow" in live else []),
        *(github if "github" in live else []),
        *(datadog if "datadog" in live else []),
    ]
    live_handlers = {t.name: t.handler for t in live_tools}
    calls: list[dict[str, Any]] = []

    def handler_for(name: str):
        def handle(**args: Any) -> Any:
            calls.append({"tool": name, "args": args})
            if name in live_handlers:
                return live_handlers[name](**args)
            for entry in case.fixtures.get(name, []):
                if _matches(entry.get("match", {}), args):
                    return entry["response"]
            return []
        return handle

    toolset = ToolSet([dataclasses.replace(t, handler=handler_for(t.name)) for t in tools], settings.max_tool_output_chars)
    toolset.recorded_calls = calls  # type: ignore[attr-defined]
    return toolset
