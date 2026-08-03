"""Input package validation and review-unit extraction.

Turns ``main.pdf`` + ``appendix.(pdf|md)`` + ``reference-notes.md`` into
paragraph / figure / table units, each staged as a payload directory with a
hash manifest. Two honesty rules distinguish this from the usual PDF
pipeline:

    * The input contract is an explicit ``InputContract`` value recorded in
      the run manifest — never a silent environment-variable override.
    * Figure/table crops carry measured evidence (image/drawing overlap
      ratios) instead of an unstated assumption that the heuristic worked.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

import fitz

from argus.core import ExtractionError, sha256_file, stable_id

CAPTION_RE = re.compile(
    r"^\s*(?P<kind>figure|fig\.|table)\s+(?P<label>[A-Za-z0-9]+)\s*[.:]\s+(?P<caption>.+)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class InputContract:
    """What the harness demands of an input package.

    Attributes:
        main_page_count: Exact page count for main.pdf, or None to accept any.
        min_text_chars: Minimum extractable characters in main.pdf.
        min_ascii_letter_ratio: English-text heuristic threshold.
    """

    main_page_count: int | None = 4
    min_text_chars: int = 500
    min_ascii_letter_ratio: float = 0.85

    def as_dict(self) -> dict[str, Any]:
        return {
            "main_page_count": self.main_page_count,
            "min_text_chars": self.min_text_chars,
            "min_ascii_letter_ratio": self.min_ascii_letter_ratio,
        }


@dataclass(frozen=True, slots=True)
class Package:
    root: Path
    main_pdf: Path
    appendix: Path
    reference_notes: Path


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def validate_package(root: Path | str, contract: InputContract) -> Package:
    """Validate the package layout and content against the contract."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ExtractionError(f"input package is not a directory: {root}")
    main = root / "main.pdf"
    references = root / "reference-notes.md"
    appendix = next(
        (path for path in (root / "appendix.pdf", root / "appendix.md") if path.is_file()), None
    )
    missing = [path.name for path in (main, references) if not path.is_file()]
    if appendix is None:
        missing.append("appendix.pdf or appendix.md")
    if missing or appendix is None:
        raise ExtractionError("missing required input", {"missing": missing})
    if main.read_bytes()[:5] != b"%PDF-":
        raise ExtractionError("main.pdf is not a PDF file")
    document = fitz.open(main)
    try:
        if contract.main_page_count is not None and document.page_count != contract.main_page_count:
            raise ExtractionError(
                "main.pdf violates the page-count contract",
                {"expected": contract.main_page_count, "found": document.page_count},
            )
        extracted = "".join(page.get_text() for page in document)
    finally:
        document.close()
    if len(extracted.strip()) < contract.min_text_chars:
        raise ExtractionError(
            "main.pdf does not contain enough extractable text",
            {"minimum": contract.min_text_chars, "found": len(extracted.strip())},
        )
    ascii_letters = sum(ch.isascii() and ch.isalpha() for ch in extracted)
    all_letters = sum(ch.isalpha() for ch in extracted)
    if all_letters and ascii_letters / all_letters < contract.min_ascii_letter_ratio:
        raise ExtractionError(
            "main.pdf does not appear to be predominantly English",
            {"ratio": round(ascii_letters / all_letters, 3)},
        )
    if not references.read_text(encoding="utf-8").strip():
        raise ExtractionError("reference-notes.md is empty")
    if appendix.suffix == ".pdf" and appendix.read_bytes()[:5] != b"%PDF-":
        raise ExtractionError("appendix.pdf is not a PDF file")
    return Package(root=root, main_pdf=main, appendix=appendix, reference_notes=references)


