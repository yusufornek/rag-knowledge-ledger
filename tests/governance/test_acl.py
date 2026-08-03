"""Tests for `ragledger.governance.acl` (FR-070..FR-076, section 12.3)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ragledger.governance.acl import (
    AclConfig,
    AclPathRule,
    AclValidationError,
    TenantConfig,
    TenantPathRule,
    UnsupportedAclEntryError,
    build_acl_assertion,
    build_tenant_assertion,
    expected_acl_entries,
    expected_tenant,
    normalize_acl_entries,
    validate_acl_entry,
)

_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class TestValidation:
    @pytest.mark.parametrize(
        "entry",
        ["PUBLIC", "USER:alice", "GROUP:engineering", "ROLE:admin", "ATTRIBUTE:region=eu"],
    )
    def test_valid_entries_accepted(self, entry: str) -> None:
        validate_acl_entry(entry)  # does not raise

    def test_deny_entry_rejected_as_unsupported(self) -> None:
        with pytest.raises(UnsupportedAclEntryError):
            validate_acl_entry("DENY:USER:alice")

    def test_deny_case_insensitive(self) -> None:
        with pytest.raises(UnsupportedAclEntryError):
            validate_acl_entry("deny:USER:alice")

    def test_malformed_entry_rejected(self) -> None:
        with pytest.raises(AclValidationError):
            validate_acl_entry("NOT_A_VALID_ENTRY")

    def test_attribute_without_equals_rejected(self) -> None:
        with pytest.raises(AclValidationError):
            validate_acl_entry("ATTRIBUTE:region")


class TestNormalization:
    def test_dedupes_and_sorts(self) -> None:
        result = normalize_acl_entries(["USER:bob", "PUBLIC", "USER:bob"])
        assert result == ["PUBLIC", "USER:bob"]

    def test_no_case_folding_by_default(self) -> None:
        result = normalize_acl_entries(["USER:Alice", "USER:alice"])
        assert result == ["USER:Alice", "USER:alice"]

    def test_case_folding_when_explicitly_requested(self) -> None:
        result = normalize_acl_entries(["USER:Alice", "USER:alice"], case_normalize=True)
        assert result == ["user:alice"]

    def test_order_of_input_does_not_affect_output(self) -> None:
        a = normalize_acl_entries(["ROLE:admin", "PUBLIC", "GROUP:eng"])
        b = normalize_acl_entries(["GROUP:eng", "ROLE:admin", "PUBLIC"])
        assert a == b


class TestAclAssertion:
    def test_hash_is_canonical_and_deterministic(self) -> None:
        first = build_acl_assertion("ver_x", ["PUBLIC", "USER:bob"], _CREATED_AT)
        second = build_acl_assertion("ver_x", ["USER:bob", "PUBLIC"], _CREATED_AT)
        assert first.acl_hash == second.acl_hash
        assert first.id == second.id

    def test_entries_are_the_normalized_set(self) -> None:
        assertion = build_acl_assertion("ver_x", ["PUBLIC", "PUBLIC", "USER:bob"], _CREATED_AT)
        assert assertion.entries == ["PUBLIC", "USER:bob"]

    def test_deny_entry_raises_before_assertion_is_built(self) -> None:
        with pytest.raises(UnsupportedAclEntryError):
            build_acl_assertion("ver_x", ["DENY:USER:bob"], _CREATED_AT)


class TestExpectedAclResolution:
    def test_direct_source_mapping(self) -> None:
        config = AclConfig(source_entries={"docs/a.md": ("PUBLIC",)})
        assert expected_acl_entries("docs/a.md", config) == ["PUBLIC"]

    def test_path_rule_fallback(self) -> None:
        config = AclConfig(path_rules=(AclPathRule("docs/*", ("PUBLIC",)),))
        assert expected_acl_entries("docs/a.md", config) == ["PUBLIC"]

    def test_no_applicable_rule_returns_none_not_empty(self) -> None:
        config = AclConfig(path_rules=(AclPathRule("docs/*", ("PUBLIC",)),))
        assert expected_acl_entries("other/a.md", config) is None

    def test_direct_mapping_takes_priority_over_path_rule(self) -> None:
        config = AclConfig(
            source_entries={"docs/a.md": ("USER:bob",)},
            path_rules=(AclPathRule("docs/*", ("PUBLIC",)),),
        )
        assert expected_acl_entries("docs/a.md", config) == ["USER:bob"]


class TestTenant:
    def test_direct_mapping(self) -> None:
        config = TenantConfig(source_tenants={"docs/a.md": ("tenant", "acme")})
        assert expected_tenant("docs/a.md", config) == ("tenant", "acme")

    def test_path_rule_fallback(self) -> None:
        config = TenantConfig(path_rules=(TenantPathRule("docs/*", "tenant", "acme"),))
        assert expected_tenant("docs/a.md", config) == ("tenant", "acme")

    def test_unresolved_tenant_returns_none(self) -> None:
        assert expected_tenant("a.md", TenantConfig()) is None

    def test_assertion_hash_and_fields(self) -> None:
        assertion = build_tenant_assertion("ver_x", "tenant", "acme", _CREATED_AT)
        assert assertion.tenant_key == "tenant"
        assert assertion.tenant_value == "acme"
        assert len(assertion.tenant_hash) == 64

    def test_typed_tenant_value_mismatch_produces_different_hash(self) -> None:
        # the design specification section 40: tenant "1" (string) vs 1 (int) is a typed
        # mismatch; this module only ever deals in strings, but a differently
        # spelled value must still hash differently.
        a = build_tenant_assertion("ver_x", "tenant", "1", _CREATED_AT)
        b = build_tenant_assertion("ver_x", "tenant", "01", _CREATED_AT)
        assert a.tenant_hash != b.tenant_hash
