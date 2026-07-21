"""Target configuration loading and connector construction for `target add`/`snapshot`.

Extends `ragledger.connectors.config`'s two spec-defined target shapes
(`QdrantTargetConfig`, `PgvectorTargetConfig`) with a third, CLI-only
`type: ndjson` shape that points at an already-written `.ndjson.zst`
snapshot file and is read back through
`ragledger.connectors.ndjson.NdjsonConnector`. This is what lets
`ragledger snapshot` be exercised end to end in this milestone's test
suite against the committed NDJSON fixtures with no live Qdrant/pgvector
service, matching `NdjsonConnector`'s own stated purpose: replaying a
committed snapshot exactly like a live connector.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from ragledger.connectors.base import VectorTargetConnector
from ragledger.connectors.config import PgvectorTargetConfig, QdrantTargetConfig
from ragledger.connectors.ndjson import NdjsonConnector
from ragledger.connectors.pgvector import PgvectorConnector
from ragledger.connectors.qdrant import QdrantConnector
from ragledger.core.models import RagledgerModel


class TargetConfigError(ValueError):
    """Raised for a malformed or unrecognized target configuration file."""


class NdjsonSourceConfig(RagledgerModel):
    """A CLI-only target shape: replay an existing `.ndjson.zst` snapshot file.

    Not part of PROJECT_SPEC.md section 35's Qdrant/pgvector target
    config shapes; exists purely so `ragledger snapshot` has a
    network-free, live-service-free source to run against in tests and
    offline/air-gapped use (the same rationale `ragledger.connectors.ndjson`
    itself documents).
    """

    type: Literal["ndjson"] = "ndjson"
    path: str


TargetConfigUnion = QdrantTargetConfig | PgvectorTargetConfig | NdjsonSourceConfig

_LOADERS: dict[str, type[QdrantTargetConfig | PgvectorTargetConfig | NdjsonSourceConfig]] = {
    "qdrant": QdrantTargetConfig,
    "pgvector": PgvectorTargetConfig,
    "ndjson": NdjsonSourceConfig,
}


def load_target_config(path: Path) -> TargetConfigUnion:
    """Load and validate a target config YAML file, dispatching on its `type` field."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TargetConfigError(f"cannot read target config {path}: {exc}") from exc
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TargetConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise TargetConfigError(f"{path} must contain a YAML mapping at the top level")
    target_type = data.get("type")
    model = _LOADERS.get(target_type) if isinstance(target_type, str) else None
    if model is None:
        raise TargetConfigError(
            f"{path} has unknown or missing 'type' {target_type!r}; expected one of "
            f"{sorted(_LOADERS)}"
        )
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise TargetConfigError(f"{path} failed validation:\n{exc}") from exc


def build_connector(config: TargetConfigUnion) -> VectorTargetConnector[Any]:
    """Construct the live-target or NDJSON-replay connector for a loaded target config."""
    if isinstance(config, QdrantTargetConfig):
        return QdrantConnector(config)
    if isinstance(config, PgvectorTargetConfig):
        return PgvectorConnector(config)
    return NdjsonConnector(Path(config.path))


def predicted_consistency_mode(config: TargetConfigUnion) -> str:
    """Return the `ConsistencyMode` value this target predictably produces.

    Deterministic given config/connector type alone (never a guess about
    a specific pass's outcome): Qdrant's scroll API gives no snapshot
    isolation (`best_effort_live`); pgvector's mode is exactly its
    configured `consistency` setting; `NdjsonConnector` always reports
    `strict_consistent` (sequential replay of an already-immutable
    file). Used to populate `SnapshotHeader.consistency_mode` before a
    pass starts, since the header is the first line written and the
    connector's own `ConsistencyInfo` is only available afterward.
    """
    if isinstance(config, QdrantTargetConfig):
        return "best_effort_live"
    if isinstance(config, PgvectorTargetConfig):
        return config.consistency
    return "strict_consistent"
