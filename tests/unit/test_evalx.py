import json
from pathlib import Path

import pytest

from argus.core import ArgusError, ProviderError, Result
from argus.evalx import load_issue_file, recall_eval
from argus.gateway import Gateway, ModelSpec
from argus.gateway.base import RawCompletion
from argus.ledger import Ledger

SPEC = ModelSpec(provider="openai-compat", model="judge")


class MatchingJudgeAdapter:
    """Labels the first issue surfaced_final, second attrited, rest missed."""

    def complete(self, prompt, spec, images=(), json_schema=None):
        issues = json.loads(
            prompt.split("INDEPENDENT ISSUES", 1)[1].split("HARNESS CONCERN LEDGER", 1)[0].strip()
        )
        concerns = json.loads(
            prompt.split("HARNESS CONCERN LEDGER", 1)[1].split("\n\nOUTPUT CONTRACT", 1)[0].strip()
        )
        final = [c["id"] for c in concerns if c["status"] == "promoted_final"]
        attrited = [c["id"] for c in concerns if c["status"] != "promoted_final"]
        matches = []
        for index, issue in enumerate(issues):
            if index == 0 and final:
                matches.append(
                    {
                        "issue_id": issue["id"],
                        "matched": "surfaced_final",
                        "concern_ids": final[:1],
                        "explanation": "Final concern covers this issue.",
                    }
                )
            elif index == 1 and attrited:
                matches.append(
                    {
                        "issue_id": issue["id"],
                        "matched": "surfaced_but_attrited",
                        "concern_ids": attrited[:1],
                        "explanation": "Divergence found it; adjudication removed it.",
                    }
                )
            else:
                matches.append(
                    {
                        "issue_id": issue["id"],
                        "matched": "missed",
                        "concern_ids": [],
                        "explanation": "No generated concern expresses this issue.",
                    }
                )
        return Result.of(RawCompletion(text=json.dumps({"matches": matches}), model="judge"))


def _seed_ledger(tmp_path: Path) -> tuple[Ledger, str]:
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    ledger.create_run("run_1", tmp_path, tmp_path)
    ledger.register_unit(
        {
            "id": "unt_1",
            "run_id": "run_1",
            "kind": "paragraph",
            "locator": "main p.1 paragraph 1",
            "source_document": "main",
            "page": 1,
            "ordinal": 0,
            "content_path": "/tmp/input.txt",
            "manifest_path": "/tmp/manifest.json",
        }
    )
    items = [
        {
            "sentence_ordinal": 1,
            "claims": [
                {
                    "claim_text": "claim",
                    "questions": [
                        {"category": "evidence", "question": "Q1?"},
                        {"category": "comparison", "question": "Q2?"},
                    ],
                }
            ],
            "not_applicable_reason": None,
        }
    ]
    ledger.record_divergence("unt_1", ["Sentence one."], items)
    first, second = [concern["id"] for concern in ledger.list_concerns("run_1")]
    ledger.claim_specific(first, "owner")
    ledger.resolve_concern(first, "owner", "promoted_final", "ev", "src", "major", "k1")
    ledger.claim_specific(second, "owner")
    ledger.resolve_concern(second, "owner", "solved_by_paper", "ev", "src", "minor", "k2")
    return ledger, "run_1"


def _issues(tmp_path: Path, count: int = 3) -> Path:
    path = tmp_path / "issues.json"
    path.write_text(
        json.dumps(
            [
                {"id": f"issue-{index}", "text": f"Independent issue {index}"}
                for index in range(count)
            ]
        ),
        encoding="utf-8",
    )
    return path


class TestLoadIssueFile:
    def test_valid_file_loads(self, tmp_path: Path):
        assert len(load_issue_file(_issues(tmp_path))) == 3

    def test_duplicate_ids_are_rejected(self, tmp_path: Path):
        path = tmp_path / "dup.json"
        path.write_text(json.dumps([{"id": "a", "text": "x"}, {"id": "a", "text": "y"}]))
        with pytest.raises(ArgusError):
            load_issue_file(path)

    def test_empty_array_is_rejected(self, tmp_path: Path):
        path = tmp_path / "empty.json"
        path.write_text("[]")
        with pytest.raises(ArgusError):
            load_issue_file(path)


class TestRecallEval:
    def test_report_counts_and_rates(self, tmp_path: Path):
        ledger, run_id = _seed_ledger(tmp_path)
        gateway = Gateway(MatchingJudgeAdapter(), tmp_path / "logs", backoff_initial=0.0)
        report = recall_eval(ledger, run_id, _issues(tmp_path), gateway, SPEC, tmp_path)
        assert report["counts"] == {
            "surfaced_final": 1,
            "surfaced_but_attrited": 1,
            "missed": 1,
        }
        assert report["final_recall"] == pytest.approx(0.333)
        assert report["generation_recall"] == pytest.approx(0.667)
        assert "model judgment" in report["caveat"]
        assert (tmp_path / "recall-report.json").is_file()

    def test_invented_concern_ids_are_rejected(self, tmp_path: Path):
        class InventingJudge:
            def complete(self, prompt, spec, images=(), json_schema=None):
                issues = json.loads(
                    prompt.split("INDEPENDENT ISSUES", 1)[1]
                    .split("HARNESS CONCERN LEDGER", 1)[0]
                    .strip()
                )
                matches = [
                    {
                        "issue_id": issue["id"],
                        "matched": "surfaced_final",
                        "concern_ids": ["con_invented"],
                        "explanation": "made up",
                    }
                    for issue in issues
                ]
                return Result.of(
                    RawCompletion(text=json.dumps({"matches": matches}), model="judge")
                )

        ledger, run_id = _seed_ledger(tmp_path)
        gateway = Gateway(InventingJudge(), tmp_path / "logs", backoff_initial=0.0)
        with pytest.raises(ArgusError) as excinfo:
            recall_eval(ledger, run_id, _issues(tmp_path, 1), gateway, SPEC, tmp_path)
        assert "invented" in str(excinfo.value)

    def test_provider_error_propagates(self, tmp_path: Path):
        class BrokenJudge:
            def complete(self, prompt, spec, images=(), json_schema=None):
                return Result.err(ProviderError("down", provider="x"))

        ledger, run_id = _seed_ledger(tmp_path)
        gateway = Gateway(BrokenJudge(), tmp_path / "logs", backoff_initial=0.0)
        with pytest.raises(ProviderError):
            recall_eval(ledger, run_id, _issues(tmp_path, 1), gateway, SPEC, tmp_path)
