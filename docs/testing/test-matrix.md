# Test and evidence matrix

This matrix maps requirement areas from `PROJECT_SPEC.md` sections 24
(test strategy) and 42 (required test/evidence mapping) to test types and
the evidence each test type is expected to produce. It is a planning
document written ahead of the corresponding code (milestone M0); the
"Evidence" column describes what will exist once each area is
implemented, not what exists today. Every `FR-*` requirement must map to
at least one row/test id once implemented (section 42); manual evidence
is used only where behavior genuinely cannot be automated, and screen
recordings are used only for UX/visual confirmation, never as a
substitute for an automated check.

## Requirement area to test type mapping

| Requirement area | Unit / property | Integration | E2E | Performance | Security | Evidence |
|---|---|---|---|---|---|---|
| Source discovery, hashing, IDs | URI/text/hash normalization, stable ID properties, Unicode edge cases | Filesystem discovery, symlink handling, changed-file re-read | Source version history shown in CLI/report output | Streaming hash over large files stays off-heap | Zip/path traversal on import; symlink escape outside source root | `tests/unit/identity/`, `tests/integration/discovery/`, golden corpus diffs |
| Parser and chunker | Golden document elements and chunk boundaries against fixed fixtures | Sandboxed real-format parsing (PDF/DOCX/HTML/Markdown/TXT) | Build detail view shows partial-parse failures and warnings | Large PDF/page/file limit enforcement | Malicious PDF/HTML exploit attempts stay contained to the parser sandbox | `tests/unit/parsing/`, `tests/integration/parser_sandbox/`, golden manifest corpus |
| Embedding | Dimension validation, vector hash determinism, NaN/Inf rejection | Pinned local Sentence Transformers model run | Manifest lineage shows model/revision/dimension for a real build | Batch embedding throughput within configured batch size limits | Model source integrity (trust_remote_code disabled, checksum snapshot) | `tests/unit/embedding/`, `tests/integration/embedding/` |
| Manifest and signature | RFC 8785 canonicalization vectors, manifest schema round-trip | Key file loading, trust store, revocation handling | CLI sign/verify against a real key, including a deliberately tampered manifest | Canonicalization/signing time for large manifests | Signature tamper detection, wrong key, untrusted key, revoked key | `tests/unit/manifest/`, `tests/integration/signing/` |
| PII and license | Redaction/HMAC-only finding shape, SPDX expression parsing | Presidio run against a synthetic corpus | Policy report contains no raw PII value end to end | Scan throughput over the golden corpus | PII leak canary across DB dump, reports, logs, metrics, SARIF, JUnit, HTML | `tests/unit/governance/`, `tests/integration/pii/`, PII leak canary suite |
| ACL and tenant | Typed ACL set comparison properties (`PUBLIC`, `USER:`, `GROUP:`, `ROLE:`, `ATTRIBUTE:`) | Connector payload-to-ACL mapping | Critical ACL/tenant drift surfaced with lineage drill-down | - | Cross-workspace ACL/tenant isolation | `tests/unit/acl/`, `tests/integration/connectors/` |
| Qdrant connector | Payload normalizer, scroll pagination logic | Real Qdrant container: scroll, pagination/resume, dimension/schema inspection | Snapshot then reconcile against a real collection | 1M-point scroll within memory/time targets | Mutation guard: only GET/scroll calls are ever issued | `tests/integration/connectors/qdrant/`, connector contract suite |
| pgvector connector | SQL identifier quoting, parameterized query generation | Read-only DB role, transaction/timeout behavior, Testcontainers Postgres | Snapshot then reconcile against a real table, parity with Qdrant fixture | 1M-row keyset pagination within memory/time targets | SQL injection attempts on configured `where` values; no INSERT/UPDATE/DELETE/DDL possible | `tests/integration/connectors/pgvector/`, connector contract suite |
| NDJSON portable connector | Stream header/trailer parsing | Huge/compressed file handling, resumable import | Air-gapped CLI round trip (export then import) | Large file streaming without full in-memory load | Archive bounds validation on import | `tests/integration/connectors/ndjson/`, connector contract suite |
| Reconciliation | Taxonomy code assignment, finding fingerprint stability, ratio zero-denominator handling | External merge/streaming join at scale, checkpoint resume | Reconciliation history diff (new/resolved/persistent) shown in report | 1M-point reconciliation within memory and time targets | - | `tests/unit/reconciliation/`, `tests/integration/reconciliation/` |
| Policy and remediation | Policy schema validation (unknown key hard error), verdict logic (PASS/WARN/FAIL/INCONCLUSIVE) | Policy evaluation against real reconciliation output | CI gate fails a build with a deliberately introduced ACL drift or stale ratio breach | - | Remediation plan never executes an action; destructive candidates explicitly flagged | `tests/unit/policy/`, `tests/integration/policy/` |
| CLI and SDK | Command argument parsing and exit codes | End-to-end command sequences against fixtures | Full CLI walkthrough: build, sign, verify, snapshot, reconcile, policy gate | - | - | `tests/unit/cli/`, `tests/integration/cli/` |
| Deployment | Configuration parsing and validation, health check logic | Compose stack start/restart | Clean-machine quickstart following the README | - | - | `tests/unit/config/`, `docker-compose.yml` based manual verification |

## Golden manifest corpus

Per section 24.1 and 42.1, a fixture corpus (legally redistributable or
synthetic only) covering PDF, DOCX, HTML/Markdown/TXT, version changes
(edit/rename/delete/duplicate), chunker configuration changes, embedding
model/dimension changes, synthetic PII entities, SPDX assertions and
conflicts, and ACL/tenant combinations produces deterministic expected
manifests. Golden manifest updates are a deliberate, reviewed command
(`just golden-manifests`), never an automatic CI action; the update
produces a diff summary of source/chunk/assertion/ID counts and changed
paths, and that review does not attribute the change to any agent or
model.

## Connector mutation guard

Per section 42.2, the same logical fixture is loaded into Qdrant,
pgvector, and NDJSON, and all three must produce the same normalized
snapshot and the same reconciliation findings. Qdrant tests use a fake
or recording transport that only permits GET/scroll/read calls;
pgvector tests run against a database role or audit configuration that
has no INSERT/UPDATE/DELETE/DDL privilege. Where feasible, an attempted
mutating call is made impossible at the type/interface level rather
than only rejected at runtime; the runtime rejection remains as defense
in depth.

## PII leak canary

Per section 42.3, a synthetic, uniquely identifiable canary email,
card number, and SSN are placed in a source fixture. The expected
result is a PII finding carrying only HMAC/masked evidence. A full
PostgreSQL logical dump, generated reports, application logs, metrics,
SARIF output, JUnit output, and HTML output are scanned for the raw
canary values; the only artifact where the raw canary value is
expected is the intentionally raw, access-restricted source artifact
itself. Export privacy modes (public/internal/sensitive/restricted) are
exercised as part of this check.
