"""initial schema

Creates every table `ragledger.server.db.models` declares (see that
package's `__init__.py` for the full list): workspaces/users/
memberships/api_tokens, source_collections/source_assets/
source_versions/pipeline_configs, builds, manifests/manifest_signatures,
vector_targets/inventory_snapshots, reconciliations/findings/
lineage_index, policies/policy_revisions/policy_evaluations,
audit_events, and jobs -- the design specification section 15.1/36.1's entity
list for this wave.

This baseline migration is generated directly from the ORM metadata
(`Base.metadata.create_all`/`drop_all`) rather than a hand-transcribed
sequence of `op.create_table` calls, so it can never drift from what
`ragledger.server.db.models` actually declares: the models are the
single source of truth, and this migration is a mechanical rendering
of them. Later migrations (schema changes on top of this baseline)
should use the usual explicit `op.*` operations instead.

Revision ID: d752b43ec01f
Revises:
Create Date: 2026-07-21 05:32:47.933172

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from ragledger.server.db import models as models  # noqa: F401  (registers all tables)
from ragledger.server.db.base import Base

# revision identifiers, used by Alembic.
revision: str = "d752b43ec01f"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create every table declared on `Base.metadata`."""
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    """Drop every table declared on `Base.metadata`."""
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=False)
