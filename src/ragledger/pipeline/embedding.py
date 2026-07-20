"""The `EmbeddingProvider` adapter contract, per PROJECT_SPEC.md section 34.5.

Ships one fully working provider, `DeterministicLocalEmbeddingProvider`:
a seeded hash-projection reference embedder that produces unit-normalized
float32 vectors of a configurable dimension. It is explicitly **not** a
semantic embedding model -- its vectors carry no learned meaning and are
not intended to power real retrieval quality. It exists so the pipeline,
manifest, caching, and reconciliation machinery can be built, tested,
and demonstrated end-to-end without a network call or a multi-hundred-
megabyte model download, and so the "same input -> same output, on any
machine, forever" determinism this whole project is about is trivially
true for the embedding stage too.

`SentenceTransformersEmbeddingProvider` and `OpenAiEmbeddingProvider`
implement the same `EmbeddingProvider` protocol with real config
plumbing (model/revision/device/batch size, or API model/dimension) so
the manifest and build-config machinery around a *named, versioned*
real provider already exists -- but their `embed()` deliberately raises
`ProviderNotAvailableError` rather than making a live network call or a
model-loading attempt neither this environment nor its test suite can
depend on. This is a documented, honest gap; see `IMPLEMENTATION_STATUS.md`.

`ExternalImportEmbeddingProvider` covers FR-047: wrapping vectors
computed entirely outside ragledger, where any metadata field the
caller did not explicitly supply is the literal string ``"unknown"``,
never guessed.
"""

from __future__ import annotations

import hashlib
import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from ragledger.core.hashing import hash_raw_bytes
from ragledger.core.models import RagledgerModel
from ragledger.pipeline.chunkers.base import WhitespaceTokenizer

# --------------------------------------------------------------------------
# Shared contract types
# --------------------------------------------------------------------------


