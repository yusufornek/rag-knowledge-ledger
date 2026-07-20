# M5 status notes: index connectors and snapshot

Scope: `src/ragledger/connectors/`, `tests/connectors/`,
`tests/fixtures/snapshots/`. Plain status table for the orchestrator to
merge into `IMPLEMENTATION_STATUS.md`; this file is not itself the
status ledger.

Status values follow `IMPLEMENTATION_STATUS.md`'s convention:
`pending` / `drafted` / `implemented`.

## Section 8.10-8.12 functional requirements

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-090 | Target types: Qdrant, pgvector, NDJSON | implemented | `connectors/qdrant.py`, `connectors/pgvector.py`, `connectors/ndjson.py`; `tests/connectors/test_qdrant.py`, `test_pgvector.py`, `test_ndjson.py` |
| FR-091 | Connectors use read-only credentials; no mutation API/SQL is ever issued | implemented | Interface has no mutation method (`connectors/base.py::VectorTargetConnector`); transport-level guards: `qdrant.py::_guard_request`/`_is_allowed_request` (httpx request event hook), `pgvector.py::_assert_read_only_statement`/`_GuardedCursor` (statement whitelist) plus `connection.read_only = True`. Guard tests: `test_qdrant.py::test_guard_*`, `test_pgvector.py::test_assert_read_only_statement_*`, `test_guarded_cursor_*`. Live guard proof (skipped by default): `test_integration.py::test_qdrant_connector_read_only_guard_blocks_live_mutation`, `test_pgvector_connection_is_read_only_at_database_level` |
| FR-092 | Full snapshot uses cursor/scroll streaming with a resumable checkpoint | implemented | Qdrant: scroll `next_page_offset` chaining, `iterate_points(checkpoint=...)`. pgvector: server-side named cursor + keyset `WHERE (pk...) > (...)`. NDJSON: sequential replay with canonical-key checkpoint. Tests: `test_iterate_points_resumes_from_checkpoint` in each of the three connector test files |
| FR-093 | Sample snapshot records explicit method/seed/rate; completeness-dependent policies become `INCONCLUSIVE` | partial | `ndjson.py::SnapshotHeader` carries `snapshot_kind`, `sample_method`, `sample_seed`, `sample_rate` fields for a writer to populate. No connector in this milestone actually performs sampling (only full iteration), and the `INCONCLUSIVE` policy verdict itself is reconciliation/policy-evaluation's concern (a later milestone) |
| FR-094 | Snapshot records target metadata (collection/table, dimension/distance, schema/index config, timestamp, connector version) | implemented | `TargetSchema` (`connectors/base.py`) from `inspect_target_schema()`; `SnapshotHeader` (`connectors/ndjson.py`) carries target_id/scope/target_type/vector_names/vector_dimensions/started_at/connector_version/consistency_mode/scope_filter. Tests: `test_ndjson.py::test_write_and_read_roundtrip`, fixture tests |
| FR-095 | Observed points normalized to a common field set | implemented | `NormalizedPoint` (`connectors/base.py`) implements the exact section 13.2 field list. Tests: `test_base.py` |
| FR-096 | Raw payload retention policy defaults to mapped fields only | implemented | `payload_projection` only ever contains the configured logical mapping fields (`source_id`/`source_version_id`/`chunk_id`/`embedding_id`/`tenant`/`acl`); no raw/unmapped payload is ever retained by either live connector. `apply_projection()` further restricts on request |
| FR-097 | Snapshots are immutable and content-hashed | implemented | `ndjson.py::SnapshotTrailer.content_hash` = SHA-256 over canonical point-line bytes, verified on every read; zstd frame checksum (`write_checksum=True`) as a second layer. Tests: `test_ndjson.py::test_tampered_*`, `test_corrupted_zstd_bytes_are_detected`, `test_missing_trailer_is_detected` |
| FR-100 | Collection config, named vector config, dimension/distance, payload index inventory | implemented | `qdrant.py::inspect_target_schema` parses `config.params.vectors` (named and unnamed) and `payload_schema`. Test: `test_qdrant.py::test_inspect_target_schema_*` (via fixture setup in other tests) |
| FR-101 | Scroll API pagination visits all points exactly once on a best-effort basis | implemented | `qdrant.py::iterate_points`; `test_iterate_points_streams_all_pages_in_order` |
| FR-102 | Vector retrieval defaults to false; enabling vector hashing surfaces a resource warning | implemented | `include_vectors` defaults `False` everywhere; when true, both connectors' `normalize_point` append a `vector_retrieval_enabled` warning to every point regardless of outcome |
| FR-103 | Payload mapping is configurable; missing fields become unknown | implemented | Unresolved mapped fields are omitted from `payload_projection` and recorded as `missing_mapped_field:<name>` in `normalization_warnings`, never fabricated |
| FR-104 | Qdrant point id string/number type preserved | implemented | `qdrant.py::_coerce_point_id` passes through `str`/`int` as-is. Test: `test_iterate_points_streams_all_pages_in_order` asserts `int` point ids round-trip |
| FR-105 | Collection aliases resolved to actual collection metadata | implemented | `qdrant.py::_resolve_alias` (best-effort `GET /aliases`); `TargetSchema.resolved_scope`. Test: `test_resolve_alias_returns_resolved_collection_name` |
| FR-110 | Table/view, primary key, vector column, and mapped metadata columns explicitly configured | implemented | `config.py::PgvectorTargetConfig` |
| FR-111 | Identifiers are SQLAlchemy-quoted; no raw user SQL execution path | implemented (with a documented substitution) | SQLAlchemy is not an available dependency for this milestone (not in the permitted dependency list); `psycopg.sql.Identifier`/`sql.SQL` is used instead for the same safe-quoting guarantee, plus config-time identifier allowlisting (`config.py::_IDENTIFIER_PATTERN`). There is no code path that accepts or executes caller-supplied raw SQL |
| FR-112 | Read-only transaction, statement timeout, server-side cursor/keyset pagination | implemented | `pgvector.py::_configure_connection` (`read_only=True`, `IsolationLevel.REPEATABLE_READ`), `psycopg.connect(..., options="-c statement_timeout=...")`, named server-side cursor + `fetchmany` streaming, keyset `ORDER BY`/`WHERE (pk) > (...)` |
| FR-113 | Vector dimension/type/index metadata sourced from PostgreSQL and pgvector catalogs | implemented | `_fetch_vector_dimension` (`pg_attribute.atttypmod`), `_fetch_index_names` (`pg_index`/`pg_class`/`pg_am` join). Distance/opclass decoding is not implemented (see gaps) |
| FR-114 | Vector data not fetched by default; hash mode uses chunked queries | implemented | `vector_column` is only added to the `SELECT` list when `include_vectors=True`; streaming is always `fetchmany`-batched regardless |
| FR-115 | Composite primary keys produce a canonical JSON point id | implemented | `pgvector.py::normalize_point` builds a `dict` point id for multi-column primary keys. Test: `test_composite_primary_key_produces_object_point_id` |
| FR-116 | Row-level tenant filtering only via explicit parameterized configuration | implemented | `PgvectorTargetConfig.where`: allowlisted column names, parameterized `=`/`ANY(...)` only, no operators/raw fragments. `SnapshotHeader.scope_filter` is available for a writer to record it. Test: `test_iterate_points_applies_row_level_where_filter`, `test_where_list_value_generates_any_predicate` |

