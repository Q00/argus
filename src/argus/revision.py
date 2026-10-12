"""Revision plan: the structural diagnosis rendered from ledger rows.

No model call happens here. The plan overlays the final review concerns
and external issues on the reverse outline so the owner can approve rows;
the manuscript itself is never modified by argus.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from argus.ledger import REVISION_PLAN_NAME, Ledger


def _cell(text: Any) -> str:
    """One markdown table cell: single line, pipes escaped."""
    return " ".join(str(text).split()).replace("|", "\\|")


def _row(values: list[Any]) -> str:
    return "| " + " | ".join(_cell(value) for value in values) + " |"


def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    if not rows:
        return ["None."]
    return [_row(header), _row(["---"] * len(header)), *(_row(row) for row in rows)]


def build_revision_plan(
    ledger: Ledger, run_id: str, output_root: Path, concern_order: list[str]
) -> dict[str, Any] | None:
    """Assemble the plan from sections.json and diagnosis rows; None for PDF runs."""
    sections_path = output_root / "sections.json"
    if not sections_path.is_file():
        return None
    sections = json.loads(sections_path.read_text(encoding="utf-8"))
    if not sections:
        return None
    units = {unit["id"]: unit for unit in ledger.list_units(run_id)}
    order = [unit_id for section in sections for unit_id in section["unit_ids"]]
    short = {unit_id: f"P{index}" for index, unit_id in enumerate(order, 1)}
    titles = {section["section_id"]: section["title"] for section in sections}
    weakness = {concern_id: f"W{index}" for index, concern_id in enumerate(concern_order, 1)}
    concern_units = {
        row["id"]: row["unit_id"]
        for row in ledger.rows(
            "SELECT id, unit_id FROM concerns WHERE run_id = ? AND status = 'promoted_final'",
            (run_id,),
        )
    }
    items = {row["unit_id"]: row for row in ledger.diagnoses(run_id, "item")}
    incoming = {row["to_id"]: row for row in ledger.diagnoses(run_id, "pair")}
    sentences = sorted(
        ledger.diagnoses(run_id, "sentence"),
        key=lambda row: (order.index(row["unit_id"]), row["sentence_ordinal"]),
    )
    concerns = sorted(
        ledger.diagnoses(run_id, "concern"),
        key=lambda row: (
            concern_order.index(row["concern_id"])
            if row["concern_id"] in concern_order
            else len(concern_order)
        ),
    )
    structure = {row["section_id"]: row for row in ledger.diagnoses(run_id, "section")}
    issues = ledger.diagnoses(run_id, "issue")

    def label(concern_id: str) -> str:
        return weakness.get(concern_id, concern_id)

    def target(identifier: str | None) -> str:
        if identifier is None:
            return ""
        return short.get(identifier) or titles.get(identifier, identifier)

    decisions = [
        {
            "item": label(row["concern_id"]),
            "paragraphs": [short[concern_units[row["concern_id"]]]]
            if concern_units.get(row["concern_id"]) in short
            else [],
            "direction": row["direction"],
        }
        for row in concerns
        if row["needs_new_computation"]
    ] + [
        {
            "item": f"Issue {row['issue_id']}",
            "paragraphs": [short[unit_id] for unit_id in row["unit_ids"] if unit_id in short],
            "direction": row["direction"],
        }
        for row in issues
        if row["needs_new_computation"]
    ]
    plan_sections: list[dict[str, Any]] = []
    for section in sections:
        rows: list[dict[str, Any]] = []
        for unit_id in section["unit_ids"]:
            unit = units[unit_id]
            item = items[unit_id]
            flags = [
                label(row["concern_id"])
                for row in concerns
                if concern_units.get(row["concern_id"]) == unit_id
            ]
            flags += [f"Issue {row['issue_id']}" for row in issues if unit_id in row["unit_ids"]]
            pair = incoming.get(unit_id)
            if pair and (not pair["takes_up_previous"] or len(pair["shifted"]) >= 2):
                shifted = ", ".join(pair["shifted"])
                flags.append(f"handoff breaks ({shifted})" if shifted else "handoff breaks")
            unit_sentences = [row for row in sentences if row["unit_id"] == unit_id]
            narrow = sum(row["verdict"] == "narrow_scope" for row in unit_sentences)
            failing = sum(row["connective_holds"] is False for row in unit_sentences)
            if narrow:
                flags.append(f"{narrow} narrow-scope")
            if failing:
                flags.append(f"{failing} connective fails")
            if item["removable_without_loss"]:
                flags.append("removable")
            directions = [item["direction"]]
            directions += [
                f"{label(row['concern_id'])}: {row['direction']}"
                for row in concerns
                if concern_units.get(row["concern_id"]) == unit_id
                and not row["needs_new_computation"]
            ]
            directions += [
                f"Issue {row['issue_id']}: {row['direction']}"
                for row in issues
                if unit_id in row["unit_ids"] and not row["needs_new_computation"]
            ]
            action = item["action"]
            if action == "move":
                action = f"move before {target(item['move_before'])}"
            rows.append(
                {
                    "id": short[unit_id],
                    "unit_id": unit_id,
                    "location": f"{unit['tex_file']}:L{unit['line_start']}-L{unit['line_end']}",
                    "learns": item["learns"],
                    "flags": flags,
                    "action": action,
                    "direction": " ".join(directions),
                }
            )
        verdict = structure.get(section["section_id"], {})
        section_action = verdict.get("action", "")
        if section_action == "move":
            section_action = f"move after {target(verdict.get('move_after'))}"
        plan_sections.append(
            {
                "section_id": section["section_id"],
                "title": section["title"],
                "parent_title": section["parent_title"],
                "start_state": verdict.get("start_state", ""),
                "arrival_state": verdict.get("arrival_state", ""),
                "next_question": verdict.get("next_question", ""),
                "takes_up_previous_question": verdict.get("takes_up_previous_question"),
                "removable_without_loss": verdict.get("removable_without_loss"),
                "action": section_action,
                "reason": verdict.get("reason", ""),
                "rows": rows,
            }
        )
    claim_ledger = [
        {
            "sentence": f"{short[row['unit_id']]} S{row['sentence_ordinal']}",
            "kind": row["kind"],
            "span": row["evidence_span"],
            "cells": row["support_cells"],
            "verdict": row["verdict"],
            "connective_holds": row["connective_holds"],
            "note": row["note"],
        }
        for row in sentences
        if row["kind"] != "none"
    ]
    sentence_notes = [
        {
            "sentence": f"{short[row['unit_id']]} S{row['sentence_ordinal']}",
            "issue": row["wording_issue"],
            "suggestion": row["wording_suggestion"],
        }
        for row in sentences
        if row["wording_issue"] != "none"
    ]
    return {
        "run_id": run_id,
        "manuscript_modified": False,
        "decisions_needed": decisions,
        "sections": plan_sections,
        "claim_ledger": claim_ledger,
        "sentence_notes": sentence_notes,
    }


def render_revision_plan(plan: dict[str, Any]) -> str:
    lines = [
        "# Revision plan",
        "",
        f"Run: `{plan['run_id']}`",
        "",
        "Argus did not modify the manuscript. Apply only the rows the owner approves.",
        "",
        "## Decisions needed",
        "",
        "These items need a new experiment or computation. They are flagged for the owner's "
        "decision and are not scheduled.",
        "",
    ]
    lines += _table(
        ["Item", "Paragraphs", "Direction"],
        [
            [entry["item"], ", ".join(entry["paragraphs"]) or "none", entry["direction"]]
            for entry in plan["decisions_needed"]
        ],
    )
    for section in plan["sections"]:
        heading = section["title"]
        if section["parent_title"]:
            heading = f"{heading} (in {section['parent_title']})"
        takes_up = section["takes_up_previous_question"]
        lines += [
            "",
            f"## {heading}",
            "",
            f"- Start -> arrival: {_cell(section['start_state'])} -> "
            f"{_cell(section['arrival_state'])}",
            f"- Next question: {_cell(section['next_question'])}",
            f"- Takes up the previous question: {'yes' if takes_up else 'no'}",
            f"- Section action: {_cell(section['action'])} ({_cell(section['reason'])})",
            "",
        ]
        lines += _table(
            ["ID", "Location", "Reader learns", "Flags", "Action", "Direction"],
            [
                [
                    row["id"],
                    row["location"],
                    row["learns"],
                    "; ".join(row["flags"]) or "none",
                    row["action"],
                    row["direction"],
                ]
                for row in section["rows"]
            ],
        )
    lines += ["", "## Claim ledger", ""]
    lines += _table(
        ["Sentence", "Span", "Cells", "Verdict", "Note"],
        [
            [
                entry["sentence"],
                entry["span"],
                ", ".join(entry["cells"]) or "none",
                entry["verdict"],
                entry["note"],
            ]
            for entry in plan["claim_ledger"]
        ],
    )
    lines += ["", "## Sentence notes", ""]
    lines += _table(
        ["Sentence", "Issue", "Suggestion"],
        [
            [entry["sentence"], entry["issue"], entry["suggestion"]]
            for entry in plan["sentence_notes"]
        ],
    )
    return "\n".join(lines).rstrip() + "\n"


def write_revision_plan(
    ledger: Ledger, run_id: str, output_root: Path, concern_order: list[str]
) -> Path | None:
    """Write revision-plan.md and revision-plan.json; None when the run has no blocks."""
    plan = build_revision_plan(ledger, run_id, output_root, concern_order)
    if plan is None:
        return None
    (output_root / "revision-plan.json").write_text(
        json.dumps(plan, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    path = output_root / REVISION_PLAN_NAME
    path.write_text(render_revision_plan(plan), encoding="utf-8")
    return path
