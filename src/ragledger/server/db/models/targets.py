"""`VectorTarget` and `InventorySnapshot`, per PROJECT_SPEC.md section 15.1/19/36.1/36.4.

`VectorTarget.credential_ciphertext` is the AES-GCM-encrypted connector
credential FR-003/section 19.2 require ("Target credential theft: AES-GCM,
write-only, master key secret, rotation"); it is produced by
`ragledger.server.security.encrypt_credential` and is never decrypted
by this module or returned by any API layer -- only a connector worker
with a legitimate reason should ever call `decrypt_credential` on it.
`credential_key_id`/`credential_version` are denormalized, non-secret
columns that let an operator audit which encryption key and rotation
generation protects a given target without decrypting anything.

`endpoint_redacted` is deliberately not the full connection string:
section 19.2 requires the *credential* never appear even in this
metadata row, so only a display-safe host/scheme summary is stored
here (whatever the caller building the row chooses to keep display-safe
is that caller's responsibility; this column has no built-in
redaction logic of its own).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ragledger.server.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin
from ragledger.server.db.models.enums import SnapshotStatus, VectorTargetType, enum_values

__all__ = ["InventorySnapshot", "VectorTarget"]


class VectorTarget(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "vector_targets"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    target_type: Mapped[VectorTargetType] = mapped_column(
        SqlEnum(
            VectorTargetType,
            name="vector_target_type",
            native_enum=True,
            values_callable=enum_values,
        ),
        nullable=False,
    )
    endpoint_redacted: Mapped[str] = mapped_column(String(512), nullable=False)
    mapping_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    credential_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    credential_key_id: Mapped[str] = mapped_column(String(16), nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    allowlist_decision: Mapped[str | None] = mapped_column(String(64), nullable=True)


class InventorySnapshot(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    __tablename__ = "inventory_snapshots"
    __table_args__ = (Index("ix_inventory_snapshots_target_created", "target_id", "created_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vector_targets.id", ondelete="CASCADE"), nullable=False
    )
    schema_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[SnapshotStatus] = mapped_column(
        SqlEnum(
            SnapshotStatus, name="snapshot_status", native_enum=True, values_callable=enum_values
        ),
        nullable=False,
        default=SnapshotStatus.PENDING,
    )
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    artifact_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    point_count: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
