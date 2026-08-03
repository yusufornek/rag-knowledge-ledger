"""Golden manifest corpus tests (the design specification section 42.1).

Two independent things are checked for each fixture in
`tests/fixtures/golden/`:

1. In-process determinism: calling the same builder twice in the same
   test run produces byte-identical canonical manifest bytes.
2. Golden byte-identity: the builder's current output still matches the
   exact bytes committed to `tests/fixtures/golden/`. A failure here
   means the canonicalization, hashing, ID derivation, or model shape
   changed in a way that is not backward compatible -- which may be an
   intentional, reviewed change (see `scripts/regenerate_golden_manifests.py`
   and the design specification section 42.1's "Golden update otomatik CI'da
   yapılmaz"), but must never happen silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ragledger.core.manifest import canonical_manifest_bytes
from ragledger.core.models import ManifestEnvelope
from tests.core import golden_fixtures as fixtures

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "golden"

_CASES = [
    ("minimal.json", fixtures.build_minimal_manifest),
    ("full_pipeline.json", fixtures.build_full_pipeline_manifest),
    ("signed.json", fixtures.build_signed_manifest),
]


@pytest.mark.parametrize(("filename", "builder"), _CASES, ids=[case[0] for case in _CASES])
class TestGoldenManifests:
    def test_builder_is_deterministic_in_process(self, filename: str, builder) -> None:
        first = canonical_manifest_bytes(builder())
        second = canonical_manifest_bytes(builder())
        assert first == second

    def test_builder_output_matches_committed_golden_bytes(self, filename: str, builder) -> None:
        golden_path = GOLDEN_DIR / filename
        expected = golden_path.read_bytes()
        actual = canonical_manifest_bytes(builder())
        assert actual == expected, (
            f"{filename} no longer matches the committed golden bytes. "
            "If this change is intentional, regenerate the fixture and note "
            "it in the changelog per the design specification section 42.1."
        )

    def test_golden_file_has_no_trailing_newline(self, filename: str, builder) -> None:
        golden_path = GOLDEN_DIR / filename
        assert not golden_path.read_bytes().endswith(b"\n")

    def test_golden_file_roundtrips_through_load_manifest(self, filename: str, builder) -> None:
        from ragledger.core.manifest import load_manifest

        golden_path = GOLDEN_DIR / filename
        loaded = load_manifest(golden_path)
        assert isinstance(loaded, ManifestEnvelope)
        assert canonical_manifest_bytes(loaded) == golden_path.read_bytes()


def test_golden_corpus_has_at_least_three_fixtures() -> None:
    assert len(list(GOLDEN_DIR.glob("*.json"))) >= 3


def test_full_pipeline_fixture_exercises_every_record_type() -> None:
    manifest = fixtures.build_full_pipeline_manifest()
    assert manifest.sources
    assert manifest.parse_runs
    assert len(manifest.chunks) >= 2
    assert manifest.embeddings
    assert manifest.index_bindings
    assert manifest.artifacts
    assertion_types = {assertion.type for assertion in manifest.assertions}
    assert assertion_types == {"PII_SCAN", "LICENSE", "ACL", "TENANT", "QUALITY"}


def test_signed_fixture_has_a_valid_signature() -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from ragledger.core.signing import (
        VerificationOverall,
        fingerprint,
        verify_manifest,
    )

    manifest = fixtures.build_signed_manifest()
    assert len(manifest.signatures) == 1

    private_key = Ed25519PrivateKey.from_private_bytes(fixtures.FIXED_SIGNING_SEED)
    public_key = private_key.public_key()
    result = verify_manifest(manifest, {fingerprint(public_key): public_key})
    assert result.overall is VerificationOverall.VALID_TRUSTED
