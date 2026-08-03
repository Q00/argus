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
