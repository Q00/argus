from pathlib import Path

import pytest

from argus.core import LedgerError
from argus.ledger import Ledger


def _make_run(tmp_path: Path) -> tuple[Ledger, str]:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.create_run("run_1", tmp_path, tmp_path)
    return ledger, "run_1"


def _register_unit(ledger: Ledger, run_id: str, unit_id: str = "unt_1") -> str:
    ledger.register_unit(
        {
            "id": unit_id,
            "run_id": run_id,
            "kind": "paragraph",
            "locator": "main p.1 paragraph 1",
            "source_document": "main",
            "page": 1,
            "ordinal": 0,
            "content_path": "/tmp/input.txt",
            "manifest_path": "/tmp/manifest.json",
        }
    )
    return unit_id


def _divergence_items(question: str = "What evidence supports this?") -> list[dict]:
    return [
        {
            "sentence_ordinal": 1,
            "claims": [
                {
                    "claim_text": "claim one",
                    "questions": [
                        {"category": "evidence", "question": question},
                        {"category": "evidence", "question": question},
                    ],
                }
            ],
            "not_applicable_reason": None,
        }
    ]


class TestStageMachine:
    def test_backward_stage_transition_is_rejected(self, tmp_path: Path):
        ledger, run_id = _make_run(tmp_path)
        ledger.set_stage(run_id, "diverging")
        with pytest.raises(LedgerError):
            ledger.set_stage(run_id, "created")

    def test_unknown_stage_is_rejected(self, tmp_path: Path):
        ledger, run_id = _make_run(tmp_path)
        with pytest.raises(LedgerError):
            ledger.set_stage(run_id, "sideways")

    def test_run_id_collision_with_different_package_is_rejected(self, tmp_path: Path):
        ledger, run_id = _make_run(tmp_path)
        other = tmp_path / "other"
        other.mkdir()
        with pytest.raises(LedgerError):
            ledger.create_run(run_id, other, tmp_path)


class TestDivergenceRecording:
    def test_duplicate_question_is_demoted_to_event_not_failure(self, tmp_path: Path):
        ledger, run_id = _make_run(tmp_path)
        unit_id = _register_unit(ledger, run_id)
        ledger.record_divergence(unit_id, ["Sentence one."], _divergence_items())
        concerns = ledger.list_concerns(run_id)
        assert len(concerns) == 1
        events = ledger.rows(
            "SELECT * FROM events WHERE run_id = ? AND event = 'duplicate_concern_skipped'",
            (run_id,),
        )
        assert len(events) == 1

    def test_rerecording_replaces_previous_divergence(self, tmp_path: Path):
        ledger, run_id = _make_run(tmp_path)
        unit_id = _register_unit(ledger, run_id)
        ledger.record_divergence(unit_id, ["Sentence one."], _divergence_items("Q1?"))
        ledger.record_divergence(unit_id, ["Sentence one."], _divergence_items("Q2?"))
        concerns = ledger.list_concerns(run_id)
        assert len(concerns) == 1
        assert concerns[0]["question"] == "Q2?"


class TestResolution:
    def _one_concern(self, tmp_path: Path) -> tuple[Ledger, str, str]:
        ledger, run_id = _make_run(tmp_path)
        unit_id = _register_unit(ledger, run_id)
        ledger.record_divergence(unit_id, ["Sentence one."], _divergence_items())
        concern_id = ledger.list_concerns(run_id)[0]["id"]
        return ledger, run_id, concern_id

    def test_resolution_requires_a_held_lease(self, tmp_path: Path):
        ledger, _, concern_id = self._one_concern(tmp_path)
        with pytest.raises(LedgerError):
            ledger.resolve_concern(
                concern_id, "nobody", "solved_by_paper", "ev", "src", "minor", "k1"
            )

    def test_identical_replay_is_idempotent(self, tmp_path: Path):
        ledger, _, concern_id = self._one_concern(tmp_path)
        ledger.claim_specific(concern_id, "owner")
        args = (concern_id, "owner", "solved_by_paper", "ev", "src", "minor", "key-1")
        ledger.resolve_concern(*args)
        ledger.resolve_concern(*args)  # no error

    def test_conflicting_second_resolution_is_rejected(self, tmp_path: Path):
        ledger, _, concern_id = self._one_concern(tmp_path)
        ledger.claim_specific(concern_id, "owner")
        ledger.resolve_concern(
            concern_id, "owner", "solved_by_paper", "ev", "src", "minor", "key-1"
        )
        with pytest.raises(LedgerError):
            ledger.resolve_concern(
                concern_id, "owner", "promoted_final", "other", "src", "major", "key-2"
            )

    def test_empty_evidence_is_rejected(self, tmp_path: Path):
        ledger, _, concern_id = self._one_concern(tmp_path)
        ledger.claim_specific(concern_id, "owner")
        with pytest.raises(LedgerError):
            ledger.resolve_concern(
                concern_id, "owner", "solved_by_paper", "  ", "src", "minor", "k"
            )

    def test_claim_next_leases_oldest_open_concern(self, tmp_path: Path):
        ledger, run_id, concern_id = self._one_concern(tmp_path)
        claimed = ledger.claim_next(run_id, "owner")
        assert claimed is not None and claimed["id"] == concern_id
        assert ledger.claim_next(run_id, "other") is None


