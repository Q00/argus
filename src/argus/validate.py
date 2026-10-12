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


def _exact_records(
    records: Any, key: Any, expected: set[Any], what: str
) -> dict[Any, dict[str, Any]]:
    """Index model records by key; reject non-objects, duplicates, and set drift."""
    if not isinstance(records, list):
        raise SchemaViolation(f"{what} must be an array")
    indexed: dict[Any, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise SchemaViolation(f"{what} entries must be objects")
        record_key = key(record)
        if record_key in indexed:
            raise SchemaViolation(f"{what} has a duplicate entry", {"key": str(record_key)})
        indexed[record_key] = record
    if set(indexed) != expected:
        raise SchemaViolation(
            f"{what} coverage mismatch",
            {
                "missing": sorted(str(item) for item in expected - set(indexed)),
                "extra": sorted(str(item) for item in set(indexed) - expected),
            },
        )
    return indexed


def _check_action(record: dict[str, Any], target_key: str, targets: set[str], own_id: str) -> None:
    action = record.get("action")
    target = record.get(target_key)
    if action == "delete" and record.get("removable_without_loss") is not True:
        raise SchemaViolation(f"{own_id}: delete requires removable_without_loss=true")
    if action == "move":
        if not isinstance(target, str) or target not in targets:
            raise SchemaViolation(f"{own_id}: move needs an existing {target_key}", {"got": target})
        if target == own_id:
            raise SchemaViolation(f"{own_id}: {target_key} must differ from the item itself")
    elif target is not None:
        raise SchemaViolation(f"{own_id}: {target_key} must be null unless action is move")


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_outline(
    value: dict[str, Any],
    unit_ids: list[str],
    pairs: list[tuple[str, str]],
    sentences: dict[tuple[str, int], str],
    concern_ids: set[str],
    cell_ids: set[str],
    move_targets: set[str],
    preserve_wording: bool,
) -> None:
    """Exact-set and rule checks for one section block's reverse outline.

    ``evidence_span`` must be a verbatim substring of its sentence: the
    model labels the sentence kind, the code only verifies the quote.
    """
    items = _exact_records(value.get("items"), lambda r: r.get("unit_id"), set(unit_ids), "items")
    for unit_id, item in items.items():
        _check_action(item, "move_before", move_targets, unit_id)
    for pair in _exact_records(
        value.get("pairs"), lambda r: (r.get("from_id"), r.get("to_id")), set(pairs), "pairs"
    ).values():
        shifted = pair.get("shifted")
        if not isinstance(shifted, list) or len(shifted) != len(set(shifted)):
            raise SchemaViolation("pair shifted dimensions must be unique")
        if not set(shifted) <= {"object", "point", "criterion"}:
            raise SchemaViolation("pair shifted dimensions are object, point, criterion")
    allowed_issues = (
        {"none", "fact_error"}
        if preserve_wording
        else {"none", "fact_error", "actor_not_subject", "new_before_given"}
    )
    records = _exact_records(
        value.get("sentences"),
        lambda r: (r.get("unit_id"), r.get("sentence_ordinal")),
        set(sentences),
        "sentences",
    )
    for key, record in records.items():
        where = f"{key[0]} S{key[1]}"
        kind = record.get("kind")
        span = record.get("evidence_span")
        holds = record.get("connective_holds")
        verdict = record.get("verdict")
        cells = record.get("support_cells")
        if kind == "none":
            if span is not None or holds is not None or verdict != "not_applicable":
                raise SchemaViolation(
                    f"{where}: kind none needs null span, null connective_holds, not_applicable"
                )
        else:
            if not isinstance(span, str) or not span.strip() or span not in sentences[key]:
                raise SchemaViolation(
                    f"{where}: evidence_span is not a verbatim substring of the sentence",
                    {"span": span},
                )
            causal = kind in {"causal_link", "causal_link_and_number"}
            if causal != isinstance(holds, bool):
                raise SchemaViolation(
                    f"{where}: connective_holds is boolean iff kind has a causal link"
                )
            if verdict == "not_applicable":
                raise SchemaViolation(f"{where}: not_applicable is only for kind none")
        if not isinstance(cells, list) or len(cells) != len(set(cells)):
            raise SchemaViolation(f"{where}: support_cells must be unique")
        unknown = sorted(set(cells) - cell_ids)
        if unknown:
            raise SchemaViolation(f"{where}: unknown table cells", {"unknown": unknown})
        if verdict == "supported" and not cells:
            raise SchemaViolation(f"{where}: supported needs at least one cell")
        if verdict == "narrow_scope" and not _nonempty(record.get("note")):
            raise SchemaViolation(f"{where}: narrow_scope needs a note with the narrower claim")
        issue = record.get("wording_issue")
        if issue not in allowed_issues:
            raise SchemaViolation(f"{where}: wording_issue not allowed", {"got": issue})
        suggestion = record.get("wording_suggestion")
        if issue != "none" and not _nonempty(suggestion):
            raise SchemaViolation(f"{where}: wording_issue needs a wording_suggestion")
        if issue == "none" and suggestion not in (None, ""):
            raise SchemaViolation(f"{where}: wording_suggestion must be empty without an issue")
    _exact_records(value.get("concerns"), lambda r: r.get("concern_id"), concern_ids, "concerns")


def validate_structure(
    value: dict[str, Any],
    section_ids: list[str],
    issue_ids: set[str],
    unit_ids: set[str],
) -> None:
    sections = _exact_records(
        value.get("sections"), lambda r: r.get("section_id"), set(section_ids), "sections"
    )
    for section_id, section in sections.items():
        _check_action(section, "move_after", set(section_ids), section_id)
    issues = _exact_records(value.get("issues"), lambda r: r.get("issue_id"), issue_ids, "issues")
    for issue_id, issue in issues.items():
        mapped = issue.get("unit_ids")
        if not isinstance(mapped, list) or len(mapped) != len(set(mapped)):
            raise SchemaViolation(f"issue {issue_id}: unit_ids must be unique")
        unknown = sorted(set(mapped) - unit_ids)
        if unknown:
            raise SchemaViolation(
                f"issue {issue_id}: unknown main-body unit ids", {"unknown": unknown}
            )
