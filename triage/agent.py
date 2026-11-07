"""The triage loop: Claude investigates with read-only tools, then calls submit_triage."""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import anthropic
from pydantic import ValidationError

from triage.config import Settings
from triage.models import Alert, TriageReport, TriageResult
from triage.prompts import NO_SUBMIT_MESSAGE, SUBMIT_TOOL, SYSTEM_PROMPT, WRAPUP_MESSAGE, render_alert
from triage.redaction import redact
from triage.tools import ToolSet

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class RunTrace:
    """What a run did, for the audit log. Kept for failed runs too."""

    tool_log: list[dict[str, Any]] = field(default_factory=list)
    usage: Counter = field(default_factory=Counter)

    def add_usage(self, u: Any) -> None:
        self.usage.update({
            "input_tokens": u.input_tokens or 0,
            "cache_write_tokens": u.cache_creation_input_tokens or 0,
            "cache_read_tokens": u.cache_read_input_tokens or 0,
            "output_tokens": u.output_tokens or 0,
        })


class TriageFailed(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.trace = RunTrace()


class TriageAgent:
    def __init__(self, client: anthropic.Anthropic, tools: ToolSet, settings: Settings):
        self.client = client
        self.tools = tools
        self.settings = settings
        self._tool_schemas = [*tools.schemas(), SUBMIT_TOOL]
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="triage-tool")

    def run(self, alert: Alert) -> TriageResult:
        trace = RunTrace()
        try:
            return self._investigate(alert, trace)
        except TriageFailed as e:
            e.trace = trace
            raise

    def _investigate(self, alert: Alert, trace: RunTrace) -> TriageResult:
        s = self.settings
        started = time.monotonic()
        deadline = started + s.time_budget_s
        messages: list[dict[str, Any]] = [{"role": "user", "content": render_alert(alert)}]
        tool_calls = 0
        wrapped_up = False

        for turn in range(s.max_turns):
            response = self._call_model(messages, deadline)
            u = response.usage
            trace.add_usage(u)
            log.info(
                "alert=%s turn=%d stop=%s in=%s cache_write=%s cache_read=%s out=%s",
                alert.id, turn, response.stop_reason, u.input_tokens, u.cache_creation_input_tokens,
                u.cache_read_input_tokens, u.output_tokens,
            )

            if response.stop_reason == "refusal":
                raise TriageFailed(f"model declined to triage ({response.stop_details})")
            if response.stop_reason == "max_tokens":
                raise TriageFailed("model output hit max_tokens")

            # Append the full content (thinking blocks included) so history stays append-only.
            messages.append({"role": "assistant", "content": response.content})
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            submit = next((b for b in tool_uses if b.name == "submit_triage"), None)
            if submit is not None:
                try:
                    report = TriageReport.model_validate(submit.input)
                except ValidationError as e:
                    # Strict tools make this unlikely; let the model fix it rather than failing the run.
                    messages.append({"role": "user", "content": [
                        _result(b.id, f"Invalid submission: {e}" if b is submit else "Not run.", True) for b in tool_uses
                    ]})
                    continue
                return TriageResult(
                    alert=alert,
                    report=report,
                    model=response.model,
                    tool_calls=tool_calls,
                    elapsed_s=round(time.monotonic() - started, 1),
                    hit_time_budget=wrapped_up,
                    tool_log=trace.tool_log,
                    usage=dict(trace.usage),
                )

            if not tool_uses:
                messages.append({"role": "user", "content": NO_SUBMIT_MESSAGE})
                continue

            # Run investigation tools concurrently; all results go back in one user message.
            outputs = list(self._pool.map(lambda b: self.tools.call(b.name, b.input), tool_uses))
            tool_calls += len(tool_uses)
            trace.tool_log += [
                {"tool": b.name, "args": redact(json.dumps(b.input, default=str)), "is_error": is_error, "output_chars": len(content)}
                for b, (content, is_error) in zip(tool_uses, outputs)
            ]
            messages.append({"role": "user", "content": [
                _result(b.id, content, is_error) for b, (content, is_error) in zip(tool_uses, outputs)
            ]})

            out_of_time = time.monotonic() > deadline - s.wrapup_s
            last_turns = turn >= s.max_turns - 2
            if not wrapped_up and (out_of_time or last_turns):
                # Mid-conversation system message: operator-level instruction that keeps the cached prefix.
                messages.append({"role": "system", "content": WRAPUP_MESSAGE})
                wrapped_up = True

        raise TriageFailed(f"no submission after {s.max_turns} turns")

    def _call_model(self, messages: list[dict[str, Any]], deadline: float):
        remaining = max(15.0, deadline - time.monotonic() + 20.0)
        try:
            return self.client.with_options(timeout=remaining).beta.messages.create(
                model=self.settings.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                tools=self._tool_schemas,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": self.settings.effort},
                cache_control={"type": "ephemeral"},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.BadRequestError as e:
            raise TriageFailed(f"bad request to Claude API: {e.message}") from e
        except anthropic.RateLimitError as e:
            raise TriageFailed("rate limited by Claude API after retries") from e
        except anthropic.APIStatusError as e:
            raise TriageFailed(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise TriageFailed(f"could not reach Claude API: {e}") from e


def _result(tool_use_id: str, content: str, is_error: bool) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}
    if is_error:
        block["is_error"] = True
    return block
