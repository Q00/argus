"""Recall evaluation: measure what the harness misses, not just what it keeps.

Input: an issue file — a JSON list of independently sourced weaknesses
(e.g. normalized public reviews) with stable IDs. A judge model matches
each issue against the run's *entire* concern ledger, including attrited
concerns, and labels it:

    surfaced_final        the final review contains it
    surfaced_but_attrited divergence found it, adjudication/audit killed it
    missed                the harness never generated it

``surfaced_but_attrited`` is the precision/recall dial made visible: those
are exactly the concerns the policy file traded away. ``missed`` measures
the divergence stage itself. Judge output is a model judgment and is
labeled as such in the report.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from argus.core import ArgusError
from argus.gateway import Gateway, ModelSpec
from argus.ledger import Ledger
from argus.schemas import recall_match_schema

_JUDGE_PROMPT = """You are judging the recall of an automated paper-review harness.

Below are (a) independently sourced review issues for a paper and (b) the harness's complete
concern ledger for the same paper: every question it generated, each with its terminal status.
Statuses `promoted_final` (and the review's final concerns) mean the issue survived to the final
review. Any other terminal status means the harness generated the concern but adjudication or the
global audit removed it.

For every issue, decide:
- surfaced_final: at least one promoted_final concern substantively expresses this issue.
- surfaced_but_attrited: no promoted_final concern matches, but a non-final concern does.
- missed: no generated concern substantively expresses this issue.

Match on substance, not wording. List the matching concern IDs (empty when missed) and give a
one-sentence explanation citing the closest concern. Return exactly one match entry per issue ID.

INDEPENDENT ISSUES
${issues}

HARNESS CONCERN LEDGER
${concerns}
"""


def load_issue_file(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise ArgusError("issue file must be a non-empty JSON array")
    seen: set[str] = set()
    for index, issue in enumerate(value):
        if not isinstance(issue, dict):
            raise ArgusError(f"issue[{index}] must be an object")
        issue_id = issue.get("id")
        text = issue.get("text")
        if not isinstance(issue_id, str) or not issue_id.strip():
            raise ArgusError(f"issue[{index}].id must be a non-empty string")
        if issue_id in seen:
            raise ArgusError(f"duplicate issue id: {issue_id}")
        seen.add(issue_id)
        if not isinstance(text, str) or not text.strip():
            raise ArgusError(f"issue[{index}].text must be a non-empty string")
    return value


def recall_eval(
    ledger: Ledger,
    run_id: str,
    issue_file: Path,
    gateway: Gateway,
    spec: ModelSpec,
    output_root: Path,
) -> dict[str, Any]:
    """Judge harness recall against an independent issue list."""
    issues = load_issue_file(issue_file)
    concerns = [
        {
            "id": concern["id"],
            "status": concern["status"],
            "terminal_stage": concern["terminal_stage"],
            "category": concern["category"],
            "question": concern["question"],
            "evidence": concern["evidence"],
            "locator": concern["locator"],
        }
        for concern in ledger.list_concerns(run_id)
    ]
    if not concerns:
        raise ArgusError(f"run {run_id} has no concerns to evaluate")
    issue_ids = {issue["id"] for issue in issues}
    from string import Template

    prompt = Template(_JUDGE_PROMPT).substitute(
        issues=json.dumps(issues, ensure_ascii=False),
        concerns=json.dumps(concerns, ensure_ascii=False),
    )
    result = gateway.complete_json("recall-judge", prompt, recall_match_schema(issue_ids), spec)
    matches = result["matches"]
    returned_ids = [match["issue_id"] for match in matches]
    if set(returned_ids) != issue_ids or len(returned_ids) != len(set(returned_ids)):
        raise ArgusError(
            "recall judge did not return exactly one match per issue",
            {"missing": sorted(issue_ids - set(returned_ids))},
        )
    known_concern_ids = {concern["id"] for concern in concerns}
    for match in matches:
        unknown = sorted(set(match["concern_ids"]) - known_concern_ids)
        if unknown:
            raise ArgusError(
                f"recall judge invented concern IDs for issue {match['issue_id']}",
                {"unknown": unknown},
            )
    counts = {"surfaced_final": 0, "surfaced_but_attrited": 0, "missed": 0}
    for match in matches:
        counts[match["matched"]] += 1
    total = len(matches)
    report = {
        "run_id": run_id,
        "issue_file": str(issue_file.resolve()),
        "issue_count": total,
        "counts": counts,
        "final_recall": round(counts["surfaced_final"] / total, 3),
        "generation_recall": round(
            (counts["surfaced_final"] + counts["surfaced_but_attrited"]) / total, 3
        ),
        "attrition_loss": counts["surfaced_but_attrited"],
        "matches": matches,
        "caveat": (
            "Judge output is an automated model judgment against an observational issue list, "
            "not ground truth and not human verification."
        ),
    }
    (output_root / "recall-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return report
