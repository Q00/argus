"""Stage orchestrator: extract → diverge → resolve → audit → diagnose → compose.

Isolation model: divergence workers are gateway calls whose payload is
exactly one unit's text (+ optional cropped image). With API adapters
nothing else exists for the model to see — isolation by construction.
There is no sandbox because there is no process to sandbox.

Resume model: the ledger plus deterministic IDs make every stage
re-entrant. The run manifest is verified before resuming so a resumed run
provably uses the configuration it started with.
"""

from __future__ import annotations

from collections.abc import Callable
import concurrent.futures
import contextlib
import dataclasses
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any
from urllib.parse import urlparse

from argus.core import ArgusError, sha256_file, stable_id
from argus.evalx import load_issue_file
from argus.extract import InputContract, Package, extract_package, validate_package
from argus.gateway import Gateway, LLMAdapter, ModelSpec, build_adapter
from argus.ledger import RUN_STAGES, TERMINAL_CONCERN_STATES, Completion, Ledger
from argus.manifest import build_manifest, verify_manifest, write_manifest
from argus.prompts import load_prompt
from argus.revision import write_revision_plan
from argus.schemas import (
    audit_schema,
    composition_schema,
    divergence_schema,
    outline_schema,
    resolution_schema,
    structure_schema,
)
from argus.segment import split_sentences
from argus.tex import catalog_cell_ids, render_catalog
from argus.validate import (
    validate_audit_partition,
    validate_composition_ids,
    validate_divergence,
    validate_outline,
    validate_resolutions,
    validate_structure,
)

ARGUS_VERSION = "0.2.0"

STAGE_NAMES = ("diverge", "resolve", "audit", "diagnose", "compose")

# Worker ceilings by adapter cost profile: a CLI provider spawns a whole
# agent process per call, an API provider is a single HTTP request.
CLI_PROVIDER_WORKER_CAP = 8
API_PROVIDER_WORKER_CAP = 16


def effective_workers(provider: str, pending: int, requested: int | None) -> int:
    """Pick a worker count from pending work, CPU budget, and provider cost.

    An explicit request is respected (clamped to the pending count); auto
    mode takes min(provider cap, cpu-2, pending). Work is network-bound, so
    the CPU term only keeps process spawn overhead reasonable.
    """
    if pending <= 0:
        return 1
    if requested is not None:
        return max(1, min(requested, pending))
    cap = CLI_PROVIDER_WORKER_CAP if provider.endswith("-cli") else API_PROVIDER_WORKER_CAP
    cpu = os.cpu_count() or 4
    return max(1, min(cap, max(2, cpu - 2), pending))


def default_stage_models(spec: ModelSpec) -> dict[str, ModelSpec]:
    """One spec for every stage; callers can mix providers per stage."""
    return dict.fromkeys(STAGE_NAMES, spec)


class PipelineConfig:
    """Everything a run needs, bound once.

    Args:
        package_root: Input package directory.
        output_root: Run output directory (ledger, logs, review).
        stage_models: Mapping of stage name → ModelSpec.
        contract: Input contract; recorded in the run manifest.
        policy: Domain policy prompt stem (e.g. ``policy-icml``).
        state_root: Directory for the active-run pointer and lock.
        max_workers: Divergence/resolution/diagnosis parallelism; None picks
            a count per stage from pending work, CPU budget, and provider cost.
        batch_size: Concerns per resolver call.
        preserve_wording: Diagnosis reports only factual errors in sentences
            (a coauthor's wording is final).
    """

    def __init__(
        self,
        package_root: Path,
        output_root: Path,
        stage_models: dict[str, ModelSpec],
        contract: InputContract | None = None,
        policy: str = "policy-icml",
        state_root: Path | None = None,
        max_workers: int | None = None,
        batch_size: int = 32,
        adapter_factory: Callable[[str], LLMAdapter] = build_adapter,
        preserve_wording: bool = False,
    ):
        missing = [stage for stage in STAGE_NAMES if stage not in stage_models]
        if missing:
            raise ArgusError("stage_models is incomplete", {"missing": missing})
        self.package_root = Path(package_root)
        self.output_root = Path(output_root).resolve()
        self.stage_models = stage_models
        self.contract = contract if contract is not None else InputContract()
        self.policy = policy
        self.state_root = Path(state_root) if state_root else Path.cwd() / ".argus"
        self.max_workers = None if max_workers is None else max(1, max_workers)
        self.batch_size = max(1, batch_size)
        self.adapter_factory = adapter_factory
        self.preserve_wording = preserve_wording

    def gateway(self, stage: str, log_dir: str) -> tuple[Gateway, ModelSpec]:
        spec = self.stage_models[stage]
        adapter = self.adapter_factory(spec.provider)
        return Gateway(adapter, self.output_root / "logs" / log_dir), spec


