"""Tests for `ragledger.governance.pii` (FR-050..FR-056, section 12.1)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from ragledger.governance.pii import (
    CustomRecognizerConfigError,
    PiiScanConfig,
    Recognizer,
    RegexTimeoutError,
    build_pii_scan_assertion,
    compute_value_hmac,
    default_recognizers,
    load_custom_recognizers,
    mask_generic,
    mask_value,
    scan_text,
)

_CANARY_EMAIL = "canary.detector.test@example.com"
_CANARY_PHONE = "555-019-2288"
_CANARY_CREDIT_CARD = "4111 1111 1111 1111"
_CANARY_SSN = "219-09-9999"
_CANARY_TCKN = "10000000146"
_CANARY_IBAN = "TR330006100519786457841326"


class TestDetectors:
    def test_email_detected(self) -> None:
        findings = scan_text(f"contact {_CANARY_EMAIL} now")
        assert any(
            f.entity_type == "EMAIL_ADDRESS" and f.raw_value == _CANARY_EMAIL for f in findings
        )

    def test_phone_detected(self) -> None:
        findings = scan_text(f"call {_CANARY_PHONE} today")
        assert any(f.entity_type == "PHONE_NUMBER" for f in findings)

    def test_credit_card_luhn_valid_detected_with_high_confidence(self) -> None:
        findings = scan_text(f"card on file: {_CANARY_CREDIT_CARD}")
        matches = [f for f in findings if f.entity_type == "CREDIT_CARD"]
        assert len(matches) == 1
        assert matches[0].confidence >= 0.8

    def test_credit_card_luhn_invalid_not_reported(self) -> None:
        findings = scan_text("random digits 1234 5678 9012 3456 here")
        assert not any(f.entity_type == "CREDIT_CARD" for f in findings)

    def test_us_ssn_plausible_detected(self) -> None:
        findings = scan_text(f"ssn {_CANARY_SSN} on file")
        assert any(f.entity_type == "US_SSN" for f in findings)

    def test_us_ssn_implausible_area_rejected(self) -> None:
        findings = scan_text("ssn 000-12-3456 on file")
        assert not any(f.entity_type == "US_SSN" for f in findings)

    def test_tckn_checksum_valid_detected(self) -> None:
        findings = scan_text(f"tckn {_CANARY_TCKN} kayitli")
        assert any(f.entity_type == "TR_TCKN" for f in findings)

    def test_tckn_checksum_invalid_rejected(self) -> None:
        findings = scan_text("random eleven digit number 12345678901 here")
        assert not any(f.entity_type == "TR_TCKN" for f in findings)

    def test_iban_checksum_valid_detected(self) -> None:
        findings = scan_text(f"iban {_CANARY_IBAN} kayitli")
        assert any(f.entity_type == "IBAN" for f in findings)

    def test_iban_checksum_invalid_rejected(self) -> None:
        findings = scan_text("iban TR000000000000000000000000 here")
        assert not any(f.entity_type == "IBAN" for f in findings)

    def test_clean_text_produces_no_findings(self) -> None:
        assert scan_text("nothing sensitive appears in this sentence at all") == []

    def test_findings_sorted_by_offset(self) -> None:
        findings = scan_text(f"{_CANARY_EMAIL} then later {_CANARY_PHONE}")
        starts = [f.start for f in findings]
        assert starts == sorted(starts)


class TestMasking:
    def test_email_mask_matches_spec_example_shape(self) -> None:
        masked = mask_value("EMAIL_ADDRESS", "john@example.com")
        assert masked == "jo***@ex***.com"
        assert "john" not in masked

    def test_generic_mask_keeps_only_last_characters(self) -> None:
        masked = mask_generic("1234567890", keep_last=4)
        assert masked == "******7890"

    def test_short_value_fully_masked(self) -> None:
        assert mask_generic("ab", keep_last=4) == "**"

    def test_masked_preview_never_contains_full_raw_value(self) -> None:
        for entity_type, raw in [
            ("EMAIL_ADDRESS", _CANARY_EMAIL),
            ("CREDIT_CARD", _CANARY_CREDIT_CARD),
            ("US_SSN", _CANARY_SSN),
            ("TR_TCKN", _CANARY_TCKN),
            ("IBAN", _CANARY_IBAN),
        ]:
            masked = mask_value(entity_type, raw)
            assert masked != raw
            assert raw not in masked


class TestValueHmac:
    def test_deterministic_for_same_secret(self) -> None:
        a = compute_value_hmac(_CANARY_EMAIL, b"secret-1")
        b = compute_value_hmac(_CANARY_EMAIL, b"secret-1")
        assert a == b

    def test_differs_across_secrets(self) -> None:
        a = compute_value_hmac(_CANARY_EMAIL, b"secret-1")
        b = compute_value_hmac(_CANARY_EMAIL, b"secret-2")
        assert a != b

    def test_never_equals_a_plain_sha256_of_the_value(self) -> None:
        import hashlib

        plain = hashlib.sha256(_CANARY_EMAIL.encode()).hexdigest()
        hmac_value = compute_value_hmac(_CANARY_EMAIL, b"secret-1")
        assert plain != hmac_value

    def test_is_a_valid_sha256_shaped_hex_string(self) -> None:
        value = compute_value_hmac(_CANARY_EMAIL, b"secret-1")
        assert len(value) == 64
        int(value, 16)


class TestAssertionConstruction:
    def test_no_findings_reported_as_no_findings_detected_not_guaranteed_clean(self) -> None:
        assertion = build_pii_scan_assertion(
            "ver_x", "totally clean text", PiiScanConfig(), datetime(2026, 1, 1, tzinfo=UTC)
        )
        assert assertion.status == "no_findings_detected"
        assert assertion.findings == []

    def test_findings_detected_status_and_no_raw_value_anywhere(self) -> None:
        assertion = build_pii_scan_assertion(
            "ver_x",
            f"reach out to {_CANARY_EMAIL} for help",
            PiiScanConfig(workspace_secret=b"workspace-secret"),
            datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert assertion.status == "findings_detected"
        assert len(assertion.findings) == 1
        finding = assertion.findings[0]
        assert finding.masked_preview is not None
        assert _CANARY_EMAIL not in finding.masked_preview
        assert finding.value_hmac is not None
        assert finding.value_hmac != _CANARY_EMAIL
        dumped = assertion.model_dump_json()
        assert _CANARY_EMAIL not in dumped

    def test_value_hmac_omitted_without_a_workspace_secret(self) -> None:
        assertion = build_pii_scan_assertion(
            "ver_x", f"contact {_CANARY_EMAIL}", PiiScanConfig(), datetime(2026, 1, 1, tzinfo=UTC)
        )
        assert assertion.findings[0].value_hmac is None

    def test_assertion_id_is_deterministic(self) -> None:
        created_at = datetime(2026, 1, 1, tzinfo=UTC)
        first = build_pii_scan_assertion("ver_x", "clean text", PiiScanConfig(), created_at)
        second = build_pii_scan_assertion("ver_x", "clean text", PiiScanConfig(), created_at)
        assert first.id == second.id

    def test_scanner_records_config_and_version(self) -> None:
        assertion = build_pii_scan_assertion(
            "ver_x", "clean text", PiiScanConfig(), datetime(2026, 1, 1, tzinfo=UTC)
        )
        assert assertion.scanner.name
        assert assertion.scanner.version
        assert assertion.scanner.config_hash is not None


class TestCustomRecognizers:
    def test_load_and_scan(self) -> None:
        recognizers = load_custom_recognizers(
            "recognizers:\n"
            "  - entity_type: EMPLOYEE_ID\n"
            "    pattern: 'EMP-[0-9]{6}'\n"
            "    confidence: 0.7\n"
        )
        findings = scan_text("badge EMP-123456 here", recognizers)
        assert findings[0].entity_type == "EMPLOYEE_ID"

    def test_invalid_yaml_rejected(self) -> None:
        with pytest.raises(CustomRecognizerConfigError):
            load_custom_recognizers("recognizers: [this is not")

    def test_missing_required_fields_rejected(self) -> None:
        with pytest.raises(CustomRecognizerConfigError):
            load_custom_recognizers("recognizers:\n  - entity_type: X\n")

    def test_invalid_regex_rejected(self) -> None:
        with pytest.raises(CustomRecognizerConfigError):
            load_custom_recognizers("recognizers:\n  - entity_type: X\n    pattern: '('\n")

    def test_config_used_via_pii_scan_config(self) -> None:
        config = PiiScanConfig(
            custom_recognizers_yaml=(
                "recognizers:\n  - entity_type: EMPLOYEE_ID\n    pattern: 'EMP-[0-9]{6}'\n"
            )
        )
        assertion = build_pii_scan_assertion(
            "ver_x", "badge EMP-654321", config, datetime(2026, 1, 1, tzinfo=UTC)
        )
        assert any(f.entity_type == "EMPLOYEE_ID" for f in assertion.findings)


class TestRegexTimeout:
    def test_catastrophic_backtracking_pattern_times_out_bounded(self) -> None:
        import re

        evil = Recognizer(
            entity_type="EVIL",
            recognizer_id="evil",
            recognizer_version="1",
            pattern=re.compile(r"(a+)+b"),
            base_confidence=0.5,
            timeout_seconds=0.3,
        )
        start = time.monotonic()
        result = scan_text("a" * 35 + "!", [evil])
        elapsed = time.monotonic() - start
        assert result == []
        assert elapsed < 3.0

    def test_regex_timeout_error_importable_and_raisable(self) -> None:
        with pytest.raises(RegexTimeoutError):
            raise RegexTimeoutError("boom")


def test_default_recognizers_cover_every_documented_entity_type() -> None:
    entity_types = {r.entity_type for r in default_recognizers()}
    assert entity_types == {
        "EMAIL_ADDRESS",
        "PHONE_NUMBER",
        "CREDIT_CARD",
        "US_SSN",
        "TR_TCKN",
        "IBAN",
    }
