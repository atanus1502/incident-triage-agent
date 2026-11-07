from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from triage.permissions import WriteAttemptBlocked
from triage.redaction import redact

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    handler: Callable[..., Any]

    def schema(self) -> dict[str, Any]:
        # Not strict: the API caps total strict-schema size, and only submit_triage needs the guarantee.
        # Bad arguments here surface to the model as a tool error it can correct.
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": self.properties,
                "required": list(self.required),
                "additionalProperties": False,
            },
        }


class ToolError(Exception):
    """An error message that is safe and useful to show the model."""


class ToolSet:
    def __init__(self, tools: list[Tool], max_output_chars: int = 20_000):
        self._tools = {t.name: t for t in tools}
        self.max_output_chars = max_output_chars

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        # Sorted so the tools prefix is byte-identical across requests (prompt caching).
        return [self._tools[n].schema() for n in self.names]

    def call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool. Returns (redacted content, is_error); never raises."""
        content, is_error = self._call(name, args)
        return redact(content), is_error

    def _call(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        tool = self._tools.get(name)
        if tool is None:
            return f"Unknown tool {name!r}.", True
        try:
            result = tool.handler(**args)
        except WriteAttemptBlocked as e:
            log.error("blocked write attempt from tool %s: %s", name, e)
            return f"Blocked: {e}. This agent is read-only.", True
        except ToolError as e:
            return str(e), True
        except httpx.HTTPStatusError as e:
            return f"{e.response.status_code} from {e.request.url.host}: {e.response.text[:500]}", True
        except httpx.HTTPError as e:
            return f"Request failed ({type(e).__name__}): {e}", True
        except Exception as e:  # a broken tool should not end the investigation
            log.exception("tool %s crashed", name)
            return f"Tool crashed ({type(e).__name__}): {e}", True

        text = result if isinstance(result, str) else json.dumps(result, default=str, indent=1)
        text = redact(text)  # before truncating, so a cut can't split a value and hide it from the patterns
        if len(text) > self.max_output_chars:
            text = (
                text[: self.max_output_chars]
                + f"\n[truncated: {len(text) - self.max_output_chars} more characters; narrow the query to see them]"
            )
        return text, False


def parse_time(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise ToolError(f"Could not parse time {value!r}; use ISO 8601 like 2026-04-02T14:05:00Z") from e
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


TIME_RANGE = {
    "start": {"type": "string", "description": "ISO 8601 start time, e.g. 2026-04-02T13:30:00Z"},
    "end": {"type": "string", "description": "ISO 8601 end time"},
}
