"""Tests for `ragledger.core.signing`.

Covers the RFC 8032 official Ed25519 test vector (TEST 1, section 7.1),
the sign/verify roundtrip through `ragledger.core.manifest`, and every
tamper scenario `verify_manifest` is meant to distinguish: content
tampering, signature-byte tampering, an unrecognized/untrusted signer
key, and a trust store entry whose public key does not actually match
its claimed fingerprint.
"""

from __future__ import annotations

from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ragledger.core.hashing import sha256_hex
from ragledger.core.manifest import build_manifest
from ragledger.core.models import BuildEnvironment, BuildRecord, ManifestEnvelope
from ragledger.core.signing import (
    SignatureStatus,
    VerificationOverall,
    fingerprint,
    generate_keypair,
    sign_manifest,
    verify_manifest,
)

CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)

# RFC 8032 section 7.1, TEST 1: secret key, public key, an empty
# message, and the resulting signature. Fetched from
# https://www.rfc-editor.org/rfc/rfc8032.txt and cross-checked (public
# key re-derived from the secret key, and the signature independently
# re-verified and re-produced) before being pinned here.
RFC8032_TEST1_SECRET_KEY_HEX = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
RFC8032_TEST1_PUBLIC_KEY_HEX = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
RFC8032_TEST1_SIGNATURE_HEX = (
    "e5564300c360ac729086e2cc806e828a"
    "84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46b"
    "d25bf5f0595bbe24655141438e7a100b"
)
RFC8032_TEST1_MESSAGE = b""


def _build(namespace: str = "signing-tests") -> ManifestEnvelope:
    build = BuildRecord(
        build_id="bld_signing_test",
        status="complete",
        source_snapshot_hash=sha256_hex(b"snapshot"),
        pipeline_config_hash=sha256_hex(b"pipeline"),
        started_at=CREATED_AT,
        completed_at=CREATED_AT,
        environment=BuildEnvironment(python_version="3.13.0"),
    )
    return build_manifest(
        namespace=namespace,
        created_at=CREATED_AT,
        build=build,
        ledger_version="0.1.0",
    )


class TestRfc8032Vector:
    """Confirms this codebase's Ed25519 usage agrees with the official
    RFC 8032 TEST 1 vector, independent of anything ragledger-specific.
    """

    def test_public_key_is_derived_correctly_from_secret_key(self) -> None:
        private_key = Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(RFC8032_TEST1_SECRET_KEY_HEX)
        )
        derived_public = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        assert derived_public.hex() == RFC8032_TEST1_PUBLIC_KEY_HEX

    def test_known_signature_verifies_against_known_public_key(self) -> None:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(RFC8032_TEST1_PUBLIC_KEY_HEX))
        public_key.verify(bytes.fromhex(RFC8032_TEST1_SIGNATURE_HEX), RFC8032_TEST1_MESSAGE)

    def test_signing_the_known_message_reproduces_the_known_signature(self) -> None:
        private_key = Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(RFC8032_TEST1_SECRET_KEY_HEX)
        )
        signature = private_key.sign(RFC8032_TEST1_MESSAGE)
        assert signature.hex() == RFC8032_TEST1_SIGNATURE_HEX

    def test_fingerprint_of_known_public_key_is_stable(self) -> None:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(RFC8032_TEST1_PUBLIC_KEY_HEX))
        value = fingerprint(public_key)
        assert value == sha256_hex(bytes.fromhex(RFC8032_TEST1_PUBLIC_KEY_HEX))
        assert len(value) == 64


class TestSignAndVerifyRoundtrip:
    def test_valid_signature_from_a_trusted_key_is_valid_trusted(self) -> None:
        manifest = _build()
        private_key, public_key = generate_keypair()
        signed = sign_manifest(manifest, private_key, signed_at=CREATED_AT)
        assert len(signed.signatures) == 1

        result = verify_manifest(signed, {fingerprint(public_key): public_key})
        assert result.hash_valid is True
        assert result.overall is VerificationOverall.VALID_TRUSTED
        assert result.signatures[0].status is SignatureStatus.VALID

    def test_signature_record_fields(self) -> None:
        manifest = _build()
        private_key, public_key = generate_keypair()
        signed = sign_manifest(manifest, private_key, signed_at=CREATED_AT, issuer="ci")
        record = signed.signatures[0]
        assert record.algorithm == "Ed25519"
        assert record.key_id == fingerprint(public_key)
        assert record.issuer == "ci"

    def test_signing_is_deterministic(self) -> None:
        manifest = _build()
        private_key, _ = generate_keypair()
        first = sign_manifest(manifest, private_key, signed_at=CREATED_AT)
        second = sign_manifest(manifest, private_key, signed_at=CREATED_AT)
        assert first.signatures[0].signature == second.signatures[0].signature

    def test_unsigned_manifest_is_incomplete(self) -> None:
        manifest = _build()
        result = verify_manifest(manifest, {})
        assert result.hash_valid is True
        assert result.overall is VerificationOverall.INCOMPLETE
        assert result.signatures == ()


