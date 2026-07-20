"""Native deterministic parser for `text/plain` sources (FR-020).

Splits on blank-line-separated paragraphs and nothing else: no OCR, no
network access, no external distribution. This parser's own module
revision (`_PARSER_VERSION`) is the reported version, since there is no
backing third-party distribution whose version could be resolved
through `importlib.metadata`.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from ragledger.core.hashing import hash_raw_bytes
from ragledger.pipeline.parsers.base import (
    LedgerDocument,
    LedgerElement,
    ParseLimits,
    ParseOutcome,
    ParserDescriptor,
)

_PARSER_NAME = "ragledger.native_text"
_PARSER_VERSION = "1"
_ALLOWED_CONFIG_KEYS = {"encoding"}


class PlainTextParser:
    """Deterministic native parser for `text/plain`."""

    def descriptor(self) -> ParserDescriptor:
        return ParserDescriptor(
            name=_PARSER_NAME, version=_PARSER_VERSION, package_distributions=[]
        )

    def supports(self, media_type: str) -> bool:
        return media_type == "text/plain"

    def validate_config(self, config: Mapping[str, Any]) -> None:
        unknown = set(config) - _ALLOWED_CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown plain text parser config keys: {sorted(unknown)}")

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
        encoding = config.get("encoding", "utf-8")
        try:
            text = data.decode(encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            return ParseOutcome(
                status="fail",
                consumed_input_hash=consumed_hash,
                errors=[f"DECODE_ERROR: {exc}"],
                duration_seconds=time.monotonic() - start,
            )
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        elements: list[LedgerElement] = []
        order = 0
        for paragraph in normalized.split("\n\n"):
            stripped = paragraph.strip()
            if not stripped:
                continue
            elements.append(
                LedgerElement(id=f"e{order}", kind="paragraph", order=order, text=stripped)
            )
            order += 1
        document = LedgerDocument(elements=elements, page_count=1)
        return ParseOutcome(
            status="success",
            document=document,
            consumed_input_hash=consumed_hash,
            duration_seconds=time.monotonic() - start,
        )
