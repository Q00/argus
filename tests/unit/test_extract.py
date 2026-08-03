import json
from pathlib import Path

import pytest

from argus.core import ExtractionError
from argus.extract import InputContract, extract_package, validate_package

CONTRACT = InputContract(main_page_count=1, min_text_chars=100)


class TestValidatePackage:
    def test_valid_package_passes(self, paper_package: Path):
        package = validate_package(paper_package, CONTRACT)
        assert package.main_pdf.name == "main.pdf"
        assert package.appendix.name == "appendix.md"

    def test_missing_files_are_listed(self, tmp_path: Path):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(ExtractionError) as excinfo:
            validate_package(empty, CONTRACT)
        assert "missing" in str(excinfo.value)

    def test_page_count_contract_is_enforced(self, paper_package: Path):
        with pytest.raises(ExtractionError) as excinfo:
            validate_package(paper_package, InputContract(main_page_count=4, min_text_chars=100))
        assert "page-count" in str(excinfo.value)

    def test_page_count_contract_can_accept_any(self, paper_package: Path):
        validate_package(paper_package, InputContract(main_page_count=None, min_text_chars=100))

    def test_non_pdf_main_is_rejected(self, paper_package: Path):
        (paper_package / "main.pdf").write_bytes(b"not a pdf")
        with pytest.raises(ExtractionError):
            validate_package(paper_package, CONTRACT)


class TestExtractPackage:
    def test_units_cover_paragraphs_and_figure(self, paper_package: Path, tmp_path: Path):
        package = validate_package(paper_package, CONTRACT)
        output = tmp_path / "out"
        result = extract_package(package, "run_1", output, CONTRACT)
        kinds = {unit["kind"] for unit in result["units"]}
        assert "paragraph" in kinds
        assert "figure" in kinds
        figure = next(unit for unit in result["units"] if unit["kind"] == "figure")
        assert Path(figure["image_path"]).is_file()

    def test_unit_manifest_records_hashes_and_crop_evidence(
        self, paper_package: Path, tmp_path: Path
    ):
        package = validate_package(paper_package, CONTRACT)
        result = extract_package(package, "run_1", tmp_path / "out", CONTRACT)
        figure = next(unit for unit in result["units"] if unit["kind"] == "figure")
        manifest = json.loads(Path(figure["manifest_path"]).read_text())
        assert manifest["payload"]["text_sha256"]
        assert manifest["payload"]["image_sha256"]
        assert "drawing_overlap_ratio" in manifest["crop_evidence"]
        # The fixture draws a rectangle inside the crop region.
        assert manifest["crop_evidence"]["drawing_overlap_ratio"] > 0

    def test_corpus_records_contract_and_hashes(self, paper_package: Path, tmp_path: Path):
        package = validate_package(paper_package, CONTRACT)
        extract_package(package, "run_1", tmp_path / "out", CONTRACT)
        corpus = json.loads((tmp_path / "out" / "corpus.json").read_text())
        assert corpus["input_contract"]["main_page_count"] == 1
        assert "main.pdf" in corpus["package_files"]
        assert "distillation" in corpus["main_text"]


def _pdf_with_references(tmp_path: Path) -> Path:
    """One-page package: body paragraph, References section, appendix resume."""
    import fitz

    package = tmp_path / "ref-package"
    package.mkdir()
    document = fitz.open()
    page = document.new_page()
    page.insert_textbox(
        fitz.Rect(60, 60, 540, 140),
        "We evaluate the proposed method on three benchmarks. Accuracy improves by 3.5 points.",
        fontsize=11,
    )
    page.insert_textbox(fitz.Rect(60, 160, 540, 190), "References", fontsize=12)
    page.insert_textbox(
        fitz.Rect(60, 200, 540, 260),
        "Ada Lovelace and Alan Turing. On computable numbers. Annals of Mathematics, 1936.",
        fontsize=9,
    )
    page.insert_textbox(
        fitz.Rect(60, 270, 540, 330),
        "Grace Hopper. The education of a computer. Proceedings of the ACM, 1952.",
        fontsize=9,
    )
    page.insert_textbox(fitz.Rect(60, 350, 540, 380), "Appendix A. Proofs", fontsize=12)
    page.insert_textbox(
        fitz.Rect(60, 390, 540, 460),
        "The full proof of the convergence theorem follows from the bounded gradient assumption.",
        fontsize=11,
    )
    document.save(package / "main.pdf")
    document.close()
    (package / "appendix.md").write_text("## Appendix\n\nExtra material.\n", encoding="utf-8")
    (package / "reference-notes.md").write_text("- Notes.\n", encoding="utf-8")
    return package


