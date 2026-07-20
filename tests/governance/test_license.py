"""Tests for `ragledger.governance.license` (FR-060..FR-064, section 12.2)."""

from __future__ import annotations

from datetime import UTC, datetime

from ragledger.governance.license import (
    NOASSERTION,
    LicenseConfig,
    PathRule,
    detect_spdx_header,
    evaluate_license,
    gather_candidates,
    read_sidecar_expression,
    validate_spdx_expression,
)

_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class TestExpressionValidation:
    def test_simple_identifier_valid(self) -> None:
        assert validate_spdx_expression("MIT") is True

    def test_or_expression_valid(self) -> None:
        assert validate_spdx_expression("MIT OR Apache-2.0") is True

    def test_and_and_parens_valid(self) -> None:
        assert validate_spdx_expression("(MIT OR Apache-2.0) AND BSD-3-Clause") is True

    def test_or_later_plus_suffix_valid(self) -> None:
        assert validate_spdx_expression("GPL-3.0-only+") is True

    def test_unknown_identifier_invalid(self) -> None:
        assert validate_spdx_expression("TotallyMadeUpLicense-9000") is False

    def test_unbalanced_parens_invalid(self) -> None:
        assert validate_spdx_expression("(MIT OR Apache-2.0") is False

    def test_noassertion_literal_valid(self) -> None:
        assert validate_spdx_expression(NOASSERTION) is True

    def test_empty_string_invalid(self) -> None:
        assert validate_spdx_expression("") is False

    def test_dangling_operator_invalid(self) -> None:
        assert validate_spdx_expression("MIT AND") is False
        assert validate_spdx_expression("AND MIT") is False


class TestSpdxHeaderDetection:
    def test_detects_header_near_top_of_file(self) -> None:
        text = "SPDX-License-Identifier: Apache-2.0\n\nSome body text.\n"
        assert detect_spdx_header(text) == "Apache-2.0"

    def test_case_insensitive_prefix(self) -> None:
        text = "spdx-license-identifier: MIT\nbody\n"
        assert detect_spdx_header(text) == "MIT"

    def test_absent_header_returns_none(self) -> None:
        assert detect_spdx_header("just a regular document with no header") is None

    def test_header_far_below_max_lines_not_detected(self) -> None:
        text = "\n".join(["filler"] * 30) + "\nSPDX-License-Identifier: MIT\n"
        assert detect_spdx_header(text, max_lines=20) is None


class TestSidecarParsing:
    def test_bare_expression(self) -> None:
        assert read_sidecar_expression("MIT\n") == "MIT"

    def test_json_object_form(self) -> None:
        assert read_sidecar_expression('{"spdx_expression": "Apache-2.0"}') == "Apache-2.0"

    def test_yaml_mapping_form(self) -> None:
        assert read_sidecar_expression("spdx_expression: CC-BY-4.0\n") == "CC-BY-4.0"

    def test_empty_content_returns_none(self) -> None:
        assert read_sidecar_expression("   \n") is None


class TestPrecedenceAndConflicts:
    def test_user_assertion_outranks_everything(self) -> None:
        config = LicenseConfig(
            user_assertions={"docs/a.md": "Apache-2.0"},
            path_rules=(PathRule("docs/*", "MIT"),),
            repository_default="CC0-1.0",
        )
        effective, candidates = evaluate_license(
            "docs/a.md", {"license": "GPL-3.0-only"}, "BSD-3-Clause", config, "ver_x", _CREATED_AT
        )
        assert effective.method == "user_assertion"
        assert effective.spdx_expression == "Apache-2.0"
        # user_assertion + sidecar + frontmatter + path_rule + repository_default
        assert len(candidates) == 5

    def test_sidecar_outranks_frontmatter_and_path_rule(self) -> None:
        config = LicenseConfig(path_rules=(PathRule("*", "MIT"),))
        effective, _ = evaluate_license(
            "a.md", {"license": "GPL-3.0-only"}, "Apache-2.0", config, "ver_x", _CREATED_AT
        )
        assert effective.method == "sidecar"
        assert effective.spdx_expression == "Apache-2.0"

    def test_disagreement_produces_cross_referenced_conflicts(self) -> None:
        config = LicenseConfig(path_rules=(PathRule("*", "MIT"),), repository_default="NOASSERTION")
        effective, candidates = evaluate_license(
            "a.md", {"license": "Apache-2.0"}, None, config, "ver_x", _CREATED_AT
        )
        assert len(candidates) == 3
        for candidate in candidates:
            assert set(candidate.conflicting_assertion_ids) == {
                c.id for c in candidates if c.id != candidate.id
            }

    def test_agreement_produces_no_conflicts(self) -> None:
        config = LicenseConfig(path_rules=(PathRule("*", "MIT"),), repository_default="MIT")
        effective, candidates = evaluate_license(
            "a.md", {"license": "MIT"}, None, config, "ver_x", _CREATED_AT
        )
        assert all(c.conflicting_assertion_ids == [] for c in candidates)

    def test_no_source_at_all_is_noassertion_never_guessed(self) -> None:
        effective, candidates = evaluate_license(
            "a.md", None, None, LicenseConfig(), "ver_x", _CREATED_AT
        )
        assert effective.spdx_expression == NOASSERTION
        assert len(candidates) == 1

    def test_unrecognized_expression_becomes_noassertion(self) -> None:
        config = LicenseConfig(repository_default="NotARealLicense")
        effective, _ = evaluate_license("a.md", None, None, config, "ver_x", _CREATED_AT)
        assert effective.spdx_expression == NOASSERTION

    def test_spdx_header_used_when_no_frontmatter_field(self) -> None:
        effective, _ = evaluate_license(
            "a.txt", None, None, LicenseConfig(), "ver_x", _CREATED_AT, spdx_header="MIT"
        )
        assert effective.method == "frontmatter"
        assert effective.spdx_expression == "MIT"

    def test_frontmatter_field_wins_over_spdx_header_when_both_present(self) -> None:
        candidates = gather_candidates(
            "a.md", {"license": "Apache-2.0"}, None, LicenseConfig(), spdx_header="MIT"
        )
        assert len(candidates) == 1
        assert candidates[0].raw_expression == "Apache-2.0"

    def test_path_rule_first_match_wins(self) -> None:
        config = LicenseConfig(
            path_rules=(PathRule("docs/*", "MIT"), PathRule("docs/legal/*", "Apache-2.0"))
        )
        candidates = gather_candidates("docs/legal/notice.md", None, None, config)
        assert candidates[0].raw_expression == "MIT"

    def test_license_list_version_is_recorded_honestly(self) -> None:
        effective, _ = evaluate_license(
            "a.md", {"license": "MIT"}, None, LicenseConfig(), "ver_x", _CREATED_AT
        )
        assert effective.license_list_version is not None
        assert (
            "spdx" not in effective.license_list_version.lower()
            or "ragledger" in effective.license_list_version
        )
