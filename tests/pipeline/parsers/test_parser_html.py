"""Tests for `ragledger.pipeline.parsers.html_parser.HtmlDocumentParser`."""

from __future__ import annotations

from pathlib import Path

import pytest

from ragledger.pipeline.parsers.base import ParseLimits
from ragledger.pipeline.parsers.html_parser import HtmlDocumentParser


@pytest.fixture
def parser() -> HtmlDocumentParser:
    return HtmlDocumentParser()


def test_supports_only_html(parser: HtmlDocumentParser) -> None:
    assert parser.supports("text/html")
    assert not parser.supports("text/plain")


def test_headings_paragraphs_and_ancestry(parser: HtmlDocumentParser) -> None:
    data = b"<h1>Title</h1><p>Intro.</p><h2>Sub</h2><p>Detail.</p>"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    elements = outcome.document.elements
    assert elements[0].kind == "title"
    assert elements[0].text == "Title"
    assert elements[1].heading_path == ["Title"]
    detail = next(e for e in elements if e.text == "Detail.")
    assert detail.heading_path == ["Title", "Sub"]


def test_list_items(parser: HtmlDocumentParser) -> None:
    data = b"<ul><li>One</li><li>Two</li></ul>"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.document is not None
    items = [e for e in outcome.document.elements if e.kind == "list_item"]
    assert [i.text for i in items] == ["One", "Two"]


def test_table_rows_header_and_caption(parser: HtmlDocumentParser) -> None:
    data = (
        b"<table><caption>Escalation matrix</caption>"
        b"<tr><th>Severity</th><th>Owner</th></tr>"
        b"<tr><td>High</td><td>a@example.com</td></tr>"
        b"<tr><td>Low</td><td>b@example.com</td></tr></table>"
    )
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.document is not None
    rows = [e for e in outcome.document.elements if e.kind == "table"]
    assert len(rows) == 3
    assert all(row.table_caption == "Escalation matrix" for row in rows)
    assert all(row.table_header == "Severity | Owner" for row in rows)
    assert rows[1].text == "High | a@example.com"


def test_pre_block_preserves_code_text(parser: HtmlDocumentParser) -> None:
    data = b"<pre>def f():\n    return 1\n</pre>"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.document is not None
    code = next(e for e in outcome.document.elements if e.kind == "code")
    assert "def f():" in code.text
    assert "return 1" in code.text


def test_script_and_style_content_never_emitted(parser: HtmlDocumentParser) -> None:
    data = b"<script>alert('xss')</script><style>body{color:red}</style><p>Real content.</p>"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.document is not None
    texts = " ".join(e.text for e in outcome.document.elements)
    assert "alert" not in texts
    assert "color:red" not in texts
    assert "Real content." in texts


def test_malformed_html_does_not_crash(parser: HtmlDocumentParser) -> None:
    data = b"<html><body><p>Unclosed paragraph <div>nested weirdly</p></div>"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.status == "success"


def test_input_over_max_bytes_fails_explicitly(parser: HtmlDocumentParser) -> None:
    outcome = parser.parse(b"<p>hi</p>", {}, ParseLimits(max_input_bytes=2))
    assert outcome.status == "fail"
    assert outcome.errors == ["INPUT_TOO_LARGE"]


def test_corpus_sample_html_parses(corpus_dir: Path, parser: HtmlDocumentParser) -> None:
    data = (corpus_dir / "sample.html").read_bytes()
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    kinds = {e.kind for e in outcome.document.elements}
    assert {"title", "heading", "paragraph", "list_item", "table"} <= kinds
