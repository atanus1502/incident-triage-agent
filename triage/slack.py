"""Posting triage results to Slack, and verifying feedback button clicks.

chat.postMessage is the only write this service performs.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import httpx

from triage.config import Settings
from triage.models import Alert, Hypothesis, TriageResult
from triage.redaction import redact

CONFIDENCE_ICON = {"high": ":large_green_circle:", "medium": ":large_yellow_circle:", "low": ":white_circle:"}
FEEDBACK_ACTIONS = ("triage_feedback_accept", "triage_feedback_reject")


def esc(text: str) -> str:
    """Redact, then escape Slack control characters so model or log text can't render mentions like <!channel>."""
    return redact(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _link(target: str | None, label: str) -> str:
    """A Slack link for web addresses. Anything else, such as a knowledge article number, stays plain text."""
    if target and target.startswith(("https://", "http://")) and not any(c in target for c in "<>| "):
        return f"<{esc(target)}|{esc(label)}>"
    return f"{esc(label)} ({esc(target)})" if target else esc(label)


def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}


def _bullets(items: list[str], numbered: bool = False) -> str:
    return "\n".join(f"{i}. {esc(x)}" if numbered else f"• {esc(x)}" for i, x in enumerate(items, 1))


def _hypothesis_line(h: Hypothesis) -> str:
    change = f" (suspected change: `{esc(h.suspected_change)}`)" if h.suspected_change else ""
    return f"{CONFIDENCE_ICON[h.confidence]} *{h.confidence} confidence*: {esc(h.summary)}{change}"


def build_blocks(result: TriageResult) -> list[dict[str, Any]]:
    a, r = result.alert, result.report
    title = _link(a.url, a.title)
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": redact(f"Triage: {a.title}")[:150]}},
        _section(f"*Incident* {title} · {esc(a.service or 'unknown service')} · {esc(a.severity or '')}"),
        _section(f"*Likely root cause*\n{_hypothesis_line(r.root_cause)}"),
    ]
    if r.root_cause.evidence:
        blocks.append(_section("*Evidence*\n" + _bullets(r.root_cause.evidence)))
    if r.runbook_steps:
        link = f"\n{_link(r.runbook_url, 'Service runbook')}" if r.runbook_url else ""
        blocks.append(_section("*Suggested first steps*\n" + _bullets(r.runbook_steps, numbered=True) + link))
    if r.alternatives:
        blocks.append(_section("*Also possible*\n" + "\n".join(_hypothesis_line(h) for h in r.alternatives)))
    if r.similar_incidents:
        blocks.append(_section("*Similar past incidents*\n" + _bullets(r.similar_incidents)))
    if r.blast_radius:
        blocks.append(_section(f"*Likely impact*\n{esc(r.blast_radius)}"))
    budget_note = " · hit time budget" if result.hit_time_budget else ""
    blocks += [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": (
            f"Advisory only, from a read-only agent · {result.tool_calls} lookups in {result.elapsed_s:.0f}s"
            f"{budget_note} · {a.source} {esc(a.id)}"
        )}]},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": FEEDBACK_ACTIONS[0], "value": a.id, "style": "primary",
             "text": {"type": "plain_text", "text": "Hypothesis was right"}},
            {"type": "button", "action_id": FEEDBACK_ACTIONS[1], "value": a.id,
             "text": {"type": "plain_text", "text": "Hypothesis was wrong"}},
        ]},
    ]
    return blocks


class SlackPoster:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.channel = settings.slack_channel
        self.http = httpx.Client(
            base_url="https://slack.com/api",
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
            timeout=10.0,
            transport=transport,
        )

    def _post(self, text: str, blocks: list[dict[str, Any]] | None = None) -> None:
        r = self.http.post("/chat.postMessage", json={"channel": self.channel, "text": text, "blocks": blocks, "unfurl_links": False})
        r.raise_for_status()
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(f"Slack error: {body.get('error')}")

    def post_result(self, result: TriageResult) -> None:
        r = result.report.root_cause
        self._post(redact(f"Triage for {result.alert.title}: {r.summary} ({r.confidence} confidence)"), build_blocks(result))

    def post_failure(self, alert: Alert, reason: str) -> None:
        self._post(
            redact(f"Triage agent could not analyse {alert.title}"),
            [_section(f":warning: Triage agent could not analyse *{esc(alert.title)}* ({esc(alert.id)}): {esc(reason)}. "
                      "Proceed with normal on-call triage.")],
        )


def verify_slack_signature(secret: str, timestamp: str, body: bytes, signature: str, now: float | None = None) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        if abs((now or time.time()) - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
