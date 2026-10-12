---
name: argus-reviewer
description: Run a ledger-audited one-shot paper review with argus. Use when asked to review a paper package (main.pdf + appendix + reference-notes.md, or a LaTeX source tree + reference-notes.md), resume an interrupted review, revise a LaTeX paper from its revision plan, or report review status/attrition.
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
- A LaTeX package instead holds a root tex file (`main.tex`, or pass
  `--main-tex NAME`), `reference-notes.md`, and optionally `issues.json`
  (`[{id, text}, ...]`). Pass `--preserve-wording` when the owner says the
  sentences are final; then only factual errors are reported.
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
  `attrition.json`, `ledger.json`, `run-manifest.json`, and for LaTeX packages
  `revision-plan.md` and `revision-plan.json`.

## Revise a LaTeX paper from the revision plan

1. After a LaTeX review completes, show the owner the revision plan tables
   from `revision-plan.md` (one table per section, plus the claim ledger and
   sentence notes).
2. Apply to the tex source only the rows the owner approves. Argus never
   edits the manuscript, and you must not apply a row the owner did not
   approve.
3. After applying a change, re-read the previous and the next paragraph of
   each changed paragraph and check that the handoff between them still
   holds.
4. List every "Decisions needed" item for the owner and wait. These items
   need a new experiment or computation; do not schedule or run anything for
   them.
5. To re-check the revised manuscript, run argus again into a new output
   directory (`-o runs/<name>-r2`); never reuse the old one.
