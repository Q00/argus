"""Input package validation and review-unit extraction.

Turns ``main.pdf`` + ``appendix.(pdf|md)`` + ``reference-notes.md`` into
paragraph / figure / table units, each staged as a payload directory with a
hash manifest. A tex package (root ``.tex`` file + ``reference-notes.md`` +
optional ``issues.json``) is segmented from its source instead, so each unit
maps to a source line range and belongs to a section block. Two honesty
rules distinguish this from the usual PDF pipeline:

    * The input contract is an explicit ``InputContract`` value recorded in
      the run manifest — never a silent environment-variable override.
    * Figure/table crops carry measured evidence (image/drawing overlap
      ratios) instead of an unstated assumption that the heuristic worked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any

import fitz

from argus.core import ExtractionError, sha256_file, stable_id
from argus.evalx import load_issue_file
from argus.tex import (
    TexDocument,
    TexLayout,
    bibliography_files,
    build_cell_catalog,
    expand_tex,
    locator,
    render_image,
    resolve_image,
    segment_document,
)

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
        paragraph_merge_chars: Merge adjacent text blocks into paragraph
            units up to roughly this many characters; 0 keeps one unit per
            PDF block. Two-column PDFs shatter paragraphs into many small
            blocks, and one model call per fragment is the dominant cost.
        main_tex: Root tex file name; its presence makes the package a tex
            package. Page count and merge size do not apply to tex packages.
        source: ``pdf`` or ``tex``, set from the validated package.
    """

    main_page_count: int | None = 4
    min_text_chars: int = 500
    min_ascii_letter_ratio: float = 0.85
    paragraph_merge_chars: int = 1200
    main_tex: str = "main.tex"
    source: str = "pdf"

    def as_dict(self) -> dict[str, Any]:
        tex = self.source == "tex"
        return {
            "main_page_count": None if tex else self.main_page_count,
            "min_text_chars": self.min_text_chars,
            "min_ascii_letter_ratio": self.min_ascii_letter_ratio,
            "paragraph_merge_chars": None if tex else self.paragraph_merge_chars,
            "source": self.source,
            "main_tex": self.main_tex if tex else None,
        }


@dataclass(frozen=True, slots=True)
class Package:
    """A validated input package.

    Attributes:
        files: Package-relative path -> file for every input whose hash
            identifies the run (the PDF mapping is main.pdf, the appendix,
            and reference-notes.md).
    """

    root: Path
    main_pdf: Path | None
    appendix: Path | None
    reference_notes: Path
    source: str = "pdf"
    main_tex: Path | None = None
    issues: Path | None = None
    files: dict[str, Path] = field(default_factory=dict)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _check_text(extracted: str, contract: InputContract, name: str) -> None:
    if len(extracted.strip()) < contract.min_text_chars:
        raise ExtractionError(
            f"{name} does not contain enough extractable text",
            {"minimum": contract.min_text_chars, "found": len(extracted.strip())},
        )
    ascii_letters = sum(ch.isascii() and ch.isalpha() for ch in extracted)
    all_letters = sum(ch.isalpha() for ch in extracted)
    if all_letters and ascii_letters / all_letters < contract.min_ascii_letter_ratio:
        raise ExtractionError(
            f"{name} does not appear to be predominantly English",
            {"ratio": round(ascii_letters / all_letters, 3)},
        )


def _validate_tex_package(root: Path, contract: InputContract) -> Package:
    references = root / "reference-notes.md"
    if not references.is_file():
        raise ExtractionError("missing required input", {"missing": ["reference-notes.md"]})
    document = expand_tex(root, contract.main_tex)
    layout = segment_document(document)
    _check_text(document.text[: layout.main_end], contract, contract.main_tex)
    if not references.read_text(encoding="utf-8").strip():
        raise ExtractionError("reference-notes.md is empty")
    files = {relative: root / relative for relative in document.files}
    for raw in layout.figure_images:
        relative, path = resolve_image(root, raw)
        files[relative] = path
    for name in bibliography_files(document, layout.main_end):
        if (root / name).is_file():
            files[name] = root / name
    files["reference-notes.md"] = references
    issues = root / "issues.json"
    if issues.is_file():
        load_issue_file(issues)
        files["issues.json"] = issues
    return Package(
        root=root,
        main_pdf=None,
        appendix=None,
        reference_notes=references,
        source="tex",
        main_tex=root / document.files[0],
        issues=issues if issues.is_file() else None,
        files=files,
    )