class EmbeddingModelDescriptor(RagledgerModel):
    """An embedding provider's immutable identity, per PROJECT_SPEC.md section 34.5."""

    provider: str = Field(min_length=1)
    name: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    dimension: int = Field(ge=1)
    dtype: str = Field(pattern=r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class EmbeddingBatchResult:
    """`embed()`'s return shape: vectors in input order, plus adapter usage metadata.

    FR-042 requires batch results preserve input order; every provider
    here does (nothing reorders or drops entries -- a partial batch is
    either fully rejected via a raised exception or fully returned,
    never silently reordered/truncated).
    """

    vectors: list[list[float]]
    usage: dict[str, Any]


class EmbeddingProviderError(RuntimeError):
    """Raised for invalid vectors (NaN/Inf, FR-043) or a dimension mismatch (FR-044)."""


class ProviderNotAvailableError(RuntimeError):
    """Raised by a provider whose config plumbing exists but whose live
    call path is not implemented in this release (never a fake response).
    """


@runtime_checkable
class EmbeddingProvider(Protocol):
    """The embedding adapter contract, per PROJECT_SPEC.md section 34.5."""

    def descriptor(self) -> EmbeddingModelDescriptor: ...

    def tokenize(self, texts: Sequence[str]) -> list[int]: ...

    def embed(self, texts: Sequence[str]) -> EmbeddingBatchResult: ...

    def healthcheck(self) -> bool: ...


def validate_finite(vector: Sequence[float]) -> None:
    """Reject a vector containing NaN or Infinity (FR-043)."""
    for value in vector:
        if math.isnan(value) or math.isinf(value):
            raise EmbeddingProviderError("embedding vector contains NaN or Infinity")


def validate_dimension(descriptor: EmbeddingModelDescriptor, vector: Sequence[float]) -> None:
    """Validate a produced vector's length against the declared dimension (FR-044)."""
    if len(vector) != descriptor.dimension:
        raise EmbeddingProviderError(
            f"vector has {len(vector)} dimensions, declared dimension is {descriptor.dimension}"
        )


def vector_hash(vector: Sequence[float]) -> str:
    """Canonical vector hash: SHA-256 of float32 little-endian bytes (FR-045).

    Always hashes the float32 representation regardless of the source
    dtype (a provider reporting a non-float32 `dtype`, for example
    `float16`, must still round-trip its vector through float32 before
    calling this -- its `EmbeddingRecord.dtype` field is what records
    the original dtype honestly, per FR-045's "input float16 ise
    original dtype metadata ve hash convention açık").
    """
    packed = struct.pack(f"<{len(vector)}f", *vector)
    return hash_raw_bytes(packed)


def _to_float32(value: float) -> float:
    """Round-trip a Python float through IEEE-754 binary32 precision."""
    unpacked: tuple[float] = struct.unpack("<f", struct.pack("<f", value))
    return unpacked[0]


def l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        # No real embedder should ever produce an all-zero vector; guard
        # against a division by zero rather than emit NaN.
        normalized = [0.0] * len(vector)
        if normalized:
            normalized[0] = 1.0
        return normalized
    return [value / norm for value in vector]


# --------------------------------------------------------------------------
# Deterministic-local reference embedder
# --------------------------------------------------------------------------

_LOCAL_PROVIDER = "ragledger-deterministic-local"
_LOCAL_MODEL = "hash-projection-reference"
_LOCAL_REVISION = "1"
_DEFAULT_DIMENSION = 32


def _hash_project(text: str, seed: int, dimension: int) -> list[float]:
    """Deterministically project `text` onto `dimension` pseudo-random values in [-1, 1).

    Expands a SHA-256 stream keyed by ``seed`` and ``text`` in 4-byte
    counter-mode blocks (a simple, dependency-free deterministic PRNG):
    the same `(seed, text, dimension)` always produces the same output,
    on any machine, forever -- this is a hash projection, not a learned
    embedding.
    """
    values: list[float] = []
    base = f"{seed}:{text}".encode()
    counter = 0
    while len(values) < dimension:
        digest = hashlib.sha256(base + counter.to_bytes(4, "big")).digest()
        for offset in range(0, len(digest) - 3, 4):
            if len(values) >= dimension:
                break
            raw = int.from_bytes(digest[offset : offset + 4], "big", signed=False)
            values.append((raw / 0xFFFFFFFF) * 2.0 - 1.0)
        counter += 1
    return values


class DeterministicLocalEmbeddingProvider:
    """Seeded hash-projection reference embedder -- not a semantic model.

    Produces unit-L2-normalized float32 vectors of a configurable
    dimension. Two provider instances with the same `seed`/`dimension`
    always produce identical vectors for identical text, and the
    descriptor's `name` encodes the seed so that changing the seed
    (which changes every produced vector) is visible as a model-identity
    change in the manifest, not a silent behavior change under a
    constant name.
    """

    def __init__(self, *, dimension: int = _DEFAULT_DIMENSION, seed: int = 0) -> None:
        if dimension < 1:
            raise ValueError("dimension must be >= 1")
        self._dimension = dimension
        self._seed = seed

    def descriptor(self) -> EmbeddingModelDescriptor:
        return EmbeddingModelDescriptor(
            provider=_LOCAL_PROVIDER,
            name=f"{_LOCAL_MODEL}-seed{self._seed}",
            revision=_LOCAL_REVISION,
            dimension=self._dimension,
            dtype="float32",
        )

    def tokenize(self, texts: Sequence[str]) -> list[int]:
        tokenizer = WhitespaceTokenizer()
        return [tokenizer.count(text) for text in texts]

    def embed(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        vectors: list[list[float]] = []
        for text in texts:
            raw = _hash_project(text, self._seed, self._dimension)
            normalized = l2_normalize(raw)
            vectors.append([_to_float32(value) for value in normalized])
        return EmbeddingBatchResult(vectors=vectors, usage={"batch_size": len(texts)})

    def healthcheck(self) -> bool:
        return True


# --------------------------------------------------------------------------
# External import provider (FR-047)
# --------------------------------------------------------------------------

_UNKNOWN = "unknown"


class ExternalImportEmbeddingProvider:
    """Wraps embeddings computed entirely outside ragledger (FR-047).

    Vectors are supplied already-computed by the caller through
    `import_vectors`; nothing here invents a model name/revision the
    caller did not provide. Any metadata field not explicitly supplied
    is the literal string ``"unknown"``, never guessed. `embed()` is not
    implemented (there is nothing to compute -- use `import_vectors`).
    """

    def __init__(
        self,
        *,
        dimension: int,
        dtype: str = "float32",
        provider: str | None = None,
        name: str | None = None,
        revision: str | None = None,
    ) -> None:
        if dimension < 1:
            raise ValueError("dimension must be >= 1")
        self._dimension = dimension
        self._dtype = dtype
        self._provider = provider or _UNKNOWN
        self._name = name or _UNKNOWN
        self._revision = revision or _UNKNOWN

    def descriptor(self) -> EmbeddingModelDescriptor:
        return EmbeddingModelDescriptor(
            provider=self._provider,
            name=self._name,
            revision=self._revision,
            dimension=self._dimension,
            dtype=self._dtype,
        )

    def tokenize(self, texts: Sequence[str]) -> list[int]:
        raise ProviderNotAvailableError(
            "ExternalImportEmbeddingProvider does not tokenize; token usage, if known, "
            "should be supplied by the caller alongside the imported vectors"
        )

    def embed(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        raise ProviderNotAvailableError(
            "ExternalImportEmbeddingProvider does not compute vectors; use import_vectors()"
        )

    def healthcheck(self) -> bool:
        return True

    def import_vectors(self, vectors: Sequence[Sequence[float]]) -> EmbeddingBatchResult:
        descriptor = self.descriptor()
        materialized: list[list[float]] = []
        for vector in vectors:
            values = [_to_float32(value) for value in vector]
            validate_finite(values)
            validate_dimension(descriptor, values)
            materialized.append(values)
        return EmbeddingBatchResult(vectors=materialized, usage={"source": "external_import"})


# --------------------------------------------------------------------------
# Real-provider interfaces: config plumbing only, no live calls (documented gap)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SentenceTransformersConfig:
    """Declared configuration for a local Sentence Transformers model (FR-041).

    `dimension` is the operator's own declared value for the specific
    model they intend to use (a real, known fact about that model, not
    a value ragledger invented) -- consistent with FR-044's "config
    identity provisional until validated": this descriptor is provisional
    until a real load-and-embed call actually confirms it, which this
    release does not perform.
    """

    model_name: str
    revision: str
    dimension: int
    device: str = "cpu"
    batch_size: int = 32
    normalize: bool = True


class SentenceTransformersEmbeddingProvider:
    """Config plumbing for a local Sentence Transformers adapter (FR-041).

    Not implemented in this release: `embed()`/`tokenize()` raise
    `ProviderNotAvailableError` rather than a fabricated response.
    Wiring this to the real `sentence-transformers` library (a large
    optional dependency with its own model download/cache semantics) is
    a documented gap; see `IMPLEMENTATION_STATUS.md`.
    """

    def __init__(self, config: SentenceTransformersConfig) -> None:
        self._config = config

    def descriptor(self) -> EmbeddingModelDescriptor:
        return EmbeddingModelDescriptor(
            provider="sentence_transformers",
            name=self._config.model_name,
            revision=self._config.revision,
            dimension=self._config.dimension,
            dtype="float32",
        )

    def tokenize(self, texts: Sequence[str]) -> list[int]:
        raise ProviderNotAvailableError(
            "SentenceTransformersEmbeddingProvider is not wired to a live model in this release"
        )

    def embed(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        raise ProviderNotAvailableError(
            "SentenceTransformersEmbeddingProvider is not wired to a live model in this release"
        )

    def healthcheck(self) -> bool:
        return False


@dataclass(frozen=True)
class OpenAiEmbeddingConfig:
    """Declared configuration for an OpenAI-compatible embedding API.

    `api_key_env` names an environment variable holding the API key;
    the key itself is never stored on this config object or logged.
    """

    model: str
    dimension: int
    api_key_env: str = "OPENAI_API_KEY"


class OpenAiEmbeddingProvider:
    """Config plumbing for a cloud OpenAI-compatible embedding API.

    Cloud embedding APIs are explicitly not mandatory for v1
    (PROJECT_SPEC.md section 5.1). `embed()`/`tokenize()` raise
    `ProviderNotAvailableError`: no network call is ever made by this
    class in this release.
    """

    def __init__(self, config: OpenAiEmbeddingConfig) -> None:
        self._config = config

    def descriptor(self) -> EmbeddingModelDescriptor:
        return EmbeddingModelDescriptor(
            provider="openai",
            name=self._config.model,
            revision=_UNKNOWN,
            dimension=self._config.dimension,
            dtype="float32",
        )

    def tokenize(self, texts: Sequence[str]) -> list[int]:
        raise ProviderNotAvailableError(
            "OpenAiEmbeddingProvider makes no live API calls in this release"
        )

    def embed(self, texts: Sequence[str]) -> EmbeddingBatchResult:
        raise ProviderNotAvailableError(
            "OpenAiEmbeddingProvider makes no live API calls in this release"
        )

    def healthcheck(self) -> bool:
        return False
