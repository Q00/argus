---
name: argus-reviewer
description: Run a ledger-audited one-shot paper review with argus. Use when asked to review a paper package (main.pdf + appendix + reference-notes.md), resume an interrupted review, or report review status/attrition.
---

# Argus reviewer

Argus runs the whole review pipeline with one command. Your job is to invoke
it, watch it finish, and report results. Do not re-implement any stage by
hand, and never edit the ledger or output files directly.

## Run a review

```bash
uv run argus review <package-dir> -o runs/<name> -m codex-cli:gpt-5.6-sol
```

- `<package-dir>` must contain `main.pdf` (exactly 4 pages by default; pass
  `--pages N`, or `--pages 0` for any length), `appendix.pdf` or
  `appendix.md`, and `reference-notes.md`.
- The command is resumable: if it fails or is interrupted, run the exact same
  command again. Completed work is never re-billed.
- Mixed stages are allowed, e.g. `--resolve-model` for a stronger adjudicator.

## Inspect progress and results

Use the `argus` MCP tools first; they read the live ledger:

- `review_status` — completion counts and the next required action.
- `review_attrition` — where concerns died, by stage and reason.
- `review_list_concerns` — concern listing, optionally filtered by status.
- `review_verify_completion` — physical completion check before declaring done.

Equivalent CLI: `uv run argus status`, `uv run argus attrition`.

## Rules

- A review is done only when `review_verify_completion` reports
  `"complete": true`. Never declare success from log output alone.
- The Stop hook blocks this session while a review is incomplete; the fix is
  to resume the review (same `argus review` command), not to fight the hook.
- Final artifacts live in the output directory: `review.md`, `review.json`,
  `attrition.json`, `ledger.json`, `run-manifest.json`.
