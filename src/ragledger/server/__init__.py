"""RAG Knowledge Ledger server: persistence, auth, and workspace foundations.

the design specification milestone M7 ("Web/API/job orchestration") wave A. This
package adds the pieces a hosted service needs on top of the
deterministic core shipped in v0.1.x (`ragledger.core`,
`ragledger.pipeline`, `ragledger.governance`, `ragledger.connectors`,
`ragledger.reconcile`, `ragledger.cli`, `ragledger.reports`): typed
settings (`ragledger.server.settings`), a SQLAlchemy 2.0 persistence
layer with an Alembic migration (`ragledger.server.db`), API token and
credential-encryption primitives (`ragledger.server.security`), an
append-only audit event writer (`ragledger.server.audit`), and a FastAPI
application factory exposing only health/version endpoints for now
(`ragledger.server.app`).

The real HTTP API surface (build/snapshot/reconciliation endpoints,
job orchestration, SSE progress) is out of scope for this wave; see
the project status notes for what is and is not covered.
"""

from __future__ import annotations
