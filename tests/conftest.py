"""Shared fixtures: synthetic paper packages built with PyMuPDF."""

from __future__ import annotations

import json
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

MAIN_TEX = r"""\documentclass{article}
\usepackage{graphicx}
% Preamble comment; the preamble input below is never expanded.
\input{macros}
\begin{document}
\maketitle

\begin{abstract}
We propose a distillation method that transfers reasoning ability from a large teacher to a compact student. The student matches the teacher on two benchmarks.
\end{abstract}

\section{Introduction}
\label{sec:intro}

Serving large models is expensive for most deployments. % trailing comment
% A comment-only line inside the paragraph.
Distillation lowers that cost by 3.5\% on average because the student is smaller.

\paragraph{Scope.} We study two benchmarks with three seeds each. Variance stays below one point.

\section{Experiments}

Our evaluation covers three datasets with five seeds each (Table~\ref{tab:main}).

\input{tables/t1}

\begin{figure}[t]
\centering
\includegraphics[width=\columnwidth]{figs/plot}
\caption{Accuracy of the student against two baselines.}
\label{fig:plot}
\end{figure}

\subsection{Ablations}

Removing the teacher signal lowers accuracy to 80.1, so the signal matters.

\bibliography{refs}

\appendix

\section{Extra Results}

The appendix reports one more table for the larger setting.

\begin{table}[h]
\caption{Appendix numbers.}
\label{tab:app}
\begin{tabular}{lr}
\hline
Setting & Score \\
\hline
Large & 92.0 \\
\end{tabular}
\end{table}

\end{document}
"""

TABLE_TEX = r"""% Generated table; do not edit by hand.
\begin{table}[t]
\centering
\caption{Main results with 95\% intervals.}
\label{tab:main}
\begin{tabular}{@{}lr@{}}
\toprule
Model & Accuracy \\
\midrule
Teacher & 91.2 \\
Student & 90.8 \\
\bottomrule
\end{tabular}
\end{table}
"""


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


@pytest.fixture
def tex_package(tmp_path: Path) -> Path:
    """LaTeX package: abstract, sections, an input table, a PDF figure, appendix, issues."""
    package = tmp_path / "tex-package"
    (package / "tables").mkdir(parents=True)
    (package / "figs").mkdir()
    (package / "main.tex").write_text(MAIN_TEX, encoding="utf-8")
    (package / "tables" / "t1.tex").write_text(TABLE_TEX, encoding="utf-8")
    document = fitz.open()
    page = document.new_page(width=200, height=120)
    page.draw_rect(fitz.Rect(20, 20, 180, 100), color=(0, 0, 0), width=1)
    document.save(package / "figs" / "plot.pdf")
    document.close()
    (package / "refs.bib").write_text(
        "@article{teacher2024, title={A Teacher Model}, year={2024}}\n", encoding="utf-8"
    )
    (package / "reference-notes.md").write_text(
        "- Baseline A (2024): reports 91.2% on the same benchmark.\n", encoding="utf-8"
    )
    (package / "issues.json").write_text(
        json.dumps(
            [
                {"id": "R1", "text": "No comparison against a matched-compute baseline."},
                {"id": "R2", "text": "The causal claim about the teacher signal is unsupported."},
            ]
        ),
        encoding="utf-8",
    )
    return package