class TestUntrustedKey:
    def test_valid_signature_from_unrecognized_key_is_valid_untrusted(self) -> None:
        manifest = _build()
        private_key, _ = generate_keypair()
        signed = sign_manifest(manifest, private_key, signed_at=CREATED_AT)

        result = verify_manifest(signed, {})  # empty trust store: signer unrecognized
        assert result.hash_valid is True
        assert result.overall is VerificationOverall.VALID_UNTRUSTED
        assert result.signatures[0].status is SignatureStatus.UNKNOWN_KEY


class TestTamperDetection:
    def test_content_tampered_after_signing_is_invalid(self) -> None:
        manifest = _build()
        private_key, public_key = generate_keypair()
        signed = sign_manifest(manifest, private_key, signed_at=CREATED_AT)

        tampered = signed.model_copy(update={"namespace": "tampered-namespace"})
        result = verify_manifest(tampered, {fingerprint(public_key): public_key})
        assert result.hash_valid is False
        assert result.overall is VerificationOverall.INVALID

    def test_flipped_signature_byte_is_invalid(self) -> None:
        manifest = _build()
        private_key, public_key = generate_keypair()
        signed = sign_manifest(manifest, private_key, signed_at=CREATED_AT)

        original_record = signed.signatures[0]
        tampered_signature_bytes = bytearray(original_record.signature.encode("ascii"))
        # Flip one base64url character to a different valid base64url
        # character, which corrupts the underlying signature bytes.
        flip_index = 0
        original_char = chr(tampered_signature_bytes[flip_index])
        replacement = "A" if original_char != "A" else "B"
        tampered_signature_bytes[flip_index] = ord(replacement)
        tampered_record = original_record.model_copy(
            update={"signature": tampered_signature_bytes.decode("ascii")}
        )
        tampered = signed.model_copy(update={"signatures": [tampered_record]})

        result = verify_manifest(tampered, {fingerprint(public_key): public_key})
        assert result.hash_valid is True  # content itself was not touched
        assert result.overall is VerificationOverall.INVALID
        assert result.signatures[0].status is SignatureStatus.INVALID

    def test_trust_store_with_wrong_public_key_under_the_right_key_id_is_invalid(self) -> None:
        """Defends against a corrupted/malicious trust store: an entry
        keyed by the real signer's fingerprint but mapped to a
        different public key must not verify.
        """
        manifest = _build()
        private_key, public_key = generate_keypair()
        signed = sign_manifest(manifest, private_key, signed_at=CREATED_AT)

        _, unrelated_public_key = generate_keypair()
        corrupted_trust_store = {fingerprint(public_key): unrelated_public_key}

        result = verify_manifest(signed, corrupted_trust_store)
        assert result.hash_valid is True
        assert result.overall is VerificationOverall.INVALID
        assert result.signatures[0].status is SignatureStatus.INVALID

    def test_signature_over_a_different_manifest_does_not_verify(self) -> None:
        manifest_a = _build(namespace="signing-tests-a")
        manifest_b = _build(namespace="signing-tests-b")
        private_key, public_key = generate_keypair()
        signed_a = sign_manifest(manifest_a, private_key, signed_at=CREATED_AT)

        # Graft manifest_a's signature onto manifest_b's content.
        grafted = manifest_b.model_copy(
            update={"integrity": signed_a.integrity, "signatures": signed_a.signatures}
        )
        result = verify_manifest(grafted, {fingerprint(public_key): public_key})
        assert result.overall is VerificationOverall.INVALID


class TestKeyManagement:
    def test_write_and_read_private_key_roundtrip(self, tmp_path) -> None:
        from ragledger.core.signing import read_private_key, write_private_key

        private_key, public_key = generate_keypair()
        path = tmp_path / "private.key"
        write_private_key(private_key, path)

        mode = path.stat().st_mode & 0o777
        assert mode == 0o600

        loaded = read_private_key(path)
        loaded_public = loaded.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        assert loaded_public == public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)

    def test_write_and_read_public_key_roundtrip(self, tmp_path) -> None:
        from ragledger.core.signing import read_public_key, write_public_key

        _, public_key = generate_keypair()
        path = tmp_path / "public.key"
        write_public_key(public_key, path)
        loaded = read_public_key(path)
        assert loaded.public_bytes(Encoding.Raw, PublicFormat.Raw) == public_key.public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )

    def test_fingerprint_differs_for_different_keys(self) -> None:
        _, public_key_a = generate_keypair()
        _, public_key_b = generate_keypair()
        assert fingerprint(public_key_a) != fingerprint(public_key_b)