def validate_package(root: Path | str, contract: InputContract) -> Package:
    """Validate the package layout and content against the contract.

    A package holding ``contract.main_tex`` is a tex package; any other
    package must satisfy the PDF contract.
    """
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ExtractionError(f"input package is not a directory: {root}")
    if (root / contract.main_tex).is_file():
        return _validate_tex_package(root, contract)
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
    _check_text(extracted, contract, "main.pdf")
    if not references.read_text(encoding="utf-8").strip():
        raise ExtractionError("reference-notes.md is empty")
    if appendix.suffix == ".pdf" and appendix.read_bytes()[:5] != b"%PDF-":
        raise ExtractionError("appendix.pdf is not a PDF file")
    return Package(
        root=root,
        main_pdf=main,
        appendix=appendix,
        reference_notes=references,
        files={
            "main.pdf": main,
            appendix.name: appendix,
            "reference-notes.md": references,
        },
    )


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
    tex_location: dict[str, Any] | None = None,
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
        "tex_location": tex_location,
        "isolation": ("payload: divergence workers receive exactly these files and nothing else"),
    }
    _write_json(unit_root / "manifest.json", manifest)
    location = tex_location or {}
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
        "section_id": location.get("section_id"),
        "section_title": location.get("section_title"),
        "tex_file": location.get("tex_file"),
        "line_start": location.get("line_start"),
        "line_end": location.get("line_end"),
    }


# Bibliography detection. Entries after a References/Bibliography heading
# become corpus evidence for the resolver instead of divergence units — a
# worker interrogating "Author, A. (2024)." can only answer not-applicable,
# at one model call per entry. A caption or an Appendix heading resumes
# normal unit extraction.
_REFERENCES_HEADING_RE = re.compile(
    r"^\s*\d{0,3}\.?\s*(references|bibliography)\b[\s\d.]*$", re.IGNORECASE
)
_APPENDIX_HEADING_RE = re.compile(r"^\s*(?:[a-z]\s*[.:]?\s+)?appendi(?:x|ces)\b", re.IGNORECASE)


def extract_pdf_units(
    pdf_path: Path,
    source_document: str,
    run_id: str,
    units_root: Path,
    start_ordinal: int,
    merge_chars: int = 1200,
) -> tuple[list[dict[str, Any]], int, str, str]:
    """Extract paragraph/figure/table units from one PDF.

    Returns (units, next_ordinal, full_text, bibliography_text). Adjacent
    prose blocks are merged into paragraph units of up to ``merge_chars``
    characters; bibliography blocks are collected instead of registered.
    """
    document = fitz.open(pdf_path)
    units: list[dict[str, Any]] = []
    full_text: list[str] = []
    bibliography: list[str] = []
    in_references = False
    ordinal = start_ordinal
    pending: list[str] = []
    pending_page = 0

    def flush_pending() -> None:
        nonlocal ordinal, pending
        if not pending:
            return
        content = "\n\n".join(pending)
        locator = f"{source_document} p.{pending_page} paragraph {ordinal + 1}"
        units.append(
            _register_unit(
                units_root,
                run_id,
                "paragraph",
                source_document,
                pending_page,
                ordinal,
                locator,
                content,
            )
        )
        ordinal += 1
        pending = []

    try:
        for page_index, page in enumerate(document):
            blocks = _text_blocks(page)
            block_index = 0
            page_number = page_index + 1
            while block_index < len(blocks):
                block = blocks[block_index]
                text = block["text"]
                rect = block["rect"]
                caption_match = _caption_match(text)
                if caption_match:
                    flush_pending()
                    in_references = False  # a Figure/Table means prose resumed
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
                if _REFERENCES_HEADING_RE.match(text) and len(text) < 40:
                    flush_pending()
                    in_references = True
                    block_index += 1
                    continue
                if in_references and _APPENDIX_HEADING_RE.match(text) and len(text) < 60:
                    in_references = False
                    block_index += 1
                    continue
                if in_references:
                    bibliography.append(text)
                    block_index += 1
                    continue
                if len(text) < 18:
                    block_index += 1
                    continue
                if merge_chars <= 0:
                    flush_pending()
                    pending.append(text)
                    pending_page = page_number
                    flush_pending()
                elif pending and sum(len(part) for part in pending) + len(text) > merge_chars:
                    flush_pending()
                    pending.append(text)
                    pending_page = page_number
                else:
                    if not pending:
                        pending_page = page_number
                    pending.append(text)
                block_index += 1
            flush_pending()
    finally:
        document.close()
    return units, ordinal, "\n\n".join(full_text), "\n\n".join(bibliography)