# -- state pointer & lock ---------------------------------------------------


def read_active_pointer(state_root: Path) -> dict[str, Any] | None:
    pointer = state_root / "active.json"
    if not pointer.is_file():
        return None
    value = json.loads(pointer.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ArgusError(f"active pointer must contain an object: {pointer}")
    for key, kind in (
        ("run_id", str),
        ("ledger_path", str),
        ("output_root", str),
        ("complete", bool),
    ):
        if not isinstance(value.get(key), kind):
            raise ArgusError(f"active pointer has invalid {key!r}: {pointer}")
    return value


def _write_active_pointer(
    state_root: Path, run_id: str, ledger_path: Path, output_root: Path, complete: bool
) -> None:
    state_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "ledger_path": str(ledger_path.resolve()),
        "output_root": str(output_root.resolve()),
        "complete": complete,
    }
    descriptor, temporary_name = tempfile.mkstemp(dir=state_root, prefix=".active.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state_root / "active.json")
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def _pipeline_lock(state_root: Path) -> Any:
    state_root.mkdir(parents=True, exist_ok=True)
    lock_path = state_root / "pipeline.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArgusError(f"another argus run holds the lock: {lock_path}") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _reject_parallel_active_run(state_root: Path, next_run_id: str) -> None:
    active = read_active_pointer(state_root)
    if active is None or active["run_id"] == next_run_id:
        return
    ledger_path = Path(active["ledger_path"])
    if not ledger_path.is_file():
        raise ArgusError(f"active review ledger is missing: {ledger_path}")
    completion = Ledger(ledger_path).completion(active["run_id"])
    if not completion.complete:
        raise ArgusError(
            f"review {completion.run_id} is still active at stage {completion.stage}; "
            f"finish or resume it before starting {next_run_id}"
        )


# -- research notes ---------------------------------------------------------


def load_research_notes(path: Path) -> dict[str, Any] | None:
    """Validate operator-supplied external research evidence.

    External research enters the pipeline only through this file — an
    explicit, hash-auditable input rather than a live search the run
    cannot reproduce.
    """
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ArgusError("research notes must contain an object")

    def nonempty(item: Any, location: str) -> str:
        if not isinstance(item, str) or not item.strip():
            raise ArgusError(f"research notes {location} must be a non-empty string")
        return item

    nonempty(value.get("query"), "query")
    accessed = nonempty(value.get("accessed_at"), "accessed_at")
    parsed = dt.datetime.fromisoformat(accessed.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ArgusError("research notes accessed_at must include a timezone")
    sources = value.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ArgusError("research notes sources must be a non-empty array")
    for index, source in enumerate(sources):
        location = f"sources[{index}]"
        if not isinstance(source, dict):
            raise ArgusError(f"research notes {location} must be an object")
        nonempty(source.get("title"), f"{location}.title")
        url = nonempty(source.get("url"), f"{location}.url")
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ArgusError(f"research notes {location}.url must be an HTTP(S) URL")
        nonempty(source.get("evidence"), f"{location}.evidence")
    return value


# -- prepare ----------------------------------------------------------------


def prepare(config: PipelineConfig) -> tuple[str, Package, Ledger]:
    package = validate_package(config.package_root, config.contract)
    contract = dataclasses.replace(config.contract, source=package.source)
    config.output_root.mkdir(parents=True, exist_ok=True)
    package_hashes = {name: sha256_file(path) for name, path in package.files.items()}
    run_id = stable_id("run", *sorted(package_hashes.values()), config.output_root)
    _reject_parallel_active_run(config.state_root, run_id)
    manifest = build_manifest(
        run_id,
        package,
        package_hashes,
        contract,
        config.stage_models,
        config.policy,
        ARGUS_VERSION,
        {"preserve_wording": config.preserve_wording},
    )
    verify_manifest(config.output_root, manifest)
    write_manifest(config.output_root, manifest)
    ledger_path = config.output_root / "ledger.sqlite3"
    ledger = Ledger(ledger_path)
    ledger.create_run(run_id, package.root, config.output_root)
    _write_active_pointer(config.state_root, run_id, ledger_path, config.output_root, False)
    run = ledger.run(run_id)
    if RUN_STAGES.index(run["stage"]) <= RUN_STAGES.index("created"):
        extracted = extract_package(package, run_id, config.output_root, contract)
        if not extracted["units"]:
            raise ArgusError("input package produced no review units")
        for unit in extracted["units"]:
            ledger.register_unit(unit)
        ledger.set_stage(run_id, "extracted")
    return run_id, package, ledger


# -- diverge ----------------------------------------------------------------


_DIVERGENCE_ROLES = {
    "visual": (
        "You are a deliberately narrow visual critic. You can see only the attached cropped "
        "{kind} and its caption text. Inspect panels, axes, legends, labels, uncertainty, "
        "baselines, scale, table headers, footnotes, and whether the visible evidence supports "
        "every caption claim."
    ),
    "paragraph": (
        "You are a deliberately narrow paragraph critic. You can see only the paragraph below. "
        "Treat evidence absent from this paragraph as absent for divergence. If a sentence relies "
        "on a Figure or Table you cannot see, ask whether that asset supports the claimed strength."
    ),
    "source": (
        "You are a deliberately narrow {kind} critic. You can see only the LaTeX source of the "
        "{kind} below and its caption; there is no rendered image. Inspect headers, labels, units, "
        "baselines, uncertainty, footnotes, and whether the values in the source support every "
        "caption claim."
    ),
}


def diverge_unit(unit: dict[str, Any], gateway: Gateway, spec: ModelSpec, ledger: Ledger) -> None:
    ledger.set_unit_status(unit["id"], "processing")
    content = Path(unit["content_path"]).read_text(encoding="utf-8").strip()
    segmentation = split_sentences(content)
    sentences = list(segmentation.sentences)
    if not sentences:
        ledger.set_unit_status(unit["id"], "failed")
        raise ArgusError(f"unit {unit['id']} contains no sentences")
    role_key = "paragraph"
    if unit["kind"] in {"figure", "table"}:
        # A tex table (or a figure without \includegraphics) has no image.
        role_key = "visual" if unit.get("image_path") else "source"
    role = _DIVERGENCE_ROLES[role_key].format(kind=unit["kind"])
    numbered = "\n".join(f"S{index}: {sentence}" for index, sentence in enumerate(sentences, 1))
    prompt = load_prompt("divergence").render(
        role=role, sentence_count=len(sentences), numbered_sentences=numbered
    )
    images = [Path(unit["image_path"])] if unit.get("image_path") else []
    try:
        result = gateway.complete_json(unit["id"], prompt, divergence_schema(), spec, images=images)
        validate_divergence(result, len(sentences))
        ledger.record_divergence(unit["id"], sentences, result["items"])
    except Exception:
        ledger.set_unit_status(unit["id"], "failed")
        raise


def diverge(run_id: str, config: PipelineConfig, ledger: Ledger) -> None:
    ledger.set_stage(run_id, "diverging")
    gateway, spec = config.gateway("diverge", "divergence")
    units = [unit for unit in ledger.list_units(run_id) if unit["status"] != "complete"]
    workers = effective_workers(spec.provider, len(units), config.max_workers)
    ledger.note(
        run_id,
        "workers_selected",
        {
            "stage": "diverge",
            "workers": workers,
            "pending": len(units),
            "requested": config.max_workers,
        },
    )
    errors: list[BaseException] = []
    lock = threading.Lock()

    def execute(unit: dict[str, Any]) -> None:
        try:
            diverge_unit(unit, gateway, spec, ledger)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(execute, unit) for unit in units]:
            future.result()
    if errors:
        raise ArgusError(f"{len(errors)} divergence workers failed; first: {errors[0]}")
    completion = ledger.completion(run_id)
    if completion.open_units or completion.uncovered_claims:
        raise ArgusError(json.dumps(completion.as_dict(), sort_keys=True))
    ledger.set_stage(run_id, "diverged")
    export_ledger(ledger, run_id, config.output_root)


# -- resolve ----------------------------------------------------------------


def resolve(run_id: str, config: PipelineConfig, ledger: Ledger) -> None:
    ledger.set_stage(run_id, "resolving")
    gateway, spec = config.gateway("resolve", "resolution")
    corpus = json.loads((config.output_root / "corpus.json").read_text(encoding="utf-8"))
    research = load_research_notes(config.output_root / "research-notes.json")
    policy = load_prompt(config.policy).text
    template = load_prompt("resolution")
    rows = ledger.rows(
        """
        SELECT c.*, cl.claim_text, cl.sentence_text
        FROM concerns c JOIN claims cl ON cl.id = c.claim_id
        WHERE c.run_id = ? AND c.status NOT IN (?, ?, ?, ?)
        ORDER BY c.created_at, c.id
        """,
        (run_id, *sorted(TERMINAL_CONCERN_STATES)),
    )
    owner = f"resolver-{run_id}"
    batches = [rows[i : i + config.batch_size] for i in range(0, len(rows), config.batch_size)]

    def adjudicate(batch_index: int, batch: list[dict[str, Any]]) -> None:
        # Leases are claimed by the worker that will actually resolve the
        # batch, not eagerly upfront — a lease should mean "in progress".
        for concern in batch:
            ledger.claim_specific(concern["id"], owner, lease_seconds=1800)
        prompt_concerns = [
            {
                "concern_id": concern["id"],
                "locator": concern["locator"],
                "sentence": concern["sentence_text"],
                "atomic_claim": concern["claim_text"],
                "category": concern["category"],
                "question": concern["question"],
            }
            for concern in batch
        ]
        expected = {concern["id"] for concern in batch}
        prompt = template.render(
            policy=policy,
            corpus=json.dumps(corpus, ensure_ascii=False),
            research=json.dumps(research, ensure_ascii=False) if research else "No supplied notes.",
            concerns=json.dumps(prompt_concerns, ensure_ascii=False),
        )
        result = gateway.complete_json(
            f"resolver-{batch_index:03d}", prompt, resolution_schema(expected), spec
        )
        validate_resolutions(result, expected)
        for resolution in result["resolutions"]:
            resolution_key = stable_id(
                "resolution",
                resolution["concern_id"],
                resolution["status"],
                resolution["evidence"],
                resolution["source"],
            )
            ledger.resolve_concern(
                resolution["concern_id"],
                owner,
                resolution["status"],
                resolution["evidence"],
                resolution["source"],
                resolution["materiality"],
                resolution_key,
                terminal_stage="resolve",
            )

    workers = effective_workers(spec.provider, len(batches), config.max_workers)
    ledger.note(
        run_id,
        "workers_selected",
        {
            "stage": "resolve",
            "workers": workers,
            "pending_batches": len(batches),
            "requested": config.max_workers,
        },
    )
    errors: list[BaseException] = []
    lock = threading.Lock()

    def execute(index_batch: tuple[int, list[dict[str, Any]]]) -> None:
        try:
            adjudicate(*index_batch)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(execute, item) for item in enumerate(batches)]:
            future.result()
    export_ledger(ledger, run_id, config.output_root)
    if errors:
        raise ArgusError(f"{len(errors)} resolution workers failed; first: {errors[0]}")
    completion = ledger.completion(run_id)
    if completion.unresolved_concerns:
        raise ArgusError(json.dumps(completion.as_dict(), sort_keys=True))
    audit_final_concerns(run_id, config, ledger, corpus)
    ledger.set_stage(run_id, "resolved")
    write_attrition(ledger, run_id, config.output_root)


