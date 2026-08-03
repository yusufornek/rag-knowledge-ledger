"""FR-004: SSRF-safe target URL validation.

Pure unit tests -- DNS resolution is injected, no network or database
is touched. The security-relevant assertions: the cloud metadata
endpoint and every other link-local/loopback address is rejected even
when private targets are allowlisted; a hostname resolving to a mix of
public and private addresses is rejected outright; and the redacted
endpoint never carries userinfo, path, or query.
"""

from __future__ import annotations

import pytest

from ragledger.server.settings import Settings
from ragledger.server.ssrf import TargetUrlNotAllowedError, validate_target_url


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.delenv("ALLOW_PRIVATE_TARGETS", raising=False)
    monkeypatch.delenv("PRIVATE_TARGET_CIDRS", raising=False)
    return Settings()


@pytest.fixture
def settings_private_allowed(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("ALLOW_PRIVATE_TARGETS", "true")
    monkeypatch.setenv("PRIVATE_TARGET_CIDRS", "10.10.0.0/16, 192.168.7.0/24")
    return Settings()


class TestSchemeAndShape:
    def test_public_ip_literal_is_allowed(self, settings: Settings) -> None:
        result = validate_target_url("https://8.8.8.8:6333/collections", settings=settings)
        assert result.decision == "public"
        assert result.resolved_addresses == ("8.8.8.8",)

    def test_disallowed_scheme_is_rejected(self, settings: Settings) -> None:
        with pytest.raises(TargetUrlNotAllowedError, match="scheme"):
            validate_target_url("ftp://8.8.8.8/data", settings=settings)

    def test_file_scheme_is_rejected(self, settings: Settings) -> None:
        with pytest.raises(TargetUrlNotAllowedError, match="scheme"):
            validate_target_url("file:///etc/passwd", settings=settings)

    def test_postgres_scheme_is_allowed(self, settings: Settings) -> None:
        result = validate_target_url("postgresql://1.1.1.1:5432/vectors", settings=settings)
        assert result.decision == "public"

    def test_missing_host_is_rejected(self, settings: Settings) -> None:
        with pytest.raises(TargetUrlNotAllowedError, match="no host"):
            validate_target_url("https:///path-only", settings=settings)


class TestRedaction:
    def test_userinfo_path_and_query_are_stripped(self, settings: Settings) -> None:
        result = validate_target_url(
            "https://user:hunter2@8.8.8.8:6333/collections?api-key=abc",
            settings=settings,
        )
        assert result.endpoint_redacted == "https://8.8.8.8:6333"
        assert "hunter2" not in result.endpoint_redacted
        assert "api-key" not in result.endpoint_redacted

    def test_error_message_never_echoes_userinfo(self, settings: Settings) -> None:
        with pytest.raises(TargetUrlNotAllowedError) as excinfo:
            validate_target_url("https://user:hunter2@127.0.0.1:6333/x", settings=settings)
        assert "hunter2" not in str(excinfo.value)


class TestAlwaysBlocked:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:6333",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]:6333",
            "http://[fe80::1]:6333",
            "http://0.0.0.0:6333",
            "http://224.0.0.1:6333",
        ],
    )
    def test_blocked_even_when_private_targets_allowed(
        self, settings_private_allowed: Settings, url: str
    ) -> None:
        with pytest.raises(TargetUrlNotAllowedError, match="always"):
            validate_target_url(url, settings=settings_private_allowed)

    def test_ipv4_mapped_ipv6_loopback_is_blocked(self, settings: Settings) -> None:
        with pytest.raises(TargetUrlNotAllowedError, match="always"):
            validate_target_url("http://[::ffff:127.0.0.1]:6333", settings=settings)


class TestPrivateAddressPolicy:
    def test_private_ip_rejected_by_default(self, settings: Settings) -> None:
        with pytest.raises(TargetUrlNotAllowedError, match="ALLOW_PRIVATE_TARGETS"):
            validate_target_url("http://10.10.3.4:6333", settings=settings)

    def test_private_ip_in_allowlisted_cidr_is_accepted(
        self, settings_private_allowed: Settings
    ) -> None:
        result = validate_target_url("http://10.10.3.4:6333", settings=settings_private_allowed)
        assert result.decision == "private_cidr_allowlisted"

    def test_private_ip_outside_allowlisted_cidr_is_rejected(
        self, settings_private_allowed: Settings
    ) -> None:
        with pytest.raises(TargetUrlNotAllowedError, match="PRIVATE_TARGET_CIDRS"):
            validate_target_url("http://10.99.3.4:6333", settings=settings_private_allowed)

    def test_ipv4_mapped_ipv6_private_needs_the_allowlist_too(self, settings: Settings) -> None:
        with pytest.raises(TargetUrlNotAllowedError, match="ALLOW_PRIVATE_TARGETS"):
            validate_target_url("http://[::ffff:10.10.3.4]:6333", settings=settings)


class TestHostnameResolution:
    def test_hostname_resolving_public_is_allowed(self, settings: Settings) -> None:
        result = validate_target_url(
            "https://qdrant.example.com:6333",
            settings=settings,
            resolver=lambda host: ["9.9.9.9"],
        )
        assert result.decision == "public"
        assert result.endpoint_redacted == "https://qdrant.example.com:6333"

    def test_hostname_resolving_to_metadata_ip_is_blocked(self, settings: Settings) -> None:
        with pytest.raises(TargetUrlNotAllowedError, match="always"):
            validate_target_url(
                "https://innocent-looking.example.com",
                settings=settings,
                resolver=lambda host: ["169.254.169.254"],
            )

    def test_mixed_public_and_private_resolution_is_rejected(self, settings: Settings) -> None:
        """One public plus one private A record must fail: a reconnect could hit the private one."""
        with pytest.raises(TargetUrlNotAllowedError, match="ALLOW_PRIVATE_TARGETS"):
            validate_target_url(
                "https://rebinding.example.com",
                settings=settings,
                resolver=lambda host: ["9.9.9.9", "10.0.0.5"],
            )

    def test_resolution_failure_is_rejected(self, settings: Settings) -> None:
        def _failing(host: str) -> list[str]:
            raise OSError("no such host")

        with pytest.raises(TargetUrlNotAllowedError, match="did not resolve"):
            validate_target_url(
                "https://does-not-exist.example.com", settings=settings, resolver=_failing
            )

    def test_empty_resolution_is_rejected(self, settings: Settings) -> None:
        with pytest.raises(TargetUrlNotAllowedError, match="did not resolve"):
            validate_target_url(
                "https://empty.example.com", settings=settings, resolver=lambda host: []
            )
