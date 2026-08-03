import pytest

from argus.core import ExtractionError
from argus.segment import SEGMENTER_VERSION, boundary_audit_sample, split_sentences


class TestSplitSentences:
    def test_academic_abbreviations_do_not_split(self):
        text = "Our method, cf. Fig. 3, improves accuracy. Eq. 2 defines the loss. See Sec. 4.2."
        result = split_sentences(text)
        assert len(result.sentences) == 3
        assert result.sentences[0].startswith("Our method, cf. Fig. 3")
        assert result.sentences[1].startswith("Eq. 2")

    def test_no_of_pattern_is_protected(self):
        result = split_sentences("No. of parameters is 7B. Training takes two days.")
        assert len(result.sentences) == 2
        assert result.sentences[0] == "No. of parameters is 7B."

    def test_lossless_invariant_is_verified(self):
        result = split_sentences("First sentence. Second sentence? Third!")
        assert result.lossless
        assert result.version == SEGMENTER_VERSION

    def test_empty_text_yields_no_sentences(self):
        assert split_sentences("   \n  ").sentences == ()

    def test_text_without_terminal_punctuation_is_one_sentence(self):
        assert split_sentences("just a fragment").sentences == ("just a fragment",)

    def test_sentinel_character_in_input_fails_loudly(self):
        with pytest.raises(ExtractionError):
            split_sentences("weird ․ char")

    def test_whitespace_is_normalized_before_segmentation(self):
        result = split_sentences("One  sentence\nacross lines. Two.")
        assert result.sentences[0] == "One sentence across lines."


class TestBoundaryAuditSample:
    def test_sample_is_deterministic(self):
        sentences = tuple(f"Sentence number {index}." for index in range(20))
        assert boundary_audit_sample(sentences) == boundary_audit_sample(sentences)

    def test_sample_respects_size_and_range(self):
        sentences = tuple(f"S{index}." for index in range(10))
        sample = boundary_audit_sample(sentences, sample_size=3)
        assert len(sample) == 3
        assert all(1 <= ordinal <= 10 for ordinal in sample)

    def test_empty_input_yields_empty_sample(self):
        assert boundary_audit_sample(()) == []
