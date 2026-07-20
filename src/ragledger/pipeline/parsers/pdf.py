"""PDF parser adapter backed by `pypdf` (FR-020's native PDF adapter).

Text extraction only. `pypdf` operates purely on the in-memory bytes
handed to it: no network access ever occurs during parsing (FR-025),
and embedded files/JavaScript/forms are never executed (FR-026).
Encrypted/password-protected PDFs are an explicit parse failure rather
than a blind empty-password attempt (FR-027); PDFs over the configured
page cap fail explicitly rather than being silently truncated (FR-014).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from io import BytesIO
from typing import Any

from pypdf import PdfReader

from ragledger.core.hashing import hash_raw_bytes
from ragledger.pipeline.parsers.base import (
    LedgerDocument,
    LedgerElement,
    ParseLimits,
    ParseOutcome,
    ParserDescriptor,
    resolve_distribution_version,
)

_PARSER_NAME = "ragledger.pypdf"
_ALLOWED_CONFIG_KEYS: set[str] = set()


class PdfParser:
    """PDF parser adapter backed by `pypdf`."""

    def descriptor(self) -> ParserDescriptor:
        return ParserDescriptor(
            name=_PARSER_NAME,
            version=resolve_distribution_version("pypdf"),
            package_distributions=["pypdf"],
        )

    def supports(self, media_type: str) -> bool:
        return media_type == "application/pdf"

    def validate_config(self, config: Mapping[str, Any]) -> None:
        unknown = set(config) - _ALLOWED_CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown pdf parser config keys: {sorted(unknown)}")

    def parse(self, data: bytes, config: Mapping[str, Any], limits: ParseLimits) -> ParseOutcome:
        start = time.monotonic()
        consumed_hash = hash_raw_bytes(data)
        if len(data) > limits.max_input_bytes:
            return ParseOutcome(
                status="fail",
                consumed_input_hash=consumed_hash,
                errors=["INPUT_TOO_LARGE"],
                duration_seconds=time.monotonic() - start,
            )
        try:
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                return ParseOutcome(
                    status="fail",
                    consumed_input_hash=consumed_hash,
                    errors=["PASSWORD_PROTECTED"],
                    duration_seconds=time.monotonic() - start,
                )
            page_count = len(reader.pages)
            if page_count > limits.max_pages:
                return ParseOutcome(
                    status="fail",
                    consumed_input_hash=consumed_hash,
                    errors=[f"PDF_PAGE_LIMIT_EXCEEDED: {page_count} > {limits.max_pages}"],
                    duration_seconds=time.monotonic() - start,
                )
            elements: list[LedgerElement] = []
            order = 0
            for page_index, page in enumerate(reader.pages):
                page_text = (page.extract_text() or "").strip()
                if not page_text:
                    continue
                elements.append(
                    LedgerElement(
                        id=f"e{order}",
                        kind="paragraph",
                        order=order,
                        text=page_text,
                        page=page_index + 1,
                    )
                )
                order += 1
        except Exception as exc:  # pypdf raises several distinct error types for malformed input
            return ParseOutcome(
                status="fail",
                consumed_input_hash=consumed_hash,
                errors=[f"PDF_PARSE_ERROR: {type(exc).__name__}: {exc}"],
                duration_seconds=time.monotonic() - start,
            )
        document = LedgerDocument(elements=elements, page_count=page_count)
        return ParseOutcome(
            status="success",
            document=document,
            consumed_input_hash=consumed_hash,
            duration_seconds=time.monotonic() - start,
        )
