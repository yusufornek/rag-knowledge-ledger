"""Tests for `ragledger.pipeline.chunkers.base`: tokenizer, size config,
templates, and positioning helpers shared by every built-in chunker.
"""

from __future__ import annotations

import pytest

from ragledger.pipeline.chunkers.base import (
    ChunkerConfigError,
    TokenizerUnavailableError,
    WhitespaceTokenizer,
    build_candidate,
    document_text,
    drop_empty_candidates,
    group_by_heading_path,
    parse_size_config,
    position_elements,
    render_contextualization_template,
    resolve_tokenizer,
    split_oversized,
    validate_contextualization_template,
)
from ragledger.pipeline.parsers.base import LedgerDocument, LedgerElement


def _doc(*texts_and_headings: tuple[str, list[str]]) -> LedgerDocument:
    elements = [
        LedgerElement(id=f"e{i}", kind="paragraph", order=i, text=text, heading_path=heading)
        for i, (text, heading) in enumerate(texts_and_headings)
    ]
    return LedgerDocument(elements=elements)


class TestTokenizer:
    def test_whitespace_tokenizer_counts_words(self) -> None:
        tokenizer = WhitespaceTokenizer()
        assert tokenizer.count("one two three") == 3
        assert tokenizer.count("") == 0

    def test_resolve_known_tokenizer(self) -> None:
        tokenizer = resolve_tokenizer(WhitespaceTokenizer.NAME)
        assert isinstance(tokenizer, WhitespaceTokenizer)

    def test_resolve_unknown_tokenizer_raises_instead_of_approximating(self) -> None:
        with pytest.raises(TokenizerUnavailableError):
            resolve_tokenizer("cl100k_base")


class TestSizeConfig:
    def test_defaults(self) -> None:
        size = parse_size_config({})
        assert size.tokenizer_name == WhitespaceTokenizer.NAME
        assert size.max_tokens == 200
        assert size.overlap_tokens == 0

    def test_overlap_must_be_less_than_max(self) -> None:
        with pytest.raises(ChunkerConfigError, match="overlap_tokens"):
            parse_size_config({"max_tokens": 10, "overlap_tokens": 10})

    def test_negative_min_tokens_rejected(self) -> None:
        with pytest.raises(ChunkerConfigError, match="min_tokens"):
            parse_size_config({"min_tokens": -1})

    def test_target_tokens_over_max_rejected(self) -> None:
        with pytest.raises(ChunkerConfigError, match="target_tokens"):
            parse_size_config({"max_tokens": 10, "target_tokens": 20})

    def test_bad_oversized_policy_rejected(self) -> None:
        with pytest.raises(ChunkerConfigError, match="oversized_element_policy"):
            parse_size_config({"oversized_element_policy": "truncate"})


class TestContextualizationTemplate:
    def test_placeholder_whitelist_rejects_unknown_field(self) -> None:
        with pytest.raises(ChunkerConfigError, match="placeholder"):
            validate_contextualization_template("{unknown_field}")

    def test_placeholder_whitelist_accepts_known_fields(self) -> None:
        validate_contextualization_template("{heading_path}\n{text}\n{table_caption}")

    def test_render_substitutes_without_using_str_format(self) -> None:
        document = _doc(("body text", ["Section A"]))
        positioned = position_elements(document)
        candidate = build_candidate(positioned, ordinal=0)
        rendered = render_contextualization_template("{heading_path} :: {text}", candidate)
        assert rendered == "Section A :: body text"

    def test_render_does_not_evaluate_attribute_access_injection(self) -> None:
        # A regex-substitution renderer never calls str.format, so a
        # str.format-style attribute-access injection like "{text.__class__}"
        # is not recognized as the "text" placeholder at all (the regex
        # only matches bare identifiers) -- it is left as inert literal
        # text in the output, never evaluated as a Python expression.
        validate_contextualization_template(
            "{text.__class__}"
        )  # does not raise: not a known placeholder
        document = _doc(("body", []))
        positioned = position_elements(document)
        candidate = build_candidate(positioned, ordinal=0)
        rendered = render_contextualization_template("{text.__class__}", candidate)
        assert rendered == "{text.__class__}"
        assert "class 'str'" not in rendered


class TestPositioning:
    def test_position_elements_accounts_for_separator(self) -> None:
        document = _doc(("abc", []), ("de", []))
        positioned = position_elements(document)
        assert positioned[0].char_start == 0
        assert positioned[0].char_end == 3
        assert positioned[1].char_start == 5  # "abc" + "\n\n" (2 chars)
        assert positioned[1].char_end == 7

    def test_document_text_matches_positions(self) -> None:
        document = _doc(("abc", []), ("de", []))
        positioned = position_elements(document)
        text = document_text(positioned)
        assert text == "abc\n\nde"
        assert text[positioned[1].char_start : positioned[1].char_end] == "de"


class TestGroupByHeadingPath:
    def test_contiguous_same_heading_grouped(self) -> None:
        document = _doc(("a", ["H1"]), ("b", ["H1"]), ("c", ["H2"]))
        positioned = position_elements(document)
        sections = group_by_heading_path(positioned)
        assert len(sections) == 2
        assert [item.element.text for item in sections[0]] == ["a", "b"]
        assert [item.element.text for item in sections[1]] == ["c"]


class TestSplitOversized:
    def test_splits_on_token_boundaries_as_exact_substrings(self) -> None:
        document = _doc(("one two three four five six", []))
        positioned = position_elements(document)[0]
        tokenizer = WhitespaceTokenizer()
        pieces = split_oversized(positioned, max_tokens=2, tokenizer=tokenizer)
        assert [p.element.text for p in pieces] == ["one two", "three four", "five six"]
        # every piece is an exact substring of the original element text at its reported offset
        original = document.elements[0].text
        for piece in pieces:
            assert (
                original[
                    piece.char_start - positioned.char_start : piece.char_end
                    - positioned.char_start
                ]
                == piece.element.text
            )


class TestDropEmptyCandidates:
    def test_whitespace_only_candidates_are_dropped_and_counted(self) -> None:
        document = _doc(("   ", []), ("real text", []))
        positioned = position_elements(document)
        candidates = iter(
            [build_candidate([positioned[0]], 0), build_candidate([positioned[1]], 1)]
        )
        kept, dropped = drop_empty_candidates(candidates)
        assert dropped == 1
        assert len(kept) == 1
        assert kept[0].raw_text == "real text"