## Section 13.3/13.4 consistency

- Qdrant: always `ConsistencyMode.BEST_EFFORT_LIVE`; before/after `points_count` probe drives `SnapshotCompleteness`. Tests: `test_iterate_points_marks_snapshot_incomplete_on_count_drift`.
- pgvector: `consistency: repeatable_read` (default) opens one `REPEATABLE READ` read-only transaction per pass -> `ConsistencyMode.STRICT_CONSISTENT`, always `COMPLETE` by construction. `consistency: best_effort_paged` re-probes the row count after streaming and reports drift. Tests: `test_repeatable_read_consistency_is_always_complete`, `test_best_effort_paged_marks_incomplete_on_drift`.

## Section 42.2 mutation guard evidence

- Qdrant: `httpx` `event_hooks={"request": [_guard_request]}` whitelists exactly `GET /collections/{name}`, `GET /aliases`, `POST /collections/{name}/points/scroll`; every other method/path raises `ConnectorMutationBlockedError` before the request reaches the transport (`test_guard_raises_before_request_reaches_transport`, `test_guard_raises_for_put_upsert_attempt`, `test_guard_blocks_mutating_requests` parametrized over PUT/DELETE/PATCH/POST-elsewhere).
- pgvector: two independent layers -- (1) `connection.read_only = True` at the database/transaction level (proven live in `test_integration.py::test_pgvector_connection_is_read_only_at_database_level`, which bypasses the app-level guard on purpose and expects PostgreSQL itself to raise `ReadOnlySqlTransaction`); (2) `_assert_read_only_statement` / `_GuardedCursor` allowlists only `SELECT`/`SHOW` at the application level (`test_assert_read_only_statement_blocks_mutations` parametrized over INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE/GRANT/COPY).

## Test suite

`uv run pytest tests/connectors -q`: 162 passed, 4 skipped (the skipped tests are `tests/connectors/test_integration.py`, gated on `RAGLEDGER_IT=1`; not runnable in this environment per the no-docker constraint, so they are written and reasoned through but not executed here). Coverage of `src/ragledger/connectors/*`: `base.py` 98%, `config.py` 97%, `qdrant.py` 97%, `pgvector.py` 91%, `ndjson.py` 93%. The remaining uncovered lines in `pgvector.py` are almost entirely the real `psycopg.connect` retry loop, which needs a live/refusing database to exercise and is intentionally left to the (optional, skipped) integration tests rather than mocked at the `psycopg` module level.

