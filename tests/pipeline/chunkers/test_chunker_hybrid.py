"""Tests for `ragledger.pipeline.chunkers.hybrid.HybridChunker`."""

from __future__ import annotations

from ragledger.pipeline.chunkers.hybrid import HybridChunker
from ragledger.pipeline.parsers.base import LedgerDocument, LedgerElement


def _long_section(paragraph_count: int, words_per_paragraph: int = 3) -> LedgerDocument:
    elements = [
        LedgerElement(
            id=f"e{i}",
            kind="paragraph",
            order=i,
            text=" ".join(f"p{i}w{j}" for j in range(words_per_paragraph)),
            heading_path=["Section"],
        )
        for i in range(paragraph_count)
    ]
    return LedgerDocument(elements=elements)


def test_descriptor() -> None:
    chunker = HybridChunker()
    descriptor = chunker.descriptor()
    assert descriptor.name and descriptor.version


def test_never_merges_across_different_headings() -> None:
    elements = [
        LedgerElement(id="e0", kind="paragraph", order=0, text="a b", heading_path=["A"]),
        LedgerElement(id="e1", kind="paragraph", order=1, text="c d", heading_path=["B"]),
    ]
    document = LedgerDocument(elements=elements)
    chunker = HybridChunker()
    candidates = list(chunker.iterate_chunks(document, {"max_tokens": 200}))
    assert {tuple(c.heading_path) for c in candidates} == {("A",), ("B",)}


def test_split_section_gets_overlap_between_consecutive_chunks() -> None:
    document = _long_section(paragraph_count=6, words_per_paragraph=3)
    chunker = HybridChunker()
    candidates = list(chunker.iterate_chunks(document, {"max_tokens": 6, "overlap_tokens": 3}))
    same_section = [c for c in candidates if c.heading_path == ["Section"]]
    assert len(same_section) > 1
    for first, second in zip(same_section, same_section[1:], strict=False):
        first_words = set(first.raw_text.split())
        second_words = set(second.raw_text.split())
        assert first_words & second_words, "consecutive chunks within a split section must overlap"


def test_zero_overlap_behaves_like_no_repetition() -> None:
    document = _long_section(paragraph_count=6, words_per_paragraph=3)
    chunker = HybridChunker()
    candidates = list(chunker.iterate_chunks(document, {"max_tokens": 6, "overlap_tokens": 0}))
    same_section = [c for c in candidates if c.heading_path == ["Section"]]
    all_words = [word for c in same_section for word in c.raw_text.split()]
    # with zero overlap every word appears in exactly one chunk
    assert len(all_words) == len(set(all_words))


def test_ordinals_strictly_increasing() -> None:
    document = _long_section(paragraph_count=6, words_per_paragraph=3)
    chunker = HybridChunker()
    candidates = list(chunker.iterate_chunks(document, {"max_tokens": 6, "overlap_tokens": 2}))
    ordinals = [c.locator.ordinal for c in candidates]
    assert ordinals == sorted(ordinals)
    assert len(set(ordinals)) == len(ordinals)


def test_deterministic_across_two_runs() -> None:
    document = _long_section(paragraph_count=8, words_per_paragraph=3)
    chunker = HybridChunker()
    config = {"max_tokens": 6, "overlap_tokens": 2}
    first = [c.raw_text for c in chunker.iterate_chunks(document, config)]
    second = [c.raw_text for c in chunker.iterate_chunks(document, config)]
    assert first == second
