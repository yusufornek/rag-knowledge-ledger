# M6 status notes: reconciliation and policy engine

Scope: `src/ragledger/reconcile/` (matching, taxonomy, engine, policy,
remediation, report) and `tests/reconcile/`. Not wired into the CLI (owned
by another agent); `ragledger.reconcile.report.exit_code_for` and
`render_ci_summary` exist specifically so that wiring is a thin adapter, not
a redesign.

## Taxonomy implemented (23 codes, spec section 9's table verbatim)

`MISSING_IN_INDEX`, `ORPHAN_IN_INDEX`, `STALE_SOURCE`, `STALE_PARSE`,
`STALE_CHUNKING`, `EMBEDDING_MODEL_MISMATCH`, `EMBEDDING_DIMENSION_MISMATCH`,
`VECTOR_HASH_MISMATCH`, `PAYLOAD_DRIFT`, `SOURCE_METADATA_MISSING`,
`DUPLICATE_POINT_ID`, `DUPLICATE_CONTENT`, `ACL_MISSING`,
`ACL_BROADER_THAN_SOURCE`, `ACL_MISMATCH`, `TENANT_MISSING`,
`TENANT_MISMATCH`, `PII_POLICY_VIOLATION`, `LICENSE_UNKNOWN`,
`LICENSE_POLICY_VIOLATION`, `UNVERIFIABLE_POINT`, `TARGET_SCHEMA_DRIFT`,
`MANIFEST_INCOMPLETE`, `SNAPSHOT_INCOMPLETE`.

These exactly match `docs/spec/policy-v1.schema.json`'s `$defs.taxonomyCode`
enum (verified by `tests/reconcile/test_taxonomy.py::test_taxonomy_matches_policy_schema_enum`),
not the task brief's paraphrased names (`STALE_PARSER`/`STALE_CHUNKER`/
`EMBEDDING_MODEL_MISMATCH` vs `STALE_EMBEDDING_MODEL`/`PAYLOAD_DRIFT` vs
`PAYLOAD_HASH_MISMATCH`/`DUPLICATE_POINT_ID` vs `DUPLICATE_POINT`/
`UNVERIFIABLE_POINT` vs `UNKNOWN_LINEAGE`): the task brief itself says to
verify exact names against spec section 9, and the schema is the more
precise, machine-checked source of truth for that table. `TARGET_SCHEMA_DRIFT`
is emitted for non-dimension schema differences the preflight check
detects (a named vector missing from the target, a distance metric
mismatch); pure dimension mismatches get the more specific, critical
`EMBEDDING_DIMENSION_MISMATCH` instead.

## Key interpretation decisions

**Matching order (section 9.1).** Implemented exactly as five levels (point
id, embedding id, chunk id, source version, content/payload hash), with
levels 1-3 closing (removing both sides from missing/orphan) and levels 4-5
suggestion-only, per the spec's own "low-confidence match missing/orphan'ı
otomatik kapatmaz, suggestion üretir." Two deviations from a literal
reading, both forced by what `NormalizedPoint` (this milestone's only
observed-side input, per `ragledger.connectors.base`) actually carries:

- Level 4 is "source version + locator" in the spec text, but
  `NormalizedPoint` has no locator field at all (the six logical payload
  fields `_PROJECTABLE_FIELDS` lists are source_id/source_version_id/
  chunk_id/embedding_id/tenant/acl -- no structural position). Level 4
  degrades to source-version-only matching. A future connector-level
  extension (a seventh mapped logical field for locator) would let this be
  implemented literally.
- Level 5 ("content hash heuristic") uses `NormalizedPoint.payload_hash`
  vs `IndexBinding.expected_payload_hash` -- the only content-derived hash
  surfaced on both the expected and observed side. Neither side exposes a
  raw chunk-text hash independent of the full payload projection.

**Finding fingerprint (section 14.5).** `derive_assertion_id("fnd", code,
target, scope, subject_id, affected_field)` -- the same
`<prefix>_sha256_<base32>` convention `ragledger.governance.identity` uses
for assertion ids, reused rather than reinvented. No timestamp, no message,
by construction: `build_finding` has no timestamp parameter at all. Verified
stable across evidence/detail changes and to change on every anchor field
in `tests/reconcile/test_taxonomy.py`.

