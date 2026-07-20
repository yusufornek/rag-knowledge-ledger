"""Tests for `ragledger.pipeline.chunkers.line_based.LineBasedChunker`."""

from __future__ import annotations

from ragledger.pipeline.chunkers.line_based import LineBasedChunker
from ragledger.pipeline.parsers.base import LedgerDocument, LedgerElement


def _document(word_count: int) -> LedgerDocument:
    text = " ".join(f"word{i}" for i in range(word_count))
    return LedgerDocument(elements=[LedgerElement(id="e0", kind="paragraph", order=0, text=text)])


def test_descriptor() -> None:
    chunker = LineBasedChunker()
    descriptor = chunker.descriptor()
    assert descriptor.name
    assert descriptor.version


def test_small_document_is_a_single_chunk() -> None:
    chunker = LineBasedChunker()
    document = _document(5)
    candidates = list(chunker.iterate_chunks(document, {"max_tokens": 50}))
    assert len(candidates) == 1
    assert candidates[0].raw_text == document.elements[0].text


def test_fixed_size_windows_with_overlap() -> None:
    chunker = LineBasedChunker()
    document = _document(20)
    candidates = list(chunker.iterate_chunks(document, {"max_tokens": 5, "overlap_tokens": 2}))
    assert len(candidates) > 1
    # every consecutive pair of chunks shares some overlapping words
    for first, second in zip(candidates, candidates[1:], strict=False):
        first_words = set(first.raw_text.split())
        second_words = set(second.raw_text.split())
        assert first_words & second_words


def test_ordinals_are_sequential_and_locators_are_document_span() -> None:
    chunker = LineBasedChunker()
    document = _document(20)
    candidates = list(chunker.iterate_chunks(document, {"max_tokens": 5, "overlap_tokens": 1}))
    assert [c.locator.ordinal for c in candidates] == list(range(len(candidates)))
    assert all(c.locator.kind == "document_span" for c in candidates)


def test_empty_document_yields_no_chunks() -> None:
    chunker = LineBasedChunker()
    document = LedgerDocument(elements=[])
    assert list(chunker.iterate_chunks(document, {})) == []


def test_contextualize_uses_configured_template() -> None:
    chunker = LineBasedChunker()
    document = _document(3)
    candidate = next(iter(chunker.iterate_chunks(document, {"max_tokens": 50})))
    result = chunker.contextualize(
        candidate, {"max_tokens": 50, "contextualization_template": "PREFIX {text}"}
    )
    assert result.contextualized_text.startswith("PREFIX ")
    assert result.token_count == len(result.contextualized_text.split())


def test_deterministic_across_two_runs() -> None:
    chunker = LineBasedChunker()
    document = _document(30)
    config = {"max_tokens": 6, "overlap_tokens": 2}
    first = [c.raw_text for c in chunker.iterate_chunks(document, config)]
    second = [c.raw_text for c in chunker.iterate_chunks(document, config)]
    assert first == second
