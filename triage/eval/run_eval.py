"""Replay historical incidents through the agent and score it.

    triage-eval --cases evals/cases --concurrency 4
    triage-eval --cases evals/cases --ids INC0041234,INC0039876
    triage-eval --cases evals/live --live servicenow,github

Every run calls the Claude API once per agent turn plus once per judge call, so it costs money.
Use --limit for a quick smoke test.
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Collection

import anthropic

from triage.agent import TriageAgent, TriageFailed
from triage.config import Settings
from triage.eval.fixtures import EvalCase, fixture_toolset
from triage.eval.grader import culprit_hit, judge

SLA_S = 120.0


def run_case(client: anthropic.Anthropic, settings: Settings, case: EvalCase, live: Collection[str] = ()) -> dict[str, Any]:
    tools = fixture_toolset(case, settings, live)
    row: dict[str, Any] = {"id": case.id, "category": case.ground_truth.category}
    try:
        result = TriageAgent(client, tools, settings).run(case.alert)
    except TriageFailed as e:
        return {**row, "status": "failed", "error": str(e)}
    verdict = judge(client, settings.model, case.ground_truth, result.report)
    return {
        **row,
        "status": "ok",
        "grade": verdict.grade,
        "judge_reasoning": verdict.reasoning,
        "runbook_useful": verdict.runbook_useful,
        "culprit_hit": culprit_hit(case.ground_truth, result.report),
        "confidence": result.report.root_cause.confidence,
        "hypothesis": result.report.root_cause.summary,
        "elapsed_s": result.elapsed_s,
        "tool_calls": result.tool_calls,
        "hit_time_budget": result.hit_time_budget,
        "calls": tools.recorded_calls,  # type: ignore[attr-defined]
    }


def _rate(rows: list[dict[str, Any]], pred) -> float | None:
    return round(sum(1 for r in rows if pred(r)) / len(rows), 3) if rows else None


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r["status"] == "ok"]
    times = sorted(r["elapsed_s"] for r in ok)
    with_culprit = [r for r in ok if r["culprit_hit"] is not None]
    high = [r for r in ok if r["confidence"] == "high"]
    by_category: dict[str, Any] = {}
    for cat in sorted({r["category"] for r in ok}):
        sub = [r for r in ok if r["category"] == cat]
        by_category[cat] = {"n": len(sub), "top1": _rate(sub, lambda r: r["grade"] == "match")}
    return {
        "cases": len(rows),
        "failed": len(rows) - len(ok),
        # Failures count as misses: the on-call engineer got nothing useful.
        "top1_accuracy": _rate(rows, lambda r: r.get("grade") == "match"),
        "match_or_partial": _rate(rows, lambda r: r.get("grade") in ("match", "partial")),
        "culprit_named": _rate(with_culprit, lambda r: r["culprit_hit"]),
        "high_confidence_share": _rate(ok, lambda r: r["confidence"] == "high"),
        "high_confidence_precision": _rate(high, lambda r: r["grade"] == "match"),
        "runbook_useful": _rate(ok, lambda r: r["runbook_useful"]),
        "p50_s": statistics.median(times) if times else None,
        "p95_s": times[min(len(times) - 1, int(0.95 * len(times)))] if times else None,
        "within_sla": _rate(ok, lambda r: r["elapsed_s"] <= SLA_S),
        "mean_tool_calls": round(statistics.mean(r["tool_calls"] for r in ok), 1) if ok else None,
        "by_category": by_category,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cases", type=Path, default=Path("evals/cases"))
    p.add_argument("--out", type=Path, default=None)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--ids", default=None, help="comma-separated case ids")
    p.add_argument("--model", default=None)
    p.add_argument("--effort", default=None)
    p.add_argument("--live", default="", help="comma-separated systems to query for real instead of recordings: servicenow,github,datadog")
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING)
    settings = Settings.from_env()
    if args.model:
        settings = replace(settings, model=args.model)
    if args.effort:
        settings = replace(settings, effort=args.effort)

    cases = EvalCase.load_dir(args.cases)
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c.id in wanted]
    cases = cases[: args.limit] if args.limit else cases
    if not cases:
        raise SystemExit(f"No cases found in {args.cases}")

    out = args.out or Path("evals/results") / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    client = anthropic.Anthropic(max_retries=4)
    live = {name.strip() for name in args.live.split(",") if name.strip()}

    print(f"Running {len(cases)} cases with {settings.model} (effort={settings.effort}) -> {out}")
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool, (out / "results.jsonl").open("w") as f:
        for row in pool.map(lambda c: run_case(client, settings, c, live), cases):
            rows.append(row)
            f.write(json.dumps(row, default=str) + "\n")
            f.flush()
            print(f"  {row['id']:<14} {row.get('grade', row['status']):<8} {row.get('confidence', ''):<7} {row.get('elapsed_s', '')}")

    summary = {"model": settings.model, "effort": settings.effort, **summarise(rows)}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
