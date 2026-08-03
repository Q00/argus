"""Run manifest: freeze provenance as data, not prose.

The manifest is written at prepare time and re-verified on every resume.
It pins: input hashes, the input contract, per-stage model specs, prompt
and policy hashes, the segmenter version, and any environment overrides
that influence behavior (base URLs — never key values). If any pinned
value changes mid-run, the pipeline refuses to resume: a run's results are
attributable to exactly one configuration or the run is invalid.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from argus.core import ArgusError
from argus.extract import InputContract, Package
from argus.gateway import ModelSpec
from argus.prompts import prompt_hashes
from argus.segment import SEGMENTER_VERSION

PROMPT_NAMES = ["divergence", "resolution", "audit", "composition"]

# Env vars that change behavior and therefore must be visible in the
# manifest. API keys are recorded as set/unset only, never by value.
BEHAVIOR_ENV_VARS = ("OPENAI_BASE_URL",)
SECRET_ENV_VARS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def build_manifest(
    run_id: str,
    package: Package,
    package_hashes: dict[str, str],
    contract: InputContract,
    stage_models: dict[str, ModelSpec],
    policy_name: str,
    argus_version: str,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "argus_version": argus_version,
        "package_root": str(package.root),
        "package_files": package_hashes,
        "input_contract": contract.as_dict(),
        "stage_models": {stage: spec.as_dict() for stage, spec in stage_models.items()},
        "prompt_sha256": prompt_hashes([*PROMPT_NAMES, policy_name]),
        "policy": policy_name,
        "segmenter_version": SEGMENTER_VERSION,
        "environment": {
            **{name: os.environ.get(name) for name in BEHAVIOR_ENV_VARS},
            **{name: bool(os.environ.get(name)) for name in SECRET_ENV_VARS},
        },
    }


def write_manifest(output_root: Path, manifest: dict[str, Any]) -> Path:
    path = output_root / "run-manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def verify_manifest(output_root: Path, manifest: dict[str, Any]) -> None:
    """Refuse to resume a run whose frozen configuration has drifted.

    Environment values are allowed to differ (keys rotate, tunnels move);
    everything that determines *what the models are asked* must match.
    """
    path = output_root / "run-manifest.json"
    if not path.is_file():
        return
    recorded = json.loads(path.read_text(encoding="utf-8"))
    frozen_keys = (
        "run_id",
        "package_files",
        "input_contract",
        "stage_models",
        "prompt_sha256",
        "policy",
        "segmenter_version",
    )
    drift = {
        key: {"recorded": recorded.get(key), "current": manifest.get(key)}
        for key in frozen_keys
        if recorded.get(key) != manifest.get(key)
    }
    if drift:
        raise ArgusError(
            "run configuration drifted since this run was created; "
            "start a new output directory or restore the recorded configuration",
            {"drifted_keys": sorted(drift)},
        )
