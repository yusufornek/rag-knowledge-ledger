"""The `line_based` built-in chunker (FR-030): fixed-size windows with overlap.

Ignores document structure entirely -- headings, tables, and paragraph
boundaries are not treated specially -- and instead slides a
`max_tokens`-wide window over the flat token stream of the whole
document, advancing by `max_tokens - overlap_tokens` tokens each step.
This is the strategy of choice for unstructured or line-oriented
sources (raw logs, exported CSV/table data) where per-section
grouping (`hierarchical`/`hybrid`) is not meaningful.

The window step is always at least 1 token (`parse_size_config`
enforces `overlap_tokens < max_tokens`), so this always terminates in a
bounded number of steps proportional to the document length.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from ragledger.pipeline.chunkers.base import (
    ChunkCandidate,
    ChunkerDescriptor,
    ContextualizedChunk,
    Tokenizer,
    WhitespaceTokenizer,
    build_candidate,
    contextualize_candidate,
    document_text,
    parse_size_config,
    position_elements,
    resolve_tokenizer,
    validate_size_and_template_config,
)
from ragledger.pipeline.parsers.base import LedgerDocument

_NAME = "ragledger.line_based"
_VERSION = "1"


class LineBasedChunker:
    """Fixed-size token window with configurable overlap (FR-030)."""

    def descriptor(self) -> ChunkerDescriptor:
        return ChunkerDescriptor(name=_NAME, version=_VERSION)

    def validate_config(self, config: Mapping[str, Any]) -> None:
        validate_size_and_template_config(config)

    def iterate_chunks(
        self, document: LedgerDocument, config: Mapping[str, Any]
    ) -> Iterator[ChunkCandidate]:
        size = parse_size_config(config)
        tokenizer = resolve_tokenizer(size.tokenizer_name)
        positioned = position_elements(document)
        if not positioned:
            return
        full_text = document_text(positioned)
        matches: list[tuple[int, int]] = (
            [
                (match.start(), match.end())
                for match in WhitespaceTokenizer.PATTERN.finditer(full_text)
            ]
            if isinstance(tokenizer, WhitespaceTokenizer)
            else list(_generic_token_spans(full_text, tokenizer))
        )
        if not matches:
            return

        step = max(1, size.max_tokens - size.overlap_tokens)
        ordinal = 0
        index = 0
        total = len(matches)
        while index < total:
            window = matches[index : index + size.max_tokens]
            char_start, char_end = window[0][0], window[-1][1]
            contributing = [
                item
                for item in positioned
                if item.char_end > char_start and item.char_start < char_end
            ]
            if contributing:
                yield build_candidate(contributing, ordinal)
                ordinal += 1
            index += step

    def contextualize(
        self, candidate: ChunkCandidate, config: Mapping[str, Any]
    ) -> ContextualizedChunk:
        size = parse_size_config(config)
        tokenizer = resolve_tokenizer(size.tokenizer_name)
        return contextualize_candidate(candidate, config, tokenizer)


def _generic_token_spans(text: str, tokenizer: Tokenizer) -> Iterator[tuple[int, int]]:
    """Fallback token-span iteration for a non-`WhitespaceTokenizer` tokenizer.

    Unreachable in this release: `resolve_tokenizer` only ever returns
    `WhitespaceTokenizer`. Kept as an explicit, honest failure point
    instead of silently reusing whitespace spans if a future tokenizer
    is added without updating this module.
    """
    raise NotImplementedError(
        f"line_based chunking is only implemented for {WhitespaceTokenizer.NAME!r}"
    )
