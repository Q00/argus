import json
from pathlib import Path

import pytest

from argus.core import ArgusError
from argus.extract import InputContract
from argus.gateway import ModelSpec
from argus.pipeline import (
    PipelineConfig,
    default_stage_models,
    read_active_pointer,
    run_pipeline,
)
from fakes import FakeStageAdapter

CONTRACT = InputContract(main_page_count=1, min_text_chars=100)
SPEC = ModelSpec(provider="openai-compat", model="fake-model")


def _config(paper_package: Path, tmp_path: Path, adapter: FakeStageAdapter) -> PipelineConfig:
    return PipelineConfig(
        package_root=paper_package,
        output_root=tmp_path / "out",
        stage_models=default_stage_models(SPEC),
        contract=CONTRACT,
        state_root=tmp_path / ".argus",
        max_workers=2,
        adapter_factory=lambda _provider: adapter,
    )


class TestFullPipeline:
    def test_end_to_end_run_completes_physically(self, paper_package: Path, tmp_path: Path):
        adapter = FakeStageAdapter()
        config = _config(paper_package, tmp_path, adapter)
        completion = run_pipeline(config)
        assert completion.complete
        assert completion.open_units == 0
        assert completion.unresolved_concerns == 0
        output = tmp_path / "out"
        assert (output / "review.md").is_file()
        assert (output / "review.json").is_file()
        assert (output / "ledger.json").is_file()
        assert (output / "run-manifest.json").is_file()
        assert "diverge" in adapter.calls and "resolve" in adapter.calls
        assert "compose" in adapter.calls
        assert "outline" not in adapter.calls and "structure" not in adapter.calls
        assert completion.undiagnosed_sections == 0
        assert not (output / "revision-plan.md").exists()

    def test_attrition_report_attributes_audit_discards(self, paper_package: Path, tmp_path: Path):
        adapter = FakeStageAdapter(audit_discards_last=True)
        config = _config(paper_package, tmp_path, adapter)
        run_pipeline(config)
        attrition = json.loads((tmp_path / "out" / "attrition.json").read_text())
        assert attrition["created"] > 0
        assert attrition["surviving_final"] >= 1
        # FakeStageAdapter promotes two concerns per batch and the audit
        # discards the last promoted concern.
        assert attrition["by_terminal_stage"].get("audit", {}).get("invalid_or_duplicate", 0) >= 1
        assert attrition["audit_discards"]

    def test_rerun_of_complete_run_is_idempotent(self, paper_package: Path, tmp_path: Path):
        adapter = FakeStageAdapter()
        config = _config(paper_package, tmp_path, adapter)
        run_pipeline(config)
        calls_after_first = len(adapter.calls)
        completion = run_pipeline(config)
        assert completion.complete
        assert len(adapter.calls) == calls_after_first  # no model calls on resume

    def test_active_pointer_tracks_completion(self, paper_package: Path, tmp_path: Path):
        adapter = FakeStageAdapter()
        config = _config(paper_package, tmp_path, adapter)
        run_pipeline(config)
        pointer = read_active_pointer(tmp_path / ".argus")
        assert pointer is not None
        assert pointer["complete"] is True

    def test_stop_after_diverged_leaves_resumable_state(self, paper_package: Path, tmp_path: Path):
        adapter = FakeStageAdapter()
        config = _config(paper_package, tmp_path, adapter)
        completion = run_pipeline(config, stop_after="diverged")
        assert not completion.complete
        assert completion.stage == "diverged"
        completion = run_pipeline(config)
        assert completion.complete


