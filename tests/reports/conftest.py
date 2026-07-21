"""Shared fixtures for `tests/reports/`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ragledger.core.artifacts import ArtifactStore
from ragledger.core.models import ManifestEnvelope
from ragledger.governance.acl import AclConfig, AclPathRule, TenantConfig, TenantPathRule
from ragledger.governance.license import LicenseConfig
from ragledger.governance.pii import PiiScanConfig
from ragledger.pipeline.build import BuildConfig, build_pipeline
from ragledger.pipeline.cache import StageCache
from ragledger.pipeline.embedding import DeterministicLocalEmbeddingProvider

_CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "corpus"
_SNAPSHOTS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "snapshots"
_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def corpus_dir() -> Path:
    return _CORPUS_DIR


@pytest.fixture
def snapshots_dir() -> Path:
    return _SNAPSHOTS_DIR


@pytest.fixture
def built_manifest(corpus_dir: Path, tmp_path: Path) -> ManifestEnvelope:
    """A full manifest built over the synthetic corpus, with PII/license/ACL/tenant assertions."""
    store = ArtifactStore(tmp_path / "artifacts")
    cache = StageCache(tmp_path / "cache")
    config = BuildConfig(
        namespace="reports-test",
        root=corpus_dir,
        build_id="bld_reports_test",
        created_at=_CREATED_AT,
        chunker_name="hierarchical",
        chunker_config={"max_tokens": 40},
        embedding_provider=DeterministicLocalEmbeddingProvider(dimension=8, seed=1),
        pii_config=PiiScanConfig(workspace_secret=b"reports-test-workspace-secret"),
        license_config=LicenseConfig(repository_default="NOASSERTION"),
        acl_config=AclConfig(path_rules=(AclPathRule("*", ("PUBLIC",)),)),
        tenant_config=TenantConfig(path_rules=(TenantPathRule("*", "tenant", "acme"),)),
        reproducible=True,
    )
    return build_pipeline(config, store, cache)
