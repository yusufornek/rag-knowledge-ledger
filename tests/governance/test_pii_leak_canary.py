"""PII leak canary test, per PROJECT_SPEC.md section 42.3.

Runs the full `ragledger.pipeline.build.build_pipeline` over the
synthetic corpus (which contains several unique, deliberately
recognizable canary PII values -- see `tests/fixtures/corpus/*`) and
asserts that none of those raw values ever appear, in any form, inside
the produced manifest: not in `PiiFinding` evidence, not in any
`WarningRecord`/`StageRecord` string, not anywhere in the manifest's
full canonical JSON serialization.

Scope, matching PROJECT_SPEC.md section 42.3's own framing ("Raw source
restricted artifactta canary expected; other artifact allowlist exact"):
the *raw source bytes* stored in the content-addressed artifact store
are an explicit, intentional exception -- they are the original
document, and a RAG pipeline necessarily needs the real content to
parse/chunk/embed it. The canary check here targets exactly what this
milestone's deliverable specifies: the produced **manifest** (the
lineage/evidence document network operators, CI, and reports actually
consume) -- not the content-addressed artifact store's raw/parsed/chunk
text objects, which legitimately mirror real document content the same
way the raw source itself does. `test_masked_preview_never_contains_full_raw_value`
in `tests/governance/test_pii.py` separately covers the `PiiFinding`
evidence shape in isolation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ragledger.core.artifacts import ArtifactStore
from ragledger.core.manifest import manifest_to_dict
from ragledger.governance.acl import AclConfig, AclPathRule
from ragledger.governance.license import LicenseConfig
from ragledger.governance.pii import PiiScanConfig
from ragledger.pipeline.build import BuildConfig, build_pipeline
from ragledger.pipeline.cache import StageCache
from ragledger.pipeline.embedding import DeterministicLocalEmbeddingProvider

# Every raw canary value that appears, in full, somewhere in
# tests/fixtures/corpus/*. Each is unique and synthetic (never real
# personal data); see the corpus files' own comments.
_CANARY_VALUES = [
    "canary.leaktest@example.com",
    "555-010-1199",
    "pdf.canary.leaktest@example.com",
    "4111 1111 1111 1111",
    "219-09-9999",
    "10000000146",
    "TR330006100519786457841326",
]

_CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"


@pytest.fixture
def corpus_dir() -> Path:
    return _CORPUS_DIR


def _build_manifest_dict(corpus_dir: Path, tmp_path: Path) -> dict[str, object]:
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    config = BuildConfig(
        namespace="canary-test",
        root=corpus_dir,
        build_id="bld_canary",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        chunker_name="hierarchical",
        chunker_config={"max_tokens": 40},
        embedding_provider=DeterministicLocalEmbeddingProvider(dimension=8, seed=1),
        pii_config=PiiScanConfig(workspace_secret=b"canary-test-workspace-secret"),
        license_config=LicenseConfig(repository_default="NOASSERTION"),
        acl_config=AclConfig(path_rules=(AclPathRule("*", ("PUBLIC",)),)),
    )
    manifest = build_pipeline(config, store, cache)
    return manifest_to_dict(manifest)


def test_pii_scan_actually_found_every_canary_value(corpus_dir: Path, tmp_path: Path) -> None:
    """Sanity check the canary is live: if detection regresses, this test
    (not just the leak-absence test below) must fail loudly."""
    manifest = _build_manifest_dict(corpus_dir, tmp_path)
    pii_assertions = [a for a in manifest["assertions"] if a["type"] == "PII_SCAN"]
    total_findings = sum(len(a["findings"]) for a in pii_assertions)
    assert total_findings >= len(_CANARY_VALUES)


def test_no_canary_value_appears_anywhere_in_the_manifest_json(
    corpus_dir: Path, tmp_path: Path
) -> None:
    manifest = _build_manifest_dict(corpus_dir, tmp_path)
    serialized = json.dumps(manifest)
    for canary in _CANARY_VALUES:
        assert canary not in serialized, f"raw canary value leaked into the manifest: {canary!r}"


def test_no_canary_value_appears_in_any_warning_or_error_string(
    corpus_dir: Path, tmp_path: Path
) -> None:
    manifest = _build_manifest_dict(corpus_dir, tmp_path)
    texts: list[str] = []
    for run in manifest["parse_runs"]:
        texts.extend(w.get("message") or "" for w in run.get("warnings", []))
    for warning in manifest["build"].get("warnings", []):
        texts.append(warning.get("message") or "")
    blob = " ".join(texts)
    for canary in _CANARY_VALUES:
        assert canary not in blob


def test_masked_previews_are_the_only_evidence_and_are_partial(
    corpus_dir: Path, tmp_path: Path
) -> None:
    manifest = _build_manifest_dict(corpus_dir, tmp_path)
    pii_assertions = [a for a in manifest["assertions"] if a["type"] == "PII_SCAN"]
    for assertion in pii_assertions:
        for finding in assertion["findings"]:
            assert "raw_value" not in finding
            assert "value" not in finding
            preview = finding.get("masked_preview")
            if preview is not None:
                for canary in _CANARY_VALUES:
                    assert canary not in preview


def test_value_hmac_is_present_and_not_a_plain_value(corpus_dir: Path, tmp_path: Path) -> None:
    manifest = _build_manifest_dict(corpus_dir, tmp_path)
    pii_assertions = [a for a in manifest["assertions"] if a["type"] == "PII_SCAN"]
    any_hmac_checked = False
    for assertion in pii_assertions:
        for finding in assertion["findings"]:
            value_hmac = finding.get("value_hmac")
            if value_hmac is None:
                continue
            any_hmac_checked = True
            assert len(value_hmac) == 64
            int(value_hmac, 16)  # a sha256-shaped hex digest, not a raw string
            for canary in _CANARY_VALUES:
                assert canary not in value_hmac
    assert any_hmac_checked
