"""Versioned prompt templates.

Every template is a file whose sha256 lands in the run manifest, so "the
prompts were frozen" is a checkable claim instead of a sentence in a
report. Domain policy (``policy-*.md``) is a separate, swappable file with
its own hash — structural prompts and domain heuristics evolve on
different clocks and must be attributable independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from string import Template

from argus.core import ArgusError, sha256_text

PROMPT_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    text: str
    sha256: str

    def render(self, **values: object) -> str:
        try:
            return Template(self.text).substitute(**values)
        except KeyError as exc:
            raise ArgusError(
                f"prompt {self.name} is missing a placeholder value", {"missing": str(exc)}
            ) from exc


def load_prompt(name: str) -> PromptTemplate:
    """Load a template by stem name (e.g. ``divergence``, ``policy-icml``)."""
    path = PROMPT_ROOT / f"{name}.md"
    if not path.is_file():
        raise ArgusError(f"unknown prompt: {name}", {"searched": str(path)})
    text = path.read_text(encoding="utf-8")
    return PromptTemplate(name=name, text=text, sha256=sha256_text(text))


def prompt_hashes(names: list[str]) -> dict[str, str]:
    return {name: load_prompt(name).sha256 for name in names}
