"""Declarative base, naming convention, and shared column mixins.

The naming convention gives every constraint and index a deterministic,
inspectable name (rather than the driver-assigned defaults, which vary
across Postgres versions and are hard to reference in a later Alembic
migration). `UUIDPrimaryKeyMixin` and `CreatedAtMixin` implement the
PROJECT_SPEC.md section 15.3 DB rules common to nearly every table:
internal UUIDv7 primary keys and UTC timestamps.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, MetaData, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

from ragledger.server.db.uuid7 import uuid7

__all__ = ["Base", "CreatedAtMixin", "UpdatedAtMixin", "UUIDPrimaryKeyMixin"]

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKeyMixin:
    """A UUIDv7 primary key column named ``id``, per section 15.3."""

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)


class CreatedAtMixin:
    """A non-null, server-defaulted, timezone-aware ``created_at`` column."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UpdatedAtMixin:
    """A non-null, server-defaulted, auto-updating ``updated_at`` column.

    Only mixed into tables that model mutable state (workspaces,
    memberships, API token revocation, target credential rotation, job
    status transitions); PROJECT_SPEC.md section 15.3's "Immutable
    entities update edilmez" tables (builds, manifests, findings, and
    so on) intentionally do not get this mixin.
    """

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
