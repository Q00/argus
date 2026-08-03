"""Typer CLI for argus.

Model syntax: ``PROVIDER:MODEL`` (e.g. ``openai-compat:gpt-4o``,
``anthropic:claude-sonnet-5``, ``codex-cli:gpt-5.6-sol``,
``claude-cli:opus``). ``--model`` sets every stage; per-stage flags
override it, so a run can diverge on a cheap model and adjudicate on a
strong one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from argus.core import ArgusError
from argus.extract import InputContract
from argus.gateway import PROVIDERS, Gateway, ModelSpec, build_adapter
from argus.ledger import Ledger
from argus.pipeline import (
    PipelineConfig,
    default_stage_models,
    read_active_pointer,
    run_pipeline,
)

app = typer.Typer(
    name="argus", help="Ledger-audited one-shot paper review harness.", no_args_is_help=True
)


def parse_model(value: str) -> ModelSpec:
    provider, separator, model = value.partition(":")
    if not separator or not model or provider not in PROVIDERS:
        raise typer.BadParameter(
            f"expected PROVIDER:MODEL with provider in {', '.join(PROVIDERS)}; got {value!r}"
        )
    return ModelSpec(provider=provider, model=model)


def _apply_endpoint(spec: ModelSpec, base_url: str | None, api_key_env: str | None) -> ModelSpec:
    if base_url is None and api_key_env is None:
        return spec
    return ModelSpec(
        provider=spec.provider,
        model=spec.model,
        base_url=base_url or spec.base_url,
        api_key_env=api_key_env or spec.api_key_env,
        temperature=spec.temperature,
        max_output_tokens=spec.max_output_tokens,
        reasoning_effort=spec.reasoning_effort,
    )


@app.command()
def review(
    package: Annotated[
        Path,
        typer.Argument(help="Input package directory (main.pdf, appendix, reference-notes.md)"),
    ],
    output: Annotated[Path, typer.Option("--output", "-o", help="Run output directory")],
    model: Annotated[str, typer.Option("--model", "-m", help="PROVIDER:MODEL for all stages")],
    diverge_model: Annotated[
        str | None, typer.Option(help="Override for the diverge stage")
    ] = None,
    resolve_model: Annotated[
        str | None, typer.Option(help="Override for the resolve stage")
    ] = None,
    audit_model: Annotated[str | None, typer.Option(help="Override for the audit stage")] = None,
    compose_model: Annotated[
        str | None, typer.Option(help="Override for the compose stage")
    ] = None,
    base_url: Annotated[str | None, typer.Option(help="OpenAI-compatible endpoint root")] = None,
    api_key_env: Annotated[str | None, typer.Option(help="Env var holding the API key")] = None,
    pages: Annotated[
        int | None,
        typer.Option(
            help="Exact main.pdf page count; omit flag for the default (4), pass 0 for any"
        ),
    ] = None,
    policy: Annotated[str, typer.Option(help="Domain policy prompt stem")] = "policy-icml",
    workers: Annotated[int, typer.Option(help="Parallel divergence workers")] = 4,
    batch_size: Annotated[int, typer.Option(help="Concerns per resolver call")] = 32,
    stop_after: Annotated[
        str | None, typer.Option(help="Stop after stage: extracted|diverged|resolved")
    ] = None,
) -> None:
    """Run (or resume) a full review of one paper package."""
    base = _apply_endpoint(parse_model(model), base_url, api_key_env)
    stage_models = default_stage_models(base)
    for stage, override in (
        ("diverge", diverge_model),
        ("resolve", resolve_model),
        ("audit", audit_model),
        ("compose", compose_model),
    ):
        if override is not None:
            stage_models[stage] = _apply_endpoint(parse_model(override), base_url, api_key_env)
    if pages is None:
        contract = InputContract()
    else:
        contract = InputContract(main_page_count=None if pages == 0 else pages)
    config = PipelineConfig(
        package_root=package,
        output_root=output,
        stage_models=stage_models,
        contract=contract,
        policy=policy,
        max_workers=workers,
        batch_size=batch_size,
    )
    try:
        completion = run_pipeline(config, stop_after=stop_after)
    except ArgusError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(completion.as_dict(), indent=2, sort_keys=True))


def _active() -> tuple[Ledger, str]:
    active = read_active_pointer(Path.cwd() / ".argus")
    if active is None:
        typer.echo("error: no active argus review in this directory", err=True)
        raise typer.Exit(code=1)
    return Ledger(Path(active["ledger_path"])), active["run_id"]


def stop_hook_decision(state_root: Path) -> dict[str, object]:
    """Codex Stop-hook decision: block the session while a review is incomplete.

    No active run, or a physically complete run, lets the session stop. A
    missing ledger blocks with an actionable reason rather than silently
    letting an interrupted review look finished.
    """
    active = read_active_pointer(state_root)
    if active is None or active["complete"]:
        return {"continue": True}
    ledger_path = Path(active["ledger_path"])
    if not ledger_path.is_file():
        return {
            "decision": "block",
            "reason": (
                f"Active review {active['run_id']} points at a missing ledger "
                f"({ledger_path}). Restore the output directory or remove "
                f"{state_root / 'active.json'} before stopping."
            ),
        }
    completion = Ledger(ledger_path).completion(active["run_id"])
    if completion.complete:
        return {"continue": True}
    return {
        "decision": "block",
        "reason": (
            f"Argus review {completion.run_id} is incomplete at stage {completion.stage}: "
            f"{completion.open_units} open units, {completion.uncovered_claims} uncovered claims, "
            f"{completion.unresolved_concerns} unresolved concerns. "
            f"Next action: {completion.next_action}."
        ),
    }


@app.command("check-completion")
def check_completion() -> None:
    """Emit a Codex Stop-hook decision for the active run."""
    import sys

    if not sys.stdin.isatty():
        raw = sys.stdin.read()
        if raw.strip():
            json.loads(raw)  # hook payload must at least be valid JSON
    typer.echo(json.dumps(stop_hook_decision(Path.cwd() / ".argus"), sort_keys=True))


@app.command()
def status() -> None:
    """Show physical completion counts for the active run."""
    ledger, run_id = _active()
    typer.echo(json.dumps(ledger.completion(run_id).as_dict(), indent=2, sort_keys=True))


@app.command()
def attrition() -> None:
    """Show where concerns died and why for the active run."""
    ledger, run_id = _active()
    typer.echo(json.dumps(ledger.attrition(run_id), indent=2, sort_keys=True, ensure_ascii=False))


@app.command("recall-eval")
def recall_eval_command(
    issues: Annotated[
        Path, typer.Argument(help="JSON array of independent issues: [{id, text}, ...]")
    ],
    model: Annotated[str, typer.Option("--model", "-m", help="PROVIDER:MODEL for the judge")],
    base_url: Annotated[str | None, typer.Option(help="OpenAI-compatible endpoint root")] = None,
    api_key_env: Annotated[str | None, typer.Option(help="Env var holding the API key")] = None,
) -> None:
    """Judge harness recall against an independent issue list."""
    from argus.evalx import recall_eval

    ledger, run_id = _active()
    output_root = Path(ledger.run(run_id)["output_path"])
    spec = _apply_endpoint(parse_model(model), base_url, api_key_env)
    gateway = Gateway(build_adapter(spec.provider), output_root / "logs" / "recall")
    try:
        report = recall_eval(ledger, run_id, issues, gateway, spec, output_root)
    except ArgusError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    app()
