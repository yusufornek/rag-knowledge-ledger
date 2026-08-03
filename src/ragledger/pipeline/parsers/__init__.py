"""Parser adapters: the `DocumentParser` contract, native implementations, and sandbox.

Per the design specification section 34.1 and 8.3.
"""

from __future__ import annotations

from ragledger.pipeline.parsers.base import (
    DocumentParser,
    ElementKind,
    LedgerDocument,
    LedgerElement,
    ParseLimits,
    ParseOutcome,
    ParserDescriptor,
    ParserRegistry,
    ParseStatus,
    default_registry,
    resolve_distribution_version,
)
from ragledger.pipeline.parsers.sandbox import ParserRef, parser_ref_for, run_sandboxed

__all__ = [
    "DocumentParser",
    "ElementKind",
    "LedgerDocument",
    "LedgerElement",
    "ParseLimits",
    "ParseOutcome",
    "ParseStatus",
    "ParserDescriptor",
    "ParserRef",
    "ParserRegistry",
    "default_registry",
    "parser_ref_for",
    "resolve_distribution_version",
    "run_sandboxed",
]
