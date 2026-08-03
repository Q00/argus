# Argus — agent instructions

Argus is a ledger-audited one-shot paper review harness. If the user asks to
review a paper, resume a review, or check review progress, use the
`argus-reviewer` skill (`.agents/skills/argus-reviewer/SKILL.md`) — do not
re-implement pipeline stages by hand.

Quick reference:

- Run/resume a review (idempotent; safe to re-run after any interruption):
  `uv run argus review <package-dir> -o runs/<name> -m codex-cli:gpt-5.6-sol`
- Live status via the `argus` MCP tools: `review_status`,
  `review_attrition`, `review_list_concerns`, `review_verify_completion`.
- A review is complete only when `review_verify_completion` returns
  `"complete": true`. Never declare success from log output.
- The Stop hook blocks this session while a review is incomplete; resume the
  review instead of working around the hook.
- Never edit `runs/**`, `.argus/**`, or the SQLite ledger by hand.

Development checks: `uv run pytest`, `uv run ruff check src tests`,
`uv run mypy src`. All tests run offline via fake adapters.
