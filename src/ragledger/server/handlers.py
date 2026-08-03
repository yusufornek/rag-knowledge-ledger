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

import json
import logging
import os
import tempfile
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ragledger.cli._build_support import build_config_from_ragledger_config, resolve_timing
from ragledger.cli._config import ConfigError, RagledgerConfig
from ragledger.connectors.base import (
    ConnectorConfigError,
    ConnectorError,
    NormalizedPoint,
    VectorTargetConnector,
)
from ragledger.connectors.config import PgvectorTargetConfig, QdrantTargetConfig
from ragledger.connectors.ndjson import NdjsonConnector, SnapshotHeader, write_snapshot
from ragledger.connectors.pgvector import PgvectorConnector
from ragledger.connectors.qdrant import QdrantConnector
from ragledger.core.artifacts import ArtifactStore
from ragledger.core.hashing import hash_canonical
from ragledger.core.manifest import canonical_manifest_bytes, compute_manifest_hash, load_manifest
from ragledger.pipeline.build import build_pipeline
from ragledger.pipeline.cache import StageCache
from ragledger.pipeline.discovery import DiscoveryConfig, discover_sources
from ragledger.reconcile.engine import reconcile_big_data, reconcile_small_data
from ragledger.reconcile.policy import (
    PolicyValidationError,
    evaluate_policy,
    load_policy_document,
)
from ragledger.reconcile.remediation import build_remediation_plan
from ragledger.reconcile.report import (
    PolicyVerdict,
    ReconciliationReport,
    to_json_bytes,
)
from ragledger.server.db.models import (
    Build,
    InventorySnapshot,
    Job,
    Manifest,
    PipelineConfig,
    PolicyEvaluation,
    PolicyRevision,
    Reconciliation,
    SourceAsset,
    SourceCollection,
    SourceVersion,
    VectorTarget,
)
from ragledger.server.db.models import Finding as FindingRow
from ragledger.server.db.models.enums import (
    BuildState,
    PolicyEvaluationResult,
    ReconciliationState,
    SnapshotStatus,
    VectorTargetType,
)
from ragledger.server.jobs import (
    JobHandler,
    PermanentJobError,
    check_cancellation,
)
from ragledger.server.security import decrypt_credential
from ragledger.server.settings import Settings

__all__ = [
    "JOB_TYPE_BUILD",
    "JOB_TYPE_RECONCILE",
    "JOB_TYPE_SCAN",
    "JOB_TYPE_SNAPSHOT",
    "make_handlers",
    "run_build_job",
    "run_reconcile_job",
    "run_scan_job",
    "run_snapshot_job",
]

logger = logging.getLogger("ragledger.server.handlers")

JOB_TYPE_SCAN = "source_collection_scan"
JOB_TYPE_BUILD = "manifest_build"
JOB_TYPE_SNAPSHOT = "inventory_snapshot"
JOB_TYPE_RECONCILE = "reconciliation"

_SNAPSHOT_CONNECTOR_VERSION = "1"
_CANCEL_CHECK_EVERY_POINTS = 500


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
    # The pipeline itself is one deterministic unit; cancellation is
    # honored at its boundary, not between its internal stages.
    check_cancellation(session, job)

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


# --------------------------------------------------------------------------
# Inventory snapshots (sections 13/21)
# --------------------------------------------------------------------------


def _connector_for_target(
    target: VectorTarget, credential: str, settings: Settings
) -> VectorTargetConnector[Any]:
    """Build the live connector for a `VectorTarget` row.

    The connector configs take credentials indirected through an
    environment variable name (`api_key_env`/`dsn_env`), by design --
    the CLI never wants a secret inside a YAML file. The server's
    credential lives AES-GCM-encrypted in the row instead, so this
    function bridges the two: the decrypted value goes into a
    process-local environment variable with a single-use unique name,
    which the connector reads at construction/connect time. The
    variable is unset again by `_ephemeral_credential_env` as soon as
    the connector is built; both connectors capture the resolved value,
    not the variable name.
    """
    env_name = f"RAGLEDGER_TARGET_CREDENTIAL_{uuid.uuid4().hex}"
    mapping: dict[str, Any] = dict(target.mapping_config)
    mapping.pop("type", None)
    if target.target_type == VectorTargetType.QDRANT:
        config_data: dict[str, Any] = {
            "type": "qdrant",
            "endpoint": target.endpoint_redacted,
            "api_key_env": env_name,
            "connect_timeout_seconds": settings.target_connect_timeout_seconds,
            "read_timeout_seconds": settings.target_read_timeout_seconds,
            **mapping,
        }
        with _ephemeral_credential_env(env_name, credential):
            return QdrantConnector(QdrantTargetConfig.model_validate(config_data))
    config_data = {
        "type": "pgvector",
        "dsn_env": env_name,
        "connect_timeout_seconds": settings.target_connect_timeout_seconds,
        **mapping,
    }
    with _ephemeral_credential_env(env_name, credential):
        return PgvectorConnector(PgvectorTargetConfig.model_validate(config_data))


