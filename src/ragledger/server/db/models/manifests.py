"""`Manifest` and `Signature`, per the design specification section 15.1/36.1/36.4.

Only manifest *metadata* and an *artifact reference* live here, per
this wave's explicit scope: the full manifest JSON (potentially large,
per section 36.2's inline-vs-sharded records) stays content-addressed
on disk/object storage through the existing
`ragledger.core.artifacts.ArtifactStore`, addressed by `artifact_ref`.
`manifest_hash` is the RFC 8785 canonical hash
`ragledger.core.manifest.compute_manifest_hash` produces, and is this
row's portable identity (section 15.3: "(workspace_id, portable_id)
unique").
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ragledger.server.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from ragledger.server.db.models.enums import ManifestStatus, enum_values

__all__ = ["Manifest", "ManifestSignature"]


class Manifest(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """Immutable once created; a new release is always a new row, never an update."""

    __tablename__ = "manifests"
    __table_args__ = (
        UniqueConstraint("workspace_id", "manifest_hash"),
        Index(
            "ix_manifests_workspace_namespace_created", "workspace_id", "namespace", "created_at"
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    build_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("builds.id", ondelete="SET NULL"), nullable=True
    )
    namespace: Mapped[str] = mapped_column(String(255), nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[ManifestStatus] = mapped_column(
        SqlEnum(
            ManifestStatus, name="manifest_status", native_enum=True, values_callable=enum_values
        ),
        nullable=False,
        default=ManifestStatus.ACTIVE,
    )
    artifact_ref: Mapped[str] = mapped_column(String(1024), nullable=False)
    profile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    chunk_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    signed: Mapped[bool] = mapped_column(nullable=False, default=False)

    signatures: Mapped[list[ManifestSignature]] = relationship(
        back_populates="manifest", cascade="all, delete-orphan"
    )


class ManifestSignature(UUIDPrimaryKeyMixin, CreatedAtMixin, Base):
    """One attached `ragledger.core.models.SignatureRecord`, mirrored for queryability."""

    __tablename__ = "manifest_signatures"
    __table_args__ = (UniqueConstraint("manifest_id", "key_id", "signature"),)

    manifest_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("manifests.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(512), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    issuer: Mapped[str | None] = mapped_column(String(255), nullable=True)

    manifest: Mapped[Manifest] = relationship(back_populates="signatures")