# -- audit ------------------------------------------------------------------


def audit_final_concerns(
    run_id: str, config: PipelineConfig, ledger: Ledger, corpus: dict[str, Any]
) -> None:
    concerns = ledger.rows(
        "SELECT id, locator, category, question, evidence, source, materiality "
        "FROM concerns WHERE run_id = ? AND status = 'promoted_final' "
        "ORDER BY CASE materiality WHEN 'major' THEN 0 ELSE 1 END, created_at, id",
        (run_id,),
    )
    if not concerns:
        return
    gateway, spec = config.gateway("audit", "audit")
    expected = {concern["id"] for concern in concerns}
    policy = load_prompt(config.policy).text
    prompt = load_prompt("audit").render(
        policy=policy,
        concern_count=len(concerns),
        corpus=json.dumps(corpus, ensure_ascii=False),
        concerns=json.dumps(concerns, ensure_ascii=False),
    )
    name = f"audit-{stable_id('audit', *sorted(expected))[-12:]}"
    result = gateway.complete_json(name, prompt, audit_schema(expected), spec)
    validate_audit_partition(result, expected)
    for item in result["discards"]:
        ledger.discard_promoted_concern(item["concern_id"], item["reason"], item["canonical_id"])
    export_ledger(ledger, run_id, config.output_root)


