# Implementation Status

This document tracks the status of every functional requirement (`FR-*`)
defined in `PROJECT_SPEC.md` section 8, and the milestone plan from
section 27. It is updated as work lands; it is not a plan of intent.

Status values:

- `pending`: not started.
- `drafted`: a design artifact exists (schema, ADR, doc) but no code
  implements or enforces the requirement yet.
- `implemented`: working code exists and is covered by tests.

## Release scope

v0.1.0 targets the standalone deterministic core: identity and manifest
core (M1), the source/parse/chunk pipeline (M2), governance and embedding
(M3), the CLI (M4), vector database connectors and snapshotting (M5), and
reconciliation and policy evaluation (M6). The server API, persistence,
job orchestration (M7), and the web UI (M8) described in the project
specification are planned for a later release and are intentionally not
part of v0.1.0. Requirements below that belong to M7/M8 (workspace
auth/tokens, credential storage, web lineage navigation, SSE progress,
and similar) remain `pending` for v0.1.0 and are tracked here for
completeness against the full specification, not as a v0.1.0 commitment.

## Functional requirements

### 8.1 Workspace, auth and targets

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-001 | Local admin bootstrap, workspace and roles (owner/editor/viewer) | pending | - |
| FR-002 | API token scopes (sources, builds, targets, snapshots, reconciliations, policies, admin) | pending | - |
| FR-003 | Target credentials AES-256-GCM encrypted, write-only, versioned | pending | - |
| FR-004 | Target URL SSRF-safe validation with explicit private-host allowlist | pending | - |
| FR-005 | Workspace export excludes secrets and raw documents by default | pending | - |

### 8.2 Source discovery

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-010 | Root directory recursion applies `.gitignore` and `.ragledgerignore` | pending | - |
| FR-011 | Symlinks not followed by default; followed symlinks must resolve within root | pending | - |
| FR-012 | Stable relative URI, POSIX-normalized, Unicode NFC | pending | - |
| FR-013 | MIME sniff plus extension; unsupported files reported, never silently skipped | pending | - |
| FR-014 | Max file size (100 MiB default) and PDF page (500 default) caps, admin-configurable | pending | - |
| FR-015 | Streaming source hash; no full-file in-memory read | pending | - |
| FR-016 | Duplicate content across paths produces a relationship record, not auto-deletion | pending | - |
| FR-017 | Deletion versus previous manifest produces a tombstone candidate | pending | - |

### 8.3 Parsing

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-020 | Docling primary parser for PDF/DOCX/HTML; native deterministic adapters for Markdown/TXT | pending | - |
| FR-021 | Exact parser version/config/model artifacts recorded | pending | - |
| FR-022 | Parse success/partial/fail separated; partial warnings enter lineage | pending | - |
| FR-023 | OCR on/off, language, engine/model, confidence config recorded | pending | - |
| FR-024 | Parsed structured document stored as canonical JSON artifact | pending | - |
| FR-025 | Parser makes no network calls; source-embedded external URLs not fetched | pending | - |
| FR-026 | Embedded files/macros never executed | pending | - |
| FR-027 | Password-protected/encrypted documents raise an explicit error | pending | - |

### 8.4 Chunking

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-030 | Built-in `hierarchical`, `hybrid`, `line_based` chunking strategies | pending | - |
| FR-031 | Exact tokenizer name/revision and max tokens/overlap/config hash recorded | pending | - |
| FR-032 | Deterministic chunk order, including under parallel parsing | pending | - |
| FR-033 | Declarative contextualization template; no arbitrary code execution | pending | - |
| FR-034 | Heading/table caption/page metadata preserved | pending | - |
| FR-035 | Table header repetition included in the chunk hash input | pending | - |
| FR-036 | Oversized indivisible element policy (split/fail/configurable); no silent truncation | pending | - |
| FR-037 | No empty/whitespace-only chunks; count reported as a warning | pending | - |
| FR-038 | Duplicate chunk content reported (exact hash identity, optional near-duplicate via MinHash) | pending | - |

### 8.5 Embedding

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-040 | Metadata-only mode produces manifest/reconciliation lineage without vectors | pending | - |
| FR-041 | Local Sentence Transformers adapter records model revision/digest, dimension, dtype, normalization | pending | - |
| FR-042 | Batch size and device config recorded as deterministic evidence | pending | - |
| FR-043 | Vector NaN/Inf values rejected | pending | - |
| FR-044 | Declared model dimension validated against the first produced vector | pending | - |
| FR-045 | Canonical vector hash over float32 little-endian bytes, with dtype metadata for non-float32 inputs | pending | - |
| FR-046 | Raw vectors excluded from the manifest by default | pending | - |
| FR-047 | Externally imported embedding metadata marked unknown when not explicitly provided | pending | - |

