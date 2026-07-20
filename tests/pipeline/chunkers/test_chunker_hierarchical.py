"""Tests for `ragledger.pipeline.chunkers.hierarchical.HierarchicalChunker`."""

from __future__ import annotations

import pytest

from ragledger.pipeline.chunkers.base import OversizedElementError
from ragledger.pipeline.chunkers.hierarchical import HierarchicalChunker
from ragledger.pipeline.parsers.base import LedgerDocument, LedgerElement


def _section_document() -> LedgerDocument:
    elements = [
        LedgerElement(id="e0", kind="title", order=0, text="Doc Title", heading_path=[]),
        LedgerElement(
            id="e1", kind="paragraph", order=1, text="para one", heading_path=["Doc Title"]
        ),
        LedgerElement(
            id="e2", kind="paragraph", order=2, text="para two", heading_path=["Doc Title"]
        ),
        LedgerElement(id="e3", kind="heading", order=3, text="Sub", heading_path=["Doc Title"]),
        LedgerElement(
            id="e4", kind="paragraph", order=4, text="sub para", heading_path=["Doc Title", "Sub"]
        ),
    ]
    return LedgerDocument(elements=elements)


def test_never_merges_across_different_headings() -> None:
    chunker = HierarchicalChunker()
    document = _section_document()
    candidates = list(chunker.iterate_chunks(document, {"max_tokens": 200}))
    heading_paths = [c.heading_path for c in candidates]
    # "Doc Title" alone and "Doc Title" > "Sub" must never be merged into one chunk
    assert ["Doc Title"] in heading_paths
    assert ["Doc Title", "Sub"] in heading_paths


def test_small_adjacent_siblings_merge_under_same_parent() -> None:
    chunker = HierarchicalChunker()
    document = _section_document()
    candidates = list(chunker.iterate_chunks(document, {"max_tokens": 200}))
    top_level = [c for c in candidates if c.heading_path == ["Doc Title"]]
    assert len(top_level) == 1
    assert "para one" in top_level[0].raw_text
    assert "para two" in top_level[0].raw_text


def test_section_split_when_exceeding_max_tokens() -> None:
    chunker = HierarchicalChunker()
    document = _section_document()
    candidates = list(chunker.iterate_chunks(document, {"max_tokens": 3}))
    top_level = [c for c in candidates if c.heading_path == ["Doc Title"]]
    assert len(top_level) >= 2  # "para one" and "para two" no longer fit in one chunk


def test_oversized_single_element_split_policy() -> None:
    chunker = HierarchicalChunker()
    long_text = " ".join(f"w{i}" for i in range(10))
    document = LedgerDocument(
        elements=[LedgerElement(id="e0", kind="paragraph", order=0, text=long_text)]
    )
    candidates = list(
        chunker.iterate_chunks(document, {"max_tokens": 3, "oversized_element_policy": "split"})
    )
    assert len(candidates) == 4  # 10 tokens split into chunks of 3
    rejoined = " ".join(c.raw_text for c in candidates)
    assert rejoined == long_text


def test_oversized_single_element_fail_policy_raises() -> None:
    chunker = HierarchicalChunker()
    long_text = " ".join(f"w{i}" for i in range(10))
    document = LedgerDocument(
        elements=[LedgerElement(id="e0", kind="paragraph", order=0, text=long_text)]
    )
    with pytest.raises(OversizedElementError):
        list(
            chunker.iterate_chunks(document, {"max_tokens": 3, "oversized_element_policy": "fail"})
        )


def test_table_header_repeated_when_table_split_across_chunks() -> None:
    chunker = HierarchicalChunker()
    rows = [
        LedgerElement(
            id=f"e{i}",
            kind="table",
            order=i,
            text=f"row{i}col1 | row{i}col2",
            table_header="col1 | col2",
        )
        for i in range(6)
    ]
    document = LedgerDocument(elements=rows)
    candidates = list(chunker.iterate_chunks(document, {"max_tokens": 4}))
    assert len(candidates) > 1
    for candidate in candidates[1:]:
        assert candidate.raw_text.startswith("col1 | col2")


def test_deterministic_across_two_runs() -> None:
    chunker = HierarchicalChunker()
    document = _section_document()
    config = {"max_tokens": 4}
    first = [c.raw_text for c in chunker.iterate_chunks(document, config)]
    second = [c.raw_text for c in chunker.iterate_chunks(document, config)]
    assert first == second
