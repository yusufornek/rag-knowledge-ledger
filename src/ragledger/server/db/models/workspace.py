"""Auth and workspace isolation entities, per the design specification section 15.1 group 1.

`User`, `Workspace`, `Membership`, `ApiToken` -- "Auth/izolasyon". Every
other domain table in this package carries a `workspace_id` foreign
key back to `Workspace` (denormalized directly onto the row, not only
reachable through a join chain), so that a repository method can
enforce the design specification section 15.3's "Cross-workspace repository
methods mandatory" rule with a single `WHERE workspace_id = :id`
clause regardless of how deep the entity sits in the domain graph.

`ApiToken` never stores the bearable secret itself: `selector` is a
public, indexed lookup value, and `salt`/`token_hash` are what
`ragledger.server.security.verify_api_token` checks the presented
secret against. See that module for the generation/verification
implementation FR-002 and section 19 require.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, DateTime, ForeignKey, LargeBinary, String, UniqueConstraint
from sqlalchemy import Enum as SqlEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ragledger.server.db.base import Base, CreatedAtMixin, UpdatedAtMixin, UUIDPrimaryKeyMixin
from ragledger.server.db.models.enums import MembershipRole, enum_values

__all__ = ["ApiToken", "Membership", "User", "Workspace"]


class Workspace(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """FR-001: a workspace is the unit of isolation every scoped entity hangs off."""

    __tablename__ = "workspaces"

    slug: Mapped[str] = mapped_column(String(63), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )
    api_tokens: Mapped[list[ApiToken]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class User(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """A local account. FR-001's "local admin bootstrap" creates the first row here."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Membership(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """A user's role within one workspace. FR-001: owner/editor/viewer."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[MembershipRole] = mapped_column(
        SqlEnum(
            MembershipRole, name="membership_role", native_enum=True, values_callable=enum_values
        ),
        nullable=False,
    )

    workspace: Mapped[Workspace] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="memberships")


class ApiToken(UUIDPrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, Base):
    """FR-002: a scoped API token. The bearable secret is never stored, only its salted hash.

    See `ragledger.server.security.issue_api_token`/`verify_api_token`.
    """

    __tablename__ = "api_tokens"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    selector: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    salt: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(String(32)), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    workspace: Mapped[Workspace] = relationship(back_populates="api_tokens")
