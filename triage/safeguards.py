"""Limits on how much triage work runs, and an audit trail of every run."""
from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger(__name__)

# USD per million tokens: input, output, cached read. Cache writes are assumed at 1.25x input. Update when prices change.
MODEL_PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
}


def estimate_cost_usd(model: str, usage: dict[str, int]) -> float | None:
    """Estimated API cost of a run, or None for a model not in MODEL_PRICES."""
    prices = MODEL_PRICES.get(model)
    if prices is None:
        return None
    input_price, output_price, read_price = prices
    total = (
        usage.get("input_tokens", 0) * input_price
        + usage.get("cache_write_tokens", 0) * input_price * 1.25
        + usage.get("cache_read_tokens", 0) * read_price
        + usage.get("output_tokens", 0) * output_price
    )
    return round(total / 1_000_000, 4)


class TriageLimiter:
    """Stops an alert storm from running up cost or hitting API rate limits."""

    def __init__(self, max_concurrent: int, max_in_flight: int, max_per_day: int, notice_interval_s: float = 600):
        self.max_in_flight = max_in_flight
        self.max_per_day = max_per_day
        self.notice_interval_s = notice_interval_s
        self._running = threading.BoundedSemaphore(max_concurrent)
        self._lock = threading.Lock()
        self._in_flight = 0
        self._day = ""
        self._today = 0
        self._last_notice: float | None = None

    def admit(self) -> str | None:
        """Reserve a place for one triage. Returns why it was refused, or None if admitted."""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            if day != self._day:
                self._day, self._today = day, 0
            if self._today >= self.max_per_day:
                return f"daily limit of {self.max_per_day} triages reached"
            if self._in_flight >= self.max_in_flight:
                return f"{self.max_in_flight} triages already in progress or waiting"
            self._today += 1
            self._in_flight += 1
            return None

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Wait for a free worker. Call once for each successful admit()."""
        try:
            with self._running:
                yield
        finally:
            with self._lock:
                self._in_flight -= 1

    def should_notify(self) -> bool:
        """During a storm, tell Slack about refusals only occasionally so the notices aren't a storm themselves."""
        now = time.monotonic()
        with self._lock:
            if self._last_notice is not None and now - self._last_notice < self.notice_interval_s:
                return False
            self._last_notice = now
            return True


class AuditLog:
    """Appends one JSON record per run. A logging failure never stops a triage."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def write(self, record: dict[str, Any]) -> None:
        try:
            line = json.dumps(record, default=str)
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a") as f:
                    f.write(line + "\n")
        except Exception:
            log.exception("could not write audit record for %s", record.get("alert_id"))
