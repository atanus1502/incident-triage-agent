from __future__ import annotations

import re
from typing import Any

import httpx

from triage.config import Settings
from triage.permissions import ReadPolicy, read_only_client
from triage.tools.base import TIME_RANGE, Tool, ToolError, parse_time

_NUMBER = re.compile(r"^(INC|CHG|PRB|KB)\d{5,12}$")
_TAGS = re.compile(r"<[^>]+>")


def _q(text: str) -> str:
    """Strip characters that would let a value break out of an encoded query."""
    return re.sub(r"[\^=\n\r]", " ", text).strip()[:200]


def _snow_time(value: str) -> str:
    return parse_time(value).strftime("%Y-%m-%d %H:%M:%S")


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _flatten(row: dict[str, Any]) -> dict[str, Any]:
    """Pick readable values, except dates: display values use the API user's time zone, raw values are UTC."""
    out = {}
    for k, v in row.items():
        if isinstance(v, dict):
            raw, shown = v.get("value"), v.get("display_value")
            if isinstance(raw, str) and _DATE.match(raw):
                out[k] = raw.replace(" ", "T") + "Z"
            else:
                out[k] = shown if shown not in (None, "") else raw
        else:
            out[k] = v
    return out


class ServiceNowTools:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.http = read_only_client(
            f"https://{settings.snow_instance}",
            ReadPolicy(),
            auth=(settings.snow_user, settings.snow_password),
            headers={"Accept": "application/json"},
            transport=transport,
        )

    def _table(self, table: str, query: str, fields: str, limit: int) -> list[dict[str, Any]]:
        r = self.http.get(
            f"/api/now/table/{table}",
            params={
                "sysparm_query": query,
                "sysparm_fields": fields,
                "sysparm_limit": max(1, min(limit, 25)),
                "sysparm_display_value": "all",
                "sysparm_exclude_reference_link": "true",
            },
        )
        r.raise_for_status()
        return [_flatten(row) for row in r.json().get("result", [])]

    def _text_search(self, table: str, text: str, filters: str, fields: str, limit: int) -> list[dict[str, Any]]:
        """Keyword search. ServiceNow requires every word to match, so retry with any word when that finds nothing."""
        words = [w for w in _q(text).split() if w.upper() != "OR"]
        rows = self._table(table, f"123TEXTQUERY321={' '.join(words)}{filters}", fields, limit)
        if rows or len(words) < 2:
            return rows
        rows = self._table(table, f"123TEXTQUERY321={' OR '.join(words)}{filters}", fields, limit)
        return [{**row, "match_quality": "matches only some keywords"} for row in rows]

    def search_incidents(self, text: str, days: int = 1095, ci: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        filters = f"^opened_at>=javascript:gs.daysAgoStart({max(1, min(days, 1095))})^stateIN6,7"
        if ci:
            filters += f"^cmdb_ci.name={_q(ci)}"
        rows = self._text_search(
            "incident", text, filters + "^ORDERBYDESCopened_at",
            "number,short_description,cmdb_ci,priority,opened_at,resolved_at,close_code,close_notes,problem_id", limit,
        )
        for row in rows:
            row["close_notes"] = (row.get("close_notes") or "")[:800]
        return rows

    def get_record(self, number: str) -> dict[str, Any]:
        if not _NUMBER.match(number):
            raise ToolError("number must look like INC0012345, CHG0012345, PRB0012345 or KB0012345")
        prefix = "KB" if number.startswith("KB") else number[:3]
        table = {"INC": "incident", "CHG": "change_request", "PRB": "problem", "KB": "kb_knowledge"}[prefix]
        rows = self._table(table, f"number={number}", "", 1)
        if not rows:
            raise ToolError(f"{number} not found")
        row = {k: v for k, v in rows[0].items() if v not in ("", None) and not k.startswith("sys_")}
        for k in ("description", "close_notes", "text", "justification", "implementation_plan", "backout_plan"):
            if k in row:
                row[k] = _TAGS.sub(" ", str(row[k]))[:3000]
        return row

    def list_changes(self, start: str, end: str, ci: str | None = None) -> list[dict[str, Any]]:
        window = f"start_date<={_snow_time(end)}^end_date>={_snow_time(start)}"
        query = window + (f"^cmdb_ci.name={_q(ci)}" if ci else "")
        # Also catch emergency changes that were logged without planned dates.
        query += f"^NQtype=emergency^opened_at>={_snow_time(start)}^opened_at<={_snow_time(end)}"
        if ci:
            query += f"^cmdb_ci.name={_q(ci)}"
        return self._table(
            "change_request", query + "^ORDERBYDESCstart_date",
            "number,short_description,type,state,start_date,end_date,cmdb_ci,assignment_group,requested_by", 25,
        )

    def search_knowledge(self, text: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = self._text_search("kb_knowledge", text, "^workflow_state=published", "number,short_description,text", limit)
        for row in rows:
            row["text"] = _TAGS.sub(" ", row.get("text") or "")[:2500]
        return rows

    def export_incidents(
        self, days: int, limit: int, ci: str | None, fields: tuple[str, ...], max_priority: int = 2
    ) -> list[dict[str, Any]]:
        """Resolved incidents with close notes, newest first. For building eval cases; not offered to the model."""
        query = (
            f"stateIN6,7^close_notesISNOTEMPTY^priority<={max_priority}"
            f"^opened_at>=javascript:gs.daysAgoStart({max(1, days)})"
        )
        if ci:
            query += f"^cmdb_ci.name={_q(ci)}"
        rows: list[dict[str, Any]] = []
        while len(rows) < limit:
            r = self.http.get(
                "/api/now/table/incident",
                params={
                    "sysparm_query": query + "^ORDERBYDESCopened_at",
                    "sysparm_fields": ",".join(fields),
                    "sysparm_limit": min(100, limit - len(rows)),
                    "sysparm_offset": len(rows),
                    "sysparm_display_value": "all",
                    "sysparm_exclude_reference_link": "true",
                },
            )
            r.raise_for_status()
            page = r.json().get("result", [])
            if not page:
                break
            rows += [_flatten(row) for row in page]
        return rows

    def tools(self) -> list[Tool]:
        return [
            Tool(
                name="servicenow_search_incidents",
                description=(
                    "Keyword-search resolved ServiceNow incidents (most recent first) to find similar past incidents "
                    "and how they were fixed. Optionally filter by CI name. Use a few distinctive words; if none match all of "
                    "them, results matching only some are returned and flagged."
                ),
                properties={
                    "text": {"type": "string", "description": "Keywords, e.g. 'payments timeout connection pool'"},
                    "days": {"type": "integer", "description": "Look-back window in days (default and max 1095). Keep it long: yearly recurrences such as certificate expiries are common"},
                    "ci": {"type": "string", "description": "ServiceNow CI name from lookup_service"},
                    "limit": {"type": "integer"},
                },
                required=("text",),
                handler=self.search_incidents,
            ),
            Tool(
                name="servicenow_get_record",
                description="Fetch one ServiceNow record by number: INC (incident), CHG (change), PRB (problem) or KB (knowledge article).",
                properties={"number": {"type": "string"}},
                required=("number",),
                handler=self.get_record,
            ),
            Tool(
                name="servicenow_changes",
                description=(
                    "List change requests whose implementation window overlaps the time range, plus emergency changes "
                    "opened in it. Optionally filter by CI name. Infra, DB and network changes often appear only here, not in GitHub."
                ),
                properties={**TIME_RANGE, "ci": {"type": "string"}},
                required=("start", "end"),
                handler=self.list_changes,
            ),
            Tool(
                name="servicenow_search_knowledge",
                description="Keyword-search published knowledge articles (runbooks, known errors).",
                properties={"text": {"type": "string"}, "limit": {"type": "integer"}},
                required=("text",),
                handler=self.search_knowledge,
            ),
        ]