class TestManifestFreeze:
    def test_changed_stage_model_refuses_resume(self, paper_package: Path, tmp_path: Path):
        adapter = FakeStageAdapter()
        config = _config(paper_package, tmp_path, adapter)
        run_pipeline(config, stop_after="diverged")
        drifted = PipelineConfig(
            package_root=paper_package,
            output_root=tmp_path / "out",
            stage_models=default_stage_models(
                ModelSpec(provider="openai-compat", model="other-model")
            ),
            contract=CONTRACT,
            state_root=tmp_path / ".argus",
            adapter_factory=lambda _provider: adapter,
        )
        with pytest.raises(ArgusError) as excinfo:
            run_pipeline(drifted)
        assert "drifted" in str(excinfo.value)

    def test_manifest_records_prompts_policy_and_segmenter(
        self, paper_package: Path, tmp_path: Path
    ):
        adapter = FakeStageAdapter()
        run_pipeline(_config(paper_package, tmp_path, adapter), stop_after="extracted")
        manifest = json.loads((tmp_path / "out" / "run-manifest.json").read_text())
        assert set(manifest["prompt_sha256"]) == {
            "divergence",
            "resolution",
            "audit",
            "composition",
            "outline",
            "structure",
            "policy-icml",
        }
        assert manifest["diagnosis"] == {"preserve_wording": False}
        assert manifest["input_contract"]["source"] == "pdf"
        assert manifest["segmenter_version"].startswith("pysbd-")
        assert manifest["stage_models"]["diverge"]["model"] == "fake-model"


class TestParallelRunRejection:
    def test_second_package_is_rejected_while_first_is_active(
        self, paper_package: Path, tmp_path: Path
    ):
        adapter = FakeStageAdapter()
        config = _config(paper_package, tmp_path, adapter)
        run_pipeline(config, stop_after="diverged")
        other_package = tmp_path / "other-package"
        import shutil

        shutil.copytree(paper_package, other_package)
        (other_package / "reference-notes.md").write_text("- Different notes.\n", encoding="utf-8")
        other_config = PipelineConfig(
            package_root=other_package,
            output_root=tmp_path / "out2",
            stage_models=default_stage_models(SPEC),
            contract=CONTRACT,
            state_root=tmp_path / ".argus",
            adapter_factory=lambda _provider: adapter,
        )
        with pytest.raises(ArgusError) as excinfo:
            run_pipeline(other_config)
        assert "still active" in str(excinfo.value)


class TestStopHookDecision:
    def test_no_active_run_continues(self, tmp_path: Path):
        from argus.cli import stop_hook_decision

        assert stop_hook_decision(tmp_path / ".argus") == {"continue": True}

    def test_incomplete_run_blocks_with_next_action(self, paper_package: Path, tmp_path: Path):
        from argus.cli import stop_hook_decision

        adapter = FakeStageAdapter()
        run_pipeline(_config(paper_package, tmp_path, adapter), stop_after="diverged")
        decision = stop_hook_decision(tmp_path / ".argus")
        assert decision["decision"] == "block"
        assert "unresolved concerns" in decision["reason"]

    def test_complete_run_continues(self, paper_package: Path, tmp_path: Path):
        from argus.cli import stop_hook_decision

        adapter = FakeStageAdapter()
        run_pipeline(_config(paper_package, tmp_path, adapter))
        assert stop_hook_decision(tmp_path / ".argus") == {"continue": True}

    def test_missing_ledger_blocks_with_recovery_hint(self, paper_package: Path, tmp_path: Path):
        from argus.cli import stop_hook_decision

        adapter = FakeStageAdapter()
        run_pipeline(_config(paper_package, tmp_path, adapter), stop_after="diverged")
        import shutil

        shutil.rmtree(tmp_path / "out")
        decision = stop_hook_decision(tmp_path / ".argus")
        assert decision["decision"] == "block"
        assert "missing ledger" in decision["reason"]


class TestManifestMigration:
    def test_added_contract_field_with_default_is_not_drift(
        self, paper_package: Path, tmp_path: Path
    ):
        """A run recorded before a contract field existed must stay resumable."""
        adapter = FakeStageAdapter()
        config = _config(paper_package, tmp_path, adapter)
        run_pipeline(config, stop_after="diverged")
        manifest_path = tmp_path / "out" / "run-manifest.json"
        recorded = json.loads(manifest_path.read_text())
        del recorded["input_contract"]["paragraph_merge_chars"]
        manifest_path.write_text(json.dumps(recorded, indent=2, sort_keys=True))
        completion = run_pipeline(config)
        assert completion.complete

    def test_changed_recorded_value_is_still_drift(self, paper_package: Path, tmp_path: Path):
        adapter = FakeStageAdapter()
        config = _config(paper_package, tmp_path, adapter)
        run_pipeline(config, stop_after="diverged")
        manifest_path = tmp_path / "out" / "run-manifest.json"
        recorded = json.loads(manifest_path.read_text())
        recorded["input_contract"]["main_page_count"] = 9
        manifest_path.write_text(json.dumps(recorded, indent=2, sort_keys=True))
        with pytest.raises(ArgusError) as excinfo:
            run_pipeline(config)
        assert "drifted" in str(excinfo.value)


