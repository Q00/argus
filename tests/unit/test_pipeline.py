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
            "policy-icml",
        }
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