def _clean_block(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return " ".join(line for line in lines if line).strip()


def _caption_match(text: str) -> re.Match[str] | None:
    return CAPTION_RE.match(_clean_block(text))


def _text_blocks(page: fitz.Page) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for block in page.get_text("dict", sort=True).get("blocks", []):
        if block.get("type") != 0:
            continue
        spans = [span for line in block.get("lines", []) for span in line.get("spans", [])]
        text = _clean_block(" ".join(str(span.get("text", "")) for span in spans))
        if not text:
            continue
        sizes = [float(span["size"]) for span in spans if isinstance(span.get("size"), int | float)]
        records.append(
            {
                "rect": fitz.Rect(block["bbox"]),
                "text": text,
                "max_font_size": max(sizes, default=0.0),
            }
        )
    return records


def _caption_continuation(caption: dict[str, Any], candidate: dict[str, Any]) -> bool:
    same_column = abs(candidate["rect"].x0 - caption["rect"].x0) <= 16
    close_below = 0 <= candidate["rect"].y0 - caption["rect"].y1 <= 8
    caption_sized = candidate["max_font_size"] <= caption["max_font_size"] + 0.3
    return (
        same_column and close_below and caption_sized and _caption_match(candidate["text"]) is None
    )


def _asset_rect(page: fitz.Page, block_rect: fitz.Rect, kind: str) -> fitz.Rect:
    height = page.rect.height
    margin = 8
    if kind == "table":
        return fitz.Rect(
            max(0, block_rect.x0 - margin),
            max(0, block_rect.y0 - margin),
            min(page.rect.width, block_rect.x1 + margin),
            min(height, block_rect.y1 + min(height * 0.38, 300)),
        )
    return fitz.Rect(
        max(0, block_rect.x0 - margin),
        max(0, block_rect.y0 - min(height * 0.42, 330)),
        min(page.rect.width, block_rect.x1 + margin),
        min(height, block_rect.y1 + margin),
    )


def _crop_evidence(page: fitz.Page, crop: fitz.Rect) -> dict[str, float]:
    """Measured evidence that the crop actually contains visual content.

    Records the fraction of the page's raster images and vector drawings
    whose bounding boxes intersect the crop. Consumers (and auditors) see a
    number, not an assumption.
    """

    def overlap_ratio(rects: list[fitz.Rect]) -> float:
        if not rects:
            return 0.0
        covered = sum(1 for rect in rects if not (rect & crop).is_empty)
        return round(covered / len(rects), 3)

    image_rects = [fitz.Rect(info["bbox"]) for info in page.get_image_info()]
    drawing_rects = [drawing["rect"] for drawing in page.get_drawings()]
    return {
        "image_overlap_ratio": overlap_ratio(image_rects),
        "drawing_overlap_ratio": overlap_ratio(drawing_rects),
        "page_image_count": float(len(image_rects)),
        "page_drawing_count": float(len(drawing_rects)),
    }


def _render_asset(page: fitz.Page, rect: fitz.Rect, target: Path) -> None:
    rect = rect & page.rect
    if rect.width < 20 or rect.height < 20:
        raise ExtractionError(f"asset crop is too small: {rect}")
    pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, alpha=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    pixmap.save(target)


def _register_unit(
    units_root: Path,
    run_id: str,
    kind: str,
    source_document: str,
    page_number: int | None,
    ordinal: int,
    locator: str,
    content: str,
    caption: str | None = None,
    image_writer: Any = None,
    crop_evidence: dict[str, float] | None = None,
) -> dict[str, Any]:
    unit_id = stable_id("unt", run_id, kind, source_document, page_number, ordinal, content)
    unit_root = units_root / unit_id
    unit_root.mkdir(parents=True, exist_ok=True)
    content_path = unit_root / "input.txt"
    content_path.write_text(content.strip() + "\n", encoding="utf-8")
    image_path: Path | None = None
    if image_writer is not None:
        image_path = unit_root / "asset.png"
        image_writer(image_path)
    manifest = {
        "unit_id": unit_id,
        "kind": kind,
        "locator": locator,
        "source_document": source_document,
        "page": page_number,
        "payload": {
            "text": content_path.name,
            "text_sha256": sha256_file(content_path),
            "image": image_path.name if image_path else None,
            "image_sha256": sha256_file(image_path) if image_path else None,
        },
        "crop_evidence": crop_evidence,
        "isolation": ("payload: divergence workers receive exactly these files and nothing else"),
    }
    _write_json(unit_root / "manifest.json", manifest)
    return {
        "id": unit_id,
        "run_id": run_id,
        "kind": kind,
        "locator": locator,
        "source_document": source_document,
        "page": page_number,
        "ordinal": ordinal,
        "content_path": str(content_path.resolve()),
        "image_path": str(image_path.resolve()) if image_path else None,
        "caption": caption,
        "manifest_path": str((unit_root / "manifest.json").resolve()),
    }


def extract_pdf_units(
    pdf_path: Path,
    source_document: str,
    run_id: str,
    units_root: Path,
    start_ordinal: int,
) -> tuple[list[dict[str, Any]], int, str]:
    """Extract paragraph/figure/table units from one PDF."""
    document = fitz.open(pdf_path)
    units: list[dict[str, Any]] = []
    full_text: list[str] = []
    ordinal = start_ordinal
    try:
        for page_index, page in enumerate(document):
            blocks = _text_blocks(page)
            block_index = 0
            while block_index < len(blocks):
                block = blocks[block_index]
                text = block["text"]
                rect = block["rect"]
                page_number = page_index + 1
                caption_match = _caption_match(text)
                if caption_match:
                    while block_index + 1 < len(blocks) and _caption_continuation(
                        block, blocks[block_index + 1]
                    ):
                        block_index += 1
                        continuation = blocks[block_index]
                        text = f"{text} {continuation['text']}"
                        rect |= continuation["rect"]
                        block = {
                            "rect": rect,
                            "text": text,
                            "max_font_size": max(
                                block["max_font_size"], continuation["max_font_size"]
                            ),
                        }
                    full_text.append(text)
                    raw_kind = caption_match.group("kind").lower()
                    kind = "table" if raw_kind == "table" else "figure"
                    label = caption_match.group("label")
                    locator = f"{source_document} p.{page_number} {kind.title()} {label}"
                    crop = _asset_rect(page, rect, kind)
                    units.append(
                        _register_unit(
                            units_root,
                            run_id,
                            kind,
                            source_document,
                            page_number,
                            ordinal,
                            locator,
                            text,
                            caption=text,
                            image_writer=lambda target, p=page, r=crop: _render_asset(p, r, target),
                            crop_evidence=_crop_evidence(page, crop),
                        )
                    )
                    ordinal += 1
                    block_index += 1
                    continue
                full_text.append(text)
                if len(text) < 18:
                    block_index += 1
                    continue
                locator = f"{source_document} p.{page_number} paragraph {ordinal + 1}"
                units.append(
                    _register_unit(
                        units_root,
                        run_id,
                        "paragraph",
                        source_document,
                        page_number,
                        ordinal,
                        locator,
                        text,
                    )
                )
                ordinal += 1
                block_index += 1
    finally:
        document.close()
    return units, ordinal, "\n\n".join(full_text)


def extract_package(
    package: Package, run_id: str, output_root: Path, contract: InputContract
) -> dict[str, Any]:
    """Build all units and the full-context corpus for one run."""
    units_root = output_root / "units"
    units_root.mkdir(parents=True, exist_ok=True)
    units, _, main_text = extract_pdf_units(package.main_pdf, "main", run_id, units_root, 0)
    if package.appendix.suffix.lower() == ".pdf":
        appendix_document = fitz.open(package.appendix)
        try:
            appendix_text = "\n\n".join(page.get_text() for page in appendix_document)
        finally:
            appendix_document.close()
    else:
        appendix_text = package.appendix.read_text(encoding="utf-8")
    corpus = {
        "main_text": main_text,
        "appendix_text": appendix_text,
        "reference_notes": package.reference_notes.read_text(encoding="utf-8"),
        "package_files": {
            "main.pdf": sha256_file(package.main_pdf),
            package.appendix.name: sha256_file(package.appendix),
            "reference-notes.md": sha256_file(package.reference_notes),
        },
        "input_contract": contract.as_dict(),
        "unit_count": len(units),
        "divergence_scope": "Every extractable prose block, Figure, Table, and caption in main.pdf.",
        "appendix_role": "Full-context convergence evidence; not a source of divergence units.",
    }
    _write_json(output_root / "corpus.json", corpus)
    _write_json(output_root / "unit-index.json", units)
    return {"units": units, "corpus": corpus}