class TestEffectiveWorkers:
    def test_explicit_request_is_respected_and_clamped_to_pending(self):
        from argus.pipeline import effective_workers

        assert effective_workers("codex-cli", 100, 12) == 12
        assert effective_workers("codex-cli", 5, 12) == 5

    def test_auto_caps_cli_providers_lower_than_api(self, monkeypatch):
        import argus.pipeline as pipeline

        monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 32)
        assert pipeline.effective_workers("codex-cli", 100, None) == 8
        assert pipeline.effective_workers("openai-compat", 100, None) == 16

    def test_auto_respects_cpu_budget(self, monkeypatch):
        import argus.pipeline as pipeline

        monkeypatch.setattr(pipeline.os, "cpu_count", lambda: 6)
        assert pipeline.effective_workers("openai-compat", 100, None) == 4

    def test_pending_count_bounds_auto(self):
        from argus.pipeline import effective_workers

        assert effective_workers("openai-compat", 3, None) == 3
        assert effective_workers("openai-compat", 0, None) == 1

    def test_worker_choice_is_recorded_in_ledger(self, paper_package: Path, tmp_path: Path):
        adapter = FakeStageAdapter()
        config = _config(paper_package, tmp_path, adapter)
        run_pipeline(config)
        from argus.ledger import Ledger

        ledger = Ledger(tmp_path / "out" / "ledger.sqlite3")
        events = ledger.rows(
            "SELECT details FROM events WHERE event = 'workers_selected' ORDER BY sequence"
        )
        stages = [json.loads(event["details"])["stage"] for event in events]
        assert "diverge" in stages and "resolve" in stages


def _tex_config(
    tex_package: Path, tmp_path: Path, adapter: FakeStageAdapter, **options
) -> PipelineConfig:
    return PipelineConfig(
        package_root=tex_package,
        output_root=tmp_path / "out",
        stage_models=default_stage_models(SPEC),
        contract=InputContract(),
        state_root=tmp_path / ".argus",
        max_workers=options.pop("max_workers", 2),
        adapter_factory=lambda _provider: adapter,
        **options,
    )


