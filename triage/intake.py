"""Webhook service: receives new incidents, triages them in the background, posts to Slack."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from triage.agent import TriageAgent, TriageFailed
from triage.config import Settings
from triage.models import Alert
from triage.prompts import SYSTEM_PROMPT
from triage.redaction import redact
from triage.safeguards import AuditLog, TriageLimiter, estimate_cost_usd
from triage.slack import FEEDBACK_ACTIONS, SlackPoster, verify_slack_signature

log = logging.getLogger(__name__)


class Deduper:
    """Drops repeat deliveries of the same incident (webhook retries, re-triggers)."""

    def __init__(self, ttl_s: float = 3600):
        self.ttl_s = ttl_s
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def first_time(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            self._seen = {k: t for k, t in self._seen.items() if now - t < self.ttl_s}
            if key in self._seen:
                return False
            self._seen[key] = now
            return True


def verify_pagerduty_signature(secret: str, body: bytes, header: str) -> bool:
    if not secret or not header:
        return False
    expected = "v1=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig.strip()) for sig in header.split(","))


def alert_from_pagerduty(payload: dict[str, Any]) -> Alert | None:
    event = payload.get("event") or {}
    if event.get("event_type") != "incident.triggered":
        return None
    d = event.get("data") or {}
    return Alert(
        source="pagerduty",
        id=d["id"],
        title=d.get("title") or "(untitled incident)",
        service=(d.get("service") or {}).get("summary"),
        severity=((d.get("priority") or {}).get("summary")) or d.get("urgency"),
        triggered_at=d.get("created_at") or event.get("occurred_at") or datetime.now(timezone.utc),
        url=d.get("html_url"),
        details={"pagerduty_service_id": (d.get("service") or {}).get("id"), "incident_number": d.get("number")},
    )


def alert_from_servicenow(payload: dict[str, Any]) -> Alert:
    opened = payload.get("opened_at")
    triggered = (
        datetime.strptime(opened, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if opened else datetime.now(timezone.utc)
    )
    return Alert(
        source="servicenow",
        id=payload["number"],
        title=payload.get("short_description") or "(no short description)",
        service=payload.get("cmdb_ci"),
        severity=payload.get("priority"),
        triggered_at=triggered,
        url=payload.get("url"),
        details={k: payload[k] for k in ("description", "category", "assignment_group") if payload.get(k)},
    )


def _parse(build: Callable[[], Alert | None]) -> Alert | None:
    """A malformed payload is the sender's error: answer 400 so webhook systems don't retry it as a server fault."""
    try:
        return build()
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise HTTPException(400, "malformed payload") from e


def create_app(settings: Settings, agent: TriageAgent, poster: SlackPoster) -> FastAPI:
    app = FastAPI(title="Incident triage agent", docs_url=None, redoc_url=None, openapi_url=None)
    dedupe = Deduper()
    feedback_lock = threading.Lock()
    limiter = TriageLimiter(settings.max_concurrent_triages, settings.max_queued_triages, settings.max_triages_per_day)
    audit = AuditLog(settings.audit_log)
    prompt_version = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12]

    def audit_record(alert: Alert, status: str, **extra: Any) -> dict[str, Any]:
        return {
            "at": datetime.now(timezone.utc).isoformat(), "alert_id": alert.id, "source": alert.source,
            "title": redact(alert.title), "status": status, "configured_model": settings.model,
            "prompt_version": prompt_version, **extra,
        }

    def triage(alert: Alert) -> None:
        record = audit_record(alert, "crashed", posted=False)
        try:
            with limiter.slot():
                result = agent.run(alert)
            log.info("triaged %s in %.1fs with %d tool calls", alert.id, result.elapsed_s, result.tool_calls)
            record.update(
                status="ok", model=result.model, elapsed_s=result.elapsed_s, hit_time_budget=result.hit_time_budget,
                tool_log=result.tool_log, usage=result.usage,
                estimated_cost_usd=estimate_cost_usd(result.model, result.usage),
                report=json.loads(redact(result.report.model_dump_json())),
            )
            poster.post_result(result)
            record["posted"] = True
        except TriageFailed as e:
            log.warning("triage failed for %s: %s", alert.id, e)
            usage = dict(e.trace.usage)
            record.update(
                status="failed", error=redact(str(e)), tool_log=e.trace.tool_log, usage=usage,
                estimated_cost_usd=estimate_cost_usd(settings.model, usage),
            )
            _notify_failure(alert, str(e))
        except Exception:
            log.exception("triage crashed for %s", alert.id)
            _notify_failure(alert, "internal error")
        finally:
            audit.write(record)

    def _notify_failure(alert: Alert, reason: str) -> None:
        try:
            poster.post_failure(alert, reason)
        except Exception:
            log.exception("could not tell Slack that triage failed for %s", alert.id)

    def enqueue(alert: Alert, tasks: BackgroundTasks) -> dict[str, str]:
        if settings.kill_switch_file.exists():
            log.warning("kill switch on: not triaging %s", alert.id)
            audit.write(audit_record(alert, "disabled"))
            return {"status": "disabled"}
        if not dedupe.first_time(f"{alert.source}:{alert.id}"):
            return {"status": "duplicate"}
        refused = limiter.admit()
        if refused:
            log.warning("not triaging %s: %s", alert.id, refused)
            audit.write(audit_record(alert, "refused", error=refused))
            if limiter.should_notify():
                tasks.add_task(_notify_failure, alert, f"{refused}, and further incidents may be skipped")
            return {"status": "refused"}
        tasks.add_task(triage, alert)
        return {"status": "accepted", "id": alert.id}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/pagerduty", status_code=202)
    async def pagerduty(request: Request, tasks: BackgroundTasks) -> dict[str, str]:
        body = await request.body()
        if not verify_pagerduty_signature(settings.pagerduty_webhook_secret, body, request.headers.get("x-pagerduty-signature", "")):
            raise HTTPException(401, "bad signature")
        alert = _parse(lambda: alert_from_pagerduty(json.loads(body)))
        if alert is None:
            return {"status": "ignored"}
        return enqueue(alert, tasks)

    @app.post("/webhooks/servicenow", status_code=202)
    async def servicenow(request: Request, tasks: BackgroundTasks) -> dict[str, str]:
        token = request.headers.get("x-triage-token", "")
        if not settings.snow_webhook_token or not hmac.compare_digest(token, settings.snow_webhook_token):
            raise HTTPException(401, "bad token")
        body = await request.body()
        return enqueue(_parse(lambda: alert_from_servicenow(json.loads(body))), tasks)

    @app.post("/slack/actions")
    async def slack_actions(request: Request) -> dict[str, Any]:
        body = await request.body()
        if not verify_slack_signature(
            settings.slack_signing_secret,
            request.headers.get("x-slack-request-timestamp", ""),
            body,
            request.headers.get("x-slack-signature", ""),
        ):
            raise HTTPException(401, "bad signature")
        form = await request.form()
        payload = json.loads(str(form.get("payload", "{}")))
        for action in payload.get("actions", []):
            if action.get("action_id") not in FEEDBACK_ACTIONS:
                continue
            record = {
                "at": datetime.now(timezone.utc).isoformat(),
                "alert_id": action.get("value"),
                "verdict": "accept" if action["action_id"] == FEEDBACK_ACTIONS[0] else "reject",
                "user": (payload.get("user") or {}).get("id"),
            }
            with feedback_lock:
                settings.feedback_log.parent.mkdir(parents=True, exist_ok=True)
                with settings.feedback_log.open("a") as f:
                    f.write(json.dumps(record) + "\n")
        return {}

    return app


def main() -> None:
    import anthropic
    import uvicorn

    from triage.tools import build_toolset

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # At INFO these log every request URL, including ServiceNow and Datadog query text.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    settings = Settings.from_env()
    app = create_app(
        settings,
        TriageAgent(anthropic.Anthropic(max_retries=2), build_toolset(settings), settings),
        SlackPoster(settings),
    )
    # Local only by default; set TRIAGE_HOST=0.0.0.0 when running behind your ingress.
    uvicorn.run(app, host=os.environ.get("TRIAGE_HOST", "127.0.0.1"), port=int(os.environ.get("TRIAGE_PORT", "8080")))


if __name__ == "__main__":
    main()
