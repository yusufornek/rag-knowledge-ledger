"""Test-only stub parsers used to exercise the sandbox's failure-mode isolation.

Not a test module (no `test_` prefix; pytest does not collect it) and
never imported directly by application code. `run_sandboxed` locates a
parser class by absolute file path (`ragledger.pipeline.parsers.sandbox.parser_ref_for`),
so these classes work as sandboxed parsers exactly like a real,
production `DocumentParser` implementation would, without needing this
file to live inside an importable package.
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


class InfiniteLoopParser:
    """Never returns: simulates a hung/misbehaving parser (proves timeout isolation)."""

    def descriptor(self) -> ParserDescriptor:
        return ParserDescriptor(
            name="test.infinite_loop_parser", version="1", package_distributions=[]
        )

    def supports(self, media_type: str) -> bool:
        return True

    def validate_config(self, config: Mapping[str, Any]) -> None:
        return None

    def parse(self, data: bytes, config: Mapping[str, Any], limits: ParseLimits) -> ParseOutcome:
        while True:
            time.sleep(0.01)


class OversizedOutputParser:
    """Returns an oversized document: simulates a "zip-bomb-like" output blowup."""

    def descriptor(self) -> ParserDescriptor:
        return ParserDescriptor(
            name="test.oversized_output_parser", version="1", package_distributions=[]
        )

    def supports(self, media_type: str) -> bool:
        return True

    def validate_config(self, config: Mapping[str, Any]) -> None:
        return None

    def parse(self, data: bytes, config: Mapping[str, Any], limits: ParseLimits) -> ParseOutcome:
        huge_text = "x" * (2 * 1024 * 1024)
        element = LedgerElement(id="e0", kind="paragraph", order=0, text=huge_text)
        document = LedgerDocument(elements=[element], page_count=1)
        return ParseOutcome(
            status="success",
            document=document,
            consumed_input_hash=hash_raw_bytes(data),
            duration_seconds=0.0,
        )


class CrashingParser:
    """Raises an unhandled exception: simulates a parser bug."""

    def descriptor(self) -> ParserDescriptor:
        return ParserDescriptor(name="test.crashing_parser", version="1", package_distributions=[])

    def supports(self, media_type: str) -> bool:
        return True

    def validate_config(self, config: Mapping[str, Any]) -> None:
        return None

    def parse(self, data: bytes, config: Mapping[str, Any], limits: ParseLimits) -> ParseOutcome:
        raise RuntimeError("simulated parser crash")


class ProcessExitParser:
    """Calls `os._exit` directly: simulates a hard crash the worker cannot catch with try/except."""

    def descriptor(self) -> ParserDescriptor:
        return ParserDescriptor(
            name="test.process_exit_parser", version="1", package_distributions=[]
        )

    def supports(self, media_type: str) -> bool:
        return True

    def validate_config(self, config: Mapping[str, Any]) -> None:
        return None

    def parse(self, data: bytes, config: Mapping[str, Any], limits: ParseLimits) -> ParseOutcome:
        import os

        os._exit(1)
