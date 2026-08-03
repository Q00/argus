<p align="center">
  <img src="assets/banner.png" alt="Argus: harness for researcher — a hundred-eyed watcher" width="100%">
</p>

<p align="center">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Lint: ruff" src="https://img.shields.io/badge/lint-ruff-261230">
  <img alt="Types: mypy" src="https://img.shields.io/badge/types-mypy-blue">
</p>

<p align="center">
  <b>Provider-agnostic, ledger-audited, one-shot paper review harness.</b><br>
  Blind critics question every claim in a paper, a full-context judge resolves each
  question with evidence, and a SQLite ledger records where every concern lived and died.
</p>

---

Argus reviews a paper package (`main.pdf` + `appendix.(pdf|md)` +
`reference-notes.md`) in one shot and leaves an audit trail strong enough to
check three claims that review pipelines usually leave unverified:

- **Coverage** — every sentence-level claim got at least one question, counted
  from ledger rows, not asserted by a model.
- **Provenance** — prompts, policy, models, and the sentence segmenter are
  hashed into a run manifest; a resumed run refuses to start if any of them drifted.
- **Attrition** — every filtered-out concern carries the stage that killed it
  and the reason, so recall loss is a number you can read instead of a blind spot.

## Quick start

Open an agent session inside the repo and ask for a review. The repo carries
its own integration for both Codex and Claude Code, discovered automatically
when a session starts here:

```bash
cd argus
uv sync     # once — the integration wrappers use this venv
codex       # or: claude
```

Then just ask:

> review the paper package at path/to/package

A package is a directory with `main.pdf` (4 pages by default; any length via
`--pages`), `appendix.pdf` or `appendix.md`, and `reference-notes.md`.

The session picks up four things: an `argus` MCP server (live ledger status,
attrition, concern claiming), a Stop hook that blocks the session while a
review is incomplete, a skill that teaches the agent the workflow, and
always-loaded instructions (`AGENTS.md` / `CLAUDE.md`). The agent runs the
pipeline, watches it, and may only declare success after the MCP completion
check passes.

### CLI, if you'd rather drive it yourself

```bash
# one model for every stage
uv run argus review path/to/package -o runs/paper1 -m anthropic:claude-sonnet-5

# cheap divergence, strong adjudication, custom endpoint (vLLM/OpenRouter/Ollama)
uv run argus review path/to/package -o runs/paper1 \
  -m openai-compat:gpt-4o-mini \
  --resolve-model openai-compat:gpt-4o \
  --base-url http://localhost:8000/v1 --api-key-env VLLM_KEY

# observability
uv run argus status
uv run argus attrition
uv run argus recall-eval public-issues.json -m anthropic:claude-sonnet-5
```

Models are named `PROVIDER:MODEL` and can differ per stage
(`--diverge-model`, `--resolve-model`, `--audit-model`, `--compose-model`).
Interrupted runs resume with the exact same command; finished work is never
re-billed.

| Provider prefix | Backends | Isolation tier |
|---|---|---|
| `openai-compat` | OpenAI, vLLM, OpenRouter, Ollama, any `/chat/completions` endpoint | `payload` |
| `anthropic` | Anthropic Messages API | `payload` |
| `codex-cli` | `codex exec` (subscription billing; hermetic flags + explicit provider injection) | `process` |
| `claude-cli` | `claude -p` (subscription billing; read-only tools, no session persistence) | `process` |

`payload` means the model receives exactly the unit text and cropped image we
send and nothing else exists for it to see: isolation holds by construction,
with no sandbox to configure or trust. `process` means a restricted local CLI
process, which is weaker; every call's audit record names its tier so the two
cannot be confused later.

## How it works

