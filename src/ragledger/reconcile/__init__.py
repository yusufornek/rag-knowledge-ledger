"""Reconciliation and policy engine (M6), per PROJECT_SPEC.md sections 8.13, 8.14, 9, and 14.

This package compares a manifest's *expected* index state (index bindings
joined with their embedding/chunk/source lineage) against a target's
*observed* state (a stream of `NormalizedPoint` records from any
`ragledger.connectors.base.VectorTargetConnector`, including the NDJSON
snapshot replay connector) and produces a deterministic, streaming-safe
reconciliation report.

- `ragledger.reconcile.matching`: the section 9.1 matching order and the
  single sort-merge-join algorithm both the small-data and big-data engine
  paths use.
- `ragledger.reconcile.taxonomy`: the finding taxonomy (section 9), finding
  model, and fingerprint (section 14.5).
- `ragledger.reconcile.report`: the reconciliation result/report models
  (summary, ratios, findings, policy verdict, remediation plan) and their
  canonical/CI-text/exit-code renderings.
- `ragledger.reconcile.engine`: the section 14 reconciliation algorithm
  (small-data in-memory and big-data external-merge paths).
- `ragledger.reconcile.policy`: policy v1 loading (against
  `docs/spec/policy-v1.schema.json`) and evaluation (section 12.4, FR-130..FR-135).
- `ragledger.reconcile.remediation`: read-only remediation plan construction
  (FR-133..FR-135); nothing in this package ever mutates a target.
"""

from __future__ import annotations
