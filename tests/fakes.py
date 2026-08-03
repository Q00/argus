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


def _payload_after(prompt: str, header: str) -> Any:
    section = prompt.split(header, 1)[1]
    section = section.split("\n\nOUTPUT CONTRACT", 1)[0]
    return json.loads(section.strip())


def _ok(value: Any) -> Result[RawCompletion, ProviderError]:
    return Result.of(RawCompletion(text=json.dumps(value), model="fake", isolation="payload"))


class FakeStageAdapter:
    """Answers divergence/resolution/audit/composition prompts correctly.

    Args:
        audit_discards_last: when True and more than one concern reaches
            the audit, the last one is discarded to exercise attrition.
    """

    def __init__(self, audit_discards_last: bool = True):
        self.audit_discards_last = audit_discards_last
        self.calls: list[str] = []

    def complete(
        self,
        prompt: str,
        spec: ModelSpec,
        images: Sequence[Path] = (),
        json_schema: dict[str, Any] | None = None,
    ) -> Result[RawCompletion, ProviderError]:
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