# -- diagnose ---------------------------------------------------------------


def _read_sections(output_root: Path) -> list[dict[str, Any]]:
    """Section blocks written at extraction; absent (PDF) means no blocks."""
    path = output_root / "sections.json"
    if not path.is_file():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ArgusError(f"sections must contain an array: {path}")
    return value


def _unit_sentences(unit: dict[str, Any]) -> list[str]:
    # The same segmentation as divergence, so ordinals match the claims table.
    content = Path(unit["content_path"]).read_text(encoding="utf-8").strip()
    return list(split_sentences(content).sentences)


def diagnose(run_id: str, config: PipelineConfig, ledger: Ledger) -> None:
    """Structural diagnosis of a tex manuscript: per-section outline, then structure.

    Step 1 runs one outline call per unfinished section block in parallel;
    step 2 runs one structure call over the whole outline. A run without
    section blocks (a PDF package) is recorded and passes with no calls.
    """
    ledger.set_stage(run_id, "diagnosing")
    sections = _read_sections(config.output_root)
    if not sections:
        ledger.note(run_id, "diagnosis_skipped", {"reason": "package has no section blocks"})
        ledger.set_stage(run_id, "diagnosed")
        return
    gateway, spec = config.gateway("diagnose", "diagnosis")
    units = {unit["id"]: unit for unit in ledger.list_units(run_id)}
    order = [unit_id for section in sections for unit_id in section["unit_ids"]]
    short = {unit_id: f"P{index}" for index, unit_id in enumerate(order, 1)}
    section_ids = [section["section_id"] for section in sections]
    tables = json.loads((config.output_root / "table-cells.json").read_text(encoding="utf-8"))[
        "tables"
    ]
    cell_ids = catalog_cell_ids(tables)
    catalog = render_catalog(tables)
    final_concerns = [
        {**concern, "short_id": short.get(concern["unit_id"])}
        for concern in ledger.rows(
            "SELECT id AS concern_id, unit_id, locator, category, question, evidence, materiality "
            "FROM concerns WHERE run_id = ? AND status = 'promoted_final' "
            "ORDER BY CASE materiality WHEN 'major' THEN 0 ELSE 1 END, created_at, id",
            (run_id,),
        )
    ]
    main_order = json.dumps(
        [
            {
                "section_id": section["section_id"],
                "title": section["title"],
                "unit_ids": section["unit_ids"],
            }
            for section in sections
        ],
        ensure_ascii=False,
    )
    move_targets = set(order) | set(section_ids)
    wording_mode = "preserve wording" if config.preserve_wording else "normal"
    template = load_prompt("outline")

    def context(unit_id: str | None) -> dict[str, Any] | None:
        if unit_id is None:
            return None
        unit = units[unit_id]
        return {
            "unit_id": unit_id,
            "short_id": short[unit_id],
            "locator": unit["locator"],
            "text": Path(unit["content_path"]).read_text(encoding="utf-8").strip(),
        }

    def outline(index: int) -> None:
        section = sections[index]
        unit_ids = section["unit_ids"]
        sentences: dict[tuple[str, int], str] = {}
        payload = []
        for unit_id in unit_ids:
            numbered = _unit_sentences(units[unit_id])
            sentences.update({(unit_id, ordinal): text for ordinal, text in enumerate(numbered, 1)})
            payload.append(
                {
                    "unit_id": unit_id,
                    "short_id": short[unit_id],
                    "kind": units[unit_id]["kind"],
                    "locator": units[unit_id]["locator"],
                    "sentences": [
                        {"ordinal": ordinal, "text": text}
                        for ordinal, text in enumerate(numbered, 1)
                    ],
                }
            )
        # Pairs join consecutive paragraphs only: a float does not take up a
        # paragraph's question, so floats in between are skipped.
        paragraphs = [unit_id for unit_id in unit_ids if units[unit_id]["kind"] == "paragraph"]
        pairs = list(zip(paragraphs, paragraphs[1:], strict=False))
        members = set(unit_ids)
        concerns = [concern for concern in final_concerns if concern["unit_id"] in members]
        concern_ids = {concern["concern_id"] for concern in concerns}
        prompt = template.render(
            wording_mode=wording_mode,
            unit_count=len(unit_ids),
            pair_count=len(pairs),
            sentence_count=len(sentences),
            concern_count=len(concerns),
            section=json.dumps(
                {
                    "section_id": section["section_id"],
                    "title": section["title"],
                    "parent_title": section["parent_title"],
                },
                ensure_ascii=False,
            ),
            main_order=main_order,
            context=json.dumps(
                {
                    "before": context(sections[index - 1]["unit_ids"][-1] if index else None),
                    "after": context(
                        sections[index + 1]["unit_ids"][0] if index + 1 < len(sections) else None
                    ),
                },
                ensure_ascii=False,
            ),
            pairs=json.dumps([{"from_id": a, "to_id": b} for a, b in pairs]),
            concerns=json.dumps(concerns, ensure_ascii=False),
            catalog=catalog,
            units=json.dumps(payload, ensure_ascii=False),
        )
        schema = outline_schema(
            unit_ids, len(pairs), len(sentences), concern_ids, config.preserve_wording
        )
        result = gateway.complete_json(f"outline-{section['section_id']}", prompt, schema, spec)
        validate_outline(
            result,
            unit_ids,
            pairs,
            sentences,
            concern_ids,
            cell_ids,
            move_targets,
            config.preserve_wording,
        )
        ledger.record_section_diagnosis(run_id, section["section_id"], result)

    done = ledger.diagnosed_sections(run_id)
    pending = [index for index, section in enumerate(sections) if section["section_id"] not in done]
    workers = effective_workers(spec.provider, len(pending), config.max_workers)
    ledger.note(
        run_id,
        "workers_selected",
        {
            "stage": "diagnose",
            "workers": workers,
            "pending_sections": len(pending),
            "requested": config.max_workers,
        },
    )
    errors: list[BaseException] = []
    lock = threading.Lock()

    def execute(index: int) -> None:
        try:
            outline(index)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(execute, index) for index in pending]:
            future.result()
    if errors:
        export_ledger(ledger, run_id, config.output_root)
        raise ArgusError(f"{len(errors)} outline workers failed; first: {errors[0]}")
    if not ledger.structure_diagnosed(run_id):
        diagnose_structure(run_id, config, ledger, sections, short, final_concerns)
    completion = ledger.completion(run_id)
    if completion.undiagnosed_sections:
        raise ArgusError(json.dumps(completion.as_dict(), sort_keys=True))
    ledger.set_stage(run_id, "diagnosed")
    export_ledger(ledger, run_id, config.output_root)


