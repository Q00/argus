from pathlib import Path

import pytest

from argus.core import ArgusError, ProviderError, Result, sha256_file, sha256_text, stable_id


class TestResult:
    def test_ok_result_unwraps_to_its_value(self):
        assert Result.of(42).unwrap() == 42

    def test_err_result_raises_contained_exception_on_unwrap(self):
        error = ProviderError("boom", provider="test")
        with pytest.raises(ProviderError):
            Result.err(error).unwrap()

    def test_error_accessor_on_ok_result_is_a_bug(self):
        with pytest.raises(ArgusError):
            _ = Result.of(1).error

    def test_is_ok_and_is_err_are_mutually_exclusive(self):
        assert Result.of(1).is_ok and not Result.of(1).is_err
        err: Result[int, str] = Result.err("nope")
        assert err.is_err and not err.is_ok


class TestStableId:
    def test_same_parts_produce_same_id(self):
        assert stable_id("x", "a", 1) == stable_id("x", "a", 1)

    def test_part_boundaries_matter(self):
        assert stable_id("x", "ab", "c") != stable_id("x", "a", "bc")


class TestErrors:
    def test_details_fold_into_str(self):
        error = ArgusError("failed", {"count": 3})
        assert "count=3" in str(error)


class TestHashes:
    def test_file_and_text_hashes_agree(self, tmp_path: Path):
        path = tmp_path / "x.txt"
        path.write_text("hello", encoding="utf-8")
        assert sha256_file(path) == sha256_text("hello")
