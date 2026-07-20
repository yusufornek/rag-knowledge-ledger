"""Tests for `ragledger.pipeline.parsers.text.PlainTextParser`."""

from __future__ import annotations

import pytest

from ragledger.pipeline.parsers.base import ParseLimits
from ragledger.pipeline.parsers.text import PlainTextParser


@pytest.fixture
def parser() -> PlainTextParser:
    return PlainTextParser()


def test_descriptor_has_no_backing_distribution(parser: PlainTextParser) -> None:
    descriptor = parser.descriptor()
    assert descriptor.name
    assert descriptor.version
    assert descriptor.package_distributions == []


def test_supports_only_plain_text(parser: PlainTextParser) -> None:
    assert parser.supports("text/plain")
    assert not parser.supports("text/markdown")
    assert not parser.supports("application/pdf")


def test_splits_blank_line_separated_paragraphs(parser: PlainTextParser) -> None:
    data = b"First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    texts = [element.text for element in outcome.document.elements]
    assert texts == ["First paragraph.", "Second paragraph.", "Third paragraph."]
    assert all(element.kind == "paragraph" for element in outcome.document.elements)
    assert [element.order for element in outcome.document.elements] == [0, 1, 2]


def test_empty_document_is_success_with_zero_elements(parser: PlainTextParser) -> None:
    outcome = parser.parse(b"   \n\n  \n", {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    assert outcome.document.elements == []


def test_input_over_max_bytes_fails_explicitly(parser: PlainTextParser) -> None:
    outcome = parser.parse(b"hello", {}, ParseLimits(max_input_bytes=2))
    assert outcome.status == "fail"
    assert outcome.errors == ["INPUT_TOO_LARGE"]
    assert outcome.document is None


def test_undecodable_bytes_fail_explicitly(parser: PlainTextParser) -> None:
    outcome = parser.parse(b"\xff\xfe\x00\x01", {"encoding": "utf-8"}, ParseLimits())
    assert outcome.status == "fail"
    assert outcome.errors[0].startswith("DECODE_ERROR")


def test_consumed_input_hash_is_always_populated(parser: PlainTextParser) -> None:
    outcome = parser.parse(b"hello", {}, ParseLimits())
    assert len(outcome.consumed_input_hash) == 64


def test_unknown_config_key_rejected(parser: PlainTextParser) -> None:
    with pytest.raises(ValueError, match="unknown"):
        parser.validate_config({"bogus": True})


def test_crlf_and_cr_line_endings_normalized(parser: PlainTextParser) -> None:
    outcome = parser.parse(b"line one\r\n\r\nline two\r", {}, ParseLimits())
    assert outcome.document is not None
    texts = [element.text for element in outcome.document.elements]
    assert texts == ["line one", "line two"]
