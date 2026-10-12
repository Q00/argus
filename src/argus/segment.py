"""Sentence segmentation — an explicit, versioned trust boundary.

The ledger's "every sentence was covered" guarantee is only as strong as
this module: coverage is coverage *of the segments produced here*. So the
segmenter (a) uses pysbd instead of a hand-rolled regex, (b) enforces a
lossless invariant — the segments must reassemble exactly into the
normalized source text, or extraction fails loudly — and (c) stamps its
version into every run manifest so results are attributable to a specific
segmentation behavior.

Exports:
    SEGMENTER_VERSION, Segmentation, split_sentences, boundary_audit_sample.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

import pysbd

from argus.core import ExtractionError

SEGMENTER_VERSION = f"pysbd-{getattr(pysbd, '__version__', 'unknown')}+argus.3"

_WHITESPACE_RE = re.compile(r"\s+")

# pysbd's English rules still split after common academic abbreviations
# (observed: "cf.", "Eq.", "No. of"). Their trailing dot is swapped for a
# sentinel before segmentation and restored afterwards; the lossless
# invariant runs on the restored output, so protection cannot hide loss.
_SENTINEL = "․"  # ONE DOT LEADER — visually a dot, never in source text
_ACADEMIC_ABBREVIATIONS = (
    "Fig.",
    "Figs.",
    "Eq.",
    "Eqs.",
    "Sec.",
    "Secs.",
    "Thm.",
    "Lem.",
    "Def.",
    "Prop.",
    "Cor.",
    "Alg.",
    "Tab.",
    "App.",
    "cf.",
    "resp.",
)
_ABBREVIATION_RES = [
    (re.compile(rf"(?<![\w.]){re.escape(abbr[:-1])}\.(?=\s)"), abbr[:-1] + _SENTINEL)
    for abbr in _ACADEMIC_ABBREVIATIONS
]
# "No." only when clearly enumerative ("No. 3", "No. of parameters").
_NO_RE = re.compile(r"(?<![\w.])No\.(?=\s+(?:\d|of\b))")
_WRT_RE = re.compile(r"(?<![\w.])w\.r\.t\.(?=\s)")
# Terminal punctuation directly before a closing brace ends a LaTeX
# argument, not a sentence: "\paragraph{Scope.} We study" and
# "\resizebox{\linewidth}{!}{" must not split. "!" and "?" get their own
# sentinels so restoration stays exact.
_BRACED_DOT_RE = re.compile(r"\.(?=\})")
_BRACED_SENTINELS = {"!": "ǃ", "?": "ʔ"}  # LATIN LETTER RETROFLEX CLICK, GLOTTAL STOP
_BRACED_MARK_RE = re.compile(r"[!?](?=\})")


def _protect(text: str) -> str:
    if any(sentinel in text for sentinel in (_SENTINEL, *_BRACED_SENTINELS.values())):
        raise ExtractionError("input text contains the segmentation sentinel character")
    for pattern, replacement in _ABBREVIATION_RES:
        text = pattern.sub(replacement, text)
    text = _NO_RE.sub("No" + _SENTINEL, text)
    text = _WRT_RE.sub(f"w{_SENTINEL}r{_SENTINEL}t{_SENTINEL}", text)
    text = _BRACED_DOT_RE.sub(_SENTINEL, text)
    text = _BRACED_MARK_RE.sub(lambda match: _BRACED_SENTINELS[match.group(0)], text)
    return text


def _restore(text: str) -> str:
    for mark, sentinel in _BRACED_SENTINELS.items():
        text = text.replace(sentinel, mark)
    return text.replace(_SENTINEL, ".")


@dataclass(frozen=True, slots=True)
class Segmentation:
    """Segmenter output with its own audit evidence.

    Attributes:
        sentences: Stripped sentences, in order.
        normalized_text: Whitespace-normalized input that was segmented.
        lossless: True when segments reassemble exactly into normalized_text.
        version: SEGMENTER_VERSION at the time of the split.
    """

    sentences: tuple[str, ...]
    normalized_text: str
    lossless: bool
    version: str


def split_sentences(text: str) -> Segmentation:
    """Segment prose into sentences with a verified lossless invariant.

    Raises:
        ExtractionError: segments failed to reassemble into the input.
            A lossy split silently corrupts the coverage guarantee, so it
            is a hard failure, not a warning.
    """
    normalized = _WHITESPACE_RE.sub(" ", text).strip()
    if not normalized:
        return Segmentation((), "", True, SEGMENTER_VERSION)
    segmenter = pysbd.Segmenter(language="en", clean=False, char_span=True)
    spans = segmenter.segment(_protect(normalized))
    reassembled = _restore("".join(span.sent for span in spans))
    lossless = reassembled == normalized
    if not lossless:
        raise ExtractionError(
            "segmentation is lossy; refusing to build a coverage ledger on it",
            {
                "input_sha256": hashlib.sha256(normalized.encode()).hexdigest()[:16],
                "input_length": len(normalized),
                "reassembled_length": len(reassembled),
            },
        )
    sentences = tuple(_restore(span.sent).strip() for span in spans if _restore(span.sent).strip())
    if not sentences:
        sentences = (normalized,)
    return Segmentation(sentences, normalized, lossless, SEGMENTER_VERSION)


def boundary_audit_sample(sentences: tuple[str, ...], sample_size: int = 5) -> list[int]:
    """Deterministic sample of sentence ordinals for boundary-quality audits.

    Seeded by content, not a RNG, so re-runs audit the same boundaries.
    Returns 1-based ordinals.
    """
    if not sentences:
        return []
    count = min(sample_size, len(sentences))
    scored = sorted(
        range(1, len(sentences) + 1),
        key=lambda ordinal: hashlib.sha256(
            f"{ordinal}\x1f{sentences[ordinal - 1]}".encode()
        ).hexdigest(),
    )
    return sorted(scored[:count])
