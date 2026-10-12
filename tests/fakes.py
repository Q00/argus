"""Fake adapters implementing the LLMAdapter protocol for offline tests.

FakeStageAdapter inspects prompt headers to decide which pipeline stage is
calling and answers with an exact-set-valid response, so the full pipeline
runs end-to-end with zero network access.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
from pathlib import Path
import re
from typing import Any

from argus.core import ProviderError, Result
from argus.gateway.base import ModelSpec, RawCompletion

_SENTENCE_RE = re.compile(r"^S(\d+): (.+)$", re.MULTILINE)
_NEXT_HEADER_RE = re.compile(r"\n\n[A-Z][A-Z ]+\n")
_TABLE_KEY_RE = re.compile(r"^\[(.+)\] ", re.MULTILINE)


def _section_after(prompt: str, header: str) -> str:
    """Text under ``header`` up to the next blank-line header or OUTPUT CONTRACT."""
    section = prompt.split(f"{header}\n", 1)[1]
    section = section.split("\n\nOUTPUT CONTRACT", 1)[0]
    following = _NEXT_HEADER_RE.search(section)
    return section[: following.start()] if following else section


def _payload_after(prompt: str, header: str) -> Any:
    return json.loads(_section_after(prompt, header).strip())


def _ok(value: Any) -> Result[RawCompletion, ProviderError]:
    return Result.of(RawCompletion(text=json.dumps(value), model="fake", isolation="payload"))


class FakeStageAdapter:
    """Answers divergence/resolution/audit/diagnosis/composition prompts correctly.

    Args:
        audit_discards_last: when True and more than one concern reaches
            the audit, the last one is discarded to exercise attrition.
        fail_outline_call: 1-based outline call that fails with a
            non-transient provider error, to exercise diagnosis resume.
    """

    def __init__(self, audit_discards_last: bool = True, fail_outline_call: int | None = None):
        self.audit_discards_last = audit_discards_last
        self.fail_outline_call = fail_outline_call
        self.calls: list[str] = []

    def complete(
        self,
        prompt: str,
        spec: ModelSpec,
        images: Sequence[Path] = (),
        json_schema: dict[str, Any] | None = None,
    ) -> Result[RawCompletion, ProviderError]:
        if "\nSECTION UNITS\n" in prompt:
            self.calls.append("outline")
            if self.calls.count("outline") == self.fail_outline_call:
                return Result.err(ProviderError("outline backend down", "fake"))
            return self._outline(prompt)
        if "\nSECTION OUTLINE\n" in prompt:
            self.calls.append("structure")
            return self._structure(prompt)
        if "UNIT TEXT" in prompt:
            self.calls.append("diverge")
            return self._diverge(prompt)
        if "CONCERNS TO ADJUDICATE" in prompt:
            self.calls.append("resolve")
            return self._resolve(prompt)
        if "PROMOTED CONCERNS" in prompt:
            self.calls.append("audit")
            return self._audit(prompt)
        if "ADJUDICATED FINAL CONCERNS" in prompt:
            self.calls.append("compose")
            return self._compose(prompt)
        raise AssertionError(f"unrecognized prompt: {prompt[:120]}")

    def _diverge(self, prompt: str) -> Result[RawCompletion, ProviderError]:
        sentences = _SENTENCE_RE.findall(prompt.split("UNIT TEXT", 1)[1])
        items = [
            {
                "sentence_ordinal": int(ordinal),
                "claims": [
                    {
                        "claim_text": text[:80],
                        "questions": [
                            {
                                "category": "evidence",
                                "question": f"What evidence supports sentence {ordinal}: {text[:60]}?",
                            }
                        ],
                    }
                ],
                "not_applicable_reason": None,
            }
            for ordinal, text in sentences
        ]
        return _ok({"items": items})

    def _resolve(self, prompt: str) -> Result[RawCompletion, ProviderError]:
        concerns = _payload_after(prompt, "CONCERNS TO ADJUDICATE")
        resolutions = []
        for index, concern in enumerate(concerns):
            promoted = index < 2
            resolutions.append(
                {
                    "concern_id": concern["concern_id"],
                    "status": "promoted_final" if promoted else "solved_by_paper",
                    "evidence": (
                        "The paper never reports the comparison this question names."
                        if promoted
                        else "Answered by the corpus main text near the unit locator."
                    ),
                    "source": concern["locator"],
                    "materiality": "major" if promoted else "minor",
                }
            )
        return _ok({"resolutions": resolutions})

    def _audit(self, prompt: str) -> Result[RawCompletion, ProviderError]:
        concerns = _payload_after(prompt, "PROMOTED CONCERNS")
        ids = [concern["id"] for concern in concerns]
        if self.audit_discards_last and len(ids) > 1:
            return _ok(
                {
                    "keep_ids": ids[:-1],
                    "discards": [
                        {
                            "concern_id": ids[-1],
                            "reason": "duplicate of the retained canonical concern",
                            "canonical_id": ids[0],
                        }
                    ],
                }
            )
        return _ok({"keep_ids": ids, "discards": []})

    def _outline(self, prompt: str) -> Result[RawCompletion, ProviderError]:
        units = _payload_after(prompt, "SECTION UNITS")
        pairs = _payload_after(prompt, "ADJACENT PAIRS")
        concerns = _payload_after(prompt, "SECTION CONCERNS")
        catalog = _section_after(prompt, "TABLE CELL CATALOG")
        keys = _TABLE_KEY_RE.findall(catalog)
        cells = [f"{keys[0]}:r1c1"] if keys else []
        items = []
        for index, unit in enumerate(units):
            move = len(units) > 1 and index == len(units) - 1
            items.append(
                {
                    "unit_id": unit["unit_id"],
                    "learns": f"What {unit['short_id']} adds to the section.",
                    "removable_without_loss": False,
                    "lost_if_removed": "The point this unit makes.",
                    "action": "move" if move else ("fix" if index == 0 else "keep"),
                    "move_before": units[0]["unit_id"] if move else None,
                    "direction": "State the result before the setup.",
                }
            )
        sentences = []
        for index, unit in enumerate(units):
            for sentence in unit["sentences"]:
                text = sentence["text"]
                record: dict[str, Any] = {
                    "unit_id": unit["unit_id"],
                    "sentence_ordinal": sentence["ordinal"],
                    "kind": "none",
                    "evidence_span": None,
                    "connective_holds": None,
                    "support_cells": [],
                    "verdict": "not_applicable",
                    "note": "",
                    "wording_issue": "none",
                    "wording_suggestion": None,
                }
                if sentence["ordinal"] == 1 and cells:
                    narrow = index % 2 == 1
                    record.update(
                        kind="number",
                        evidence_span=text[:12],
                        support_cells=cells,
                        verdict="narrow_scope" if narrow else "supported",
                        note="The cell supports only the reported setting." if narrow else "",
                    )
                elif sentence["ordinal"] == 2:
                    record.update(
                        kind="causal_link",
                        evidence_span=text[:10],
                        connective_holds=False,
                        verdict="narrow_scope",
                        note="State the association without the causal claim.",
                    )
                if index == 0 and sentence["ordinal"] == 1:
                    record.update(
                        wording_issue="fact_error", wording_suggestion="A corrected sentence."
                    )
                sentences.append(record)
        return _ok(
            {
                "items": items,
                "pairs": [
                    {
                        "from_id": pair["from_id"],
                        "to_id": pair["to_id"],
                        "takes_up_previous": index != 0,
                        "shifted": ["object", "point"] if index == 0 else [],
                        "note": "The relation holds without the connective.",
                    }
                    for index, pair in enumerate(pairs)
                ],
                "sentences": sentences,
                "concerns": [
                    {
                        "concern_id": concern["concern_id"],
                        "direction": "Narrow the claim to the evaluated setting.",
                        "needs_new_computation": index == 0,
                    }
                    for index, concern in enumerate(concerns)
                ],
            }
        )

    def _structure(self, prompt: str) -> Result[RawCompletion, ProviderError]:
        outline = _payload_after(prompt, "SECTION OUTLINE")
        issues = _payload_after(prompt, "EXTERNAL ISSUES")
        first_unit = outline[0]["units"][0]["unit_id"]
        return _ok(
            {
                "sections": [
                    {
                        "section_id": section["section_id"],
                        "start_state": "What problem does the paper address?",
                        "arrival_state": "The section answers it for one setting.",
                        "next_question": "Does the answer hold elsewhere?",
                        "takes_up_previous_question": True,
                        "removable_without_loss": False,
                        "action": "keep",
                        "move_after": None,
                        "reason": "The section carries a step of the argument.",
                    }
                    for section in outline
                ],
                "issues": [
                    {
                        "issue_id": issue["id"],
                        "unit_ids": [first_unit],
                        "direction": "Report the missing comparison or narrow the claim.",
                        "needs_new_computation": index == 0,
                        "note": "",
                    }
                    for index, issue in enumerate(issues)
                ],
            }
        )

    def _compose(self, prompt: str) -> Result[RawCompletion, ProviderError]:
        concerns = _payload_after(prompt, "ADJUDICATED FINAL CONCERNS")
        ids = [concern["id"] for concern in concerns]
        return _ok(
            {
                "summary": "The paper proposes a method and evaluates it on benchmarks.",
                "strengths": ["Clear problem statement grounded in the corpus."],
                "concern_order": ids,
                "soundness": 3,
                "presentation": 3,
                "significance": 2,
                "originality": 3,
                "overall": 4,
                "confidence": 4,
                "key_question_ids": ids[:1],
                "ethics": "",
                "limitations": ["Automated one-shot review without rebuttal."],
            }
        )


class ScriptedAdapter:
    """Returns queued results in order; useful for retry/repair tests."""

    def __init__(self, outcomes: list[Result[RawCompletion, ProviderError] | str]):
        self._outcomes = list(outcomes)
        self.calls = 0

    def complete(
        self,
        prompt: str,
        spec: ModelSpec,
        images: Sequence[Path] = (),
        json_schema: dict[str, Any] | None = None,
    ) -> Result[RawCompletion, ProviderError]:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, str):
            return Result.of(RawCompletion(text=outcome, model="fake"))
        return outcome