def extract_tex_units(
    package_root: Path,
    document: TexDocument,
    layout: TexLayout,
    run_id: str,
    units_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Register every main-body unit of a segmented tex document.

    Returns (units, sections); sections list each block's id, title,
    parent title, and unit ids in order.
    """
    units: list[dict[str, Any]] = []
    sections: list[dict[str, Any]] = []
    for block in layout.blocks:
        unit_ids: list[str] = []
        for unit in block.units:
            image_writer = None
            if unit.image is not None:
                _, image = resolve_image(package_root, unit.image)
                image_writer = lambda target, source=image: render_image(source, target)  # noqa: E731
            tex_file, line_start = document.origins[unit.first_line]
            record = _register_unit(
                units_root,
                run_id,
                unit.kind,
                "main",
                None,
                len(units),
                locator(document, unit.first_line, unit.last_line, block.title),
                unit.text,
                caption=unit.caption,
                image_writer=image_writer,
                tex_location={
                    "section_id": block.section_id,
                    "section_title": block.title,
                    "tex_file": tex_file,
                    "line_start": line_start,
                    "line_end": document.origins[unit.last_line][1],
                },
            )
            units.append(record)
            unit_ids.append(record["id"])
        sections.append(
            {
                "section_id": block.section_id,
                "title": block.title,
                "parent_title": block.parent_title,
                "unit_ids": unit_ids,
            }
        )
    return units, sections


def _extract_tex_package(
    package: Package, run_id: str, output_root: Path, contract: InputContract
) -> dict[str, Any]:
    assert package.main_tex is not None
    units_root = output_root / "units"
    units_root.mkdir(parents=True, exist_ok=True)
    main_tex = package.main_tex.relative_to(package.root).as_posix()
    document = expand_tex(package.root, main_tex)
    layout = segment_document(document)
    units, sections = extract_tex_units(package.root, document, layout, run_id, units_root)
    tables = build_cell_catalog(document, layout)
    bibliography = [
        (package.root / name).read_text(encoding="utf-8")
        for name in bibliography_files(document, layout.main_end)
        if (package.root / name).is_file()
    ]
    appendix_text = ""
    if layout.main_end < len(document.text):
        appendix_text = document.text[layout.main_end :].split("\\appendix", 1)[1].strip()
    corpus = {
        "main_text": document.text[: layout.main_end].strip(),
        "main_bibliography": "\n\n".join(bibliography),
        "appendix_text": appendix_text,
        "reference_notes": package.reference_notes.read_text(encoding="utf-8"),
        "package_files": {name: sha256_file(path) for name, path in package.files.items()},
        "input_contract": contract.as_dict(),
        "unit_count": len(units),
        "divergence_scope": (
            f"Every paragraph, figure environment, and table environment of the expanded "
            f"{main_tex} body before \\appendix and the bibliography command. The "
            f"bibliography and the appendix are corpus evidence, not divergence units."
        ),
        "appendix_role": (
            "Text after \\appendix is full-context convergence evidence; not a source of "
            "divergence units. Its tables are in the table cell catalog."
        ),
    }
    _write_json(output_root / "corpus.json", corpus)
    _write_json(output_root / "unit-index.json", units)
    _write_json(output_root / "sections.json", sections)
    _write_json(output_root / "table-cells.json", {"tables": tables})
    return {"units": units, "corpus": corpus, "sections": sections, "tables": tables}


def extract_package(
    package: Package, run_id: str, output_root: Path, contract: InputContract
) -> dict[str, Any]:
    """Build all units and the full-context corpus for one run."""
    if package.source == "tex":
        return _extract_tex_package(package, run_id, output_root, contract)
    assert package.main_pdf is not None and package.appendix is not None
    units_root = output_root / "units"
    units_root.mkdir(parents=True, exist_ok=True)
    units, _, main_text, bibliography = extract_pdf_units(
        package.main_pdf,
        "main",
        run_id,
        units_root,
        0,
        merge_chars=contract.paragraph_merge_chars,
    )
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
        "main_bibliography": bibliography,
        "appendix_text": appendix_text,
        "reference_notes": package.reference_notes.read_text(encoding="utf-8"),
        "package_files": {
            "main.pdf": sha256_file(package.main_pdf),
            package.appendix.name: sha256_file(package.appendix),
            "reference-notes.md": sha256_file(package.reference_notes),
        },
        "input_contract": contract.as_dict(),
        "unit_count": len(units),
        "divergence_scope": (
            "Every extractable body prose block, Figure, Table, and caption in main.pdf. "
            "Detected bibliography blocks are corpus evidence, not divergence units."
        ),
        "appendix_role": "Full-context convergence evidence; not a source of divergence units.",
    }
    _write_json(output_root / "corpus.json", corpus)
    _write_json(output_root / "unit-index.json", units)
    return {"units": units, "corpus": corpus}
