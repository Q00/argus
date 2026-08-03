"""CLI agent adapters: ``codex exec`` and ``claude -p``.

These exist for subscription-billed usage. They are honest about being the
weaker isolation tier: a CLI agent is a process on this machine, so the
completion is recorded with ``isolation="process"`` rather than
``payload``. Each invocation runs in a fresh temporary workspace containing
only the staged payload, with the most restrictive flags each tool offers
(read-only sandbox, no web search, no user config), but the guarantee is
"restricted process", not "isolation by construction". Pick the API
adapters when the isolation claim matters.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any

from argus.core import ProviderError, Result
from argus.gateway.base import ModelSpec, RawCompletion

_TRANSIENT_MARKERS = ("timeout", "timed out", "rate limit", "overloaded", "connection", "retry")


def _transient(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def _codex_provider_flags(spec: ModelSpec) -> list[str]:
    """Explicit provider injection for hermetic codex runs.

    ``--ignore-user-config`` discards ``~/.codex/config.toml`` — including
    any custom provider the user's auth depends on (observed: a proxy
    provider with ``env_key`` auth; without this injection codex falls back
    to api.openai.com unauthenticated and dies with 401). When a base URL
    and key are available (spec first, then OPENAI_BASE_URL/OPENAI_API_KEY),
    the provider is passed explicitly so the run stays hermetic AND
    authenticated. The base URL is already recorded in the run manifest.
    """
    base_url = spec.base_url or os.environ.get("OPENAI_BASE_URL", "").strip()
    key_env = spec.api_key_env or "OPENAI_API_KEY"
    if not base_url or not os.environ.get(key_env, "").strip():
        return []
    return [
        "-c",
        'model_provider="argus_env"',
        "-c",
        'model_providers.argus_env.name="Argus environment provider"',
        "-c",
        f"model_providers.argus_env.base_url={json.dumps(base_url)}",
        "-c",
        'model_providers.argus_env.wire_api="responses"',
        "-c",
        f"model_providers.argus_env.env_key={json.dumps(key_env)}",
    ]


def _run(
    command: list[str],
    prompt: str,
    cwd: Path,
    provider: str,
    timeout_seconds: float,
) -> Result[subprocess.CompletedProcess[str], ProviderError]:
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=cwd,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Result.err(
            ProviderError(
                f"CLI call exceeded {timeout_seconds}s",
                provider=provider,
                details={"transient": True},
            )
        )
    except OSError as exc:
        return Result.err(ProviderError(f"failed to launch CLI: {exc}", provider=provider))
    if completed.returncode != 0:
        return Result.err(
            ProviderError(
                f"CLI exited {completed.returncode}",
                provider=provider,
                details={
                    "stderr": completed.stderr[-2000:],
                    "transient": _transient(completed.stderr),
                },
            )
        )
    return Result.of(completed)


class CodexCliAdapter:
    """``codex exec`` with an ephemeral read-only workspace."""

    def __init__(self, cli_path: str | None = None, timeout_seconds: float = 1200.0):
        self._cli_path = cli_path
        self._timeout = timeout_seconds

    def complete(
        self,
        prompt: str,
        spec: ModelSpec,
        images: Sequence[Path] = (),
        json_schema: dict[str, Any] | None = None,
    ) -> Result[RawCompletion, ProviderError]:
        codex = self._cli_path or shutil.which("codex")
        if codex is None:
            return Result.err(ProviderError("codex CLI not found in PATH", provider="codex-cli"))
        with tempfile.TemporaryDirectory(prefix="argus-codex-") as workdir:
            workspace = Path(workdir)
            output_path = workspace / "output.txt"
            command = [
                codex,
                "-a",
                "never",
                "--disable",
                "web_search",
                *_codex_provider_flags(spec),
            ]
            if spec.reasoning_effort:
                command.extend(["-c", f'model_reasoning_effort="{spec.reasoning_effort}"'])
            command.extend(
                [
                    "exec",
                    "--skip-git-repo-check",
                    "--ephemeral",
                    "--ignore-user-config",
                    "--ignore-rules",
                    "-s",
                    "read-only",
                    "-C",
                    str(workspace),
                    "-m",
                    spec.model,
                    "-o",
                    str(output_path),
                ]
            )
            staged: list[Path] = []
            for index, image in enumerate(images):
                target = workspace / f"asset-{index}{image.suffix}"
                shutil.copy2(image, target)
                staged.append(target)
            for target in staged:
                command.extend(["-i", str(target)])
            command.append("-")
            result = _run(command, prompt, workspace, "codex-cli", self._timeout)
            if result.is_err:
                return Result.err(result.error)
            completed = result.unwrap()
            text = (
                output_path.read_text(encoding="utf-8")
                if output_path.is_file()
                else completed.stdout
            )
            if not text.strip():
                return Result.err(
                    ProviderError("codex produced empty output", provider="codex-cli")
                )
            return Result.of(RawCompletion(text=text, model=spec.model, isolation="process"))


class ClaudeCliAdapter:
    """``claude -p`` one-shot, read-only tools, no session persistence."""

    def __init__(self, cli_path: str | None = None, timeout_seconds: float = 1200.0):
        self._cli_path = cli_path
        self._timeout = timeout_seconds

    def complete(
        self,
        prompt: str,
        spec: ModelSpec,
        images: Sequence[Path] = (),
        json_schema: dict[str, Any] | None = None,
    ) -> Result[RawCompletion, ProviderError]:
        claude = self._cli_path or shutil.which("claude")
        if claude is None:
            return Result.err(ProviderError("claude CLI not found in PATH", provider="claude-cli"))
        with tempfile.TemporaryDirectory(prefix="argus-claude-") as workdir:
            workspace = Path(workdir)
            full_prompt = prompt
            if images:
                names = []
                for index, image in enumerate(images):
                    target = workspace / f"asset-{index}{image.suffix}"
                    shutil.copy2(image, target)
                    names.append(target.name)
                full_prompt = (
                    prompt
                    + "\n\nAttached visual assets (read them with the Read tool): "
                    + ", ".join(names)
                )
            command = [
                claude,
                "-p",
                "--model",
                spec.model,
                "--output-format",
                "text",
                "--no-session-persistence",
                "--allowedTools",
                "Read" if images else "",
                "--disallowedTools",
                "Bash,Write,Edit,WebSearch,WebFetch,Agent",
            ]
            result = _run(command, full_prompt, workspace, "claude-cli", self._timeout)
            if result.is_err:
                return Result.err(result.error)
            text = result.unwrap().stdout
            if not text.strip():
                return Result.err(
                    ProviderError("claude produced empty output", provider="claude-cli")
                )
            return Result.of(RawCompletion(text=text, model=spec.model, isolation="process"))


def parse_cli_json(text: str) -> Any:
    """Best-effort parse for CLI outputs that may wrap JSON in logs."""
    return json.loads(text)
