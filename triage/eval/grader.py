from __future__ import annotations

from typing import Literal

import anthropic
from pydantic import BaseModel

from triage.eval.fixtures import GroundTruth
from triage.models import TriageReport

JUDGE_PROMPT = """\
You are grading an incident triage agent against the confirmed root cause from the post-incident review.

Confirmed root cause:
<ground_truth>{truth}</ground_truth>

Agent's primary hypothesis ({confidence} confidence):
<primary>{primary}</primary>

Agent's alternative hypotheses:
<alternatives>{alternatives}</alternatives>

Grade the primary hypothesis:
- "match": it identifies the same component and the same mechanism or triggering change as the confirmed cause. \
Wording can differ; an engineer acting on it would have gone straight to the real fix.
- "partial": it points at the right component or the right change but gets the mechanism wrong, or the confirmed \
cause appears only among the alternatives.
- "miss": it would have sent the engineer in the wrong direction.

Also say whether the runbook steps, if followed, would have led toward the actual fix.
"""


class Verdict(BaseModel):
    reasoning: str
    grade: Literal["match", "partial", "miss"]
    runbook_useful: bool


def culprit_hit(truth: GroundTruth, report: TriageReport) -> bool | None:
    """Deterministic check: did the agent name the responsible change anywhere in its primary hypothesis?"""
    if not truth.culprit:
        return None
    needle = truth.culprit.lower()
    r = report.root_cause
    return needle in f"{r.suspected_change or ''} {r.summary} {' '.join(r.evidence)}".lower()


def judge(client: anthropic.Anthropic, model: str, truth: GroundTruth, report: TriageReport) -> Verdict:
    def fmt(h) -> str:
        return f"{h.summary} (suspected change: {h.suspected_change}; evidence: {'; '.join(h.evidence)})"

    prompt = JUDGE_PROMPT.format(
        truth=truth.root_cause + (f" (responsible change: {truth.culprit})" if truth.culprit else ""),
        confidence=report.root_cause.confidence,
        primary=fmt(report.root_cause),
        alternatives="\n".join(fmt(h) for h in report.alternatives) or "(none)",
    ) + "\nRunbook steps:\n" + "\n".join(f"- {s}" for s in report.runbook_steps)

    response = client.beta.messages.parse(
        model=model,
        max_tokens=4000,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium"},
        output_format=Verdict,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"judge returned no verdict (stop_reason={response.stop_reason})")
    return response.parsed_output
