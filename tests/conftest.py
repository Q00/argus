"""Shared fixtures: synthetic paper packages built with PyMuPDF."""

from __future__ import annotations

from pathlib import Path
import sys

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

PARAGRAPHS = [
    (
        "We propose a distillation method that transfers reasoning ability from a large teacher "
        "to a compact student. The student matches the teacher on two benchmarks. Training cost "
        "drops by a factor of three compared to standard fine-tuning."
    ),
    (
        "Our evaluation covers three datasets with five seeds each. The proposed method improves "
        "average accuracy by 3.5 points over the strongest baseline. Variance across seeds stays "
        "below one point in every setting we measured."
    ),
]

CAPTION = "Figure 1: Accuracy of the proposed method against two baselines on three datasets."


@pytest.fixture
def paper_package(tmp_path: Path) -> Path:
    """One-page main.pdf with two paragraphs and one figure, plus appendix/refs."""
    package = tmp_path / "package"
    package.mkdir()
    document = fitz.open()
    page = document.new_page()
    page.insert_textbox(fitz.Rect(60, 60, 540, 200), PARAGRAPHS[0], fontsize=11)
    page.insert_textbox(fitz.Rect(60, 220, 540, 360), PARAGRAPHS[1], fontsize=11)
    page.draw_rect(fitz.Rect(100, 400, 300, 500), color=(0, 0, 0), width=1)
    page.insert_textbox(fitz.Rect(60, 510, 540, 560), CAPTION, fontsize=9)
    document.save(package / "main.pdf")
    document.close()
    (package / "appendix.md").write_text(
        "## Appendix\n\nAdditional ablations show the trend holds at larger scales.\n",
        encoding="utf-8",
    )
    (package / "reference-notes.md").write_text(
        "- Baseline A (2024): reports 91.2% on the same benchmark.\n", encoding="utf-8"
    )
    return package