def diagnose_structure(
    run_id: str,
    config: PipelineConfig,
    ledger: Ledger,
    sections: list[dict[str, Any]],
    short: dict[str, str],
    final_concerns: list[dict[str, Any]],
) -> None:
    gateway, spec = config.gateway("diagnose", "diagnosis")
    units = {unit["id"]: unit for unit in ledger.list_units(run_id)}
    items = {row["unit_id"]: row for row in ledger.diagnoses(run_id, "item")}
    issues_path = Path(config.package_root) / "issues.json"
    issues = load_issue_file(issues_path) if issues_path.is_file() else []
    section_ids = [section["section_id"] for section in sections]
    issue_ids = {issue["id"] for issue in issues}
    outline = [
        {
            "section_id": section["section_id"],
            "title": section["title"],
            "parent_title": section["parent_title"],
            "units": [
                {
                    "unit_id": unit_id,
                    "short_id": short[unit_id],
                    "locator": units[unit_id]["locator"],
                    "learns": items[unit_id]["learns"],
                    "action": items[unit_id]["action"],
                    "move_before": items[unit_id]["move_before"],
                }
                for unit_id in section["unit_ids"]
            ],
        }
        for section in sections
    ]
    body = [
        {
            "unit_id": unit_id,
            "short_id": short[unit_id],
            "section_id": section["section_id"],
            "text": Path(units[unit_id]["content_path"]).read_text(encoding="utf-8").strip(),
        }
        for section in sections
        for unit_id in section["unit_ids"]
    ]
    prompt = load_prompt("structure").render(
        section_count=len(sections),
        issue_count=len(issues),
        outline=json.dumps(outline, ensure_ascii=False),
        concerns=json.dumps(final_concerns, ensure_ascii=False),
        issues=json.dumps(issues, ensure_ascii=False),
        body=json.dumps(body, ensure_ascii=False),
    )
    result = gateway.complete_json(
        "structure", prompt, structure_schema(section_ids, issue_ids), spec
    )
    validate_structure(result, section_ids, issue_ids, set(short))
    ledger.record_structure_diagnosis(run_id, result)