class _ephemeral_credential_env:
    """Set an environment variable for the duration of a `with` block, then remove it."""

    def __init__(self, name: str, value: str) -> None:
        self._name = name
        self._value = value

    def __enter__(self) -> None:
        os.environ[self._name] = self._value

    def __exit__(self, *exc_info: object) -> None:
        os.environ.pop(self._name, None)


def run_snapshot_job(session: Session, job: Job, *, settings: Settings) -> None:
    """Stream a target's points into an immutable NDJSON snapshot artifact.

    The same `write_snapshot` path as the CLI's `ragledger snapshot`:
    header with target schema/consistency, zstd-compressed point lines,
    content-hashed trailer. The snapshot file is content-addressed into
    the artifact store, and the `InventorySnapshot` row records status
    (`completed` or `incomplete` per the pass's consistency outcome),
    point count, content hash, schema hash, and a resume checkpoint.
    """
    snapshot_id = job.payload.get("snapshot_id")
    snapshot = session.get(InventorySnapshot, snapshot_id)
    if snapshot is None or snapshot.workspace_id != job.workspace_id:
        raise PermanentJobError(f"snapshot {snapshot_id!r} not found in workspace")
    target = session.get(VectorTarget, snapshot.target_id)
    if target is None:
        raise PermanentJobError("snapshot references a missing target")

    snapshot.status = SnapshotStatus.RUNNING
    session.flush()

    credential = decrypt_credential(target.credential_ciphertext, settings=settings).decode("utf-8")
    connector = _connector_for_target(target, credential, settings)
    try:
        try:
            connector.validate_configuration()
        except ConnectorConfigError as exc:
            raise PermanentJobError(f"target configuration invalid: {exc}") from exc

        test_result = connector.test_connection()
        if not test_result.ok:
            raise ConnectorError(f"target unreachable: {test_result.message}")

        schema = connector.inspect_target_schema()
        header = SnapshotHeader(
            target_id=schema.target_id,
            scope=schema.scope,
            target_type=target.target_type.value,
            vector_names=[field.name for field in schema.vector_fields],
            vector_dimensions={field.name: field.dimension for field in schema.vector_fields},
            started_at=datetime.now(UTC),
            connector_version=_SNAPSHOT_CONNECTOR_VERSION,
            # Predicted from the target type (the CLI's
            # `predicted_consistency_mode` rule): the header is written
            # before the pass, and a connector's own ConsistencyInfo only
            # exists after it.
            consistency_mode=(
                "best_effort_live"
                if target.target_type == VectorTargetType.QDRANT
                else str(target.mapping_config.get("consistency", "repeatable_read"))
            ),
        )

        last_point_id: Any = None
        point_count = 0

        def _tracked_points() -> Iterator[NormalizedPoint]:
            nonlocal last_point_id, point_count
            for point in connector.iterate_points(checkpoint=None, include_vectors=False):
                if point_count % _CANCEL_CHECK_EVERY_POINTS == 0:
                    check_cancellation(session, job)
                last_point_id = point.point_id
                point_count += 1
                yield point

        store = ArtifactStore(settings.artifact_store_root)  # also creates the root directory
        with tempfile.TemporaryDirectory(dir=settings.artifact_store_root) as tmp_dir:
            snapshot_path = Path(tmp_dir) / "snapshot.ndjson.zst"
            trailer = write_snapshot(
                snapshot_path,
                header,
                _tracked_points(),
                finished_at=datetime.now(UTC),
                consistency_provider=connector.get_consistency_info,
            )
            stored = store.put(snapshot_path.read_bytes())

        consistency = connector.get_consistency_info()
    finally:
        connector.close()

    snapshot.status = (
        SnapshotStatus.COMPLETED
        if consistency.completeness.value == "complete"
        else SnapshotStatus.INCOMPLETE
    )
    snapshot.point_count = trailer.point_count
    snapshot.content_hash = trailer.content_hash
    snapshot.schema_hash = hash_canonical(
        {
            "target_id": schema.target_id,
            "scope": schema.scope,
            "point_id_type": schema.point_id_type,
            "vector_fields": [
                {"name": item.name, "dimension": item.dimension, "distance": item.distance}
                for item in schema.vector_fields
            ],
            "payload_indexes": list(schema.payload_indexes),
        }
    )
    snapshot.artifact_ref = f"artifacts/{stored.sha256}"
    if last_point_id is not None:
        snapshot.checkpoint = {"last_point_id": last_point_id}
    session.flush()
    logger.info(
        "snapshot %s of target %s: %d points, status=%s",
        snapshot.id,
        target.id,
        point_count,
        snapshot.status.value,
    )


