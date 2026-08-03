"""The `DocumentParser` adapter contract, per the design specification section 34.1.

`LedgerDocument`/`LedgerElement` are the stable structural representation
every native parser maps its own output onto (the design specification section
34.2: "adapter stable `LedgerDocument` representation'a map eder"), so
that a specific parser's internal schema never leaks into the manifest's
public contract. They are plain pydantic models (reusing
`ragledger.core.models.RagledgerModel` for the same "unknown fields are
a hard error" strictness the manifest models use) so that they can be
serialized as the canonical JSON artifact referenced by
`ParseRecord.parsed_artifact_ref` (FR-024) and round-tripped through the
subprocess sandbox (`ragledger.pipeline.parsers.sandbox`) as plain JSON.

`ParserDescriptor.version` and `.package_distributions` are always real,
installed values resolved through `importlib.metadata`
(`resolve_distribution_version`) for parsers backed by a third-party
distribution (for example `pypdf`); native parsers with no backing
distribution report a version pinned to this module's own revision
constant, never a guess.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field

from ragledger.core.models import OcrInfo, RagledgerModel, WarningRecord

ElementKind = Literal[
    "title",
    "heading",
    "paragraph",
    "list_item",
    "table",
    "caption",
    "code",
    "formula",
    "image_reference",
    "page_header",
    "page_footer",
    "footnote",
    "unknown",
]
"""The element kinds `LedgerDocument` can carry, per the design specification section 34.2."""

ParseStatus = Literal["success", "partial", "fail"]


class LedgerElement(RagledgerModel):
    """One structural element of a parsed document (the design specification section 34.2).

    `id` is stable only within one parse run (it is what
    `StructuralLocator.element_ids` references), not a global manifest
    identifier. `heading_path` is the ancestry of headings active at
    this element's position, outermost first. `table_header` carries a
    table row element's reconstructed header text so a chunker can
    repeat it when a table is split across chunks (FR-035); it is
    distinct from `table_caption`, which is an authored caption/label
    for the table as a whole (FR-034).
    """

    id: str = Field(min_length=1)
    kind: ElementKind
    order: int = Field(ge=0)
    text: str
    page: int | None = Field(default=None, ge=1)
    heading_path: list[str] = Field(default_factory=list)
    parent_id: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    table_caption: str | None = None
    table_header: str | None = None


class LedgerDocument(RagledgerModel):
    """A parser's stable output representation (the design specification section 34.1/34.2).

    This is what gets serialized as the canonical JSON artifact
    referenced by `ParseRecord.parsed_artifact_ref` (FR-024).
    `frontmatter` carries a Markdown source's parsed YAML frontmatter
    block (if any) so `ragledger.governance.license` can read a
    declared `license:` field without re-parsing the source; it is
    `None` for formats that have no frontmatter concept.
    """

    elements: list[LedgerElement] = Field(default_factory=list)
    page_count: int | None = Field(default=None, ge=0)
    frontmatter: dict[str, Any] | None = None


@dataclass(frozen=True)
class ParseLimits:
    """Resource caps enforced around one parse run (the design specification FR-014).

    Defaults match the spec's stated defaults: 100 MiB max source size,
    500 PDF pages. `max_output_bytes` and `timeout_seconds` bound the
    subprocess sandbox (`ragledger.pipeline.parsers.sandbox`);
    `max_memory_bytes` is a best-effort `RLIMIT_AS` cap applied to the
    sandboxed subprocess where the platform supports it.
    """

    max_input_bytes: int = 100 * 1024 * 1024
    max_pages: int = 500
    max_output_bytes: int = 50 * 1024 * 1024
    timeout_seconds: float = 30.0
    max_memory_bytes: int | None = 512 * 1024 * 1024


class ParseOutcome(RagledgerModel):
    """The result of one parser run (the design specification section 34.1).

    `consumed_input_hash` is always populated, even on failure: it
    records the exact bytes the parser was handed, independent of
    whether it could make sense of them. `status` distinguishes a clean
    parse (`success`), a parse that produced a partial/degraded document
    with warnings (`partial`), and one that produced nothing usable
    (`fail`) -- FR-022.
    """

    status: ParseStatus
    document: LedgerDocument | None = None
    warnings: list[WarningRecord] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    ocr: OcrInfo | None = None
    consumed_input_hash: str = Field(min_length=1)
    duration_seconds: float = Field(ge=0)


class ParserDescriptor(RagledgerModel):
    """A parser adapter's immutable identity (the design specification section 34.1).

    Never hand-typed: `version` for a distribution-backed parser is
    resolved through `resolve_distribution_version`; a native parser
    with no backing distribution reports its own module-level revision
    constant. Both are real, observed values, never a guess (the design specification
    section 0 rule 2).
    """

    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    package_distributions: list[str] = Field(default_factory=list)
    model_digests: list[str] | None = None
    container_digest: str | None = None


def resolve_distribution_version(distribution_name: str) -> str:
    """Return the installed version of `distribution_name` via `importlib.metadata`.

    Raises `importlib.metadata.PackageNotFoundError` if the distribution
    is not installed: a parser must never report a guessed version for a
    library it depends on.
    """
    return importlib.metadata.version(distribution_name)


@runtime_checkable
class DocumentParser(Protocol):
    """The parser adapter contract, per the design specification section 34.1.

    Implementations must never perform network I/O (FR-025), never
    execute embedded files/macros (FR-026), and must return a `fail`
    `ParseOutcome` rather than raise for input the parser cannot handle
    (encrypted documents, corrupt files, oversized input) so a single
    bad source never crashes a build -- this contract is also what the
    subprocess sandbox in `ragledger.pipeline.parsers.sandbox` degrades
    to on a crash or timeout it observes from the outside.
    """

    def descriptor(self) -> ParserDescriptor: ...

    def supports(self, media_type: str) -> bool: ...

    def validate_config(self, config: Mapping[str, Any]) -> None: ...

    def parse(
        self, data: bytes, config: Mapping[str, Any], limits: ParseLimits
    ) -> ParseOutcome: ...


class ParserRegistry:
    """A deterministic media-type -> parser adapter registry."""

    def __init__(self) -> None:
        self._parsers: dict[str, DocumentParser] = {}

    def register(self, media_type: str, parser: DocumentParser) -> None:
        self._parsers[media_type] = parser

    def get(self, media_type: str) -> DocumentParser | None:
        return self._parsers.get(media_type)

    def media_types(self) -> list[str]:
        return sorted(self._parsers)


def default_registry() -> ParserRegistry:
    """Build the registry of native parsers shipped with ragledger.

    Imports are local to avoid a module-import cycle (each parser module
    imports types from this module).
    """
    from ragledger.pipeline.parsers.csv_parser import CsvParser
    from ragledger.pipeline.parsers.html_parser import HtmlDocumentParser
    from ragledger.pipeline.parsers.json_parser import JsonParser
    from ragledger.pipeline.parsers.markdown import MarkdownParser
    from ragledger.pipeline.parsers.pdf import PdfParser
    from ragledger.pipeline.parsers.text import PlainTextParser

    registry = ParserRegistry()
    registry.register("text/plain", PlainTextParser())
    registry.register("text/markdown", MarkdownParser())
    registry.register("text/html", HtmlDocumentParser())
    registry.register("application/json", JsonParser())
    registry.register("text/csv", CsvParser())
    registry.register("application/pdf", PdfParser())
    return registry
