import json
from pathlib import Path

import pytest

from argus.core import ExtractionError
from argus.extract import InputContract, extract_package, validate_package
from argus.tex import (
    build_cell_catalog,
    catalog_cell_ids,
    expand_tex,
    render_catalog,
    segment_document,
)

CONTRACT = InputContract()


def _line(path: Path, marker: str) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    return next(number for number, line in enumerate(lines, 1) if marker in line)


def _extract(tex_package: Path, tmp_path: Path) -> dict:
    package = validate_package(tex_package, CONTRACT)
    return extract_package(package, "run_1", tmp_path / "out", CONTRACT)


def _texts(result: dict) -> list[str]:
    return [Path(unit["content_path"]).read_text() for unit in result["units"]]


class TestExpansion:
    def test_inputs_are_inlined_with_a_line_map(self, tex_package: Path):
        document = expand_tex(tex_package, "main.tex")
        assert document.files == ("main.tex", "tables/t1.tex")
        index = next(i for i, line in enumerate(document.text.split("\n")) if "Teacher &" in line)
        assert document.origins[index] == (
            "tables/t1.tex",
            _line(tex_package / "tables" / "t1.tex", "Teacher &"),
        )

    def test_preamble_is_not_expanded(self, tex_package: Path):
        # \input{macros} in the preamble names a file that does not exist.
        document = expand_tex(tex_package, "main.tex")
        assert "documentclass" not in document.text

    def test_comments_are_stripped_but_escaped_percent_stays(self, tex_package: Path):
        document = expand_tex(tex_package, "main.tex")
        assert "trailing comment" not in document.text
        assert "comment-only line" not in document.text
        assert "Generated table" not in document.text
        assert "3.5\\%" in document.text
        assert "95\\% intervals" in document.text

    def test_missing_input_is_an_extraction_error(self, tex_package: Path):
        (tex_package / "tables" / "t1.tex").unlink()
        with pytest.raises(ExtractionError) as excinfo:
            expand_tex(tex_package, "main.tex")
        assert "tables/t1.tex" in str(excinfo.value)


