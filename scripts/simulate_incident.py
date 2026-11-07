"""Sends a synthetic connection-pool incident to a Datadog test account, in real time.

Usage: simulate_incident.py <deploy-epoch-seconds> <minutes> <state-file>
Datadog only accepts recent metrics, so create a fresh sandbox deployment first and run this straight after.
Uses the Datadog API key only (ingest); this script is setup, not part of the agent.
"""
import json
import os
import random
import sys
import time

import httpx

deploy_at, minutes, state_file = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
site, key = os.environ["DD_SITE"], os.environ["DD_API_KEY"]
headers = {"DD-API-KEY": key, "Content-Type": "application/json"}
api = httpx.Client(base_url=f"https://api.{site}", headers=headers, timeout=20)
logs = httpx.Client(base_url=f"https://http-intake.logs.{site}", headers=headers, timeout=20)
random.seed(7)
TAGS = ["service:payments-api", "env:prod", "sandbox:true"]


def metrics(ts: int, errors: float, conns: float, requests: float) -> None:
    def series(name, kind, value, tags):
        return {"metric": name, "type": kind, "interval": 60, "points": [{"timestamp": ts, "value": value}], "tags": tags}

    r = api.post("/api/v2/series", json={"series": [
        series("trace.http.request.errors", 1, errors, TAGS),
        series("trace.http.request.hits", 1, requests, TAGS),
        series("postgresql.connections", 3, conns, ["db:payments", "env:prod", "sandbox:true"]),
    ]})
    r.raise_for_status()


def log(status: str, message: str, service: str = "payments-api", host: str = "pay-7c9f-1") -> None:
    r = logs.post("/api/v2/logs", json=[{
        "ddsource": "triage-sandbox", "ddtags": "env:prod,sandbox:true", "hostname": host,
        "service": service, "status": status, "message": message,
    }])
    r.raise_for_status()


def event(title: str, text: str, at: int, tags: list[str]) -> None:
    api.post("/api/v1/events", json={"title": title, "text": text, "date_happened": at, "tags": tags}).raise_for_status()


# Healthy baseline before the deploy, plus an unrelated event as a distractor.
for t in range(deploy_at - 2400, deploy_at, 60):
    metrics(t, random.randint(0, 4), random.randint(55, 70), random.randint(900, 1100))
event("Autoscaling: payments-api replicas 6 -> 8", "Scheduled scale-up", deploy_at - 1500, TAGS)
event("Deployed payments-api Release 2026.1-3", "sha 83a3b63 to production", deploy_at, TAGS)
print("baseline sent", flush=True)

end = deploy_at + minutes * 60
while time.time() < end:
    now = int(time.time())
    p = (now - deploy_at) / (minutes * 60)  # 0..1 through the incident
    metrics(now, 4 + 1500 * p ** 3, min(200, 65 + 170 * p), 1000)
    host = f"pay-7c9f-{random.randint(1, 4)}"
    if p < 0.3:
        log("warn", f"slow request POST /v2/transfers duration_ms={int(900 + 4000 * p)} db_queries={random.randint(20, 45)}", host=host)
    else:
        log("error", "sqlalchemy.exc.TimeoutError: QueuePool limit of size 40 overflow 10 reached, connection timed out, timeout 5.00", host=host)
        log("error", "POST /v2/transfers 503 upstream_timeout duration_ms=5012", host=host)
    log("info", "scored transaction in 42ms", service="fraud-scoring", host="fraud-3a1")
    time.sleep(30)

with open(state_file, "w") as f:
    json.dump({"deploy_at": deploy_at, "alert_at": int(time.time())}, f)
print("incident done", flush=True)