# -- compose ----------------------------------------------------------------


def _render_review(composition: dict[str, Any], final_concerns: list[dict[str, Any]]) -> str:
    by_id = {concern["id"]: concern for concern in final_concerns}
    lines = ["# Review", "", "## Summary", "", composition["summary"].strip(), ""]
    lines.extend(["## Strengths", ""])
    if composition["strengths"]:
        lines.extend(f"- {strength.strip()}" for strength in composition["strengths"])
    else:
        lines.append("- No material strength could be established from the supplied evidence.")
    lines.extend(["", "## Weaknesses", ""])
    if not composition["concern_order"]:
        lines.append("No material concern remained after full-context adjudication.")
    for index, concern_id in enumerate(composition["concern_order"], 1):
        concern = by_id[concern_id]
        lines.extend(
            [
                f"### W{index}. {concern['category'].replace('_', ' ').title()} ({concern['materiality']})",
                "",
                concern["question"].strip(),
                "",
                f"Evidence: {concern['evidence'].strip()}",
                "",
                f"Locator/source: {concern['locator']}; {concern['source'].strip()}",
                "",
            ]
        )
    lines.extend(
        [
            "## Scores",
            "",
            f"- Soundness: {composition['soundness']}/4",
            f"- Presentation: {composition['presentation']}/4",
            f"- Significance: {composition['significance']}/4",
            f"- Originality: {composition['originality']}/4",
            f"- Overall recommendation: {composition['overall']}/6",
            f"- Confidence: {composition['confidence']}/5",
            "",
            "## Key Questions",
            "",
        ]
    )
    if composition["key_question_ids"]:
        lines.extend(
            f"{index}. {by_id[concern_id]['question'].strip()}"
            for index, concern_id in enumerate(composition["key_question_ids"], 1)
        )
    else:
        lines.append("No decision-relevant question remains beyond the final weaknesses above.")
    lines.extend(
        [
            "",
            "## Ethics",
            "",
            composition["ethics"].strip() or "No specific ethics concern was established.",
            "",
            "## Limitations Of This Review",
            "",
        ]
    )
    if composition["limitations"]:
        lines.extend(f"- {item.strip()}" for item in composition["limitations"])
    else:
        lines.append(
            "- The review is limited to the supplied paper package and verifiable public evidence."
        )
    return "\n".join(lines).rstrip() + "\n"


