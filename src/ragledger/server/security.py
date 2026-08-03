"""API tokens, credential encryption, and workspace scoping, per FR-002/003/004 and section 19.

Three independent primitives live here:

- `issue_api_token`/`verify_api_token` (FR-002, section 19): a token is
  ``<prefix>_<selector>.<secret>``. `selector` is a public, indexed
  lookup value a caller uses to find the candidate `ApiToken` row in
  one query; the bearable secret itself is never stored -- only a
  per-token-salted SHA-256 hash of it
  (`hashlib.sha256(salt + secret_bytes)`), checked with
  `hmac.compare_digest` so a timing attack cannot narrow down the
  correct hash byte by byte.
- `encrypt_credential`/`decrypt_credential` (FR-003, section 19.2: "AES-GCM,
  write-only, master key secret, rotation"): AES-256-GCM under whichever
  `APP_ENCRYPTION_KEY_V<n>` `ragledger.server.settings.Settings` currently
  considers "current" (the highest-numbered configured key). The
  returned ciphertext blob is self-describing --
  ``MAGIC | key_id_len | key_id | nonce | ciphertext+tag`` -- so
  `decrypt_credential` can always tell which key a given stored
  credential needs, even after `APP_ENCRYPTION_KEY_V1` is rotated out
  in favor of `_V2`: old rows keep decrypting under the old key without
  any migration step, which is what "rotation friendly" means here.
- `require_workspace_scope` (section 15.3: "Cross-workspace repository
  methods mandatory; negative tests"): the one check every repository
  method wraps a fetched row in before handing it back to a caller.

None of these three ever writes a secret value into a log line or
exception message aimed at a human; error messages name key ids,
selectors, and workspace ids, never token secrets, key bytes, or
credential plaintext.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import uuid
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ragledger.server.settings import Settings

__all__ = [
    "CredentialDecryptionError",
    "IssuedApiToken",
    "WorkspaceScopeViolationError",
    "decrypt_credential",
    "encrypt_credential",
    "issue_api_token",
    "require_workspace_scope",
    "verify_api_token",
]

_SELECTOR_BYTES = 9
_SECRET_BYTES = 32
_SALT_BYTES = 16
_NONCE_BYTES = 12
_CREDENTIAL_MAGIC = b"RLC1"


# --------------------------------------------------------------------------
# API tokens
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class IssuedApiToken:
    """The result of `issue_api_token`. ``token`` is shown to the caller exactly once."""

    token: str
    prefix: str
    selector: str
    salt: bytes
    token_hash: bytes


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def issue_api_token(*, prefix: str = "rlk") -> IssuedApiToken:
    """Generate a new random API token.

    Persist `prefix`/`selector`/`salt`/`token_hash` (for example onto a
    `ragledger.server.db.models.ApiToken` row); return `token` to the
    caller once. There is no way to recover `token` from what is
    persisted -- that is the point of storing only a salted hash.
    """
    selector = _b64url_encode(secrets.token_bytes(_SELECTOR_BYTES))
    secret = secrets.token_bytes(_SECRET_BYTES)
    salt = secrets.token_bytes(_SALT_BYTES)
    token_hash = hashlib.sha256(salt + secret).digest()
    token = f"{prefix}_{selector}.{_b64url_encode(secret)}"
    return IssuedApiToken(
        token=token, prefix=prefix, selector=selector, salt=salt, token_hash=token_hash
    )


def parse_api_token(token: str) -> tuple[str, str, bytes] | None:
    """Split a presented token into ``(prefix, selector, secret_bytes)``, or `None` if malformed."""
    if "." not in token or "_" not in token:
        return None
    head, _, secret_part = token.rpartition(".")
    prefix, _, selector = head.partition("_")
    if not prefix or not selector or not secret_part:
        return None
    try:
        secret = _b64url_decode(secret_part)
    except (binascii.Error, ValueError):
        return None
    return prefix, selector, secret


def token_selector(token: str) -> str | None:
    """Return just the selector component of ``token``, for an `ApiToken` lookup query."""
    parsed = parse_api_token(token)
    return None if parsed is None else parsed[1]


def verify_api_token(token: str, *, salt: bytes, expected_hash: bytes) -> bool:
    """Verify a presented ``token`` against a stored ``salt``/``expected_hash``.

    Uses `hmac.compare_digest` for the actual hash comparison so
    verification takes the same time regardless of where the first
    mismatching byte falls. Returns `False` (never raises) for a
    structurally malformed token, exactly as it would for a
    well-formed one that simply hashes to the wrong value -- both mean
    "reject this token."
    """
    parsed = parse_api_token(token)
    if parsed is None:
        return False
    _prefix, _selector, secret = parsed
    candidate_hash = hashlib.sha256(salt + secret).digest()
    return hmac.compare_digest(candidate_hash, expected_hash)


# --------------------------------------------------------------------------
# Credential encryption (FR-003, section 19.2)
# --------------------------------------------------------------------------


class CredentialDecryptionError(Exception):
    """Raised when a stored credential ciphertext cannot be decrypted."""


def _decode_key_material(raw: str) -> bytes:
    """Decode an `APP_ENCRYPTION_KEY_V*` value into exactly 32 raw AES-256 key bytes.

    Accepts standard or URL-safe base64, padded or not. Rejects
    anything that does not decode to exactly 32 bytes -- a
    misconfigured key must fail loudly at first use, not silently
    truncate or pad into a weaker key.
    """
    candidate = raw.strip()
    padded = candidate + "=" * (-len(candidate) % 4)
    # Normalize URL-safe alphabet characters to the standard alphabet so a
    # single `validate=True` decode accepts either encoding.
    normalized = padded.translate(str.maketrans("-_", "+/"))
    try:
        decoded = base64.b64decode(normalized, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("encryption key must be base64-encoded (standard or URL-safe)") from exc
    if len(decoded) != 32:
        raise ValueError(f"encryption key must decode to exactly 32 bytes, got {len(decoded)}")
    return decoded


def encrypt_credential(plaintext: bytes, *, settings: Settings) -> tuple[bytes, str]:
    """Encrypt ``plaintext`` with the current `APP_ENCRYPTION_KEY_V<n>`.

    Returns ``(ciphertext_blob, key_id)``. Store `ciphertext_blob`
    as-is (for example as `VectorTarget.credential_ciphertext`); store
    `key_id` alongside it only for display/audit purposes (for example
    `VectorTarget.credential_key_id`) -- it is also embedded inside the
    blob itself, which is what `decrypt_credential` actually reads.
    """
    key_id, key = settings.current_encryption_key()
    key_bytes = _decode_key_material(key.get_secret_value())
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key_bytes).encrypt(nonce, plaintext, None)
    key_id_bytes = key_id.encode("ascii")
    blob = _CREDENTIAL_MAGIC + bytes([len(key_id_bytes)]) + key_id_bytes + nonce + ciphertext
    return blob, key_id


def decrypt_credential(blob: bytes, *, settings: Settings) -> bytes:
    """Decrypt a ``blob`` produced by `encrypt_credential`.

    Raises `CredentialDecryptionError` if the blob is malformed, names
    a key id this process has no `APP_ENCRYPTION_KEY_V*` for, or fails
    AES-GCM authentication (tampered ciphertext, or the wrong key).
    """
    try:
        if blob[:4] != _CREDENTIAL_MAGIC:
            raise CredentialDecryptionError("unrecognized credential ciphertext format")
        key_id_len = blob[4]
        offset = 5
        key_id = blob[offset : offset + key_id_len].decode("ascii")
        offset += key_id_len
        nonce = blob[offset : offset + _NONCE_BYTES]
        offset += _NONCE_BYTES
        ciphertext = blob[offset:]
        if len(nonce) != _NONCE_BYTES or not ciphertext:
            raise CredentialDecryptionError("malformed credential ciphertext")
    except IndexError as exc:
        raise CredentialDecryptionError("malformed credential ciphertext") from exc
    except UnicodeDecodeError as exc:
        raise CredentialDecryptionError("malformed credential ciphertext") from exc

    try:
        key = settings.require_encryption_key(key_id)
    except KeyError as exc:
        raise CredentialDecryptionError(str(exc)) from exc

    key_bytes = _decode_key_material(key.get_secret_value())
    try:
        return AESGCM(key_bytes).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise CredentialDecryptionError(
            "credential ciphertext failed authentication (tampered, or wrong key)"
        ) from exc


# --------------------------------------------------------------------------
# Workspace scoping (section 15.3)
# --------------------------------------------------------------------------


class WorkspaceScopeViolationError(Exception):
    """Raised when a resource's `workspace_id` does not match the caller's authorized workspace."""


def require_workspace_scope(
    resource_workspace_id: uuid.UUID, caller_workspace_id: uuid.UUID
) -> None:
    """Raise `WorkspaceScopeViolationError` unless ``resource_workspace_id == caller_workspace_id``.

    Every repository method that fetches a row by its own primary key
    (not already filtered by workspace in the query itself) must call
    this before returning the row to a caller, per the design specification
    section 15.3: "Cross-workspace repository methods mandatory;
    negative tests." This turns a stale or forged id from another
    workspace into an explicit, typed error instead of a silent
    cross-tenant data leak.
    """
    if resource_workspace_id != caller_workspace_id:
        raise WorkspaceScopeViolationError(
            f"resource belongs to workspace {resource_workspace_id}, "
            f"not caller's workspace {caller_workspace_id}"
        )
