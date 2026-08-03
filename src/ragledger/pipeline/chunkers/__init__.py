"""Chunker adapters: the `Chunker` contract and built-in strategies.

Per the design specification section 34.3/34.4 and FR-030..FR-038.
"""

from __future__ import annotations

from ragledger.pipeline.chunkers.base import (
    ChunkCandidate,
    Chunker,
    ChunkerConfigError,
    ChunkerDescriptor,
    ChunkerRegistry,
    ChunkSizeConfig,
    ContextualizedChunk,
    OversizedElementError,
    Tokenizer,
    TokenizerDescriptor,
    TokenizerUnavailableError,
    WhitespaceTokenizer,
    default_registry,
    drop_empty_candidates,
    parse_size_config,
    resolve_tokenizer,
)

__all__ = [
    "Chunker",
    "ChunkCandidate",
    "ChunkSizeConfig",
    "ChunkerConfigError",
    "ChunkerDescriptor",
    "ChunkerRegistry",
    "ContextualizedChunk",
    "OversizedElementError",
    "Tokenizer",
    "TokenizerDescriptor",
    "TokenizerUnavailableError",
    "WhitespaceTokenizer",
    "default_registry",
    "drop_empty_candidates",
    "parse_size_config",
    "resolve_tokenizer",
]
