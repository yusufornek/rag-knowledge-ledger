"""Source discovery and pipeline configuration entities (design specification 15.1/36.1/36.4).

`SourceCollection` is a source namespace/root config; `SourceAsset` is a
logical source (a stable identity across content changes);
`SourceVersion` is one exact set of bytes for that logical source, its
`portable_id` matching `ragledger.core.ids.source_version_id`.
`PipelineConfig` is the immutable parser/chunker/embed/governance
configuration a build runs against, keyed by its secret-free canonical
JSON hash.

All four are immutable once written except `SourceCollection` (root
config can be edited between builds) and `SourceAsset` (its `status`
transitions as discovery observes create/rename/tombstone events, per
FR-017); section 15.3's "Immutable entities update edilmez" applies to
`SourceVersion` and `PipelineConfig` rows themselves.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ragledger.server.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin

__all__ = ["PipelineConfig", "SourceAsset", "SourceCollection", "SourceVersion"]


class SourceCollection(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """A source namespace/root config a workspace discovers sources under."""

    __tablename__ = "source_collections"
    __table_args__ = (UniqueConstraint("workspace_id", "namespace"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    root_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class SourceAsset(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """A logical source: a stable identity that persists across content revisions."""

    __tablename__ = "source_assets"
    __table_args__ = (
        UniqueConstraint("workspace_id", "portable_id"),
        Index("ix_source_assets_collection_uri", "collection_id", "uri"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_collections.id", ondelete="CASCADE"), nullable=False
    )
    portable_id: Mapped[str] = mapped_column(String(80), nullable=False)
    uri: Mapped[str] = mapped_column(String(4096), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class SourceVersion(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One exact set of bytes for a `SourceAsset`. Immutable; never updated in place."""

    __tablename__ = "source_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "portable_id"),
        Index("ix_source_versions_asset_content_hash", "source_asset_id", "content_hash"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_assets.id", ondelete="CASCADE"), nullable=False
    )
    portable_id: Mapped[str] = mapped_column(String(80), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_ref: Mapped[str] = mapped_column(String(1024), nullable=False)


class PipelineConfig(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """An immutable parser/chunker/embed/governance configuration, keyed by its content hash."""

    __tablename__ = "pipeline_configs"
    __table_args__ = (UniqueConstraint("workspace_id", "config_hash"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
