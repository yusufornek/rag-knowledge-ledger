"""Tests for `ragledger.pipeline.parsers.pdf.PdfParser`."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from pypdf import PdfWriter

from ragledger.pipeline.parsers.base import ParseLimits
from ragledger.pipeline.parsers.pdf import PdfParser


@pytest.fixture
def parser() -> PdfParser:
    return PdfParser()


def test_descriptor_uses_real_installed_pypdf_version(parser: PdfParser) -> None:
    import importlib.metadata

    descriptor = parser.descriptor()
    assert descriptor.package_distributions == ["pypdf"]
    assert descriptor.version == importlib.metadata.version("pypdf")


def test_supports_only_pdf(parser: PdfParser) -> None:
    assert parser.supports("application/pdf")
    assert not parser.supports("text/plain")


def test_corpus_sample_pdf_extracts_text(corpus_dir: Path, parser: PdfParser) -> None:
    data = (corpus_dir / "sample.pdf").read_bytes()
    outcome = parser.parse(data, {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    assert outcome.document.page_count == 1
    full_text = " ".join(e.text for e in outcome.document.elements)
    assert "pdf.canary.leaktest@example.com" in full_text
    assert all(e.page == 1 for e in outcome.document.elements)


def test_blank_page_pdf_is_success_with_zero_elements(parser: PdfParser) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    outcome = parser.parse(buffer.getvalue(), {}, ParseLimits())
    assert outcome.status == "success"
    assert outcome.document is not None
    assert outcome.document.page_count == 1
    assert outcome.document.elements == []


def test_encrypted_pdf_fails_explicitly_without_guessing_password(parser: PdfParser) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt(user_password="secret123", owner_password="ownersecret")
    buffer = io.BytesIO()
    writer.write(buffer)
    outcome = parser.parse(buffer.getvalue(), {}, ParseLimits())
    assert outcome.status == "fail"
    assert outcome.errors == ["PASSWORD_PROTECTED"]


def test_corrupt_bytes_fail_without_raising(parser: PdfParser) -> None:
    outcome = parser.parse(b"this is not a pdf file at all", {}, ParseLimits())
    assert outcome.status == "fail"
    assert outcome.errors[0].startswith("PDF_PARSE_ERROR")


def test_page_count_over_cap_fails_explicitly(parser: PdfParser) -> None:
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    outcome = parser.parse(buffer.getvalue(), {}, ParseLimits(max_pages=2))
    assert outcome.status == "fail"
    assert outcome.errors[0].startswith("PDF_PAGE_LIMIT_EXCEEDED")


def test_input_over_max_bytes_fails_explicitly(parser: PdfParser, corpus_dir: Path) -> None:
    data = (corpus_dir / "sample.pdf").read_bytes()
    outcome = parser.parse(data, {}, ParseLimits(max_input_bytes=10))
    assert outcome.status == "fail"
    assert outcome.errors == ["INPUT_TOO_LARGE"]
