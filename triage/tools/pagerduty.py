from __future__ import annotations

import re
from typing import Any

import httpx

from triage.config import Settings
from triage.permissions import ReadPolicy, read_only_client
from triage.tools.base import TIME_RANGE, Tool, ToolError, parse_time

_ID = re.compile(r"^[A-Z0-9]{5,20}$")


class PagerDutyTools:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.http = read_only_client(
            "https://api.pagerduty.com",
            ReadPolicy(),
            headers={
                "Authorization": f"Token token={settings.pagerduty_api_key}",
                "Accept": "application/vnd.pagerduty+json;version=2",
            },
            transport=transport,
        )

    def incident_alerts(self, incident_id: str) -> list[dict[str, Any]]:
        if not _ID.match(incident_id):
            raise ToolError("incident_id must be a PagerDuty id like Q1ABCDEF2GHIJK")
        r = self.http.get(f"/incidents/{incident_id}/alerts", params={"limit": 25})
        r.raise_for_status()
        return [
            {"created_at": a.get("created_at"), "status": a.get("status"), "summary": a.get("summary"),
             "details": str((a.get("body") or {}).get("details"))[:1500]}
            for a in r.json().get("alerts", [])
        ]

    def recent_incidents(self, service_id: str, start: str, end: str) -> list[dict[str, Any]]:
        if not _ID.match(service_id):
            raise ToolError("service_id must be a PagerDuty service id like PXXXXXX")
        r = self.http.get(
            "/incidents",
            params={"service_ids[]": service_id, "since": parse_time(start).isoformat(),
                    "until": parse_time(end).isoformat(), "limit": 50},
        )
        r.raise_for_status()
        return [
            {"id": i["id"], "number": i.get("incident_number"), "title": i.get("title"), "status": i.get("status"),
             "urgency": i.get("urgency"), "created_at": i.get("created_at")}
            for i in r.json().get("incidents", [])
        ]

    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="pagerduty_incident_alerts",
                description="Get the raw alerts grouped under a PagerDuty incident, including their custom details payload.",
                properties={"incident_id": {"type": "string"}},
                required=("incident_id",),
                handler=self.incident_alerts,
            ),
            Tool(
                name="pagerduty_recent_incidents",
                description="List PagerDuty incidents on a service within a time range. Use to spot flapping or related pages.",
                properties={"service_id": {"type": "string"}, **TIME_RANGE},
                required=("service_id", "start", "end"),
                handler=self.recent_incidents,
            ),
        ]
