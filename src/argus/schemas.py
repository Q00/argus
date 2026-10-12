"""JSON Schemas for every model-facing contract.

Schemas are the first enforcement layer; exact-set validation in the
pipeline is the second. ID enums are injected per call so a model cannot
invent or drop IDs even at the schema level.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

QUESTION_CATEGORIES = [
    "evidence",
    "logic",
    "methodology",
    "evaluation",
    "comparison",
    "clarity",
    "citation",
    "scope",
    "reproducibility",
    "visual_support",
    "ethics",
]

TERMINAL_STATUSES = [
    "solved_by_paper",
    "solved_by_reference_or_consensus",
    "promoted_final",
    "invalid_or_duplicate",
]


def divergence_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["sentence_ordinal", "claims", "not_applicable_reason"],
                    "properties": {
                        "sentence_ordinal": {"type": "integer", "minimum": 1},
                        "claims": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["claim_text", "questions"],
                                "properties": {
                                    "claim_text": {"type": "string", "minLength": 1},
                                    "questions": {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": {
                                            "type": "object",
                                            "additionalProperties": False,
                                            "required": ["category", "question"],
                                            "properties": {
                                                "category": {
                                                    "type": "string",
                                                    "enum": QUESTION_CATEGORIES,
                                                },
                                                "question": {"type": "string", "minLength": 1},
                                            },
                                        },
                                    },
                                },
                            },
                        },
                        "not_applicable_reason": {"type": ["string", "null"]},
                    },
                },
            }
        },
    }


def resolution_schema(concern_ids: Iterable[str]) -> dict[str, Any]:
    allowed = sorted(set(concern_ids))
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["resolutions"],
        "properties": {
            "resolutions": {
                "type": "array",
                "minItems": len(allowed),
                "maxItems": len(allowed),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["concern_id", "status", "evidence", "source", "materiality"],
                    "properties": {
                        "concern_id": {"type": "string", "enum": allowed},
                        "status": {"type": "string", "enum": TERMINAL_STATUSES},
                        "evidence": {"type": "string", "minLength": 1},
                        "source": {"type": "string", "minLength": 1},
                        "materiality": {"type": "string", "enum": ["minor", "major"]},
                    },
                },
            }
        },
    }


def audit_schema(concern_ids: Iterable[str]) -> dict[str, Any]:
    allowed = sorted(set(concern_ids))
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["keep_ids", "discards"],
        "properties": {
            "keep_ids": {"type": "array", "items": {"type": "string", "enum": allowed}},
            "discards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["concern_id", "reason", "canonical_id"],
                    "properties": {
                        "concern_id": {"type": "string", "enum": allowed},
                        "reason": {"type": "string", "minLength": 1},
                        "canonical_id": {"enum": [None, *allowed]},
                    },
                },
            },
        },
    }


def composition_schema(concern_ids: Iterable[str]) -> dict[str, Any]:
    allowed = sorted(set(concern_ids))
    id_schema: dict[str, Any] = {"type": "string"}
    if allowed:
        id_schema["enum"] = allowed
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "summary",
            "strengths",
            "concern_order",
            "soundness",
            "presentation",
            "significance",
            "originality",
            "overall",
            "confidence",
            "key_question_ids",
            "ethics",
            "limitations",
        ],
        "properties": {
            "summary": {"type": "string", "minLength": 1},
            "strengths": {"type": "array", "items": {"type": "string"}},
            "concern_order": {
                "type": "array",
                "items": id_schema,
                "minItems": len(allowed),
                "maxItems": len(allowed),
            },
            "soundness": {"type": "integer", "minimum": 1, "maximum": 4},
            "presentation": {"type": "integer", "minimum": 1, "maximum": 4},
            "significance": {"type": "integer", "minimum": 1, "maximum": 4},
            "originality": {"type": "integer", "minimum": 1, "maximum": 4},
            "overall": {"type": "integer", "minimum": 1, "maximum": 6},
            "confidence": {"type": "integer", "minimum": 1, "maximum": 5},
            "key_question_ids": {"type": "array", "maxItems": 5, "items": id_schema},
            "ethics": {"type": "string"},
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
    }


def recall_match_schema(issue_ids: Iterable[str]) -> dict[str, Any]:
    allowed = sorted(set(issue_ids))
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["matches"],
        "properties": {
            "matches": {
                "type": "array",
                "minItems": len(allowed),
                "maxItems": len(allowed),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["issue_id", "matched", "concern_ids", "explanation"],
                    "properties": {
                        "issue_id": {"type": "string", "enum": allowed},
                        "matched": {
                            "type": "string",
                            "enum": ["surfaced_final", "surfaced_but_attrited", "missed"],
                        },
                        "concern_ids": {"type": "array", "items": {"type": "string"}},
                        "explanation": {"type": "string", "minLength": 1},
                    },
                },
            }
        },
    }


DIAGNOSIS_ACTIONS = ["keep", "fix", "delete", "move"]
SHIFT_DIMENSIONS = ["object", "point", "criterion"]
SENTENCE_KINDS = ["causal_link", "number", "causal_link_and_number", "none"]
SENTENCE_VERDICTS = ["supported", "narrow_scope", "not_applicable"]
WORDING_ISSUES = ["none", "fact_error", "actor_not_subject", "new_before_given"]
PRESERVED_WORDING_ISSUES = ["none", "fact_error"]


def _id_schema(ids: Iterable[str]) -> dict[str, Any]:
    allowed = sorted(set(ids))
    schema: dict[str, Any] = {"type": "string"}
    if allowed:
        schema["enum"] = allowed
    return schema


def _exact_array(item: dict[str, Any], count: int) -> dict[str, Any]:
    return {"type": "array", "minItems": count, "maxItems": count, "items": item}


def outline_schema(
    unit_ids: Iterable[str],
    pair_count: int,
    sentence_count: int,
    concern_ids: Iterable[str],
    preserve_wording: bool,
) -> dict[str, Any]:
    """Reverse-outline contract for one section block.

    Move targets and table-cell IDs are checked by ``validate_outline``,
    not as enums: both sets span the whole manuscript.
    """
    units = sorted(set(unit_ids))
    concerns = sorted(set(concern_ids))
    unit_id = _id_schema(units)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["items", "pairs", "sentences", "concerns"],
        "properties": {
            "items": _exact_array(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "unit_id",
                        "learns",
                        "removable_without_loss",
                        "lost_if_removed",
                        "action",
                        "move_before",
                        "direction",
                    ],
                    "properties": {
                        "unit_id": unit_id,
                        "learns": {"type": "string", "minLength": 1},
                        "removable_without_loss": {"type": "boolean"},
                        "lost_if_removed": {"type": "string"},
                        "action": {"type": "string", "enum": DIAGNOSIS_ACTIONS},
                        "move_before": {"type": ["string", "null"]},
                        "direction": {"type": "string", "minLength": 1},
                    },
                },
                len(units),
            ),
            "pairs": _exact_array(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["from_id", "to_id", "takes_up_previous", "shifted", "note"],
                    "properties": {
                        "from_id": unit_id,
                        "to_id": unit_id,
                        "takes_up_previous": {"type": "boolean"},
                        "shifted": {
                            "type": "array",
                            "uniqueItems": True,
                            "items": {"type": "string", "enum": SHIFT_DIMENSIONS},
                        },
                        "note": {"type": "string"},
                    },
                },
                pair_count,
            ),
            "sentences": _exact_array(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "unit_id",
                        "sentence_ordinal",
                        "kind",
                        "evidence_span",
                        "connective_holds",
                        "support_cells",
                        "verdict",
                        "note",
                        "wording_issue",
                        "wording_suggestion",
                    ],
                    "properties": {
                        "unit_id": unit_id,
                        "sentence_ordinal": {"type": "integer", "minimum": 1},
                        "kind": {"type": "string", "enum": SENTENCE_KINDS},
                        "evidence_span": {"type": ["string", "null"]},
                        "connective_holds": {"type": ["boolean", "null"]},
                        "support_cells": {"type": "array", "items": {"type": "string"}},
                        "verdict": {"type": "string", "enum": SENTENCE_VERDICTS},
                        "note": {"type": "string"},
                        "wording_issue": {
                            "type": "string",
                            "enum": PRESERVED_WORDING_ISSUES
                            if preserve_wording
                            else WORDING_ISSUES,
                        },
                        "wording_suggestion": {"type": ["string", "null"]},
                    },
                },
                sentence_count,
            ),
            "concerns": _exact_array(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["concern_id", "direction", "needs_new_computation"],
                    "properties": {
                        "concern_id": _id_schema(concerns),
                        "direction": {"type": "string", "minLength": 1},
                        "needs_new_computation": {"type": "boolean"},
                    },
                },
                len(concerns),
            ),
        },
    }


def structure_schema(section_ids: Iterable[str], issue_ids: Iterable[str]) -> dict[str, Any]:
    """Section-level structure contract; issue unit IDs are checked by the validator."""
    sections = sorted(set(section_ids))
    issues = sorted(set(issue_ids))
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["sections", "issues"],
        "properties": {
            "sections": _exact_array(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "section_id",
                        "start_state",
                        "arrival_state",
                        "next_question",
                        "takes_up_previous_question",
                        "removable_without_loss",
                        "action",
                        "move_after",
                        "reason",
                    ],
                    "properties": {
                        "section_id": _id_schema(sections),
                        "start_state": {"type": "string", "minLength": 1},
                        "arrival_state": {"type": "string", "minLength": 1},
                        "next_question": {"type": "string"},
                        "takes_up_previous_question": {"type": "boolean"},
                        "removable_without_loss": {"type": "boolean"},
                        "action": {"type": "string", "enum": DIAGNOSIS_ACTIONS},
                        "move_after": {"enum": [None, *sections]},
                        "reason": {"type": "string", "minLength": 1},
                    },
                },
                len(sections),
            ),
            "issues": _exact_array(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "issue_id",
                        "unit_ids",
                        "direction",
                        "needs_new_computation",
                        "note",
                    ],
                    "properties": {
                        "issue_id": _id_schema(issues),
                        "unit_ids": {"type": "array", "items": {"type": "string"}},
                        "direction": {"type": "string", "minLength": 1},
                        "needs_new_computation": {"type": "boolean"},
                        "note": {"type": "string"},
                    },
                },
                len(issues),
            ),
        },
    }
