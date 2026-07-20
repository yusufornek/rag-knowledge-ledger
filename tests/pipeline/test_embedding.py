"""Tests for `ragledger.pipeline.embedding`."""

from __future__ import annotations

import math

import pytest

from ragledger.pipeline.embedding import (
    DeterministicLocalEmbeddingProvider,
    EmbeddingProviderError,
    ExternalImportEmbeddingProvider,
    OpenAiEmbeddingConfig,
    OpenAiEmbeddingProvider,
    ProviderNotAvailableError,
    SentenceTransformersConfig,
    SentenceTransformersEmbeddingProvider,
    l2_normalize,
    validate_dimension,
    validate_finite,
    vector_hash,
)


class TestDeterministicLocalProvider:
    def test_same_text_same_seed_same_vector(self) -> None:
        provider = DeterministicLocalEmbeddingProvider(dimension=16, seed=7)
        first = provider.embed(["hello world"]).vectors[0]
        second = provider.embed(["hello world"]).vectors[0]
        assert first == second

    def test_different_text_different_vector(self) -> None:
        provider = DeterministicLocalEmbeddingProvider(dimension=16, seed=7)
        a, b = provider.embed(["hello", "goodbye"]).vectors
        assert a != b

    def test_different_seed_different_vector(self) -> None:
        a = DeterministicLocalEmbeddingProvider(dimension=16, seed=1).embed(["x"]).vectors[0]
        b = DeterministicLocalEmbeddingProvider(dimension=16, seed=2).embed(["x"]).vectors[0]
        assert a != b

    def test_vectors_are_unit_l2_normalized(self) -> None:
        provider = DeterministicLocalEmbeddingProvider(dimension=32, seed=0)
        vector = provider.embed(["sample text"]).vectors[0]
        norm = math.sqrt(sum(v * v for v in vector))
        assert norm == pytest.approx(1.0, abs=1e-5)

    def test_configurable_dimension(self) -> None:
        provider = DeterministicLocalEmbeddingProvider(dimension=4, seed=0)
        vector = provider.embed(["x"]).vectors[0]
        assert len(vector) == 4
        assert provider.descriptor().dimension == 4

    def test_batch_order_preserved(self) -> None:
        provider = DeterministicLocalEmbeddingProvider(dimension=8, seed=0)
        texts = ["a", "b", "c"]
        vectors = provider.embed(texts).vectors
        for text, vector in zip(texts, vectors, strict=True):
            assert vector == provider.embed([text]).vectors[0]

    def test_descriptor_is_honest_about_being_a_reference_embedder(self) -> None:
        descriptor = DeterministicLocalEmbeddingProvider().descriptor()
        assert descriptor.provider == "ragledger-deterministic-local"
        assert descriptor.dtype == "float32"

    def test_healthcheck_always_true(self) -> None:
        assert DeterministicLocalEmbeddingProvider().healthcheck() is True

    def test_tokenize_returns_word_counts(self) -> None:
        provider = DeterministicLocalEmbeddingProvider()
        assert provider.tokenize(["one two three"]) == [3]

    def test_invalid_dimension_rejected(self) -> None:
        with pytest.raises(ValueError):
            DeterministicLocalEmbeddingProvider(dimension=0)


class TestValidation:
    def test_finite_accepts_normal_vector(self) -> None:
        validate_finite([0.1, -0.2, 0.3])

    def test_finite_rejects_nan(self) -> None:
        with pytest.raises(EmbeddingProviderError):
            validate_finite([0.1, float("nan")])

    def test_finite_rejects_infinity(self) -> None:
        with pytest.raises(EmbeddingProviderError):
            validate_finite([float("inf"), 0.0])

    def test_dimension_mismatch_rejected(self) -> None:
        provider = DeterministicLocalEmbeddingProvider(dimension=8)
        with pytest.raises(EmbeddingProviderError):
            validate_dimension(provider.descriptor(), [0.0, 0.0])


class TestL2Normalize:
    def test_normalizes_to_unit_length(self) -> None:
        result = l2_normalize([3.0, 4.0])
        assert result == pytest.approx([0.6, 0.8])

    def test_zero_vector_does_not_divide_by_zero(self) -> None:
        result = l2_normalize([0.0, 0.0, 0.0])
        assert not any(math.isnan(v) for v in result)


class TestVectorHash:
    def test_stable_for_same_vector(self) -> None:
        vector = [0.1, 0.2, 0.3]
        assert vector_hash(vector) == vector_hash(vector)

    def test_differs_for_different_vector(self) -> None:
        assert vector_hash([0.1, 0.2]) != vector_hash([0.1, 0.3])

    def test_is_a_sha256_hex_digest(self) -> None:
        digest = vector_hash([0.1, 0.2])
        assert len(digest) == 64
        int(digest, 16)  # does not raise


class TestExternalImportProvider:
    def test_unknown_metadata_defaults_are_literal_unknown(self) -> None:
        provider = ExternalImportEmbeddingProvider(dimension=4)
        descriptor = provider.descriptor()
        assert descriptor.provider == "unknown"
        assert descriptor.name == "unknown"
        assert descriptor.revision == "unknown"

    def test_explicit_metadata_preserved(self) -> None:
        provider = ExternalImportEmbeddingProvider(
            dimension=4, provider="acme", name="acme-embed", revision="v3"
        )
        descriptor = provider.descriptor()
        assert (descriptor.provider, descriptor.name, descriptor.revision) == (
            "acme",
            "acme-embed",
            "v3",
        )

    def test_import_vectors_validates_dimension_and_finiteness(self) -> None:
        provider = ExternalImportEmbeddingProvider(dimension=3)
        result = provider.import_vectors([[0.1, 0.2, 0.3]])
        assert len(result.vectors[0]) == 3

        with pytest.raises(EmbeddingProviderError):
            provider.import_vectors([[0.1, 0.2]])  # wrong dimension

        with pytest.raises(EmbeddingProviderError):
            provider.import_vectors([[float("nan"), 0.0, 0.0]])

    def test_embed_and_tokenize_are_not_implemented(self) -> None:
        provider = ExternalImportEmbeddingProvider(dimension=3)
        with pytest.raises(ProviderNotAvailableError):
            provider.embed(["text"])
        with pytest.raises(ProviderNotAvailableError):
            provider.tokenize(["text"])


class TestUnwiredRealProviders:
    """`SentenceTransformersEmbeddingProvider`/`OpenAiEmbeddingProvider` are
    config plumbing only in this release; they must never fabricate a
    response (PROJECT_SPEC.md section 0's "no fake connector" rule).
    """

    def test_sentence_transformers_provider_never_makes_a_live_call(self) -> None:
        provider = SentenceTransformersEmbeddingProvider(
            SentenceTransformersConfig(
                model_name="all-MiniLM-L6-v2", revision="abc123", dimension=384
            )
        )
        assert provider.descriptor().name == "all-MiniLM-L6-v2"
        assert provider.healthcheck() is False
        with pytest.raises(ProviderNotAvailableError):
            provider.embed(["text"])
        with pytest.raises(ProviderNotAvailableError):
            provider.tokenize(["text"])

    def test_openai_provider_never_makes_a_live_call(self) -> None:
        provider = OpenAiEmbeddingProvider(
            OpenAiEmbeddingConfig(model="text-embedding-3-small", dimension=1536)
        )
        assert provider.descriptor().provider == "openai"
        assert provider.healthcheck() is False
        with pytest.raises(ProviderNotAvailableError):
            provider.embed(["text"])
