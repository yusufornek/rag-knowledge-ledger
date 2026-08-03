"""Tests for `ragledger.server.security`: API tokens, credential encryption, workspace scoping."""

from __future__ import annotations

import base64
import uuid

import pytest

from ragledger.server.security import (
    CredentialDecryptionError,
    WorkspaceScopeViolationError,
    decrypt_credential,
    encrypt_credential,
    issue_api_token,
    parse_api_token,
    require_workspace_scope,
    token_selector,
    verify_api_token,
)
from ragledger.server.settings import Settings


class TestApiTokens:
    def test_issued_token_has_expected_prefix_and_shape(self) -> None:
        issued = issue_api_token(prefix="rlk")
        assert issued.token.startswith("rlk_")
        assert "." in issued.token
        parsed = parse_api_token(issued.token)
        assert parsed is not None
        prefix, selector, _secret = parsed
        assert prefix == "rlk"
        assert selector == issued.selector

    def test_correct_token_verifies(self) -> None:
        issued = issue_api_token()
        assert verify_api_token(issued.token, salt=issued.salt, expected_hash=issued.token_hash)

    def test_wrong_secret_does_not_verify(self) -> None:
        issued = issue_api_token()
        parsed = parse_api_token(issued.token)
        assert parsed is not None
        prefix, selector, secret = parsed
        tampered_secret = bytes([secret[0] ^ 0xFF]) + secret[1:]
        tampered_secret_b64 = base64.urlsafe_b64encode(tampered_secret).decode("ascii").rstrip("=")
        tampered_token = f"{prefix}_{selector}.{tampered_secret_b64}"
        assert not verify_api_token(
            tampered_token, salt=issued.salt, expected_hash=issued.token_hash
        )

    def test_wrong_salt_does_not_verify(self) -> None:
        issued = issue_api_token()
        wrong_salt = bytes((b + 1) % 256 for b in issued.salt)
        assert not verify_api_token(issued.token, salt=wrong_salt, expected_hash=issued.token_hash)

    def test_malformed_token_does_not_verify_and_does_not_raise(self) -> None:
        issued = issue_api_token()
        assert not verify_api_token(
            "not-a-well-formed-token", salt=issued.salt, expected_hash=issued.token_hash
        )
        assert not verify_api_token("", salt=issued.salt, expected_hash=issued.token_hash)

    def test_secret_is_never_recoverable_from_stored_fields(self) -> None:
        issued = issue_api_token()
        assert issued.token_hash != issued.salt
        # The persisted fields alone (salt, hash) never encode the secret in
        # a way that lets a reader reconstruct the original token string.
        assert issued.token.encode() not in issued.salt
        assert issued.token.encode() not in issued.token_hash

    def test_two_tokens_have_distinct_selectors_and_secrets(self) -> None:
        first = issue_api_token()
        second = issue_api_token()
        assert first.selector != second.selector
        assert first.token != second.token

    def test_token_selector_helper_matches_issued_selector(self) -> None:
        issued = issue_api_token()
        assert token_selector(issued.token) == issued.selector

    def test_token_selector_helper_returns_none_for_malformed_token(self) -> None:
        assert token_selector("garbage") is None

    def test_verification_uses_constant_time_compare(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`verify_api_token` must use `hmac.compare_digest`, never `==`, on the hash."""
        calls = []
        import hmac as hmac_module

        real_compare_digest = hmac_module.compare_digest

        def spy(a: bytes, b: bytes) -> bool:
            calls.append((a, b))
            return real_compare_digest(a, b)

        monkeypatch.setattr("ragledger.server.security.hmac.compare_digest", spy)
        issued = issue_api_token()
        verify_api_token(issued.token, salt=issued.salt, expected_hash=issued.token_hash)
        assert len(calls) == 1


class TestCredentialEncryption:
    @pytest.fixture
    def settings_with_key(self, monkeypatch: pytest.MonkeyPatch) -> Settings:
        monkeypatch.setenv("APP_ENCRYPTION_KEY_V1", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
        return Settings()

    def test_roundtrip(self, settings_with_key: Settings) -> None:
        plaintext = b"postgresql://user:pass@host:5432/db"
        blob, key_id = encrypt_credential(plaintext, settings=settings_with_key)
        assert key_id == "v1"
        assert decrypt_credential(blob, settings=settings_with_key) == plaintext

    def test_ciphertext_does_not_contain_plaintext(self, settings_with_key: Settings) -> None:
        plaintext = b"a-very-recognizable-secret-value"
        blob, _key_id = encrypt_credential(plaintext, settings=settings_with_key)
        assert plaintext not in blob

    def test_two_encryptions_of_same_plaintext_differ(self, settings_with_key: Settings) -> None:
        plaintext = b"same-secret-both-times"
        blob_one, _ = encrypt_credential(plaintext, settings=settings_with_key)
        blob_two, _ = encrypt_credential(plaintext, settings=settings_with_key)
        assert blob_one != blob_two  # distinct random nonces

    def test_tampered_ciphertext_fails_to_decrypt(self, settings_with_key: Settings) -> None:
        blob, _key_id = encrypt_credential(b"secret", settings=settings_with_key)
        tampered = bytearray(blob)
        tampered[-1] ^= 0x01
        with pytest.raises(CredentialDecryptionError):
            decrypt_credential(bytes(tampered), settings=settings_with_key)

    def test_malformed_blob_raises_decryption_error(self, settings_with_key: Settings) -> None:
        with pytest.raises(CredentialDecryptionError):
            decrypt_credential(b"not-a-real-blob", settings=settings_with_key)

    def test_unknown_key_id_raises_decryption_error(
        self, settings_with_key: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blob, _key_id = encrypt_credential(b"secret", settings=settings_with_key)
        monkeypatch.delenv("APP_ENCRYPTION_KEY_V1", raising=False)
        settings_without_key = Settings()
        with pytest.raises(CredentialDecryptionError):
            decrypt_credential(blob, settings=settings_without_key)

    def test_key_rotation_old_ciphertext_still_decrypts_under_old_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APP_ENCRYPTION_KEY_V1", "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
        settings_v1 = Settings()
        blob, key_id = encrypt_credential(b"pre-rotation-secret", settings=settings_v1)
        assert key_id == "v1"

        monkeypatch.setenv("APP_ENCRYPTION_KEY_V2", "ZmVkY2JhOTg3NjU0MzIxMGZlZGNiYTk4NzY1NDMyMTA=")
        settings_after_rotation = Settings()
        # New encryptions use the new current key...
        new_blob, new_key_id = encrypt_credential(
            b"post-rotation-secret", settings=settings_after_rotation
        )
        assert new_key_id == "v2"
        # ...but the old blob, encrypted under v1, still decrypts correctly
        # because both keys remain configured.
        assert decrypt_credential(blob, settings=settings_after_rotation) == b"pre-rotation-secret"
        assert (
            decrypt_credential(new_blob, settings=settings_after_rotation)
            == b"post-rotation-secret"
        )


class TestWorkspaceScope:
    def test_matching_workspace_ids_do_not_raise(self) -> None:
        workspace_id = uuid.uuid4()
        require_workspace_scope(workspace_id, workspace_id)

    def test_mismatched_workspace_ids_raise(self) -> None:
        with pytest.raises(WorkspaceScopeViolationError):
            require_workspace_scope(uuid.uuid4(), uuid.uuid4())