class TestSegmentation:
    def test_blocks_titles_and_parents(self, tex_package: Path):
        layout = segment_document(expand_tex(tex_package, "main.tex"))
        assert [(b.section_id, b.title, b.parent_title) for b in layout.blocks] == [
            ("sec_1", "Abstract", None),
            ("sec_2", "Introduction", None),
            ("sec_3", "Experiments", None),
            ("sec_4", "Ablations", "Experiments"),
        ]
        assert [[unit.kind for unit in block.units] for block in layout.blocks] == [
            ["paragraph"],
            ["paragraph", "paragraph"],
            ["paragraph", "table", "figure"],
            ["paragraph"],
        ]

    def test_units_locators_and_line_ranges(self, tex_package: Path, tmp_path: Path):
        result = _extract(tex_package, tmp_path)
        main = tex_package / "main.tex"
        first, second = (
            unit for unit in result["units"] if unit["section_title"] == "Introduction"
        )
        start = _line(main, "Serving large models")
        end = _line(main, "Distillation lowers")
        assert first["locator"] == f"main.tex:L{start}-L{end} (Introduction)"
        assert (first["tex_file"], first["line_start"], first["line_end"]) == (
            "main.tex",
            start,
            end,
        )
        assert first["section_id"] == "sec_2"
        table = next(unit for unit in result["units"] if unit["kind"] == "table")
        t1 = tex_package / "tables" / "t1.tex"
        assert table["locator"] == (
            f"tables/t1.tex:L{_line(t1, 'begin{table}')}-L{_line(t1, 'end{table}')} (Experiments)"
        )
        assert table["image_path"] is None
        assert second["locator"].startswith(f"main.tex:L{_line(main, 'paragraph{Scope')}")

    def test_comment_only_line_does_not_split_a_paragraph(self, tex_package: Path, tmp_path: Path):
        texts = _texts(_extract(tex_package, tmp_path))
        paragraph = next(text for text in texts if "Serving large models" in text)
        assert "Distillation lowers" in paragraph
        assert "%" not in paragraph.replace("\\%", "")

    def test_run_in_paragraph_heading_stays_in_its_paragraph(
        self, tex_package: Path, tmp_path: Path
    ):
        texts = _texts(_extract(tex_package, tmp_path))
        assert any(text.startswith("\\paragraph{Scope.} We study") for text in texts)

    def test_headings_and_command_only_chunks_are_not_units(
        self, tex_package: Path, tmp_path: Path
    ):
        texts = _texts(_extract(tex_package, tmp_path))
        assert not any("\\section" in text or "\\maketitle" in text for text in texts)
        assert not any(text.strip() == "\\label{sec:intro}" for text in texts)

    def test_figure_renders_its_pdf_to_png(self, tex_package: Path, tmp_path: Path):
        result = _extract(tex_package, tmp_path)
        figure = next(unit for unit in result["units"] if unit["kind"] == "figure")
        assert Path(figure["image_path"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        assert figure["caption"] == "Accuracy of the student against two baselines."

    def test_missing_figure_image_is_an_extraction_error(self, tex_package: Path):
        (tex_package / "figs" / "plot.pdf").unlink()
        with pytest.raises(ExtractionError) as excinfo:
            validate_package(tex_package, CONTRACT)
        assert "figs/plot" in str(excinfo.value)

    def test_appendix_is_corpus_not_units(self, tex_package: Path, tmp_path: Path):
        result = _extract(tex_package, tmp_path)
        assert not any("appendix reports" in text for text in _texts(result))
        corpus = json.loads((tmp_path / "out" / "corpus.json").read_text())
        assert "appendix reports" in corpus["appendix_text"]
        assert "appendix reports" not in corpus["main_text"]
        assert "teacher2024" in corpus["main_bibliography"]
        assert corpus["unit_count"] == 7


class TestCellCatalog:
    def test_cells_from_main_and_appendix_tables(self, tex_package: Path, tmp_path: Path):
        _extract(tex_package, tmp_path)
        tables = json.loads((tmp_path / "out" / "table-cells.json").read_text())["tables"]
        by_key = {table["key"]: table for table in tables}
        assert by_key["tab:main"]["rows"] == [
            ["Model", "Accuracy"],
            ["Teacher", "91.2"],
            ["Student", "90.8"],
        ]
        assert by_key["tab:main"]["caption"] == "Main results with 95\\% intervals."
        assert by_key["tab:main"]["in_appendix"] is False
        assert by_key["tab:app"]["rows"] == [["Setting", "Score"], ["Large", "92.0"]]
        assert by_key["tab:app"]["in_appendix"] is True
        assert by_key["tab:app"]["locator"].endswith("(Extra Results)")
        ids = catalog_cell_ids(tables)
        assert {"tab:main:r3c2", "tab:app:r2c1"} <= ids
        assert "tab:main:r4c1" not in ids

    def test_render_is_compact(self, tex_package: Path):
        document = expand_tex(tex_package, "main.tex")
        rendered = render_catalog(build_cell_catalog(document, segment_document(document)))
        assert "[tab:main] Main results with 95\\% intervals.\n r1: c1=Model | c2=Accuracy" in (
            rendered
        )

    def test_rows_split_only_at_top_level(self, tmp_path: Path):
        package = tmp_path / "p"
        package.mkdir()
        (package / "main.tex").write_text(
            "\\begin{document}\n"
            "\\begin{table*}[!tp]\n"
            "\\begin{tabularx}{\\textwidth}{@{}Xc@{}}\n"
            "\\toprule\n"
            " & \\multicolumn{1}{c}{All} \\\\\n"
            "\\cmidrule(lr){2-2}\\addlinespace[2pt]\n"
            "Policy & \\shortstack{Good\\\\lost} \\\\[2pt]\n"
            "A \\& B & 81.3\\% \\\\\n"
            "\\bottomrule\n"
            "\\end{tabularx}\n"
            "\\end{table*}\n"
            "\\end{document}\n",
            encoding="utf-8",
        )
        document = expand_tex(package, "main.tex")
        (table,) = build_cell_catalog(document, segment_document(document))
        assert table["key"] == "table1"
        assert table["rows"] == [
            ["", "\\multicolumn{1}{c}{All}"],
            ["Policy", "\\shortstack{Good\\\\lost}"],
            ["A \\& B", "81.3\\%"],
        ]


class TestTexPackageContract:
    def test_package_files_cover_every_input(self, tex_package: Path):
        package = validate_package(tex_package, CONTRACT)
        assert package.source == "tex"
        assert set(package.files) == {
            "main.tex",
            "tables/t1.tex",
            "figs/plot.pdf",
            "refs.bib",
            "reference-notes.md",
            "issues.json",
        }

    def test_contract_records_source_and_drops_pdf_only_fields(self, tex_package: Path):
        from dataclasses import replace

        recorded = replace(CONTRACT, source="tex").as_dict()
        assert recorded["source"] == "tex"
        assert recorded["main_tex"] == "main.tex"
        assert recorded["main_page_count"] is None
        assert recorded["paragraph_merge_chars"] is None
        assert InputContract().as_dict()["source"] == "pdf"

    def test_reference_notes_are_required(self, tex_package: Path):
        (tex_package / "reference-notes.md").unlink()
        with pytest.raises(ExtractionError):
            validate_package(tex_package, CONTRACT)

    def test_invalid_issue_file_is_rejected(self, tex_package: Path):
        from argus.core import ArgusError

        (tex_package / "issues.json").write_text("[]", encoding="utf-8")
        with pytest.raises(ArgusError):
            validate_package(tex_package, CONTRACT)

    def test_main_tex_name_selects_the_root_file(self, tex_package: Path):
        (tex_package / "main.tex").rename(tex_package / "paper.tex")
        package = validate_package(tex_package, InputContract(main_tex="paper.tex"))
        assert "paper.tex" in package.files