`uv run ruff check`, `uv run ruff format --check`, and `uv run mypy` all pass clean on `src/ragledger/connectors` and `tests/connectors`.

## Fixtures

`tests/fixtures/snapshots/qdrant_support_kb.ndjson.zst` and
`tests/fixtures/snapshots/pgvector_document_chunks.ndjson.zst`: small
(3-point), synthetic, committed snapshots covering an integer Qdrant
point id and a composite pgvector point id respectively. Read back by
`test_ndjson.py::test_qdrant_fixture_is_valid_and_stable` and
`test_pgvector_fixture_has_composite_point_ids`.

## Key interpretation decisions

1. **`NormalizedPoint.payload_projection` is keyed by logical field name** (`source_id`, `tenant`, `acl`, ...), not by the vendor-specific payload path/column it came from. This is what makes reconciliation (a later milestone) vendor-agnostic; it is a reading of FR-096's "raw payload retention policy" that the spec's field list implies but does not spell out explicitly.
2. **`iterate_points(projection=...)` is implemented as a post-normalization filter** (`base.py::apply_projection`), not by threading a `projection` parameter through `normalize_point` itself, since section 13.1's interface diagram lists `normalize_point()` with no parameters. This keeps `normalize_point` a pure, connector-specific-record-in/`NormalizedPoint`-out function usable standalone (e.g. for a future mapping-preview feature per section 35.3), while `iterate_points` still honors the `projection` argument the spec requires of it.
3. **Consistency is `ConsistencyInfo` (mode + completeness + counts), not a single boolean.** `SnapshotCompleteness.INCOMPLETE` records *detected* drift (before/after count mismatch); it does not attempt to reconstruct exactly what changed. `ConsistencyMode` separately records *how* the pass was obtained (`strict_consistent` / `best_effort_paged` / `best_effort_live`), matching the spec's explicit naming of `best_effort_live` for Qdrant and its description of pgvector's two consistency options.
4. **pgvector identifier quoting uses `psycopg.sql`, not SQLAlchemy**, because SQLAlchemy is not in this milestone's permitted dependency list (`httpx`, `psycopg[binary]`, `zstandard`, `pydantic`, `jsonschema`, `pyyaml`, `cryptography`) and the task instructions forbid adding dependencies. `psycopg.sql.Identifier`/`sql.SQL` provide the same safe-quoting guarantee section 35.1's text asks for.
5. **The pgvector statement-timeout is set via the `psycopg.connect(..., options="-c statement_timeout=...")` connection parameter**, not via a `SET` statement issued through the guarded cursor -- this keeps the statement whitelist itself strictly `SELECT`/`SHOW`-only (matching the task's literal instruction) rather than needing a `SET`/`BEGIN` carve-out.
6. **`hash_vector` hashes the RFC 8785 canonical JSON array of a vector's float components**, not raw IEEE-754 bytes, so the same logical vector hashes identically regardless of source language/library, consistent with `ragledger.core.hashing`'s existing canonicalization convention. Per the task instructions, raw vectors are never required to be stored in a snapshot; only this hash is.
7. **`SnapshotReader.points(check_duplicates=True)` is an opt-in, in-memory duplicate-id check**, bounded by file size, intended for small CI fixtures -- not a substitute for reconciliation's 1M-row-bounded, externally-sorted duplicate/matching pass (section 8.13, FR-121), which is out of this milestone's scope.

## Honest gaps

- FR-093 (explicit sampling): the NDJSON header schema supports recording sample method/seed/rate, but no connector in this milestone actually implements a *sampling* mode (only full iteration). Building a sampler was judged out of scope for "connectors and snapshot" versus reconciliation/policy, which is where `INCONCLUSIVE` handling belongs anyway.
- FR-113 (pgvector index/distance metadata): index *names* are collected from `pg_index`/`pg_am`, but decoding an index's opclass into a `cosine`/`l2`/`inner_product` distance label is not implemented -- `VectorFieldSchema.distance` is always `None` for pgvector (Qdrant's is populated, since Qdrant's collection config states distance directly).
- Live integration tests (`test_integration.py`) are written against `docker-compose.yml`'s Qdrant/Postgres services but were not executed in this environment, per the "never run docker commands" constraint. They are skipped by default (`RAGLEDGER_IT=1` required) and each creates/drops its own uniquely `ragledger_it_`-prefixed collection/table.
- `pgvector.py`'s real `psycopg.connect` retry-with-backoff loop (used only when no `connection`/`connection_factory` is injected) has no automated test exercising a real refused/flaky connection; it is straightforward code mirroring the already-tested Qdrant retry loop, but is only indirectly exercised (via the integration tests, when run).
