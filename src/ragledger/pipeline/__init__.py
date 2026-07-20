"""The source/parse/chunk/embed pipeline (PROJECT_SPEC.md sections 8.2-8.5, 10, 34).

Submodules:

- `ragledger.pipeline.discovery`: filesystem source discovery (FR-010..FR-017).
- `ragledger.pipeline.parsers`: the `DocumentParser` adapter contract, native
  parsers, and the subprocess sandbox (FR-020..FR-027, section 34.1/34.6).
- `ragledger.pipeline.chunkers`: the `Chunker` adapter contract and built-in
  strategies (FR-030..FR-038, section 34.3/34.4).
- `ragledger.pipeline.embedding`: the `EmbeddingProvider` adapter contract and
  the deterministic reference embedder (FR-040..FR-047, section 34.5).
- `ragledger.pipeline.cache`: content-addressed pipeline stage caching (10.1).
- `ragledger.pipeline.build`: end-to-end orchestration into a manifest.
"""

from __future__ import annotations