def compose(run_id: str, config: PipelineConfig, ledger: Ledger) -> Path:
    ledger.set_stage(run_id, "composing")
    gateway, spec = config.gateway("compose", "composition")
    corpus = json.loads((config.output_root / "corpus.json").read_text(encoding="utf-8"))
    final_concerns = ledger.rows(
        "SELECT * FROM concerns WHERE run_id = ? AND status = 'promoted_final' "
        "ORDER BY CASE materiality WHEN 'major' THEN 0 ELSE 1 END, created_at, id",
        (run_id,),
    )
    prompt_concerns = [
        {
            "id": concern["id"],
            "locator": concern["locator"],
            "category": concern["category"],
            "question": concern["question"],
            "evidence": concern["evidence"],
            "source": concern["source"],
            "materiality": concern["materiality"],
        }
        for concern in final_concerns
    ]
    expected = {concern["id"] for concern in final_concerns}
    prompt = load_prompt("composition").render(
        corpus=json.dumps(corpus, ensure_ascii=False),
        concerns=json.dumps(prompt_concerns, ensure_ascii=False),
    )
    result = gateway.complete_json("composer", prompt, composition_schema(expected), spec)
    validate_composition_ids(result, expected)
    review_json = {
        "run_id": run_id,
        "review": result,
        "final_concerns": prompt_concerns,
        "ledger_counts": ledger.completion(run_id).as_dict(),
        "policy": "one-shot review; no rebuttal interaction",
    }
    (config.output_root / "review.json").write_text(
        json.dumps(review_json, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    review_path = config.output_root / "review.md"
    review_path.write_text(_render_review(result, final_concerns), encoding="utf-8")
    write_revision_plan(ledger, run_id, config.output_root, result["concern_order"])
    completion = ledger.finalize(run_id, review_path)
    if not completion.complete:
        raise ArgusError(json.dumps(completion.as_dict(), sort_keys=True))
    review_json["ledger_counts"] = completion.as_dict()
    (config.output_root / "review.json").write_text(
        json.dumps(review_json, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    export_ledger(ledger, run_id, config.output_root)
    write_attrition(ledger, run_id, config.output_root)
    _write_active_pointer(config.state_root, run_id, ledger.path, config.output_root, complete=True)
    return review_path


# -- exports ----------------------------------------------------------------


def export_ledger(ledger: Ledger, run_id: str, output_root: Path) -> None:
    export = {
        "run": ledger.run(run_id),
        "completion": ledger.completion(run_id).as_dict(),
        "units": ledger.list_units(run_id),
        "claims": ledger.list_claims(run_id),
        "concerns": ledger.list_concerns(run_id),
        "diagnoses": ledger.rows(
            "SELECT * FROM diagnoses WHERE run_id = ? ORDER BY kind, subject_id", (run_id,)
        ),
        "events": ledger.rows("SELECT * FROM events WHERE run_id = ? ORDER BY sequence", (run_id,)),
    }
    (output_root / "ledger.json").write_text(
        json.dumps(export, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def write_attrition(ledger: Ledger, run_id: str, output_root: Path) -> Path:
    path = output_root / "attrition.json"
    path.write_text(
        json.dumps(ledger.attrition(run_id), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


# -- entry ------------------------------------------------------------------


def run_pipeline(config: PipelineConfig, stop_after: str | None = None) -> Completion:
    """Run or resume the full pipeline. Returns the final completion state."""
    with _pipeline_lock(config.state_root):
        run_id, _package, ledger = prepare(config)
        if stop_after == "extracted":
            export_ledger(ledger, run_id, config.output_root)
            return ledger.completion(run_id)
        if RUN_STAGES.index(ledger.run(run_id)["stage"]) <= RUN_STAGES.index("diverging"):
            diverge(run_id, config, ledger)
        if stop_after == "diverged":
            return ledger.completion(run_id)
        if RUN_STAGES.index(ledger.run(run_id)["stage"]) <= RUN_STAGES.index("resolving"):
            resolve(run_id, config, ledger)
        if stop_after == "resolved":
            export_ledger(ledger, run_id, config.output_root)
            return ledger.completion(run_id)
        if RUN_STAGES.index(ledger.run(run_id)["stage"]) <= RUN_STAGES.index("diagnosing"):
            diagnose(run_id, config, ledger)
        if RUN_STAGES.index(ledger.run(run_id)["stage"]) <= RUN_STAGES.index("composing"):
            compose(run_id, config, ledger)
        return ledger.completion(run_id)
