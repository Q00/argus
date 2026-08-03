"""Exact-set validators — the second enforcement layer behind JSON Schema.

Schema validation proves shape; these prove accounting: every sentence
ordinal covered exactly once, every requested concern ID resolved exactly
once, every audited ID partitioned exactly once. A model output that
passes schema but fails accounting is rejected loudly.
"""

from __future__ import annotations

from typing import Any

from argus.core import SchemaViolation


def validate_divergence(value: dict[str, Any], sentence_count: int) -> None:
    items = value.get("items")
    if not isinstance(items, list):
        raise SchemaViolation("divergence result has no items array")
    by_ordinal: dict[int, dict[str, Any]] = {}
    for item in items:
        ordinal = item.get("sentence_ordinal")
        if not isinstance(ordinal, int) or ordinal in by_ordinal:
            raise SchemaViolation("divergence sentence ordinals must be unique integers")
        by_ordinal[ordinal] = item
        claims = item.get("claims")
        reason = item.get("not_applicable_reason")
        if claims:
            if reason:
                raise SchemaViolation(
                    f"sentence {ordinal} cannot have both claims and a not-applicable reason"
                )
            for claim in claims:
                if not claim.get("claim_text", "").strip() or not claim.get("questions"):
                    raise SchemaViolation(f"sentence {ordinal} has an uncovered atomic claim")
        elif not isinstance(reason, str) or not reason.strip():
            raise SchemaViolation(f"sentence {ordinal} has neither questions nor a reason")
    expected = set(range(1, sentence_count + 1))
    if set(by_ordinal) != expected:
        raise SchemaViolation(
            "divergence coverage mismatch",
            {"expected": sorted(expected), "got": sorted(by_ordinal)},
        )


def validate_resolutions(value: dict[str, Any], concern_ids: set[str]) -> None:
    resolutions = value.get("resolutions")
    if not isinstance(resolutions, list):
        raise SchemaViolation("resolver output has no resolutions array")
    returned = [item.get("concern_id") for item in resolutions]
    if len(returned) != len(set(returned)):
        raise SchemaViolation("resolver returned duplicate concern ids")
    if set(returned) != concern_ids:
        raise SchemaViolation(
            "resolver coverage mismatch",
            {
                "missing": sorted(concern_ids - set(returned)),
                "extra": sorted(set(returned) - concern_ids),
            },
        )


def validate_audit_partition(value: dict[str, Any], expected: set[str]) -> None:
    keep_ids = value.get("keep_ids")
    discards = value.get("discards")
    if not isinstance(keep_ids, list) or not isinstance(discards, list):
        raise SchemaViolation("audit requires keep_ids and discards arrays")
    discard_ids = [item.get("concern_id") for item in discards if isinstance(item, dict)]
    if len(discard_ids) != len(discards):
        raise SchemaViolation("audit discard entries must be objects with concern_id")
    if len(keep_ids) != len(set(keep_ids)) or len(discard_ids) != len(set(discard_ids)):
        raise SchemaViolation("audit IDs must be unique")
    keep = set(keep_ids)
    discarded = set(discard_ids)
    if keep & discarded or keep | discarded != expected:
        raise SchemaViolation(
            "audit changed the concern set",
            {
                "missing": sorted(expected - keep - discarded),
                "extra": sorted((keep | discarded) - expected),
                "overlap": sorted(keep & discarded),
            },
        )
    for item in discards:
        canonical_id = item.get("canonical_id")
        if canonical_id is not None and canonical_id not in keep:
            raise SchemaViolation(f"audit canonical ID is not retained: {canonical_id}")


def validate_composition_ids(value: dict[str, Any], expected: set[str]) -> None:
    ordered = value.get("concern_order")
    if not isinstance(ordered, list) or len(ordered) != len(set(ordered)):
        raise SchemaViolation("composer concern_order must contain unique IDs")
    if set(ordered) != expected:
        raise SchemaViolation(
            "composer changed the final concern set",
            {
                "missing": sorted(expected - set(ordered)),
                "extra": sorted(set(ordered) - expected),
            },
        )
    key_ids = value.get("key_question_ids")
    if not isinstance(key_ids, list) or len(key_ids) != len(set(key_ids)):
        raise SchemaViolation("composer key_question_ids must contain unique IDs")
    if not set(key_ids) <= expected:
        raise SchemaViolation(
            "composer invented key-question IDs",
            {"invented": sorted(set(key_ids) - expected)},
        )
