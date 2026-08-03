"""The `hybrid` built-in chunker (FR-030): structure-aware with sliding overlap.

Identical section-based grouping to `hierarchical`, with one addition:
when a section has to be split across more than one chunk, the trailing
whole elements of the previous chunk (up to `overlap_tokens` worth) are
repeated at the start of the next chunk within the same section. The
repeated text naturally hashes differently in each chunk because each
chunk's `StructuralLocator` (element refs, character offsets, ordinal)
differs, per the design specification section 34.3: "Overlap neighbor
relationship; overlapping text iki chunkta doğal olarak hash farklı
locator/context ile."
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from ragledger.pipeline.chunkers.base import (
    ChunkCandidate,
    ChunkerDescriptor,
    ContextualizedChunk,
    OversizedElementError,
    PositionedElement,
    Tokenizer,
    build_candidate,
    contextualize_candidate,
    group_by_heading_path,
    parse_size_config,
    position_elements,
    resolve_tokenizer,
    split_oversized,
    validate_size_and_template_config,
)
from ragledger.pipeline.parsers.base import LedgerDocument

_NAME = "ragledger.hybrid"
_VERSION = "1"


class HybridChunker:
    """Structure-aware chunking with sliding-window overlap within a section."""

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
        sections = group_by_heading_path(positioned)

        ordinal = 0
        for section in sections:
            group: list[PositionedElement] = []
            group_tokens = 0
            for item in section:
                item_tokens = tokenizer.count(item.element.text)
                if item_tokens > size.max_tokens:
                    if group:
                        yield build_candidate(group, ordinal)
                        ordinal += 1
                        group, group_tokens = [], 0
                    if size.oversized_element_policy == "fail":
                        raise OversizedElementError(
                            f"element {item.element.id!r} has {item_tokens} tokens, "
                            f"exceeding max_tokens={size.max_tokens} "
                            "(oversized_element_policy=fail)"
                        )
                    for piece in split_oversized(item, size.max_tokens, tokenizer):
                        yield build_candidate([piece], ordinal)
                        ordinal += 1
                    continue
                if group and group_tokens + item_tokens > size.max_tokens:
                    yield build_candidate(group, ordinal)
                    ordinal += 1
                    group = _overlap_tail(group, size.overlap_tokens, tokenizer)
                    group_tokens = sum(tokenizer.count(part.element.text) for part in group)
                group.append(item)
                group_tokens += item_tokens
            if group:
                yield build_candidate(group, ordinal)
                ordinal += 1

    def contextualize(
        self, candidate: ChunkCandidate, config: Mapping[str, Any]
    ) -> ContextualizedChunk:
        size = parse_size_config(config)
        tokenizer = resolve_tokenizer(size.tokenizer_name)
        return contextualize_candidate(candidate, config, tokenizer)


def _overlap_tail(
    group: Sequence[PositionedElement], overlap_tokens: int, tokenizer: Tokenizer
) -> list[PositionedElement]:
    """Return the trailing whole elements of `group` totalling at most `overlap_tokens`."""
    if overlap_tokens <= 0:
        return []
    tail: list[PositionedElement] = []
    total = 0
    for item in reversed(group):
        item_tokens = tokenizer.count(item.element.text)
        if total + item_tokens > overlap_tokens:
            break
        tail.insert(0, item)
        total += item_tokens
    return tail