class TestReferencesFilter:
    def test_bibliography_blocks_become_corpus_not_units(self, tmp_path: Path):
        package_root = _pdf_with_references(tmp_path)
        contract = InputContract(main_page_count=1, min_text_chars=100)
        package = validate_package(package_root, contract)
        result = extract_package(package, "run_1", tmp_path / "out", contract)
        unit_texts = [Path(unit["content_path"]).read_text() for unit in result["units"]]
        assert not any("Lovelace" in text for text in unit_texts)
        assert "Lovelace" in result["corpus"]["main_bibliography"]
        assert "Hopper" in result["corpus"]["main_bibliography"]

    def test_appendix_heading_resumes_unit_extraction(self, tmp_path: Path):
        package_root = _pdf_with_references(tmp_path)
        contract = InputContract(main_page_count=1, min_text_chars=100)
        package = validate_package(package_root, contract)
        result = extract_package(package, "run_1", tmp_path / "out", contract)
        unit_texts = [Path(unit["content_path"]).read_text() for unit in result["units"]]
        assert any("convergence theorem" in text for text in unit_texts)

    def test_bibliography_stays_out_of_divergence_but_reaches_resolver_corpus(self, tmp_path: Path):
        package_root = _pdf_with_references(tmp_path)
        contract = InputContract(main_page_count=1, min_text_chars=100)
        package = validate_package(package_root, contract)
        result = extract_package(package, "run_1", tmp_path / "out", contract)
        corpus = json.loads((tmp_path / "out" / "corpus.json").read_text())
        assert corpus["main_bibliography"] == result["corpus"]["main_bibliography"]
        assert "bibliography" in corpus["divergence_scope"].lower()


class TestParagraphMerging:
    def test_adjacent_blocks_merge_up_to_target(self, paper_package: Path, tmp_path: Path):
        contract = InputContract(main_page_count=1, min_text_chars=100, paragraph_merge_chars=1200)
        package = validate_package(paper_package, contract)
        result = extract_package(package, "run_1", tmp_path / "out", contract)
        paragraphs = [unit for unit in result["units"] if unit["kind"] == "paragraph"]
        # The fixture's two ~230-char paragraphs fit one merged unit.
        assert len(paragraphs) == 1
        merged = Path(paragraphs[0]["content_path"]).read_text()
        assert "distillation" in merged and "five seeds" in merged

    def test_merge_disabled_keeps_one_unit_per_block(self, paper_package: Path, tmp_path: Path):
        contract = InputContract(main_page_count=1, min_text_chars=100, paragraph_merge_chars=0)
        package = validate_package(paper_package, contract)
        result = extract_package(package, "run_1", tmp_path / "out", contract)
        paragraphs = [unit for unit in result["units"] if unit["kind"] == "paragraph"]
        assert len(paragraphs) == 2

    def test_merge_target_is_recorded_in_contract(self, paper_package: Path, tmp_path: Path):
        contract = InputContract(main_page_count=1, min_text_chars=100, paragraph_merge_chars=800)
        package = validate_package(paper_package, contract)
        extract_package(package, "run_1", tmp_path / "out", contract)
        corpus = json.loads((tmp_path / "out" / "corpus.json").read_text())
        assert corpus["input_contract"]["paragraph_merge_chars"] == 800
