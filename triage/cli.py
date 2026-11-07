"""Triage one incident from the command line and print the result (no Slack post).

    triage-once --title "Payments API 5xx > 5%" --service payments-api --at 2026-04-02T14:05:00Z
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

import anthropic

from triage.agent import TriageAgent
from triage.config import Settings
from triage.models import Alert
from triage.tools import build_toolset


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--title", required=True)
    p.add_argument("--service")
    p.add_argument("--severity")
    p.add_argument("--at", help="trigger time, ISO 8601 (default: now)")
    p.add_argument("--id", default="manual")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = Settings.from_env()
    alert = Alert(
        source="pagerduty",
        id=args.id,
        title=args.title,
        service=args.service,
        severity=args.severity,
        triggered_at=args.at or datetime.now(timezone.utc),
    )
    agent = TriageAgent(anthropic.Anthropic(), build_toolset(settings), settings)
    print(agent.run(alert).model_dump_json(indent=2))


if __name__ == "__main__":
    main()
