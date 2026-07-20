"""Tests for `ragledger.pipeline.parsers.json_parser.JsonParser`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ragledger.pipeline.parsers.base import ParseLimits
from ragledger.pipeline.parsers.json_parser import JsonParser


@pytest.fixture
def parser() -> JsonParser:
    return JsonParser()


def test_supports_only_json(parser: JsonParser) -> None:
    assert parser.supports("application/json")
    assert not parser.supports("text/plain")


def test_array_of_objects_one_element_per_item(parser: JsonParser) -> None:
    data = json.dumps([{"a": 1}, {"b": 2}]).encode()
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    assert len(outcome.document.elements) == 2
    assert '"a": 1' in outcome.document.elements[0].text


def test_object_one_element_per_sorted_key(parser: JsonParser) -> None:
    data = json.dumps({"zeta": 1, "alpha": 2}).encode()
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.document is not None
    assert [e.heading_path for e in outcome.document.elements] == [["alpha"], ["zeta"]]


def test_scalar_top_level_is_one_element(parser: JsonParser) -> None:
    outcome = parser.parse(b'"just a string"', {}, ParseLimits())
    assert outcome.document is not None
    assert len(outcome.document.elements) == 1


def test_rendering_is_key_order_independent(parser: JsonParser) -> None:
    a = parser.parse(json.dumps([{"x": 1, "y": 2}]).encode(), {}, ParseLimits())
    b = parser.parse(json.dumps([{"y": 2, "x": 1}]).encode(), {}, ParseLimits())
    assert a.document is not None and b.document is not None
    assert a.document.elements[0].text == b.document.elements[0].text


def test_invalid_json_fails_explicitly(parser: JsonParser) -> None:
    outcome = parser.parse(b"{not valid json", {}, ParseLimits())
    assert outcome.status == "fail"
    assert outcome.errors[0].startswith("JSON_DECODE_ERROR")


def test_corpus_sample_json_parses(corpus_dir: Path, parser: JsonParser) -> None:
    data = (corpus_dir / "sample.json").read_bytes()
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    assert len(outcome.document.elements) == 3
