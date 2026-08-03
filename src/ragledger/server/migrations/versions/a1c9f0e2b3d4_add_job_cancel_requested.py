"""add jobs.cancel_requested

Cooperative cancellation (PROJECT_SPEC.md section 21): the API's
`:cancel` endpoints set this flag; a running worker polls it between
units of work and aborts cleanly.

Guarded: the baseline migration (d752b43ec01f) renders tables directly
from the current ORM metadata, so a *fresh* database already gets this
column at baseline time and this migration must be a no-op there. Only
a database migrated before this column existed actually needs the
ALTER. The inspector check keeps both paths correct.

Revision ID: a1c9f0e2b3d4
Revises: d752b43ec01f
Create Date: 2026-08-04

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c9f0e2b3d4"
down_revision: str | Sequence[str] | None = "d752b43ec01f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists() -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(column["name"] == "cancel_requested" for column in inspector.get_columns("jobs"))


def upgrade() -> None:
    if not _column_exists():
        op.add_column(
            "jobs",
            sa.Column(
                "cancel_requested",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    if _column_exists():
        op.drop_column("jobs", "cancel_requested")