# --------------------------------------------------------------------------
# Reconciliation (sections 9/14, FR-120..FR-135)
# --------------------------------------------------------------------------

_RECONCILE_SMALL_DATA_MAX_POINTS = 100_000


def _artifact_path(artifact_ref: str, settings: Settings) -> Path:
    digest = artifact_ref.rpartition("/")[2]
    path = Path(settings.artifact_store_root) / "artifacts" / digest
    if not path.is_file():
        raise PermanentJobError(f"artifact {artifact_ref!r} is not present in the store")
    return path


def _wrap_evidence(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return {"value": value}


def run_reconcile_job(session: Session, job: Job, *, settings: Settings) -> None:
    """Reconcile a manifest against a snapshot; persist findings and the full report.

    The same engine path as `ragledger reconcile --auto`: the in-memory
    join for ordinary snapshot sizes, falling back to the external
    sort/merge path above `_RECONCILE_SMALL_DATA_MAX_POINTS` points.
    The full `ReconciliationReport` (result + policy verdict +
    remediation plan) is content-addressed into the artifact store;
    `Finding` rows carry the bounded, queryable summary of each finding
    (FR-123), and a `PolicyEvaluation` row records the gate outcome
    when a policy revision was attached.
    """
    reconciliation_id = job.payload.get("reconciliation_id")
    reconciliation = session.get(Reconciliation, reconciliation_id)
    if reconciliation is None or reconciliation.workspace_id != job.workspace_id:
        raise PermanentJobError(f"reconciliation {reconciliation_id!r} not found in workspace")
    manifest_row = session.get(Manifest, reconciliation.manifest_id)
    snapshot_row = session.get(InventorySnapshot, reconciliation.snapshot_id)
    if manifest_row is None or snapshot_row is None:
        raise PermanentJobError("reconciliation references a missing manifest or snapshot")
    if snapshot_row.artifact_ref is None:
        raise PermanentJobError("snapshot has no artifact; it has not completed successfully")

    reconciliation.state = ReconciliationState.RUNNING
    session.flush()
    check_cancellation(session, job)

    envelope = load_manifest(_artifact_path(manifest_row.artifact_ref, settings))

    policy_document = None
    if reconciliation.policy_revision_id is not None:
        revision = session.get(PolicyRevision, reconciliation.policy_revision_id)
        if revision is None:
            raise PermanentJobError("reconciliation references a missing policy revision")
        try:
            policy_document = load_policy_document(
                json.dumps(revision.rules_json), document_format="json"
            )
        except (PolicyValidationError, ValueError) as exc:
            raise PermanentJobError(f"stored policy revision is invalid: {exc}") from exc

    connector = NdjsonConnector(_artifact_path(snapshot_row.artifact_ref, settings))
    try:
        connector.validate_configuration()
        schema = connector.inspect_target_schema()
        try:
            result = reconcile_small_data(
                envelope,
                connector,
                target=schema.target_id,
                scope=schema.scope,
                policy=policy_document,
                max_in_memory_points=_RECONCILE_SMALL_DATA_MAX_POINTS,
            )
        except ValueError:
            with tempfile.TemporaryDirectory(prefix="ragledger-reconcile-") as work_dir:
                result = reconcile_big_data(
                    envelope,
                    connector,
                    target=schema.target_id,
                    scope=schema.scope,
                    work_dir=Path(work_dir),
                    policy=policy_document,
                )
    finally:
        connector.close()

    if policy_document is not None:
        policy_verdict = evaluate_policy(policy_document, result)
    else:
        policy_verdict = PolicyVerdict(policy_name="(none)", verdict="PASS", rule_results=[])
    report = ReconciliationReport(
        result=result,
        policy=policy_verdict,
        remediation=build_remediation_plan(result.findings),
    )
    stored = ArtifactStore(settings.artifact_store_root).put(to_json_bytes(report))
    report_ref = f"artifacts/{stored.sha256}"

    findings_by_fingerprint = {finding.fingerprint: finding for finding in result.findings}
    for finding in findings_by_fingerprint.values():
        evidence = finding.evidence or {}
        session.add(
            FindingRow(
                workspace_id=reconciliation.workspace_id,
                reconciliation_id=reconciliation.id,
                fingerprint=finding.fingerprint,
                code=finding.code.value,
                severity=finding.severity,
                source_hash=finding.affected_lineage.source_id,
                chunk_hash=finding.affected_lineage.chunk_id,
                point_hash=(
                    hash_canonical(finding.locator.point_id)
                    if finding.locator.point_id is not None
                    else None
                ),
                expected_evidence=_wrap_evidence(evidence.get("expected")),
                observed_evidence=_wrap_evidence(
                    evidence.get("observed")
                    if "observed" in evidence or "expected" in evidence
                    else (evidence or None)
                ),
                artifact_ref=report_ref,
            )
        )

    if policy_document is not None and reconciliation.policy_revision_id is not None:
        # The DB gate enum is pass/warn/fail; INCONCLUSIVE gates as a
        # failure (it must not pass CI), with the true verdict preserved
        # in the evaluation's summary and the reconciliation summary.
        result_value = {
            "PASS": PolicyEvaluationResult.PASS,
            "WARN": PolicyEvaluationResult.WARN,
        }.get(policy_verdict.verdict, PolicyEvaluationResult.FAIL)
        session.add(
            PolicyEvaluation(
                workspace_id=reconciliation.workspace_id,
                reconciliation_id=reconciliation.id,
                policy_revision_id=reconciliation.policy_revision_id,
                result=result_value,
                summary={"verdict": policy_verdict.verdict},
            )
        )

    reconciliation.state = ReconciliationState.COMPLETED
    reconciliation.summary = {
        "verdict": policy_verdict.verdict,
        "finding_count": len(findings_by_fingerprint),
        "summary": result.summary.model_dump(mode="json"),
        "ratios": result.ratios.model_dump(mode="json"),
        "consistency": result.consistency.model_dump(mode="json"),
        "report_artifact": report_ref,
    }
    session.flush()
    logger.info(
        "reconciliation %s: %d findings, verdict=%s",
        reconciliation.id,
        len(findings_by_fingerprint),
        policy_verdict.verdict,
    )


def make_handlers(settings: Settings) -> dict[str, JobHandler]:
    """The job-type registry `run_pending_jobs` executes against."""
    return {
        JOB_TYPE_SCAN: lambda session, job: run_scan_job(session, job, settings=settings),
        JOB_TYPE_BUILD: lambda session, job: run_build_job(session, job, settings=settings),
        JOB_TYPE_SNAPSHOT: lambda session, job: run_snapshot_job(session, job, settings=settings),
        JOB_TYPE_RECONCILE: lambda session, job: run_reconcile_job(session, job, settings=settings),
    }


def _finalize_cancelled_build(session: Session, job: Job) -> None:
    build = session.get(Build, job.payload.get("build_id"))
    if build is not None and build.state in (BuildState.PENDING, BuildState.RUNNING):
        build.state = BuildState.CANCELLED
        build.completed_at = datetime.now(UTC)
        session.flush()


def _finalize_cancelled_snapshot(session: Session, job: Job) -> None:
    snapshot = session.get(InventorySnapshot, job.payload.get("snapshot_id"))
    if snapshot is not None and snapshot.status in (
        SnapshotStatus.PENDING,
        SnapshotStatus.RUNNING,
    ):
        snapshot.status = SnapshotStatus.CANCELLED
        session.flush()


def _finalize_cancelled_reconciliation(session: Session, job: Job) -> None:
    reconciliation = session.get(Reconciliation, job.payload.get("reconciliation_id"))
    if reconciliation is not None and reconciliation.state in (
        ReconciliationState.PENDING,
        ReconciliationState.RUNNING,
    ):
        reconciliation.state = ReconciliationState.CANCELLED
        session.flush()


def make_cancel_finalizers() -> dict[str, JobHandler]:
    """Per-job-type cleanup run after a cooperative cancellation is honored.

    A handler's in-flight writes roll back with the cancellation, so
    the related entity would otherwise be left in its pre-run state;
    these finalizers stamp the entity's own terminal `cancelled` status
    in the same transaction that marks the job `CANCELLED` (section
    21's cancellation contract: completed immutable artifacts remain,
    the entity's status says cancelled, and an audit of the request was
    already written by the `:cancel` endpoint).
    """
    return {
        JOB_TYPE_BUILD: _finalize_cancelled_build,
        JOB_TYPE_SNAPSHOT: _finalize_cancelled_snapshot,
        JOB_TYPE_RECONCILE: _finalize_cancelled_reconciliation,
    }
