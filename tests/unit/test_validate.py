import pytest

from argus.core import SchemaViolation
from argus.validate import (
    validate_audit_partition,
    validate_composition_ids,
    validate_divergence,
    validate_resolutions,
)


def _item(ordinal: int, questions: bool = True, reason: str | None = None) -> dict:
    claims = (
        [{"claim_text": "c", "questions": [{"category": "evidence", "question": "q?"}]}]
        if questions
        else []
    )
    return {"sentence_ordinal": ordinal, "claims": claims, "not_applicable_reason": reason}


class TestValidateDivergence:
    def test_exact_coverage_passes(self):
        validate_divergence({"items": [_item(1), _item(2)]}, 2)

    def test_missing_ordinal_fails(self):
        with pytest.raises(SchemaViolation):
            validate_divergence({"items": [_item(1)]}, 2)

    def test_extra_ordinal_fails(self):
        with pytest.raises(SchemaViolation):
            validate_divergence({"items": [_item(1), _item(2), _item(3)]}, 2)

    def test_duplicate_ordinal_fails(self):
        with pytest.raises(SchemaViolation):
            validate_divergence({"items": [_item(1), _item(1)]}, 2)

    def test_sentence_needs_questions_or_reason(self):
        with pytest.raises(SchemaViolation):
            validate_divergence({"items": [_item(1, questions=False)]}, 1)
        validate_divergence({"items": [_item(1, questions=False, reason="bibliographic")]}, 1)

    def test_claims_and_reason_together_fail(self):
        with pytest.raises(SchemaViolation):
            validate_divergence({"items": [_item(1, questions=True, reason="also na")]}, 1)


class TestValidateResolutions:
    def test_exact_set_passes(self):
        validate_resolutions(
            {"resolutions": [{"concern_id": "a"}, {"concern_id": "b"}]}, {"a", "b"}
        )

    def test_missing_id_fails(self):
        with pytest.raises(SchemaViolation):
            validate_resolutions({"resolutions": [{"concern_id": "a"}]}, {"a", "b"})

    def test_duplicate_id_fails(self):
        with pytest.raises(SchemaViolation):
            validate_resolutions({"resolutions": [{"concern_id": "a"}, {"concern_id": "a"}]}, {"a"})


class TestValidateAuditPartition:
    def test_exact_partition_passes(self):
        validate_audit_partition(
            {"keep_ids": ["a"], "discards": [{"concern_id": "b", "canonical_id": "a"}]},
            {"a", "b"},
        )

    def test_dropped_id_fails(self):
        with pytest.raises(SchemaViolation):
            validate_audit_partition({"keep_ids": ["a"], "discards": []}, {"a", "b"})

    def test_overlap_fails(self):
        with pytest.raises(SchemaViolation):
            validate_audit_partition(
                {"keep_ids": ["a"], "discards": [{"concern_id": "a", "canonical_id": None}]},
                {"a"},
            )

    def test_canonical_must_be_retained(self):
        with pytest.raises(SchemaViolation):
            validate_audit_partition(
                {"keep_ids": ["a"], "discards": [{"concern_id": "b", "canonical_id": "b"}]},
                {"a", "b"},
            )


class TestValidateCompositionIds:
    def test_exact_order_passes(self):
        validate_composition_ids(
            {"concern_order": ["a", "b"], "key_question_ids": ["a"]}, {"a", "b"}
        )

    def test_invented_key_question_fails(self):
        with pytest.raises(SchemaViolation):
            validate_composition_ids({"concern_order": ["a"], "key_question_ids": ["z"]}, {"a"})

    def test_changed_concern_set_fails(self):
        with pytest.raises(SchemaViolation):
            validate_composition_ids({"concern_order": ["a"], "key_question_ids": []}, {"a", "b"})