```
                        ┌──────────────────────────────────────┐
                        │        CLI (typer)  /  MCP server    │
                        └──────────────────┬───────────────────┘
                                           │
     ┌─────────────────────────────────────▼──────────────────────────────────┐
     │                     Pipeline Orchestrator (stage machine)              │
     │    extract ─► diverge ─► resolve ─► audit ─► compose ─► complete       │
     │    · no backward stages  · every stage resumable                       │
     │    · run-manifest freeze: config/prompt/policy hashes verified on resume│
     └──────┬──────────────────────┬────────────────────────┬─────────────────┘
            │                      │                        │
 ┌──────────▼──────────┐  ┌────────▼───────────┐  ┌─────────▼──────────────┐
 │  Extraction          │  │  Model Gateway     │  │  Ledger (SQLite/WAL)   │
 │  · pysbd segmenter = │  │  · complete_json(  │  │  · runs/units/claims   │
 │    versioned trust   │  │     prompt, schema,│  │    /concerns/events    │
 │    boundary w/ loss- │  │     images)        │  │  · exact-set guards    │
 │    less invariant    │  │  · directive +     │  │  · resolution_key      │
 │  · fig/table crops   │  │    jsonschema +    │  │    uniqueness          │
 │    with measured     │  │    repair retry    │  │  · attrition queries   │
 │    overlap evidence  │  │  · per-call audit  │  │  · physical completion │
 └──────────────────────┘  │    (hashes, usage, │  │    gate                │
                           │    isolation tier) │  └────────────────────────┘
                           ├────────────────────┤
                           │ OpenAICompatAdapter│ ← OpenAI/vLLM/OpenRouter/Ollama
                           │ AnthropicAdapter   │ ← isolation: payload
                           │ Codex/Claude CLI   │ ← isolation: process
                           └────────────────────┘
```

Divergence workers see one paragraph, figure, or table at a time and must
produce at least one falsifiable question per atomic claim, with exact
sentence-ordinal accounting enforced twice (JSON Schema, then set equality).
The resolver then reads the whole paper, appendix, and reference notes and
closes every question into one of four terminal states with evidence and a
locator. A global audit removes cross-batch duplicates and overreads. The
composer can order and select from adjudicated concerns but cannot add,
drop, or rewrite them; validators reject any output that changes the set.

### Where concerns live and die

```
 diverge                resolve                     audit             compose
 questions ──────┬──► solved_by_paper ──────┐
 (uncapped,      ├──► solved_by_reference ──┼─ terminal (evidence+source required)
  per atomic     ├──► invalid_or_duplicate ─┘
  claim)         └──► promoted ─────► keep ─────────────────────► review.md
                               └────► discard(reason, canonical_id)
```

`argus attrition` reads this flow back out of the ledger: kill counts per
stage, reason distributions, and the full audit discard list.
`argus recall-eval` goes one step further, matching an independent issue list
(normalized public reviews, for example) against the entire concern ledger and
labeling each issue `surfaced_final`, `surfaced_but_attrited`, or `missed`.
`surfaced_but_attrited` is the precision/recall trade made visible: those are
exactly the concerns the domain policy filtered away.

## What a run leaves behind

| Artifact | What it answers |
|---|---|
| `review.md` / `review.json` | The final review, human- and machine-readable |
| `ledger.sqlite3` / `ledger.json` | Every unit, claim, concern, and event, queryable |
| `attrition.json` | Where concerns died, by stage, status, and reason |
| `run-manifest.json` | Exactly which prompts, models, policy, segmenter, and inputs ran |
| `recall-report.json` | Recall against an independent issue list (optional) |
| `logs/*/…` | Per-call audit: prompt/response/schema hashes, usage, isolation tier |

External research enters only through `research-notes.json` in the output
directory: an explicit, hash-auditable file, never a live search inside a
worker that the run cannot reproduce.

## Limitations

- Resolver, audit, and composer outputs are still model judgments. The ledger
  verifies accounting, not truth.
- The recall judge is itself a model; its report says so in a `caveat` field.
- PDF block and caption heuristics can miss structure. Crops carry measured
  image/drawing overlap ratios so a wrong crop is at least visible, but the
  crop placement itself is still a heuristic.
- The run lock uses `fcntl`, so runs need a POSIX system. Everything else is
  cross-platform.
- `codex-cli` and `claude-cli` are restricted processes, not payload isolation.

## Development

```bash
uv sync
uv run pytest          # all tests run offline via fake adapters
uv run ruff check src tests
uv run mypy src
```

The full pipeline is tested end-to-end against a synthetic PDF with a fake
adapter that parses each stage's prompt and answers with exact-set-valid
JSON, so no test needs a network or an API key.
