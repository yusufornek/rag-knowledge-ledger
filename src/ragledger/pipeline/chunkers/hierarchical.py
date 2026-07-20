"""The `hierarchical` built-in chunker (FR-030): structure-aware, section-based.

Groups elements into maximal contiguous runs sharing the same
`heading_path` (a "section") and greedily accumulates whole elements
into a chunk up to `max_tokens`; a chunk boundary is only ever placed at
a size limit or at a change of structural parent, never splitting small
adjacent siblings that share the same parent (section 34.4). A single
element that alone exceeds `max_tokens` is handled per
`oversized_element_policy` (FR-036): `"split"` divides it into
token-bounded pieces (never a silent truncation), `"fail"` raises
`OversizedElementError`.

No overlap is introduced between chunks: chunk boundaries here are
structural, and `hybrid` is the strategy that additionally applies
overlap within a section that had to be split.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from ragledger.pipeline.chunkers.base import (
    ChunkCandidate,
    ChunkerDescriptor,
    ContextualizedChunk,
    OversizedElementError,
    PositionedElement,
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

_NAME = "ragledger.hierarchical"
_VERSION = "1"


class HierarchicalChunker:
    """Structure-aware chunking by heading/table section (FR-030)."""

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
                    group, group_tokens = [], 0
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
