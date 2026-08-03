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