**Ratios (section 14.4).** `ragledger.reconcile.report.ratio(n, d)` returns
`None` (not 0.0, not 1.0) for a zero denominator, and `Ratios` fields are
`float | None` throughout. `lineage_coverage` = verifiable observed / all
observed (verifiable = not `UNVERIFIABLE_POINT`); `acl_compliance`'s
denominator is matched pairs where the *expected* binding actually declared
an ACL projection (an unconfigured-ACL binding is not "required").

**Policy schema gaps (`docs/spec/policy-v1.schema.json`), not silently
worked around:**

- `$defs.rule` has no field naming which ratio a `category: "ratio"` rule
  checks, and no severity-value field for `category: "severity"` beyond
  `threshold`. `ragledger.reconcile.policy._evaluate_rule` implements a
  generic count-of-matching-findings evaluation (optionally filtered by
  `taxonomy_codes`, compared via `comparator`/`threshold`) for every
  category, with `severity` additionally reading `threshold` as a minimum
  severity rank (0=low..3=critical) when no `taxonomy_codes` are given.
  `source_path`/`media_type`/`age` rules' `pattern`/`max_age_days` fields
  are accepted (schema-valid) but not evaluated against anything --
  `Finding` does not carry a source URI or an age today, so there is
  nothing in a finding to compare a glob or a day count against. This is a
  real gap: a future schema revision should add `ratio_name`,
  `severity_at_least`, and reconciliation should attach `source_uri`/
  `observed_at` to relevant findings' evidence so these categories become
  meaningful, not just schema-valid.
