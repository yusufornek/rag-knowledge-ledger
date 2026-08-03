"""Ed25519 manifest signing and verification.

Implements the design specification section 11.1 and
`docs/architecture/adr/0002-signing-algorithm.md`:

1. Compute the manifest's signing view hash (``ragledger.core.manifest.compute_manifest_hash``).
2. Sign ``DOMAIN_SEPARATOR + digest`` with Ed25519 (`cryptography`'s
   `Ed25519PrivateKey`, which does not take or need a random nonce --
   signing the same message with the same key is deterministic).
3. Attach a `SignatureRecord` (algorithm, key id, base64url signature,
   an explicit ``signed_at``, optional issuer) to the manifest.

Verification (`verify_manifest`) recomputes the same signing view hash
from the manifest under test and checks it against the stored
`integrity.manifest_hash` (``hash_valid``), then, for each attached
signature, either finds the signer's public key in the caller-supplied
trust store and cryptographically verifies it, or reports the key as
unrecognized. This distinguishes three outcomes a caller can act on
differently:

- content or signature bytes were tampered with (`SignatureStatus.INVALID`,
  overall `VerificationOverall.INVALID`);
- the signature is cryptographically valid but made by a key this
  caller does not recognize/trust (`SignatureStatus.UNKNOWN_KEY`,
  overall `VerificationOverall.VALID_UNTRUSTED`);
- the signature is cryptographically valid and made by a trusted key
  (`SignatureStatus.VALID`, overall `VerificationOverall.VALID_TRUSTED`).

Per the design specification section 33.6, "v1 CLI supports any trusted": if
several signatures are attached, the manifest is `VALID_TRUSTED` as
soon as any one of them is valid and trusted.

Key management here covers the two v1 options the design specification section
11.2 names for local/CI use: a raw, unencrypted private key file with
file mode ``0600`` (`write_private_key`/`read_private_key`), and a
public key file used purely for verification
(`write_public_key`/`read_public_key`). There is no KMS integration and
no password-encrypted key file in this module; both are out of scope
for v0.1.0.
"""

from __future__ import annotations

import base64
import os
import stat
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from ragledger.core.hashing import sha256_hex
from ragledger.core.manifest import compute_manifest_hash
from ragledger.core.models import ManifestEnvelope, SignatureRecord

DOMAIN_SEPARATOR = b"RAGLEDGER-MANIFEST-V1\x00"
"""The fixed domain separator the design specification section 11.1 requires be
signed together with the manifest hash digest, so an Ed25519 signature
produced for this purpose can never be replayed as a signature over
some unrelated ``RAGLEDGER-MANIFEST-V1``-shaped message from another
context."""


