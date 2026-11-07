from __future__ import annotations

from triage.config import Settings
from triage.tools.base import Tool, ToolSet
from triage.tools.catalog import ServiceCatalog
from triage.tools.datadog import DatadogTools
from triage.tools.github import GitHubTools
from triage.tools.pagerduty import PagerDutyTools
from triage.tools.servicenow import ServiceNowTools


def build_toolset(settings: Settings) -> ToolSet:
    """All read-only investigation tools, wired to live systems."""
    catalog = ServiceCatalog.load(settings.service_catalog)
    tools: list[Tool] = [
        *catalog.tools(),
        *DatadogTools(settings).tools(),
        *GitHubTools(settings, catalog.repos).tools(),
        *ServiceNowTools(settings).tools(),
        *PagerDutyTools(settings).tools(),
    ]
    return ToolSet(tools, settings.max_tool_output_chars)


__all__ = ["Tool", "ToolSet", "build_toolset"]
