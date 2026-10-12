"""LaTeX source packages: expansion, main-body segmentation, table cells.

The tex input path reads the manuscript source instead of a rendered PDF so
every review unit maps 1:1 to a line range of a source file. Regular
expressions here only recognize LaTeX syntax (commands, braces,
environments); nothing in this module classifies the meaning of a sentence.

Exports:
    TexDocument, TexUnit, TexBlock, TexLayout, expand_tex, segment_document,
    build_cell_catalog, catalog_cell_ids, render_catalog, resolve_image,
    render_image, bibliography_files.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

import fitz

from argus.core import ExtractionError

_VERBATIM_BEGIN_RE = re.compile(r"\\begin\{(verbatim\*?|lstlisting|minted|Verbatim)\}")
_INPUT_RE = re.compile(r"\\(?:input|include)\s*\{([^{}]*)\}")
_CONTROL_RE = re.compile(r"\\(?:[A-Za-z@]+|.)", re.DOTALL)
_EVENT_RE = re.compile(
    r"\\(?:(?P<heading>section|subsection|subsubsection)(?![A-Za-z@])\*?"
    r"|begin\{(?P<begin>abstract|figure\*?|table\*?)\}"
    r"|end\{(?P<end>abstract)\})"
)
_APPENDIX_RE = re.compile(r"\\appendix(?![A-Za-z@])")
_BIBLIOGRAPHY_RE = re.compile(r"\\(?:bibliography|printbibliography)(?![A-Za-z@])")
_BIBLIOGRAPHY_FILES_RE = re.compile(r"\\bibliography\s*\{([^{}]*)\}")
_TABLE_RE = re.compile(r"\\begin\{(table\*?)\}")
_TABULAR_RE = re.compile(r"\\begin\{(tabular\*?|tabularx)\}")
_CAPTION_RE = re.compile(r"\\caption(?![A-Za-z@])")
_LABEL_RE = re.compile(r"\\label\s*\{([^{}]*)\}")
_GRAPHICS_RE = re.compile(r"\\includegraphics(?![A-Za-z@])\*?")
# Table rule commands carry no cell content; each pattern consumes exactly
# the arguments its command takes so a following cell is never eaten.
_RULE_RE = re.compile(
    r"\\(?:"
    r"(?:hline|toprule|midrule|bottomrule|addlinespace|morecmidrules)(?![A-Za-z@])"
    r"(?:\s*\[[^\]]*\])?"
    r"|cmidrule(?![A-Za-z@])(?:\s*\([^)]*\))?(?:\s*\[[^\]]*\])?\s*\{[^{}]*\}"
    r"|cline\s*\{[^{}]*\}"
    r"|specialrule\s*\{[^{}]*\}\s*\{[^{}]*\}\s*\{[^{}]*\}"
    r")"
)
_HEADING_LEVELS = {"section": 1, "subsection": 2, "subsubsection": 3}
_TABULAR_ARGS = {
    "tabular": ("opt", "req"),
    "tabular*": ("req", "opt", "req"),
    "tabularx": ("req", "req"),
}
_IMAGE_SUFFIXES = ("", ".pdf", ".png", ".jpg")
_RASTER_SUFFIXES = {".png", ".jpg", ".jpeg"}
FRONT_MATTER_TITLE = "Front matter"
MIN_PROSE_CHARS = 18


@dataclass(frozen=True, slots=True)
class TexDocument:
    """Expanded ``document`` body with a per-line source map.

    Attributes:
        text: Comment-free body with every ``\\input``/``\\include`` inlined.
        origins: For each line of ``text``: (package-relative file, 1-based line).
        files: Package-relative tex files in inclusion order, root first.
    """

    text: str
    origins: tuple[tuple[str, int], ...]
    files: tuple[str, ...]
    line_starts: tuple[int, ...] = field(default=())

    def line_index(self, offset: int) -> int:
        return max(0, bisect.bisect_right(self.line_starts, offset) - 1)


@dataclass(frozen=True, slots=True)
class TexUnit:
    """One review unit: a paragraph chunk or a float environment."""

    kind: str
    text: str
    first_line: int
    last_line: int
    caption: str | None = None
    image: str | None = None


@dataclass(slots=True)
class TexBlock:
    """One section block of the main body, with its units in order."""

    section_id: str
    title: str
    parent_title: str | None
    units: list[TexUnit] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TexLayout:
    """Segmentation of an expanded document.

    Attributes:
        blocks: Main-body section blocks that hold at least one unit.
        main_end: Offset of ``\\appendix`` (or end of text).
        units_end: Offset where main-body units stop (bibliography command).
        figure_images: Raw ``\\includegraphics`` paths of main-body figures.
    """

    blocks: list[TexBlock]
    main_end: int
    units_end: int
    figure_images: tuple[str, ...]


# -- expansion ----------------------------------------------------------------


def _strip_comment(line: str) -> tuple[str, bool]:
    """Remove an unescaped ``%`` comment; report whether one was found."""
    index = 0
    while True:
        index = line.find("%", index)
        if index == -1:
            return line, False
        backslashes = 0
        cursor = index - 1
        while cursor >= 0 and line[cursor] == "\\":
            backslashes += 1
            cursor -= 1
        if backslashes % 2 == 0:
            return line[:index], True
        index += 1


def _clean_lines(raw: str, relative: str) -> list[tuple[str, str, int, bool]]:
    """Comment-strip one file. Returns (text, file, line, verbatim) records.

    A line that held only a comment is dropped: LaTeX treats it as absent,
    so it must not act as a paragraph break. Verbatim environments are kept
    byte for byte.
    """
    records: list[tuple[str, str, int, bool]] = []
    verbatim_end: str | None = None
    for number, line in enumerate(raw.splitlines(), 1):
        if verbatim_end is not None:
            records.append((line, relative, number, True))
            if verbatim_end in line:
                verbatim_end = None
            continue
        text, commented = _strip_comment(line)
        if commented and not text.strip():
            continue
        text = text.rstrip()
        records.append((text, relative, number, False))
        match = _VERBATIM_BEGIN_RE.search(text)
        if match:
            end = f"\\end{{{match.group(1)}}}"
            if end not in text[match.end() :]:
                verbatim_end = end
    return records


def _package_relative(root: Path, raw: str, kind: str) -> tuple[str, Path]:
    path = (root / raw).resolve()
    if not path.is_relative_to(root):
        raise ExtractionError(f"{kind} path leaves the package", {"path": raw})
    return path.relative_to(root).as_posix(), path


def _inline(
    records: list[tuple[str, str, int, bool]],
    root: Path,
    stack: tuple[str, ...],
    files: list[str],
) -> list[tuple[str, str, int]]:
    output: list[tuple[str, str, int]] = []
    for text, relative, number, verbatim in records:
        if verbatim:
            output.append((text, relative, number))
            continue
        cursor = 0
        for match in _INPUT_RE.finditer(text):
            prefix = text[cursor : match.start()]
            if prefix.strip():
                output.append((prefix, relative, number))
            output.extend(_include(match.group(1).strip(), root, stack, files))
            cursor = match.end()
        if cursor == 0:
            output.append((text, relative, number))
        elif text[cursor:].strip():
            output.append((text[cursor:], relative, number))
    return output


def _include(
    name: str, root: Path, stack: tuple[str, ...], files: list[str]
) -> list[tuple[str, str, int]]:
    if not name.endswith(".tex"):
        name += ".tex"
    relative, path = _package_relative(root, name, "\\input")
    if not path.is_file():
        raise ExtractionError("missing \\input file", {"file": relative, "from": stack[-1]})
    if relative in stack:
        raise ExtractionError("cyclic \\input", {"file": relative, "chain": list(stack)})
    if relative not in files:
        files.append(relative)
    records = _clean_lines(path.read_text(encoding="utf-8"), relative)
    return _inline(records, root, (*stack, relative), files)


def expand_tex(root: Path, main_tex: str) -> TexDocument:
    """Expand the root file's ``document`` body with a line map.

    Raises:
        ExtractionError: missing root or input file, cyclic input, or no
            ``\\begin{document}``/``\\end{document}`` pair in the root.
    """
    root = root.resolve()
    main_relative, main_path = _package_relative(root, main_tex, "main tex")
    if not main_path.is_file():
        raise ExtractionError("missing main tex file", {"file": main_relative})
    records = _clean_lines(main_path.read_text(encoding="utf-8"), main_relative)
    begin = next((i for i, record in enumerate(records) if "\\begin{document}" in record[0]), None)
    end = next(
        (
            i
            for i, record in enumerate(records)
            if begin is not None and i > begin and "\\end{document}" in record[0]
        ),
        None,
    )
    if begin is None or end is None:
        raise ExtractionError(
            "main tex file has no \\begin{document} ... \\end{document} pair",
            {"file": main_relative},
        )
    first_text, first_file, first_number, _ = records[begin]
    last_text, last_file, last_number, _ = records[end]
    body = [(first_text.split("\\begin{document}", 1)[1], first_file, first_number, False)]
    body.extend(records[begin + 1 : end])
    body.append((last_text.split("\\end{document}", 1)[0], last_file, last_number, False))
    files = [main_relative]
    expanded = _inline(body, root, (main_relative,), files)
    text = "\n".join(line for line, _, _ in expanded)
    starts = [0]
    for line, _, _ in expanded[:-1]:
        starts.append(starts[-1] + len(line) + 1)
    return TexDocument(
        text=text,
        origins=tuple((relative, number) for _, relative, number in expanded),
        files=tuple(files),
        line_starts=tuple(starts),
    )


# -- brace-level helpers ------------------------------------------------------


def _match_group(text: str, start: int) -> int | None:
    """Index of the bracket closing ``text[start]`` (``{`` or ``[``), or None."""
    opening = text[start]
    closing = "}" if opening == "{" else "]"
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _optional_arg(text: str, index: int) -> int:
    probe = _skip_space(text, index)
    if probe < len(text) and text[probe] == "[":
        close = _match_group(text, probe)
        if close is not None:
            return close + 1
    return index


def _required_arg(text: str, index: int, what: str) -> tuple[str, int]:
    probe = _skip_space(text, index)
    if probe >= len(text) or text[probe] != "{":
        raise ExtractionError(f"{what} has no braced argument", {"near": text[index : index + 60]})
    close = _match_group(text, probe)
    if close is None:
        raise ExtractionError(f"{what} has unbalanced braces", {"near": text[index : index + 60]})
    return text[probe + 1 : close], close + 1


def _environment_end(text: str, begin: int, name: str) -> tuple[int, int]:
    """(start, end) offsets of the ``\\end{name}`` matching the begin at ``begin``."""
    pattern = re.compile(r"\\(begin|end)\{" + re.escape(name) + r"\}")
    depth = 0
    for match in pattern.finditer(text, begin):
        depth += 1 if match.group(1) == "begin" else -1
        if depth == 0:
            return match.start(), match.end()
    raise ExtractionError(f"unclosed {name} environment", {"near": text[begin : begin + 60]})


def _squash(text: str) -> str:
    return " ".join(text.split())


def prose_text(text: str) -> str:
    """Approximate the prose of a chunk by removing LaTeX commands.

    A command's bracket/brace arguments are dropped when they hold a single
    token (labels, lengths, keys) and kept when they hold words, so
    ``\\label{sec:x}`` disappears while ``\\emph{some words}`` survives.
    """
    output: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            match = _CONTROL_RE.match(text, index)
            assert match is not None
            word = match.group(0)
            index = match.end()
            if not word[1:].isalpha():
                output.append(" " if word == "\\\\" else word[1:])
                continue
            while True:
                probe = _skip_space(text, index)
                if probe >= len(text) or text[probe] not in "[{":
                    break
                close = _match_group(text, probe)
                if close is None:
                    break
                inner = text[probe + 1 : close].strip()
                if any(part.isspace() for part in inner):
                    output.append(" " + prose_text(inner) + " ")
                index = close + 1
            output.append(" ")
            continue
        output.append(" " if char in "{}" else char)
        index += 1
    return _squash("".join(output))


# -- segmentation -------------------------------------------------------------


def _heading(text: str, match: re.Match[str]) -> tuple[str, int] | None:
    """(title, end offset) of a sectioning command, or None when it has no
    braced title: the token is then an argument, as in
    ``\\renewcommand{\\subsection}[1]{...}`` or ``\\let\\x\\subsection``."""
    after = _optional_arg(text, match.end())
    probe = _skip_space(text, after)
    if probe >= len(text) or text[probe] != "{":
        return None
    title, end = _required_arg(text, after, f"\\{match.group('heading')}")
    return _squash(title), end


def _headings(text: str, limit: int) -> list[tuple[int, str]]:
    """(start, title) for every sectioning command before ``limit``."""
    found: list[tuple[int, str]] = []
    for match in _EVENT_RE.finditer(text, 0, limit):
        if match.group("heading"):
            heading = _heading(text, match)
            if heading is not None:
                found.append((match.start(), heading[0]))
    return found


def _caption(env: str) -> str | None:
    match = _CAPTION_RE.search(env)
    if match is None:
        return None
    caption, _ = _required_arg(env, _optional_arg(env, match.end()), "\\caption")
    return _squash(caption)


def _first_graphic(env: str) -> str | None:
    match = _GRAPHICS_RE.search(env)
    if match is None:
        return None
    path, _ = _required_arg(env, _optional_arg(env, match.end()), "\\includegraphics")
    return path.strip()


def _paragraph_units(document: TexDocument, start: int, end: int) -> list[TexUnit]:
    """Split ``[start, end)`` on blank lines and file boundaries."""
    if end <= start:
        return []
    units: list[TexUnit] = []
    chunk: list[tuple[int, str]] = []

    def flush() -> None:
        if chunk:
            text = "\n".join(line for _, line in chunk).strip()
            if len(prose_text(text)) >= MIN_PROSE_CHARS:
                units.append(TexUnit("paragraph", text, chunk[0][0], chunk[-1][0]))
        chunk.clear()

    for index in range(document.line_index(start), document.line_index(end - 1) + 1):
        line_start = document.line_starts[index]
        line_end = document.text.find("\n", line_start)
        if line_end == -1:
            line_end = len(document.text)
        piece = document.text[max(start, line_start) : min(end, line_end)]
        if not piece.strip():
            flush()
            continue
        if chunk and document.origins[chunk[-1][0]][0] != document.origins[index][0]:
            flush()
        chunk.append((index, piece))
    flush()
    return units


def _float_unit(document: TexDocument, begin: int, end: int, name: str) -> TexUnit:
    env = document.text[begin:end]
    first = document.line_index(begin)
    last = document.line_index(end - 1)
    # A float split across files is located in the file where it begins.
    while last > first and document.origins[last][0] != document.origins[first][0]:
        last -= 1
    kind = "figure" if name.startswith("figure") else "table"
    return TexUnit(
        kind,
        env,
        first,
        last,
        caption=_caption(env),
        image=_first_graphic(env) if kind == "figure" else None,
    )


def segment_document(document: TexDocument) -> TexLayout:
    """Split the main body into section blocks of paragraph/figure/table units.

    The main body ends at ``\\appendix``; units stop at ``\\bibliography`` or
    ``\\printbibliography``. Headings start blocks and never enter unit text;
    ``\\paragraph`` run-in headings stay inside their paragraph.
    """
    text = document.text
    appendix = _APPENDIX_RE.search(text)
    main_end = appendix.start() if appendix else len(text)
    bibliography = _BIBLIOGRAPHY_RE.search(text, 0, main_end)
    units_end = bibliography.start() if bibliography else main_end
    raw_blocks: list[TexBlock] = []
    current: TexBlock | None = None
    titles: dict[int, str] = {}

    def block() -> TexBlock:
        nonlocal current
        if current is None:
            current = TexBlock("", FRONT_MATTER_TITLE, None)
            raw_blocks.append(current)
        return current

    cursor = search = 0
    while True:
        match = _EVENT_RE.search(text, search, units_end)
        heading = _heading(text, match) if match and match.group("heading") else None
        if match and match.group("heading") and heading is None:
            search = match.end()
            continue
        stop = match.start() if match else units_end
        paragraphs = _paragraph_units(document, cursor, stop)
        if paragraphs:
            block().units.extend(paragraphs)
        if match is None:
            break
        if heading is not None:
            level = _HEADING_LEVELS[match.group("heading")]
            title, cursor = heading
            titles = {lvl: name for lvl, name in titles.items() if lvl < level}
            parent = titles[max(titles)] if titles else None
            titles[level] = title
            current = TexBlock("", title, parent)
            raw_blocks.append(current)
        elif match.group("begin") == "abstract":
            current = TexBlock("", "Abstract", None)
            raw_blocks.append(current)
            cursor = match.end()
        elif match.group("end"):
            current = None
            cursor = match.end()
        else:
            name = match.group("begin")
            _, after = _environment_end(text, match.start(), name)
            block().units.append(_float_unit(document, match.start(), after, name))
            cursor = after
        search = cursor
    blocks = [candidate for candidate in raw_blocks if candidate.units]
    for number, candidate in enumerate(blocks, 1):
        candidate.section_id = f"sec_{number}"
    images = tuple(
        unit.image for candidate in blocks for unit in candidate.units if unit.image is not None
    )
    return TexLayout(blocks=blocks, main_end=main_end, units_end=units_end, figure_images=images)


def locator(document: TexDocument, first_line: int, last_line: int, title: str) -> str:
    file_name, start = document.origins[first_line]
    _, end = document.origins[last_line]
    return f"{file_name}:L{start}-L{end} ({title})"


def bibliography_files(document: TexDocument, main_end: int) -> list[str]:
    """``.bib`` names from ``\\bibliography{a,b}`` in the main body, as written."""
    names: list[str] = []
    for match in _BIBLIOGRAPHY_FILES_RE.finditer(document.text, 0, main_end):
        for name in match.group(1).split(","):
            name = name.strip()
            if name:
                names.append(name if name.endswith(".bib") else f"{name}.bib")
    return names


# -- figures ------------------------------------------------------------------


def resolve_image(root: Path, raw: str) -> tuple[str, Path]:
    """Find an ``\\includegraphics`` target: as written, then .pdf/.png/.jpg.

    Raises:
        ExtractionError: no candidate exists, or its format is unsupported.
    """
    root = root.resolve()
    for suffix in _IMAGE_SUFFIXES:
        relative, path = _package_relative(root, raw + suffix, "\\includegraphics")
        if path.is_file():
            if path.suffix.lower() not in {".pdf", *_RASTER_SUFFIXES}:
                raise ExtractionError("unsupported \\includegraphics format", {"file": relative})
            return relative, path
    raise ExtractionError("missing \\includegraphics file", {"file": raw})


def render_image(source: Path, target: Path) -> None:
    """Write ``target`` as PNG: page 1 of a PDF at 2x, or a raster re-encoded."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".pdf":
        document = fitz.open(source)
        try:
            if document.page_count < 1:
                raise ExtractionError("figure PDF has no pages", {"file": str(source)})
            document[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(target)
        finally:
            document.close()
        return
    # Re-encode instead of copying: adapters infer the media type from the
    # asset suffix, so asset.png must hold PNG bytes.
    fitz.Pixmap(str(source)).save(target)


# -- table cell catalog -------------------------------------------------------


def _split_top_level(body: str, separator: str) -> list[str]:
    """Split on ``\\\\`` rows or ``&`` cells outside braces and nested envs."""
    parts: list[str] = []
    braces = environments = 0
    start = index = 0
    while index < len(body):
        char = body[index]
        top = braces == 0 and environments == 0
        if char == "\\":
            match = _CONTROL_RE.match(body, index)
            assert match is not None
            word = match.group(0)
            if separator == "row" and top and word in {"\\\\", "\\tabularnewline"}:
                parts.append(body[start:index])
                index = match.end()
                if index < len(body) and body[index] == "*":
                    index += 1
                index = _optional_arg(body, index)
                start = index
                continue
            if word == "\\begin":
                environments += 1
            elif word == "\\end":
                environments = max(0, environments - 1)
            index = match.end()
            continue
        if char == "{":
            braces += 1
        elif char == "}":
            braces = max(0, braces - 1)
        elif char == "&" and separator == "cell" and top:
            parts.append(body[start:index])
            start = index + 1
        index += 1
    parts.append(body[start:])
    return parts


def _tabular_rows(env: str) -> list[list[str]]:
    rows: list[list[str]] = []
    cursor = 0
    while True:
        match = _TABULAR_RE.search(env, cursor)
        if match is None:
            return rows
        name = match.group(1)
        index = match.end()
        for kind in _TABULAR_ARGS[name]:
            if kind == "opt":
                index = _optional_arg(env, index)
            else:
                _, index = _required_arg(env, index, f"\\begin{{{name}}}")
        end_start, end_after = _environment_end(env, match.start(), name)
        for raw_row in _split_top_level(env[index:end_start], "row"):
            row = _RULE_RE.sub(" ", raw_row)
            if not row.strip():
                continue
            rows.append([_squash(cell) for cell in _split_top_level(row, "cell")])
        cursor = end_after


def build_cell_catalog(document: TexDocument, layout: TexLayout) -> list[dict[str, Any]]:
    """Every ``table``/``table*`` environment, main body and appendix.

    Each entry: key (``\\label`` or ``table<n>``), caption, locator, file and
    line range, rows of cell strings. Cell IDs are ``<key>:r<row>c<col>``.
    """
    text = document.text
    headings = _headings(text, len(text))
    boundaries = list(headings)
    for event in _EVENT_RE.finditer(text):
        if event.group("begin") == "abstract":
            boundaries.append((event.start(), "Abstract"))
        elif event.group("end"):
            boundaries.append((event.start(), FRONT_MATTER_TITLE))
    boundaries.sort()
    offsets = [offset for offset, _ in boundaries]
    tables: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    cursor = 0
    while True:
        match = _TABLE_RE.search(text, cursor)
        if match is None:
            return tables
        _, after = _environment_end(text, match.start(), match.group(1))
        env = text[match.start() : after]
        label = _LABEL_RE.search(env)
        key = label.group(1).strip() if label else f"table{len(tables) + 1}"
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            key = f"{key}#{seen[key]}"
        position = bisect.bisect_right(offsets, match.start()) - 1
        title = boundaries[position][1] if position >= 0 else FRONT_MATTER_TITLE
        unit = _float_unit(document, match.start(), after, match.group(1))
        file_name, line_start = document.origins[unit.first_line]
        tables.append(
            {
                "key": key,
                "caption": unit.caption,
                "locator": locator(document, unit.first_line, unit.last_line, title),
                "tex_file": file_name,
                "line_start": line_start,
                "line_end": document.origins[unit.last_line][1],
                "in_appendix": match.start() >= layout.main_end,
                "rows": _tabular_rows(env),
            }
        )
        cursor = after


def catalog_cell_ids(tables: list[dict[str, Any]]) -> set[str]:
    return {
        f"{table['key']}:r{row}c{column}"
        for table in tables
        for row, cells in enumerate(table["rows"], 1)
        for column in range(1, len(cells) + 1)
    }


def render_catalog(tables: list[dict[str, Any]]) -> str:
    """Compact prompt rendering: ``[key] caption`` then ``r1: c1=... | c2=...``."""
    if not tables:
        return "No tables."
    lines: list[str] = []
    for table in tables:
        lines.append(f"[{table['key']}] {table['caption'] or '(no caption)'}")
        for row, cells in enumerate(table["rows"], 1):
            rendered = " | ".join(f"c{column}={cell}" for column, cell in enumerate(cells, 1))
            lines.append(f" r{row}: {rendered}")
    return "\n".join(lines)