### 8.6 PII

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-050 | Presidio analyzer plus deterministic regex/checksum recognizers | pending | - |
| FR-051 | Scanner language/config/version recorded | pending | - |
| FR-052 | No raw PII value written to any finding, database, or log | pending | - |
| FR-053 | PII scan runs separately over parsed source text and contextualized chunk text | pending | - |
| FR-054 | Allowlist/denylist custom recognizers in YAML, with bounded/timeout-safe regex | pending | - |
| FR-055 | Zero findings reported as "no findings detected", never "guaranteed clean" | pending | - |
| FR-056 | Policy warn/block driven by entity type, confidence, and count | pending | - |

### 8.7 License

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-060 | License source precedence: user assertion, frontmatter, sidecar, path rule; content text is never a v1 fact | pending | - |
| FR-061 | SPDX identifier/expression validated; unrecognized value becomes `NOASSERTION` | pending | - |
| FR-062 | Multiple conflicting assertions produce a conflict finding | pending | - |
| FR-063 | Policy allow/deny/unknown license behavior | pending | - |
| FR-064 | License evidence stored as source locator/hash, not a required full text copy | pending | - |

### 8.8 ACL and tenant

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-070 | Expected ACL canonical set derived from source metadata/path policy/sidecar | pending | - |
| FR-071 | Principal identifiers normalized; redaction policy applied on public export | pending | - |
| FR-072 | Expected tenant mandatory/optional policy | pending | - |
| FR-073 | Observed payload field mapping via target-configured JSONPath/column mapping | pending | - |
| FR-074 | Missing/broader/narrower/mismatched ACL reported as distinct finding types | pending | - |
| FR-075 | Tenant missing/mismatch/cross-tenant duplicate can be marked critical by policy | pending | - |
| FR-076 | ACL canonical sort (order carries no semantics); wildcard is a distinct typed value | pending | - |

### 8.9 Build and manifest

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-080 | Build plan preview: source count, estimated parse/chunk/embed cost, resource caps | pending | - |
| FR-081 | Pipeline stage artifact caching keyed by content/config hash | pending | - |
| FR-082 | Same input/config/reproducible epoch produces a byte-identical canonical manifest | implemented | `ragledger.core.manifest.build_manifest`/`canonical_manifest_bytes`; `tests/core/test_canonical.py`, `tests/core/test_manifest.py::TestCanonicalBytesAndRoundtrip`, `tests/core/test_golden_manifests.py` |
| FR-083 | Partial build manifest marked `incomplete`; policy fails on it by default | pending | - |
| FR-084 | Manifest validated against its JSON Schema | implemented | `ragledger.core.manifest.validate_manifest_document`; `tests/core/test_manifest.py`, `tests/core/test_models.py`, `tests/core/test_golden_manifests.py` |
| FR-085 | Manifest supports detached/embedded Ed25519 signature | implemented | `ragledger.core.signing.sign_manifest` attaches to the embedded `signatures[]` array (the `SignatureRecord` model also serializes standalone for a detached file); CLI-level detached-file conventions are M4 scope; `tests/core/test_signing.py` |
| FR-086 | Signature key id is the public key fingerprint; private key never in the manifest | implemented | `ragledger.core.signing.fingerprint`; `tests/core/test_signing.py::TestRfc8032Vector`, `tests/core/test_signing.py::TestSignAndVerifyRoundtrip` |
| FR-087 | Verify command checks hash, schema, signature, and optional deep artifact checksums | pending | Underlying primitives implemented and tested (`ragledger.core.signing.verify_manifest` for hash/signature, `ragledger.core.manifest.load_manifest` for schema, `ragledger.core.artifacts.ArtifactStore.verify` for artifact checksums); the CLI `verify` command assembling them is M4 scope |

### 8.10 Index target and snapshot

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-090 | Target types: Qdrant, pgvector, NDJSON | pending | - |
| FR-091 | Connectors use read-only credentials; no mutation API/SQL is ever issued | pending | - |
| FR-092 | Full snapshot uses cursor/scroll streaming with a resumable checkpoint | pending | - |
| FR-093 | Sample snapshot records explicit method/seed/rate; completeness-dependent policies become `INCONCLUSIVE` | pending | - |
| FR-094 | Snapshot records target metadata (collection/table, dimension/distance, schema/index config, timestamp, connector version) | pending | - |
| FR-095 | Observed points normalized to a common field set | pending | - |
| FR-096 | Raw payload retention policy defaults to mapped fields only | pending | - |
| FR-097 | Snapshots are immutable and content-hashed | pending | - |