- The schema has no field for the acceptance-scenario-D "report masking
  mode for principals" decision. `PolicyVerdict.principal_masking` is
  fixed to `"hash"`: `ragledger.reconcile.engine` always masks ACL
  principal identifiers in finding evidence via `taxonomy.mask_acl_entry`
  (HMAC-keyed when a workspace secret is supplied, else a plain SHA-256),
  never a raw passthrough mode. This satisfies the HARD RULE ("Findings
  never contain raw PII or raw principals") unconditionally rather than
  making it policy-configurable; a real "policy decides" mode would need a
  new schema field plus a documented threat-model justification for ever
  allowing raw principals into a report, which felt out of scope to add
  unilaterally.

**Staleness classification without a previous-manifest input.** `STALE_SOURCE`
is detected purely from the *current* manifest: an observed point's payload
carries a `source_version_id` (a reserved payload key connectors write at
snapshot time, per `ragledger.core.models.ChunkMetadata`'s docstring), and
if it disagrees with the source's *current* `version_id`, that alone is
enough -- no history lookup needed. Distinguishing `STALE_PARSE` from
`STALE_CHUNKING` for an orphan whose source version is unchanged but whose
chunk id is unknown genuinely requires knowing the *previous* parser/
chunker config to tell which one changed; without that input, `engine.py`
classifies this case as `STALE_CHUNKING` (documented as a conservative
default in `_classify_orphan`'s and `_compare_matched_pair`'s docstrings/
comments) rather than guessing. A `previous_manifest` parameter threading
real historical parse-run lookups through would resolve this precisely; not
implemented here for time-budget reasons. Section 28 scenario B's "tombstone
evidence" is similarly a hint (`evidence["tombstone_hint"] = True` when an
observed source id is not found among the current manifest's sources at
all), not a previous-manifest-confirmed tombstone status lookup.

**Big-data path memory bound.** External sort/merge is implemented per
section 14.2: both expected bindings and the observed connector stream are
spilled to chunk-size-bounded sorted run files and k-way merged
(`heapq.merge`) at each of the three closing matching levels; leftovers
between rounds are streamed directly into the next round's spill writer
(`engine._ChunkedRunWriter`), never fully materialized as a Python list.
This is verified functionally (100k+ points, correct findings, well under a
30-second budget -- `tests/reconcile/test_engine_big_data.py`) and by
construction (chunk-size-bounded buffers), not by an explicit peak-RSS
measurement in CI; a real memory-ceiling assertion would need a
platform-specific RSS sampler, which felt like scope creep for a milestone
already this large. One known non-bound: levels 4-5 (relocation
suggestions) run in memory over the *final* leftover pool after level 3,
which is normally small (genuine anomalies) but is not itself
externally-sorted -- a pathological all-missing/all-orphan dataset would
not stay memory-bounded through the suggestion pass. Internal spill-file
serialization deliberately uses plain `json`/pydantic's `model_dump_json`
rather than `ragledger.core.canonical.canonical_bytes`: RFC 8785's
UTF-16-key-ordering pass is the right choice for a manifest's cross-process
content identity, but pure overhead for scratch files this process both
writes and reads within one call (see `matching.expected_point_to_json_bytes`'s
docstring) -- switching this cut the 100k-point benchmark from ~18s to ~6s.

**Cancel/restart idempotence.** `reconcile_big_data` sweeps its
caller-supplied `work_dir` at the START of every call (idempotent restart:
a prior crashed attempt's leftover spill files are cleared before fresh
work begins) and again after a SUCCESSFUL run (self-cleanup). It does
*not* wrap the run body in a `finally`-based cleanup: an exception mid-run
leaves that run's spill files on disk, which is what makes the start-of-run
sweep meaningful to test (`tests/reconcile/test_engine_big_data.py::test_cancel_restart_idempotence`
injects a real exception mid-merge via monkeypatch and asserts both the
leftover-after-crash and clean-after-rerun properties).

**FR-125 (idempotent result cache) and FR-126 (new/resolved/persistent
finding history)** are explicitly out of scope here: `Finding.fingerprint`
is stable across reruns of the same logical input, which is the primitive
a cache/diff layer would need, but no caching or persistence/history layer
exists in this package -- that reads as a database/reporting-service
concern (section 15's "domain and database", not built yet) layered on top
of this deterministic, in-process engine.

**Scenario F (signing)** is only partially exercised: reconciliation checks
signature *presence* (`Summary.manifest_signed`) against
`PolicyDocument.requirements.manifest_signature`, which is what a
reconciliation-time policy gate can meaningfully do. Cryptographic
tamper/verify-integrity and the "unknown key cryptographically valid but
policy untrusted" trust-store distinction are `ragledger.core.signing`'s
already-implemented, already-tested responsibility from an earlier
milestone, not duplicated here.

## Verification

- `uv run ruff check src/ragledger/reconcile tests/reconcile` -- clean.
- `uv run ruff format --check src/ragledger/reconcile tests/reconcile` -- clean.
- `uv run mypy src/ragledger/reconcile` -- clean (strict mode, 7 source files).
- `uv run pytest tests/reconcile -q` -- 115 passed, ~17-30s wall clock
  (100k-point big-data test included). Per-file coverage:
  `engine.py` 94%, `matching.py` 93%, `policy.py` 94%, `remediation.py`
  100%, `report.py` 99%, `taxonomy.py` 98%, `__init__.py` 100%.

## Files

- `src/ragledger/reconcile/matching.py` -- section 9.1 matching order,
  `stream_merge_join` (the one algorithm both engine paths share),
  `ExpectedPoint` and its manifest resolution/spill-file (de)serialization.
- `src/ragledger/reconcile/taxonomy.py` -- `FindingCode`/`FindingSeverity`,
  `Finding`, fingerprinting, ACL principal masking.
- `src/ragledger/reconcile/report.py` -- `Ratios`, `ConsistencyCaveat`,
  `ReconciliationResult`, `PolicyVerdict`, `RemediationPlan`,
  `ReconciliationReport`, canonical/CI-text/exit-code renderings.
- `src/ragledger/reconcile/engine.py` -- `reconcile_small_data`,
  `reconcile_big_data`, preflight schema check, matched-pair field
  comparison, missing/orphan classification, duplicate detection.
- `src/ragledger/reconcile/policy.py` -- `PolicyDocument` model (mirrors
  the schema), `load_policy_document`, `evaluate_policy`.
- `src/ragledger/reconcile/remediation.py` -- `build_remediation_plan`
  (read-only; never touches a target).
- `tests/reconcile/builders.py` -- shared manifest/connector test builders
  (named `builders.py`, not `conftest.py`, matching `tests/core/golden_fixtures.py`'s
  convention: a plain importable module, not pytest's special file, since
  `tests/reconcile` has no `__init__.py` and pytest's rootdir-insertion
  import mode makes `builders` importable directly by sibling test modules).
- `tests/reconcile/test_matching.py`, `test_taxonomy.py`, `test_report.py`,
  `test_policy.py`, `test_remediation.py`, `test_engine.py`,
  `test_engine_big_data.py`, `test_engine_equivalence.py`,
  `test_acceptance_scenarios.py`, `test_pii_masking_canary.py`.
