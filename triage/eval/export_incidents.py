"""Export resolved incidents from ServiceNow to a CSV for building eval cases. Read-only.

    triage-export --out ~/incident_export.csv --days 365 --limit 20

Reviewers fill in the blank columns from the post-incident review. Close notes are a starting point, not
the confirmed root cause. The output can contain sensitive incident text, so keep real exports out of
synced folders and check they're approved for use before sending them anywhere.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

from triage.config import Settings
from triage.tools.servicenow import ServiceNowTools

EXPORT_FIELDS = (
    "number", "short_description", "description", "cmdb_ci", "priority", "opened_at", "resolved_at",
    "close_code", "close_notes", "problem_id", "caused_by", "rfc",
)
REVIEW_FIELDS = ("confirmed_root_cause", "culprit_change", "category", "reviewed_by")


def _csv_safe(value: Any) -> str:
    """Stop incident text from running as a formula when the file is opened in a spreadsheet."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([*EXPORT_FIELDS, *REVIEW_FIELDS])
        for row in rows:
            writer.writerow([*(_csv_safe(row.get(k)) for k in EXPORT_FIELDS), *("" for _ in REVIEW_FIELDS)])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--ci", default=None, help="only incidents on this CI name")
    p.add_argument("--max-priority", type=int, default=2, help="lowest severity to include: 2 means P1 and P2 (default)")
    args = p.parse_args()

    tools = ServiceNowTools(Settings.from_env())
    rows = tools.export_incidents(args.days, args.limit, args.ci, EXPORT_FIELDS, args.max_priority)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    write_csv(rows, args.out)
    print(f"Wrote {len(rows)} incidents to {args.out}")


if __name__ == "__main__":
    main()
