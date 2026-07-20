"""Native deterministic parser for `text/csv` sources (FR-020).

Uses the standard library `csv` module. Every data row becomes its own
`table`-kind `LedgerElement` (row-level granularity, so a chunker can
group rows into size-bounded chunks); `table_header` on every row holds
the reconstructed header text so a chunker that splits the table across
multiple chunks can repeat the header in each one, per FR-035 ("Table
header repetition hash inputuna dahildir").
"""

from __future__ import annotations

import csv
import io
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

_PARSER_NAME = "ragledger.native_csv"
_PARSER_VERSION = "1"
_ALLOWED_CONFIG_KEYS = {"encoding", "delimiter"}


class CsvParser:
    """Deterministic native parser for `text/csv`."""

    def descriptor(self) -> ParserDescriptor:
        return ParserDescriptor(
            name=_PARSER_NAME, version=_PARSER_VERSION, package_distributions=[]
        )

    def supports(self, media_type: str) -> bool:
        return media_type == "text/csv"

    def validate_config(self, config: Mapping[str, Any]) -> None:
        unknown = set(config) - _ALLOWED_CONFIG_KEYS
        if unknown:
            raise ValueError(f"unknown csv parser config keys: {sorted(unknown)}")

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
        delimiter = config.get("delimiter", ",")
        try:
            text = data.decode(encoding)
        except (LookupError, UnicodeDecodeError) as exc:
            return ParseOutcome(
                status="fail",
                consumed_input_hash=consumed_hash,
                errors=[f"DECODE_ERROR: {exc}"],
                duration_seconds=time.monotonic() - start,
            )
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        except csv.Error as exc:
            return ParseOutcome(
                status="fail",
                consumed_input_hash=consumed_hash,
                errors=[f"CSV_PARSE_ERROR: {exc}"],
                duration_seconds=time.monotonic() - start,
            )
        rows = [row for row in rows if any(cell.strip() for cell in row)]
        if not rows:
            return ParseOutcome(
                status="success",
                document=LedgerDocument(elements=[], page_count=1),
                consumed_input_hash=consumed_hash,
                duration_seconds=time.monotonic() - start,
            )
        header_text = " | ".join(cell.strip() for cell in rows[0])
        elements = [
            LedgerElement(
                id=f"e{index}",
                kind="table",
                order=index,
                text=" | ".join(cell.strip() for cell in row),
                table_header=header_text,
            )
            for index, row in enumerate(rows)
        ]
        document = LedgerDocument(elements=elements, page_count=1)
        return ParseOutcome(
            status="success",
            document=document,
            consumed_input_hash=consumed_hash,
            duration_seconds=time.monotonic() - start,
        )
