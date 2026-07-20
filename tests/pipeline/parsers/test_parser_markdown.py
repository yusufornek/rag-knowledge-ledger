"""Tests for `ragledger.pipeline.parsers.markdown.MarkdownParser`."""

from __future__ import annotations

from pathlib import Path

import pytest

from ragledger.pipeline.parsers.base import ParseLimits
from ragledger.pipeline.parsers.markdown import MarkdownParser


@pytest.fixture
def parser() -> MarkdownParser:
    return MarkdownParser()


def test_supports_only_markdown(parser: MarkdownParser) -> None:
    assert parser.supports("text/markdown")
    assert not parser.supports("text/plain")


def test_frontmatter_parsed_and_excluded_from_body(parser: MarkdownParser) -> None:
    data = b"---\nlicense: MIT\ntenant: acme\n---\n\n# Title\n\nBody text.\n"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    assert outcome.document.frontmatter == {"license": "MIT", "tenant": "acme"}
    texts = [element.text for element in outcome.document.elements]
    assert "license: MIT" not in " ".join(texts)
    assert texts[0] == "Title"


def test_invalid_frontmatter_yaml_produces_warning_not_failure(parser: MarkdownParser) -> None:
    data = b"---\n: not: valid: yaml: [\n---\n\nBody.\n"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    assert outcome.document.frontmatter is None
    assert any(w.code == "FRONTMATTER_YAML_INVALID" for w in outcome.warnings)


def test_heading_levels_and_title_kind(parser: MarkdownParser) -> None:
    data = b"# Top\n\n## Sub\n\nParagraph under sub.\n"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.document is not None
    kinds = [(e.kind, e.text) for e in outcome.document.elements]
    assert kinds[0] == ("title", "Top")
    assert kinds[1] == ("heading", "Sub")
    assert outcome.document.elements[2].heading_path == ["Top", "Sub"]


def test_heading_ancestry_resets_on_same_level_sibling(parser: MarkdownParser) -> None:
    data = b"# Doc\n\n## A\n\nUnder A.\n\n## B\n\nUnder B.\n"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.document is not None
    under_a = next(e for e in outcome.document.elements if e.text == "Under A.")
    under_b = next(e for e in outcome.document.elements if e.text == "Under B.")
    assert under_a.heading_path == ["Doc", "A"]
    assert under_b.heading_path == ["Doc", "B"]


def test_list_items(parser: MarkdownParser) -> None:
    data = b"- first item\n- second item\n1. numbered item\n"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.document is not None
    items = [e for e in outcome.document.elements if e.kind == "list_item"]
    assert [i.text for i in items] == ["first item", "second item", "numbered item"]


def test_fenced_code_block_preserved(parser: MarkdownParser) -> None:
    data = b"Intro.\n\n```python\ndef f():\n    return 1\n```\n\nOutro.\n"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.document is not None
    code = next(e for e in outcome.document.elements if e.kind == "code")
    assert "def f():" in code.text
    assert "return 1" in code.text


def test_table_rows_and_header_and_caption(parser: MarkdownParser) -> None:
    data = (
        b"Table: Regional contacts\n\n"
        b"| Region | Contact |\n"
        b"|---|---|\n"
        b"| EU | eu@example.com |\n"
        b"| US | us@example.com |\n"
    )
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.document is not None
    rows = [e for e in outcome.document.elements if e.kind == "table"]
    assert len(rows) == 3  # header row + two data rows
    assert all(row.table_caption == "Regional contacts" for row in rows)
    assert all(row.table_header == "Region | Contact" for row in rows)
    assert rows[1].text == "EU | eu@example.com"
    # the caption paragraph itself must not also appear as its own element
    assert not any(e.kind == "paragraph" and "Table:" in e.text for e in outcome.document.elements)


def test_element_order_is_sequential(parser: MarkdownParser) -> None:
    data = b"# T\n\nP1.\n\nP2.\n"
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.document is not None
    assert [e.order for e in outcome.document.elements] == list(
        range(len(outcome.document.elements))
    )


def test_corpus_sample_markdown_parses(corpus_dir: Path, parser: MarkdownParser) -> None:
    data = (corpus_dir / "sample.md").read_bytes()
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    assert outcome.document.frontmatter == {"license": "MIT", "tenant": "acme-support"}
    kinds = {e.kind for e in outcome.document.elements}
    assert {"title", "heading", "paragraph", "list_item", "table", "code"} <= kinds
