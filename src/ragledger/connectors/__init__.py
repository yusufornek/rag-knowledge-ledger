"""Read-only vector target connectors and the NDJSON snapshot format.

Per PROJECT_SPEC.md section 13, this package implements:

- `ragledger.connectors.base`: the `VectorTargetConnector` interface
  and the vendor-neutral `NormalizedPoint` shape every connector
  reduces an observed index point to.
- `ragledger.connectors.config`: target configuration models (section
  35) and environment-variable credential resolution.
- `ragledger.connectors.qdrant`: a Qdrant REST connector built on
  `httpx`.
- `ragledger.connectors.pgvector`: a pgvector connector built on
  `psycopg`.
- `ragledger.connectors.ndjson`: the `.ndjson.zst` snapshot writer/
  reader (section 13.5), and `NdjsonConnector`, which replays a
  snapshot file as a `VectorTargetConnector`.

Every connector here is read-only by construction (no method on
`VectorTargetConnector` can express a mutation) and, for the two live
targets, additionally enforced at the transport layer per section
42.2 -- see `ragledger.connectors.qdrant._guard_request` and
`ragledger.connectors.pgvector._assert_read_only_statement`.
"""

from __future__ import annotations

from ragledger.connectors.base import (
    Checkpoint,
    ConnectionTestResult,
    ConnectorCapabilities,
    ConnectorConfigError,
    ConnectorConnectionError,
    ConnectorError,
    ConnectorMutationBlockedError,
    ConsistencyInfo,
    ConsistencyMode,
    NormalizedPoint,
    SnapshotCompleteness,
    TargetSchema,
    VectorFieldSchema,
    VectorTargetConnector,
)
from ragledger.connectors.config import (
    PgvectorTargetConfig,
    QdrantTargetConfig,
    resolve_env_credential,
    run_preflight,
)
from ragledger.connectors.ndjson import (
    NdjsonConnector,
    SnapshotHeader,
    SnapshotIntegrityError,
    SnapshotReader,
    SnapshotTrailer,
    write_snapshot,
)
from ragledger.connectors.pgvector import PgvectorConnector
from ragledger.connectors.qdrant import QdrantConnector

__all__ = [
    "Checkpoint",
    "ConnectionTestResult",
    "ConnectorCapabilities",
    "ConnectorConfigError",
    "ConnectorConnectionError",
    "ConnectorError",
    "ConnectorMutationBlockedError",
    "ConsistencyInfo",
    "ConsistencyMode",
    "NdjsonConnector",
    "NormalizedPoint",
    "PgvectorConnector",
    "PgvectorTargetConfig",
    "QdrantConnector",
    "QdrantTargetConfig",
    "SnapshotCompleteness",
    "SnapshotHeader",
    "SnapshotIntegrityError",
    "SnapshotReader",
    "SnapshotTrailer",
    "TargetSchema",
    "VectorFieldSchema",
    "VectorTargetConnector",
    "resolve_env_credential",
    "run_preflight",
    "write_snapshot",
]
