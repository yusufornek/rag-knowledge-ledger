"""The `Chunker` adapter contract, per the design specification section 34.3/34.4.

Shared primitives every built-in chunker (`line_based`, `hierarchical`,
`hybrid`) uses: the deterministic reference tokenizer, chunk-size config
parsing (FR-031, section 34.4's size policies), the declarative
contextualization template renderer (FR-033), and element positioning
helpers used to compute `StructuralLocator.character_start`/`_end`
relative to the normalized parsed document text.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from ragledger.core.models import RagledgerModel, StructuralLocator
from ragledger.pipeline.parsers.base import LedgerDocument, LedgerElement

# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------


class TokenizerUnavailableError(RuntimeError):
    """A configured tokenizer could not be resolved.

    Per the design specification section 40's edge-case decision ("Tokenizer
    unavailable: build fail, not approximate whitespace tokens"): a
    build must fail outright rather than silently substituting an
    approximate word count for a tokenizer the caller asked for by
    name. Raising this instead of falling back is how that rule is
    enforced.
    """


@dataclass(frozen=True)
class TokenizerDescriptor:
    name: str
    revision: str


@runtime_checkable
class Tokenizer(Protocol):
    def descriptor(self) -> TokenizerDescriptor: ...

    def count(self, text: str) -> int: ...


class WhitespaceTokenizer:
    """The deterministic reference tokenizer shipped with ragledger.

    Counts runs of non-whitespace characters. This is honestly named --
    it never claims to stand in for a real named tokenizer such as
    `cl100k_base` -- so declaring it in chunker config is not
    "approximating" anything per the design specification section 40; it simply
    is what its name says it is. Integration with a real installed
    tokenizer library is a documented gap.
    """

    NAME = "ragledger-simple-tokenizer"
    REVISION = "1"
    PATTERN = re.compile(r"\S+")

    def descriptor(self) -> TokenizerDescriptor:
        return TokenizerDescriptor(name=self.NAME, revision=self.REVISION)

    def count(self, text: str) -> int:
        return len(self.PATTERN.findall(text))


def resolve_tokenizer(name: str) -> Tokenizer:
    """Resolve a tokenizer by declared name.

    Raises `TokenizerUnavailableError` for any name other than the
    shipped reference tokenizer, rather than approximating with
    whitespace splitting for a tokenizer that was not actually
    resolved.
    """
    if name == WhitespaceTokenizer.NAME:
        return WhitespaceTokenizer()
    raise TokenizerUnavailableError(
        f"tokenizer {name!r} is not available in this ragledger installation; "
        f"only {WhitespaceTokenizer.NAME!r} ships in this release, and a build "
        "refuses to approximate an unavailable tokenizer with whitespace token "
        "counts (the design specification section 40)"
    )


# --------------------------------------------------------------------------
# Chunk candidate / contextualized chunk
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkCandidate:
    """One chunk boundary a chunker proposes, per the design specification section 34.3.

    `locator` is a fully-formed `StructuralLocator` ready to feed
    directly into `ragledger.core.ids.chunk_id` and `ChunkRecord`.
    """

    element_refs: tuple[str, ...]
    raw_text: str
    locator: StructuralLocator
    heading_path: list[str]
    table_caption: str | None


@dataclass(frozen=True)
class ContextualizedChunk:
    """The exact string sent to the embedder, plus its token count."""

    candidate: ChunkCandidate
    contextualized_text: str
    token_count: int


class ChunkerDescriptor(RagledgerModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)


class ChunkerConfigError(ValueError):
    """Raised by `validate_config`/`parse_size_config` for invalid chunker config."""


class OversizedElementError(RuntimeError):
    """Raised when an indivisible element exceeds `max_tokens` and the
    configured `oversized_element_policy` is `"fail"` (FR-036).
    """


@runtime_checkable
class Chunker(Protocol):
    """The chunker adapter contract, per the design specification section 34.3."""

    def descriptor(self) -> ChunkerDescriptor: ...

    def validate_config(self, config: Mapping[str, Any]) -> None: ...

    def iterate_chunks(
        self, document: LedgerDocument, config: Mapping[str, Any]
    ) -> Iterator[ChunkCandidate]: ...

    def contextualize(
        self, candidate: ChunkCandidate, config: Mapping[str, Any]
    ) -> ContextualizedChunk: ...


class ChunkerRegistry:
    """A deterministic name -> chunker adapter registry."""

    def __init__(self) -> None:
        self._chunkers: dict[str, Chunker] = {}

    def register(self, name: str, chunker: Chunker) -> None:
        self._chunkers[name] = chunker

    def get(self, name: str) -> Chunker | None:
        return self._chunkers.get(name)

    def names(self) -> list[str]:
        return sorted(self._chunkers)


def default_registry() -> ChunkerRegistry:
    """Build the registry of built-in chunkers, per FR-030."""
    from ragledger.pipeline.chunkers.hierarchical import HierarchicalChunker
    from ragledger.pipeline.chunkers.hybrid import HybridChunker
    from ragledger.pipeline.chunkers.line_based import LineBasedChunker

    registry = ChunkerRegistry()
    registry.register("line_based", LineBasedChunker())
    registry.register("hierarchical", HierarchicalChunker())
    registry.register("hybrid", HybridChunker())
    return registry


# --------------------------------------------------------------------------
# Chunk-size configuration (section 34.4)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkSizeConfig:
    tokenizer_name: str
    max_tokens: int
    target_tokens: int | None
    overlap_tokens: int
    min_tokens: int
    oversized_element_policy: str


_SIZE_CONFIG_KEYS = {
    "tokenizer_name",
    "max_tokens",
    "target_tokens",
    "overlap_tokens",
    "min_tokens",
    "oversized_element_policy",
}
_TEMPLATE_CONFIG_KEY = "contextualization_template"


def parse_size_config(config: Mapping[str, Any]) -> ChunkSizeConfig:
    """Parse and validate the section 34.4 chunk-size policy fields.

    Raises `ChunkerConfigError` for any value that violates the spec's
    stated invariants (max tokens hard, overlap strictly less than max,
    non-negative min/overlap, a recognized oversized-element policy).
    """
    tokenizer_name = config.get("tokenizer_name", WhitespaceTokenizer.NAME)
    max_tokens = config.get("max_tokens", 200)
    target_tokens = config.get("target_tokens")
    overlap_tokens = config.get("overlap_tokens", 0)
    min_tokens = config.get("min_tokens", 0)
    oversized_element_policy = config.get("oversized_element_policy", "split")

    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ChunkerConfigError("max_tokens must be a positive integer")
    if (
        not isinstance(overlap_tokens, int)
        or isinstance(overlap_tokens, bool)
        or overlap_tokens < 0
    ):
        raise ChunkerConfigError("overlap_tokens must be a non-negative integer")
    if overlap_tokens >= max_tokens:
        raise ChunkerConfigError(
            "overlap_tokens must be strictly less than max_tokens (design specification 34.4)"
        )
    if not isinstance(min_tokens, int) or isinstance(min_tokens, bool) or min_tokens < 0:
        raise ChunkerConfigError("min_tokens must be a non-negative integer")
    if target_tokens is not None and (
        not isinstance(target_tokens, int)
        or isinstance(target_tokens, bool)
        or target_tokens <= 0
        or target_tokens > max_tokens
    ):
        raise ChunkerConfigError(
            "target_tokens must be a positive integer not exceeding max_tokens"
        )
    if oversized_element_policy not in ("split", "fail"):
        raise ChunkerConfigError("oversized_element_policy must be 'split' or 'fail'")
    if not isinstance(tokenizer_name, str) or not tokenizer_name:
        raise ChunkerConfigError("tokenizer_name must be a non-empty string")

    return ChunkSizeConfig(
        tokenizer_name=tokenizer_name,
        max_tokens=max_tokens,
        target_tokens=target_tokens,
        overlap_tokens=overlap_tokens,
        min_tokens=min_tokens,
        oversized_element_policy=oversized_element_policy,
    )


def validate_size_and_template_config(config: Mapping[str, Any]) -> None:
    unknown = set(config) - _SIZE_CONFIG_KEYS - {_TEMPLATE_CONFIG_KEY}
    if unknown:
        raise ChunkerConfigError(f"unknown chunker config keys: {sorted(unknown)}")
    parse_size_config(config)
    template = config.get(_TEMPLATE_CONFIG_KEY)
    if template is not None:
        validate_contextualization_template(template)


# --------------------------------------------------------------------------
# Declarative contextualization template (FR-033)
# --------------------------------------------------------------------------

_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
_ALLOWED_PLACEHOLDERS = {"heading_path", "text", "table_caption"}
DEFAULT_TEMPLATE = "{text}"


def validate_contextualization_template(template: str) -> None:
    """Reject a template referencing any placeholder outside the fixed whitelist.

    FR-033 requires the contextualization template be declarative, with
    no arbitrary code execution; enforcing a closed placeholder
    whitelist (checked here) and rendering by direct regex substitution
    rather than `str.format` (see `render_contextualization_template`,
    which never calls `.format` on caller-controlled objects, avoiding
    format-string attribute-access tricks) are both part of that
    guarantee.
    """
    for name in _TEMPLATE_PLACEHOLDER_RE.findall(template):
        if name not in _ALLOWED_PLACEHOLDERS:
            raise ChunkerConfigError(
                f"unknown contextualization template placeholder {{{name}}}; "
                f"allowed placeholders are {sorted(_ALLOWED_PLACEHOLDERS)}"
            )


def render_contextualization_template(template: str, candidate: ChunkCandidate) -> str:
    values = {
        "heading_path": " > ".join(candidate.heading_path),
        "text": candidate.raw_text,
        "table_caption": candidate.table_caption or "",
    }

    def _substitute(match: re.Match[str]) -> str:
        return values[match.group(1)]

    return _TEMPLATE_PLACEHOLDER_RE.sub(_substitute, template)


def contextualize_candidate(
    candidate: ChunkCandidate, config: Mapping[str, Any], tokenizer: Tokenizer
) -> ContextualizedChunk:
    template = config.get(_TEMPLATE_CONFIG_KEY, DEFAULT_TEMPLATE)
    text = render_contextualization_template(template, candidate)
    return ContextualizedChunk(
        candidate=candidate, contextualized_text=text, token_count=tokenizer.count(text)
    )


# --------------------------------------------------------------------------
# Element positioning (character offsets relative to normalized parsed text)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionedElement:
    """One element's location within the concatenated, normalized document text.

    Elements are joined with a two-character ``"\\n\\n"`` separator, the
    same separator `raw_text` construction uses, so `char_start`/`char_end`
    here are consistent with the offsets a caller would get by locating
    a chunk's `raw_text` inside the full concatenated document text.
    """

    element: LedgerElement
    char_start: int
    char_end: int


def position_elements(document: LedgerDocument) -> list[PositionedElement]:
    positioned: list[PositionedElement] = []
    cursor = 0
    for index, element in enumerate(document.elements):
        if index > 0:
            cursor += 2  # the "\n\n" separator between elements
        start = cursor
        cursor += len(element.text)
        positioned.append(PositionedElement(element=element, char_start=start, char_end=cursor))
    return positioned


def document_text(positioned: Sequence[PositionedElement]) -> str:
    return "\n\n".join(item.element.text for item in positioned)


def split_oversized(
    positioned: PositionedElement, max_tokens: int, tokenizer: Tokenizer
) -> list[PositionedElement]:
    """Split one oversized, indivisible element into `max_tokens`-bounded pieces.

    Splits strictly on the tokenizer's own token boundaries (never mid
    multi-byte character, never a byte truncation -- section 34.4: "No
    byte truncate causing invalid Unicode"), and each piece is an exact
    contiguous substring of the original text, so its character offsets
    within the full document text are computed exactly, not
    approximated.
    """
    if not isinstance(tokenizer, WhitespaceTokenizer):
        raise TokenizerUnavailableError(
            "oversized-element splitting is only implemented for the shipped "
            f"{WhitespaceTokenizer.NAME!r} tokenizer"
        )
    text = positioned.element.text
    matches = list(WhitespaceTokenizer.PATTERN.finditer(text))
    if not matches:
        return [positioned]
    pieces: list[PositionedElement] = []
    for group_start in range(0, len(matches), max_tokens):
        group = matches[group_start : group_start + max_tokens]
        local_start, local_end = group[0].start(), group[-1].end()
        piece_element = positioned.element.model_copy(update={"text": text[local_start:local_end]})
        pieces.append(
            PositionedElement(
                element=piece_element,
                char_start=positioned.char_start + local_start,
                char_end=positioned.char_start + local_end,
            )
        )
    return pieces


def build_candidate(parts: Sequence[PositionedElement], ordinal: int) -> ChunkCandidate:
    """Build a `ChunkCandidate` from one or more contiguous positioned elements.

    Repeats a table's header text at the start of `raw_text` when the
    chunk continues a table split across multiple chunks and does not
    itself start with the header row, so the repeated header
    contributes to this chunk's content hash (FR-035).
    """
    elements = [part.element for part in parts]
    heading_path = elements[0].heading_path
    pages = [element.page for element in elements if element.page is not None]
    element_ids = [element.id for element in elements]
    table_caption = next(
        (element.table_caption for element in elements if element.table_caption), None
    )

    raw_text = "\n\n".join(element.text for element in elements)
    first = elements[0]
    if (
        first.kind == "table"
        and first.table_header
        and first.text.strip() != first.table_header.strip()
    ):
        raw_text = f"{first.table_header}\n{raw_text}"

    locator = StructuralLocator(
        kind="document_span",
        page_start=min(pages) if pages else None,
        page_end=max(pages) if pages else None,
        heading_path=heading_path or None,
        element_ids=element_ids,
        character_start=parts[0].char_start,
        character_end=parts[-1].char_end,
        ordinal=ordinal,
    )
    return ChunkCandidate(
        element_refs=tuple(element_ids),
        raw_text=raw_text,
        locator=locator,
        heading_path=heading_path,
        table_caption=table_caption,
    )


def group_by_heading_path(positioned: Sequence[PositionedElement]) -> list[list[PositionedElement]]:
    """Group elements into maximal contiguous runs sharing the same `heading_path`.

    Used by the structure-aware chunkers (`hierarchical`, `hybrid`) so a
    chunk boundary is only ever placed at a size limit or a change of
    structural parent, never splitting small siblings that share the
    same parent across an arbitrary boundary (section 34.4: "küçük
    adjacent siblings merge only same structural parent").
    """
    sections: list[list[PositionedElement]] = []
    for item in positioned:
        if sections and sections[-1][-1].element.heading_path == item.element.heading_path:
            sections[-1].append(item)
        else:
            sections.append([item])
    return sections


def drop_empty_candidates(candidates: Iterator[ChunkCandidate]) -> tuple[list[ChunkCandidate], int]:
    """Filter out whitespace-only candidates (FR-037), returning the dropped count."""
    kept: list[ChunkCandidate] = []
    dropped = 0
    for candidate in candidates:
        if candidate.raw_text.strip():
            kept.append(candidate)
        else:
            dropped += 1
    return kept, dropped
