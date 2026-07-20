"""Tests for `ragledger.pipeline.parsers.csv_parser.CsvParser`."""

from __future__ import annotations

from pathlib import Path

import pytest

from ragledger.pipeline.parsers.base import ParseLimits
from ragledger.pipeline.parsers.csv_parser import CsvParser


@pytest.fixture
def parser() -> CsvParser:
    return CsvParser()


def test_supports_only_csv(parser: CsvParser) -> None:
    assert parser.supports("text/csv")
    assert not parser.supports("text/plain")


def test_one_element_per_row_including_header(parser: CsvParser) -> None:
    data = b"a,b\n1,2\n3,4\n"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    rows = outcome.document.elements
    assert len(rows) == 3
    assert all(row.kind == "table" for row in rows)
    assert rows[0].text == "a | b"
    assert rows[1].text == "1 | 2"


def test_header_repeated_on_every_row(parser: CsvParser) -> None:
    data = b"a,b\n1,2\n3,4\n"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.document is not None
    assert all(row.table_header == "a | b" for row in outcome.document.elements)


def test_blank_rows_skipped(parser: CsvParser) -> None:
    data = b"a,b\n\n1,2\n"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.document is not None
    assert len(outcome.document.elements) == 2


def test_empty_file_is_success_with_zero_elements(parser: CsvParser) -> None:
    outcome = parser.parse(b"", {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    assert outcome.document.elements == []


def test_custom_delimiter(parser: CsvParser) -> None:
    data = b"a;b\n1;2\n"
    outcome = parser.parse(data, {"delimiter": ";"}, ParseLimits())
    assert outcome.document is not None
    assert outcome.document.elements[0].text == "a | b"


def test_corpus_sample_csv_parses(corpus_dir: Path, parser: CsvParser) -> None:
    data = (corpus_dir / "sample.csv").read_bytes()
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    assert len(outcome.document.elements) == 4  # header + 3 rows
