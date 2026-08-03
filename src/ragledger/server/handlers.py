"""Job handlers: source-collection scan and manifest build, per PROJECT_SPEC.md sections 10/21.

These run inside `ragledger.server.jobs.run_pending_jobs`' work
transaction: every row they write commits or rolls back together with
the job's own terminal status.

The build handler deliberately reuses the CLI's configuration
translation (`ragledger.cli._build_support.build_config_from_ragledger_config`)
and the deterministic core (`ragledger.pipeline.build.build_pipeline`)
rather than reimplementing either -- a server build and a CLI build of
the same tree with the same config produce the same manifest bytes,
which is the whole point of the deterministic core.

Configuration errors (bad root, invalid pipeline config) raise
`PermanentJobError`: retrying cannot fix them (section 21: "auth/config
no retry").
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ragledger.cli._build_support import build_config_from_ragledger_config, resolve_timing
from ragledger.cli._config import ConfigError, RagledgerConfig
from ragledger.core.artifacts import ArtifactStore
from ragledger.core.manifest import canonical_manifest_bytes, compute_manifest_hash
from ragledger.pipeline.build import build_pipeline
from ragledger.pipeline.cache import StageCache
from ragledger.pipeline.discovery import DiscoveryConfig, discover_sources
from ragledger.server.db.models import (
    Build,
    Job,
    Manifest,
    PipelineConfig,
    SourceAsset,
    SourceCollection,
    SourceVersion,
)
from ragledger.server.db.models.enums import BuildState
from ragledger.server.jobs import JobHandler, PermanentJobError
from ragledger.server.settings import Settings

__all__ = ["JOB_TYPE_BUILD", "JOB_TYPE_SCAN", "make_handlers", "run_build_job", "run_scan_job"]

logger = logging.getLogger("ragledger.server.handlers")

JOB_TYPE_SCAN = "source_collection_scan"
JOB_TYPE_BUILD = "manifest_build"


def _collection_root(collection: SourceCollection, settings: Settings) -> Path:
    root_raw = collection.root_config.get("root")
    if not isinstance(root_raw, str) or not root_raw:
        raise PermanentJobError(f"source collection {collection.id} has no root path configured")
    root = Path(root_raw)
    if not root.is_absolute() or not root.is_dir():
        raise PermanentJobError(f"source collection root {root_raw!r} is not an existing directory")
    if not settings.is_source_root_allowed(root):
        raise PermanentJobError(f"source collection root {root_raw!r} is not an allowed base")
    return root


def run_scan_job(session: Session, job: Job, *, settings: Settings) -> None:
    """Discover sources under a collection's root and upsert asset/version rows.

    Discovery is the same `ragledger.pipeline.discovery.discover_sources`
    the CLI build uses (ignore files, symlink policy, streaming hashes,
    NFC-normalized URIs). A `SourceAsset` is keyed by the discovery
    record's stable source id; re-scanning the same tree is idempotent,
    and a changed file produces a new immutable `SourceVersion` under
    the same asset. Assets that disappear from the tree are marked
    `tombstone` (FR-017's candidate state), never deleted.
    """
    collection_id = job.payload.get("collection_id")
    collection = session.get(SourceCollection, collection_id)
    if collection is None or collection.workspace_id != job.workspace_id:
        raise PermanentJobError(f"source collection {collection_id!r} not found in workspace")
    root = _collection_root(collection, settings)

    records = discover_sources(root, collection.namespace, DiscoveryConfig())

    existing_assets = {
        asset.portable_id: asset
        for asset in session.execute(
            select(SourceAsset).where(SourceAsset.collection_id == collection.id)
        )
        .scalars()
        .all()
    }
    seen: set[str] = set()
    created_versions = 0
    for record in records:
        if record.status != "active":
            continue
        seen.add(record.id)
        asset = existing_assets.get(record.id)
        if asset is None:
            asset = SourceAsset(
                workspace_id=collection.workspace_id,
                collection_id=collection.id,
                portable_id=record.id,
                uri=record.uri,
                status="active",
            )
            session.add(asset)
            session.flush()
            existing_assets[record.id] = asset
        else:
            asset.status = "active"
            asset.uri = record.uri
        version_exists = session.execute(
            select(SourceVersion.id).where(SourceVersion.portable_id == record.version_id)
        ).first()
        if version_exists is None:
            session.add(
                SourceVersion(
                    workspace_id=collection.workspace_id,
                    source_asset_id=asset.id,
                    portable_id=record.version_id,
                    content_hash=record.content_hash,
                    media_type=record.media_type,
                    size_bytes=record.size_bytes,
                    artifact_ref=record.uri,
                )
            )
            created_versions += 1

    tombstoned = 0
    for portable_id, asset in existing_assets.items():
        if portable_id not in seen and asset.status != "tombstone":
            asset.status = "tombstone"
            tombstoned += 1

    session.flush()
    logger.info(
        "scan of collection %s: %d active sources, %d new versions, %d tombstoned",
        collection.id,
        len(seen),
        created_versions,
        tombstoned,
    )


def run_build_job(session: Session, job: Job, *, settings: Settings) -> None:
    """Run the deterministic build pipeline for a `Build` row and persist its manifest."""
    build_id = job.payload.get("build_id")
    build = session.get(Build, build_id)
    if build is None or build.workspace_id != job.workspace_id:
        raise PermanentJobError(f"build {build_id!r} not found in workspace")
    collection = session.get(SourceCollection, build.source_collection_id)
    pipeline_config = session.get(PipelineConfig, build.pipeline_config_id)
    if collection is None or pipeline_config is None:
        raise PermanentJobError("build references a missing collection or pipeline config")

    build.state = BuildState.RUNNING
    build.started_at = datetime.now(UTC)
    session.flush()

    root = _collection_root(collection, settings)
    try:
        config = RagledgerConfig.model_validate(
            {
                "namespace": collection.namespace,
                "sources": {"root": str(root)},
                **pipeline_config.config_json,
            }
        )
    except (ConfigError, ValueError) as exc:
        raise PermanentJobError(f"pipeline config {pipeline_config.id} is invalid: {exc}") from exc

    # `timing.build_id` derives from the resolved timestamp (identical for
    # two builds of the same epoch), not from the `Build` row's UUID --
    # a row-unique id inside the manifest would defeat FR-082's
    # byte-identical reproducibility across builds.
    timing = resolve_timing(job.payload.get("epoch"), force_reproducible=None)
    build_config = build_config_from_ragledger_config(
        config,
        root=root,
        config_dir=root,
        build_id=timing.build_id,
        created_at=timing.created_at,
        reproducible=timing.reproducible,
        log=logger.info,
    )

    artifact_store = ArtifactStore(settings.artifact_store_root)
    cache = StageCache(settings.artifact_store_root / "stage-cache")
    manifest = build_pipeline(build_config, artifact_store, cache)

    manifest_bytes = canonical_manifest_bytes(manifest)
    stored = artifact_store.put(manifest_bytes)
    manifest_hash = compute_manifest_hash(manifest)

    existing = session.execute(
        select(Manifest).where(
            Manifest.workspace_id == build.workspace_id, Manifest.manifest_hash == manifest_hash
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            Manifest(
                workspace_id=build.workspace_id,
                build_id=build.id,
                namespace=manifest.namespace,
                manifest_hash=manifest_hash,
                artifact_ref=f"artifacts/{stored.sha256}",
                source_count=len(manifest.sources),
                chunk_count=len(manifest.chunks),
                embedding_count=len(manifest.embeddings),
                signed=bool(manifest.signatures),
            )
        )

    build.state = BuildState.COMPLETED
    build.completed_at = datetime.now(UTC)
    build.counters = {
        "manifest_status": manifest.build.status,
        "manifest_hash": manifest_hash,
        "sources": len(manifest.sources),
        "parse_runs": len(manifest.parse_runs),
        "chunks": len(manifest.chunks),
        "embeddings": len(manifest.embeddings),
        "warnings": len(manifest.build.warnings),
    }
    session.flush()


def make_handlers(settings: Settings) -> dict[str, JobHandler]:
    """The job-type registry `run_pending_jobs` executes against."""
    return {
        JOB_TYPE_SCAN: lambda session, job: run_scan_job(session, job, settings=settings),
        JOB_TYPE_BUILD: lambda session, job: run_build_job(session, job, settings=settings),
    }