# --------------------------------------------------------------------------
# Key management
# --------------------------------------------------------------------------


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Generate a new random Ed25519 keypair."""
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


def write_private_key(private_key: Ed25519PrivateKey, path: Path) -> None:
    """Write a raw 32-byte Ed25519 private key to ``path`` with file mode 0600.

    The key is written unencrypted; per the design specification section 11.2,
    password-based encryption of this file is a separate, optional v1
    CLI concern layered on top of this primitive, not implemented here.
    """
    raw = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    path.write_bytes(raw)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def read_private_key(path: Path) -> Ed25519PrivateKey:
    """Read a raw 32-byte Ed25519 private key written by `write_private_key`."""
    return Ed25519PrivateKey.from_private_bytes(Path(path).read_bytes())


def write_public_key(public_key: Ed25519PublicKey, path: Path) -> None:
    """Write a raw 32-byte Ed25519 public key to ``path``."""
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    path.write_bytes(raw)


def read_public_key(path: Path) -> Ed25519PublicKey:
    """Read a raw 32-byte Ed25519 public key written by `write_public_key`."""
    return Ed25519PublicKey.from_public_bytes(Path(path).read_bytes())


def fingerprint(public_key: Ed25519PublicKey) -> str:
    """Return the SHA-256 hex fingerprint of a raw Ed25519 public key.

    This is the value stored as a signature record's `key_id`, and the
    key a trust store passed to `verify_manifest` is keyed by.
    """
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return sha256_hex(raw)


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------


def _signing_message(manifest: ManifestEnvelope) -> tuple[str, bytes]:
    digest_hex = compute_manifest_hash(manifest)
    return digest_hex, DOMAIN_SEPARATOR + bytes.fromhex(digest_hex)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_manifest(
    manifest: ManifestEnvelope,
    private_key: Ed25519PrivateKey,
    *,
    signed_at: datetime,
    issuer: str | None = None,
) -> ManifestEnvelope:
    """Return a copy of ``manifest`` with one more Ed25519 signature attached.

    The manifest's signing view hash is (re)computed here rather than
    trusted from `manifest.integrity.manifest_hash`, so this function is
    correct even if it is handed a manifest object that was hand-edited
    after `ragledger.core.manifest.build_manifest` produced it;
    `integrity.manifest_hash` in the returned manifest always matches
    what was actually signed. ``signed_at`` must be supplied explicitly
    -- this function never reads the wall clock.
    """
    digest_hex, message = _signing_message(manifest)
    signature_bytes = private_key.sign(message)
    record = SignatureRecord(
        key_id=fingerprint(private_key.public_key()),
        signature=_b64url_encode(signature_bytes),
        signed_at=signed_at,
        issuer=issuer,
    )
    updated_integrity = manifest.integrity.model_copy(update={"manifest_hash": digest_hex})
    return manifest.model_copy(
        update={
            "integrity": updated_integrity,
            "signatures": [*manifest.signatures, record],
        }
    )


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


class SignatureStatus(StrEnum):
    """The per-signature outcome of `verify_manifest`."""

    VALID = "valid"
    """Cryptographically valid, made by a key present in the trust store."""

    INVALID = "invalid"
    """Signature bytes do not verify against the manifest hash and the
    claimed key -- content or signature tampering."""

    UNKNOWN_KEY = "unknown_key"
    """``key_id`` is not present in the trust store passed to
    `verify_manifest`; the signature was not cryptographically checked
    because there is no public key to check it against."""


class VerificationOverall(StrEnum):
    """The manifest-level outcome of `verify_manifest` (design specification 33.6)."""

    VALID_TRUSTED = "VALID_TRUSTED"
    """Content hash intact, and at least one attached signature is valid
    and made by a trusted key."""

    VALID_UNTRUSTED = "VALID_UNTRUSTED"
    """Content hash intact, and at least one attached signature is
    cryptographically valid, but no valid signature is from a trusted
    key (unknown/untrusted signer)."""

    INVALID = "INVALID"
    """Content hash does not match `integrity.manifest_hash`, or every
    attached signature failed cryptographic verification."""

    INCOMPLETE = "INCOMPLETE"
    """Content hash intact, but the manifest carries no signatures at
    all, so authenticity cannot be assessed."""


@dataclass(frozen=True)
class SignatureVerification:
    key_id: str
    status: SignatureStatus


@dataclass(frozen=True)
class VerificationResult:
    hash_valid: bool
    signatures: tuple[SignatureVerification, ...]
    overall: VerificationOverall


def verify_manifest(
    manifest: ManifestEnvelope,
    trusted_keys: dict[str, Ed25519PublicKey],
) -> VerificationResult:
    """Verify ``manifest``'s content hash and every attached signature.

    ``trusted_keys`` maps a signature's `key_id` (the signer's public
    key fingerprint) to the corresponding `Ed25519PublicKey`. This
    function never mutates ``manifest`` (the design specification section 33.6:
    "Verify sonucu manifesti mutate etmez").
    """
    digest_hex, message = _signing_message(manifest)
    hash_valid = digest_hex == manifest.integrity.manifest_hash

    results = []
    for record in manifest.signatures:
        public_key = trusted_keys.get(record.key_id)
        if public_key is None:
            results.append(SignatureVerification(record.key_id, SignatureStatus.UNKNOWN_KEY))
            continue
        try:
            public_key.verify(_b64url_decode(record.signature), message)
        except InvalidSignature:
            results.append(SignatureVerification(record.key_id, SignatureStatus.INVALID))
        else:
            results.append(SignatureVerification(record.key_id, SignatureStatus.VALID))

    overall = _overall_status(hash_valid, results)
    return VerificationResult(hash_valid=hash_valid, signatures=tuple(results), overall=overall)


def _overall_status(hash_valid: bool, results: list[SignatureVerification]) -> VerificationOverall:
    if not hash_valid:
        return VerificationOverall.INVALID
    if not results:
        return VerificationOverall.INCOMPLETE
    if any(result.status is SignatureStatus.VALID for result in results):
        return VerificationOverall.VALID_TRUSTED
    if any(result.status is SignatureStatus.UNKNOWN_KEY for result in results):
        return VerificationOverall.VALID_UNTRUSTED
    return VerificationOverall.INVALID