class TestAuditDiscard:
    def test_discard_requires_promoted_final_status(self, tmp_path: Path):
        ledger, run_id = _make_run(tmp_path)
        unit_id = _register_unit(ledger, run_id)
        ledger.record_divergence(unit_id, ["Sentence one."], _divergence_items())
        concern_id = ledger.list_concerns(run_id)[0]["id"]
        with pytest.raises(LedgerError):
            ledger.discard_promoted_concern(concern_id, "reason", None)

    def test_discard_sets_audit_terminal_stage(self, tmp_path: Path):
        ledger, run_id = _make_run(tmp_path)
        unit_id = _register_unit(ledger, run_id)
        ledger.record_divergence(unit_id, ["Sentence one."], _divergence_items())
        concern_id = ledger.list_concerns(run_id)[0]["id"]
        ledger.claim_specific(concern_id, "owner")
        ledger.resolve_concern(concern_id, "owner", "promoted_final", "ev", "src", "major", "k1")
        ledger.discard_promoted_concern(concern_id, "cross-batch duplicate", None)
        concern = ledger.list_concerns(run_id)[0]
        assert concern["status"] == "invalid_or_duplicate"
        assert concern["terminal_stage"] == "audit"


class TestAttrition:
    def test_attrition_attributes_stage_and_reason(self, tmp_path: Path):
        ledger, run_id = _make_run(tmp_path)
        unit_id = _register_unit(ledger, run_id)
        items = [
            {
                "sentence_ordinal": 1,
                "claims": [
                    {
                        "claim_text": "claim one",
                        "questions": [
                            {"category": "evidence", "question": "Q1?"},
                            {"category": "comparison", "question": "Q2?"},
                        ],
                    }
                ],
                "not_applicable_reason": None,
            }
        ]
        ledger.record_divergence(unit_id, ["Sentence one."], items)
        first, second = [concern["id"] for concern in ledger.list_concerns(run_id)]
        for concern_id, status in ((first, "promoted_final"), (second, "promoted_final")):
            ledger.claim_specific(concern_id, "owner")
            ledger.resolve_concern(
                concern_id, "owner", status, "ev", "src", "major", f"key-{concern_id}"
            )
        ledger.discard_promoted_concern(second, "duplicate of first", first)
        report = ledger.attrition(run_id)
        assert report["created"] == 2
        assert report["surviving_final"] == 1
        assert report["by_terminal_stage"]["audit"]["invalid_or_duplicate"] == 1
        assert report["audit_discards"][0]["canonical_id"] == first


class TestCompletion:
    def test_incomplete_run_reports_next_action(self, tmp_path: Path):
        ledger, run_id = _make_run(tmp_path)
        _register_unit(ledger, run_id)
        completion = ledger.completion(run_id)
        assert not completion.complete
        assert completion.open_units == 1
        assert "divergence" in completion.next_action

    def test_finalize_refuses_unresolved_concerns(self, tmp_path: Path):
        ledger, run_id = _make_run(tmp_path)
        unit_id = _register_unit(ledger, run_id)
        ledger.record_divergence(unit_id, ["Sentence one."], _divergence_items())
        review = tmp_path / "review.md"
        review.write_text("# Review\n", encoding="utf-8")
        with pytest.raises(LedgerError):
            ledger.finalize(run_id, review)