class TestTexPipeline:
    def test_end_to_end_writes_the_revision_plan(self, tex_package: Path, tmp_path: Path):
        adapter = FakeStageAdapter()
        completion = run_pipeline(_tex_config(tex_package, tmp_path, adapter))
        assert completion.complete
        assert completion.undiagnosed_sections == 0
        assert adapter.calls.count("outline") == 4
        assert adapter.calls.count("structure") == 1
        output = tmp_path / "out"
        assert (output / "review.md").is_file()
        plan = (output / "revision-plan.md").read_text()
        assert "\u2014" not in plan
        assert (
            "Argus did not modify the manuscript. Apply only the rows the owner approves." in plan
        )
        for heading in (
            "## Decisions needed",
            "## Abstract",
            "## Introduction",
            "## Ablations (in Experiments)",
            "## Claim ledger",
            "## Sentence notes",
        ):
            assert heading in plan
        assert "| ID | Location | Reader learns | Flags | Action | Direction |" in plan
        assert "| P2 | main.tex:L" in plan
        assert "Start -> arrival:" in plan
        assert "Takes up the previous question: yes" in plan
        assert "| Issue R1 | P1 |" in plan
        assert "move before P" in plan
        assert "narrow-scope" in plan and "narrow_scope" in plan
        assert "connective fails" in plan
        assert "handoff breaks (object, point)" in plan
        assert "| W1 | P" in plan
        assert "fact_error" in plan
        data = json.loads((output / "revision-plan.json").read_text())
        assert data["manuscript_modified"] is False
        assert [section["section_id"] for section in data["sections"]] == [
            "sec_1",
            "sec_2",
            "sec_3",
            "sec_4",
        ]
        assert [row["id"] for section in data["sections"] for row in section["rows"]] == [
            f"P{index}" for index in range(1, 8)
        ]
        manifest = json.loads((output / "run-manifest.json").read_text())
        assert manifest["input_contract"]["source"] == "tex"
        assert manifest["argus_version"] == "0.2.0"

    def test_adjacent_pairs_join_paragraphs_only(self, tex_package: Path, tmp_path: Path):
        from argus.ledger import Ledger

        run_pipeline(_tex_config(tex_package, tmp_path, FakeStageAdapter()))
        ledger = Ledger(tmp_path / "out" / "ledger.sqlite3")
        run_id = ledger.rows("SELECT id FROM runs")[0]["id"]
        kinds = {unit["id"]: unit["kind"] for unit in ledger.list_units(run_id)}
        pairs = ledger.diagnoses(run_id, "pair")
        # Only Introduction holds two paragraphs; Experiments holds one
        # paragraph followed by a table and a figure, so it has no pair.
        assert len(pairs) == 1
        assert {kinds[pairs[0]["from_id"]], kinds[pairs[0]["to_id"]]} == {"paragraph"}
        floats = [unit_id for unit_id, kind in kinds.items() if kind != "paragraph"]
        assert {row["unit_id"] for row in ledger.diagnoses(run_id, "item")} >= set(floats)

    def test_tex_table_divergence_role_names_the_source(self, tex_package: Path, tmp_path: Path):
        prompts: list[str] = []
        adapter = FakeStageAdapter()
        original = adapter.complete

        def spy(prompt, spec, images=(), json_schema=None):
            prompts.append(prompt)
            return original(prompt, spec, images=images, json_schema=json_schema)

        adapter.complete = spy  # type: ignore[method-assign]
        run_pipeline(_tex_config(tex_package, tmp_path, adapter), stop_after="diverged")
        table_prompt = next(p for p in prompts if "UNIT TEXT" in p and "tabular" in p)
        assert "only the LaTeX source of the table below and its caption" in table_prompt

    def test_rerun_of_complete_tex_run_makes_no_model_calls(
        self, tex_package: Path, tmp_path: Path
    ):
        adapter = FakeStageAdapter()
        config = _tex_config(tex_package, tmp_path, adapter)
        run_pipeline(config)
        calls = len(adapter.calls)
        assert run_pipeline(config).complete
        assert len(adapter.calls) == calls

    def test_interrupted_diagnosis_resumes_only_missing_sections(
        self, tex_package: Path, tmp_path: Path
    ):
        failing = FakeStageAdapter(fail_outline_call=3)
        with pytest.raises(ArgusError):
            run_pipeline(_tex_config(tex_package, tmp_path, failing, max_workers=1))
        assert failing.calls.count("outline") == 4
        assert "structure" not in failing.calls
        from argus.ledger import Ledger

        ledger = Ledger(tmp_path / "out" / "ledger.sqlite3")
        run_id = ledger.rows("SELECT id FROM runs")[0]["id"]
        stalled = ledger.completion(run_id)
        assert stalled.stage == "diagnosing"
        assert stalled.undiagnosed_sections == 2  # one section block + the structure step
        assert stalled.next_action == "resume structural diagnosis"
        adapter = FakeStageAdapter()
        completion = run_pipeline(_tex_config(tex_package, tmp_path, adapter, max_workers=1))
        assert completion.complete
        assert adapter.calls == ["outline", "structure", "compose"]

    def test_completion_counts_undiagnosed_sections(self, tex_package: Path, tmp_path: Path):
        adapter = FakeStageAdapter()
        completion = run_pipeline(
            _tex_config(tex_package, tmp_path, adapter), stop_after="resolved"
        )
        assert completion.undiagnosed_sections == 5  # four section blocks + structure
        assert not completion.complete

    def test_preserve_wording_is_frozen_in_the_manifest(self, tex_package: Path, tmp_path: Path):
        adapter = FakeStageAdapter()
        run_pipeline(_tex_config(tex_package, tmp_path, adapter), stop_after="resolved")
        drifted = _tex_config(tex_package, tmp_path, adapter, preserve_wording=True)
        with pytest.raises(ArgusError) as excinfo:
            run_pipeline(drifted)
        assert "diagnosis" in str(excinfo.value)
