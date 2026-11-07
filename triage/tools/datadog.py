from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from triage.config import Settings
from triage.permissions import ReadPolicy, read_only_client
from triage.tools.base import TIME_RANGE, Tool, parse_time

# Datadog's search endpoints are POSTs that only read.
DATADOG_POLICY = ReadPolicy(read_only_posts=(r"/api/v2/logs/events/search", r"/api/v2/spans/events/search"))


class DatadogTools:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.http = read_only_client(
            f"https://api.{settings.dd_site}",
            DATADOG_POLICY,
            headers={"DD-API-KEY": settings.dd_api_key, "DD-APPLICATION-KEY": settings.dd_app_key},
            transport=transport,
        )

    def search_logs(self, query: str, start: str, end: str, limit: int = 50) -> list[dict[str, Any]]:
        r = self.http.post(
            "/api/v2/logs/events/search",
            json={
                "filter": {"query": query, "from": parse_time(start).isoformat(), "to": parse_time(end).isoformat()},
                "page": {"limit": max(1, min(limit, 200))},
                "sort": "-timestamp",
            },
        )
        r.raise_for_status()
        out = []
        for event in r.json().get("data", []):
            a = event.get("attributes", {})
            out.append({
                "timestamp": a.get("timestamp"),
                "service": a.get("service"),
                "status": a.get("status"),
                "host": a.get("host"),
                "message": (a.get("message") or "")[:600],
            })
        return out

    def query_metrics(self, query: str, start: str, end: str) -> list[dict[str, Any]]:
        r = self.http.get(
            "/api/v1/query",
            params={"query": query, "from": int(parse_time(start).timestamp()), "to": int(parse_time(end).timestamp())},
        )
        r.raise_for_status()
        out = []
        for s in r.json().get("series", []):
            points = [p for p in s.get("pointlist", []) if p[1] is not None]
            step = max(1, len(points) // 40)  # ~40 points is enough to see a step change
            out.append({
                "scope": s.get("scope"),
                "unit": (s.get("unit") or [{}])[0] if s.get("unit") else None,
                "points": [[_iso_ms(t), round(v, 4)] for t, v in points[::step]],
            })
        return out

    def list_alerting_monitors(self, tags: str) -> list[dict[str, Any]]:
        r = self.http.get("/api/v1/monitor", params={"monitor_tags": tags, "group_states": "alert,warn"})
        r.raise_for_status()
        return [
            {"name": m.get("name"), "state": m.get("overall_state"), "query": m.get("query"), "tags": m.get("tags")}
            for m in r.json()
            if m.get("overall_state") in ("Alert", "Warn")
        ]

    def list_events(self, tags: str, start: str, end: str) -> list[dict[str, Any]]:
        r = self.http.get(
            "/api/v1/events",
            params={"tags": tags, "start": int(parse_time(start).timestamp()), "end": int(parse_time(end).timestamp())},
        )
        r.raise_for_status()
        return [
            {"date": _iso_ms(e.get("date_happened", 0) * 1000), "title": e.get("title"), "text": (e.get("text") or "")[:400],
             "source": e.get("source_type_name"), "tags": e.get("tags")}
            for e in r.json().get("events", [])
        ]

    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="datadog_search_logs",
                description=(
                    "Search Datadog logs. Use Datadog log query syntax, e.g. "
                    "'service:payments-api env:prod status:error'. Returns newest first."
                ),
                properties={
                    "query": {"type": "string"},
                    **TIME_RANGE,
                    "limit": {"type": "integer", "description": "Max events, default 50, max 200"},
                },
                required=("query", "start", "end"),
                handler=self.search_logs,
            ),
            Tool(
                name="datadog_query_metrics",
                description=(
                    "Query a Datadog metric timeseries, e.g. "
                    "'sum:trace.http.request.errors{service:payments-api,env:prod}.as_count()' or "
                    "'avg:postgresql.connections{db:payments}'. Returns ~40 downsampled points per series."
                ),
                properties={"query": {"type": "string"}, **TIME_RANGE},
                required=("query", "start", "end"),
                handler=self.query_metrics,
            ),
            Tool(
                name="datadog_alerting_monitors",
                description="List Datadog monitors currently in Alert or Warn for the given monitor tags, e.g. 'service:payments-api'.",
                properties={"tags": {"type": "string"}},
                required=("tags",),
                handler=self.list_alerting_monitors,
            ),
            Tool(
                name="datadog_events",
                description=(
                    "List Datadog events (deploy markers, config changes, autoscaling, feature-flag flips) "
                    "for comma-separated tags, e.g. 'service:payments-api,env:prod'."
                ),
                properties={"tags": {"type": "string"}, **TIME_RANGE},
                required=("tags", "start", "end"),
                handler=self.list_events,
            ),
        ]


def _iso_ms(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