### 8.11 Qdrant connector

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-100 | Collection config, named vector config, dimension/distance, payload index inventory | pending | - |
| FR-101 | Scroll API pagination visits all points exactly once on a best-effort basis | pending | - |
| FR-102 | Vector retrieval defaults to false; enabling vector hashing surfaces a resource warning | pending | - |
| FR-103 | Payload mapping is configurable; missing fields become unknown | pending | - |
| FR-104 | Qdrant point id string/number type preserved | pending | - |
| FR-105 | Collection aliases resolved to actual collection metadata | pending | - |

### 8.12 pgvector connector

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-110 | Table/view, primary key, vector column, and mapped metadata columns explicitly configured | pending | - |
| FR-111 | Identifiers are SQLAlchemy-quoted; no raw user SQL execution path | pending | - |
| FR-112 | Read-only transaction, statement timeout, server-side cursor/keyset pagination | pending | - |
| FR-113 | Vector dimension/type/index metadata sourced from PostgreSQL and pgvector catalogs | pending | - |
| FR-114 | Vector data not fetched by default; hash mode uses chunked queries | pending | - |
| FR-115 | Composite primary keys produce a canonical JSON point id | pending | - |
| FR-116 | Row-level tenant filtering only via explicit parameterized configuration | pending | - |

### 8.13 Reconciliation

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-120 | Expected manifest and observed snapshot scope compatibility check | pending | - |
| FR-121 | Streaming hash-join reconciliation bounded to 1 GiB memory at 1M points | pending | - |
| FR-122 | Complete finding taxonomy | pending | - |
| FR-123 | Findings carry expected/observed evidence refs, severity, confidence, remediation | pending | - |
| FR-124 | Summary ratios reported with denominator and sample completeness | pending | - |
| FR-125 | Identical reconciliation inputs produce an idempotent cached result | pending | - |
| FR-126 | Reconciliation history diff (new/resolved/persistent findings) | pending | - |

### 8.14 Policy and remediation

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-130 | Typed YAML/JSON policy schema; unknown keys are a hard error | drafted | `docs/spec/policy-v1.schema.json` |
| FR-131 | Rule categories: count/ratio, severity, source path/media/license/PII/ACL/tenant, age, completeness | pending | - |
| FR-132 | Policy verdicts: PASS/WARN/FAIL/INCONCLUSIVE | pending | - |
| FR-133 | Remediation plan lists read-only candidate operations only | pending | - |
| FR-134 | Remediation plan never executes any action | pending | - |
| FR-135 | Remediation plan exportable as JSON/CSV, destructive candidates explicitly flagged | pending | - |

### 8.15 Reporting and web

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-140 | JSON/NDJSON/CSV/HTML/SARIF/JUnit export formats | pending | - |
| FR-141 | Web lineage navigation source -> chunk -> embedding -> point and reverse | pending | - |
| FR-142 | Web findings filter, history, diff, policy, target health views | pending | - |
| FR-143 | Raw sensitive artifact reveal/download is audited | pending | - |
| FR-144 | SSE progress, cancel, and retry-failed-stage support | pending | - |

## Milestones

| Milestone | Scope | Status |
|---|---|---|
| M0 | Foundation: repository scaffolding, CI, Compose, base docs, schema skeletons, threat model and test strategy | in progress |
| M1 | Identity and manifest core: canonicalization, stable IDs, manifest schema, artifacts, signing/verify | done |
| M2 | Source/parse/chunk pipeline: discovery, Docling/native parsers in a sandbox, structural artifacts, chunkers, caching | pending |
| M3 | Governance and embedding: local embeddings, PII, SPDX, ACL/tenant assertions, policy facts | pending |
| M4 | CLI build/report: standalone build, validate/sign/verify, JSON/HTML reporting | pending |
| M5 | Connectors/snapshot: Qdrant, pgvector, NDJSON, checkpointing, consistency, read-only enforcement | pending |
| M6 | Reconciliation/policy: external merge, taxonomy, ratios, history, remediation plan, CI outputs | pending |
| M7 | Persistence/API/auth/jobs: Postgres/Redis/S3, credentials, SSRF protection, SSE, audit trail (deferred beyond v0.1.0) | pending |
| M8 | Web: all screens, lineage explorer, policy views, accessibility (deferred beyond v0.1.0) | pending |
| M9 | Hardening/release: performance, security, backup/restore, documentation, v1.0 (spec's own v1.0 milestone; v0.1.0 release readiness is scoped to M0-M6) | pending |
