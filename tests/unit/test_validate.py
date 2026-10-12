import pytest

from argus.core import SchemaViolation
from argus.validate import (
    validate_audit_partition,
    validate_composition_ids,
    validate_divergence,
    validate_outline,
    validate_resolutions,
    validate_structure,
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


SENTENCES = {
    ("u1", 1): "Accuracy rises to 91.2 on the test set.",
    ("u1", 2): "This happens because the student is smaller.",
    ("u2", 1): "We discuss limits.",
}


def _outline_value() -> dict:
    return {
        "items": [
            {
                "unit_id": "u1",
                "learns": "a",
                "removable_without_loss": False,
                "lost_if_removed": "x",
                "action": "keep",
                "move_before": None,
                "direction": "d",
            },
            {
                "unit_id": "u2",
                "learns": "b",
                "removable_without_loss": False,
                "lost_if_removed": "y",
                "action": "move",
                "move_before": "u1",
                "direction": "d",
            },
        ],
        "pairs": [
            {
                "from_id": "u1",
                "to_id": "u2",
                "takes_up_previous": False,
                "shifted": ["object"],
                "note": "",
            }
        ],
        "sentences": [
            {
                "unit_id": "u1",
                "sentence_ordinal": 1,
                "kind": "number",
                "evidence_span": "91.2",
                "connective_holds": None,
                "support_cells": ["tab:x:r2c2"],
                "verdict": "supported",
                "note": "",
                "wording_issue": "none",
                "wording_suggestion": None,
            },
            {
                "unit_id": "u1",
                "sentence_ordinal": 2,
                "kind": "causal_link",
                "evidence_span": "because the student is smaller",
                "connective_holds": False,
                "support_cells": [],
                "verdict": "narrow_scope",
                "note": "The tables show an association only.",
                "wording_issue": "actor_not_subject",
                "wording_suggestion": "The smaller student causes this.",
            },
            {
                "unit_id": "u2",
                "sentence_ordinal": 1,
                "kind": "none",
                "evidence_span": None,
                "connective_holds": None,
                "support_cells": [],
                "verdict": "not_applicable",
                "note": "",
                "wording_issue": "none",
                "wording_suggestion": None,
            },
        ],
        "concerns": [{"concern_id": "c1", "direction": "d", "needs_new_computation": True}],
    }


def _validate_outline(value: dict, preserve_wording: bool = False) -> None:
    validate_outline(
        value,
        ["u1", "u2"],
        [("u1", "u2")],
        SENTENCES,
        {"c1"},
        {"tab:x:r2c2"},
        {"u1", "u2", "sec_1"},
        preserve_wording,
    )


class TestValidateOutline:
    def test_valid_outline_passes(self):
        _validate_outline(_outline_value())

    @pytest.mark.parametrize("field", ["items", "pairs", "sentences", "concerns"])
    def test_missing_entry_fails(self, field: str):
        value = _outline_value()
        value[field] = value[field][:-1]
        with pytest.raises(SchemaViolation):
            _validate_outline(value)

    @pytest.mark.parametrize("field", ["items", "pairs", "sentences", "concerns"])
    def test_duplicate_entry_fails(self, field: str):
        value = _outline_value()
        value[field] = [*value[field], value[field][0]]
        with pytest.raises(SchemaViolation):
            _validate_outline(value)

    def test_non_verbatim_span_fails(self):
        value = _outline_value()
        value["sentences"][1]["evidence_span"] = "because it is smaller"
        with pytest.raises(SchemaViolation) as excinfo:
            _validate_outline(value)
        assert "verbatim" in str(excinfo.value)

    def test_unknown_cell_fails(self):
        value = _outline_value()
        value["sentences"][0]["support_cells"] = ["tab:x:r9c9"]
        with pytest.raises(SchemaViolation):
            _validate_outline(value)

    def test_supported_needs_a_cell_and_narrow_scope_needs_a_note(self):
        value = _outline_value()
        value["sentences"][0]["support_cells"] = []
        with pytest.raises(SchemaViolation):
            _validate_outline(value)
        value = _outline_value()
        value["sentences"][1]["note"] = " "
        with pytest.raises(SchemaViolation):
            _validate_outline(value)

    def test_kind_none_must_be_not_applicable(self):
        value = _outline_value()
        value["sentences"][2]["verdict"] = "supported"
        with pytest.raises(SchemaViolation):
            _validate_outline(value)

    def test_connective_holds_follows_kind(self):
        value = _outline_value()
        value["sentences"][1]["connective_holds"] = None
        with pytest.raises(SchemaViolation):
            _validate_outline(value)

    def test_delete_without_removable_fails(self):
        value = _outline_value()
        value["items"][0]["action"] = "delete"
        with pytest.raises(SchemaViolation):
            _validate_outline(value)
        value["items"][0]["removable_without_loss"] = True
        _validate_outline(value)

    def test_move_without_valid_target_fails(self):
        for target in (None, "u9", "u2"):
            value = _outline_value()
            value["items"][1]["move_before"] = target
            with pytest.raises(SchemaViolation):
                _validate_outline(value)
        value = _outline_value()
        value["items"][0]["move_before"] = "u2"
        with pytest.raises(SchemaViolation):
            _validate_outline(value)

    def test_preserve_wording_allows_only_fact_errors(self):
        value = _outline_value()
        with pytest.raises(SchemaViolation):
            _validate_outline(value, preserve_wording=True)
        value["sentences"][1]["wording_issue"] = "fact_error"
        _validate_outline(value, preserve_wording=True)

    def test_wording_issue_needs_a_suggestion(self):
        value = _outline_value()
        value["sentences"][1]["wording_suggestion"] = None
        with pytest.raises(SchemaViolation):
            _validate_outline(value)

    def test_preserve_wording_schema_enum(self):
        from argus.schemas import outline_schema

        def enum(preserve: bool) -> list[str]:
            schema = outline_schema(["u1"], 0, 1, [], preserve)
            return schema["properties"]["sentences"]["items"]["properties"]["wording_issue"]["enum"]

        assert enum(True) == ["none", "fact_error"]
        assert "actor_not_subject" in enum(False)


def _structure_value() -> dict:
    return {
        "sections": [
            {
                "section_id": "sec_1",
                "start_state": "q",
                "arrival_state": "a",
                "next_question": "n",
                "takes_up_previous_question": True,
                "removable_without_loss": False,
                "action": "keep",
                "move_after": None,
                "reason": "r",
            },
            {
                "section_id": "sec_2",
                "start_state": "q",
                "arrival_state": "a",
                "next_question": "n",
                "takes_up_previous_question": False,
                "removable_without_loss": False,
                "action": "move",
                "move_after": "sec_1",
                "reason": "r",
            },
        ],
        "issues": [
            {
                "issue_id": "R1",
                "unit_ids": ["u1"],
                "direction": "d",
                "needs_new_computation": False,
                "note": "",
            }
        ],
    }


def _validate_structure(value: dict) -> None:
    validate_structure(value, ["sec_1", "sec_2"], {"R1"}, {"u1", "u2"})


class TestValidateStructure:
    def test_valid_structure_passes(self):
        _validate_structure(_structure_value())

    @pytest.mark.parametrize("field", ["sections", "issues"])
    def test_exact_coverage(self, field: str):
        value = _structure_value()
        value[field] = value[field][:-1]
        with pytest.raises(SchemaViolation):
            _validate_structure(value)
        value = _structure_value()
        value[field] = [*value[field], value[field][0]]
        with pytest.raises(SchemaViolation):
            _validate_structure(value)

    def test_move_needs_another_section(self):
        value = _structure_value()
        value["sections"][1]["move_after"] = "sec_2"
        with pytest.raises(SchemaViolation):
            _validate_structure(value)

    def test_delete_needs_removable(self):
        value = _structure_value()
        value["sections"][0]["action"] = "delete"
        with pytest.raises(SchemaViolation):
            _validate_structure(value)

    def test_issue_unit_ids_must_exist(self):
        value = _structure_value()
        value["issues"][0]["unit_ids"] = ["u9"]
        with pytest.raises(SchemaViolation):
            _validate_structure(value)
