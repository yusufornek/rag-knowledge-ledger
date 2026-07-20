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
| FR-010 | Root directory recursion applies `.gitignore` and `.ragledgerignore` | implemented | `ragledger.pipeline.discovery.discover_sources`; `tests/pipeline/test_discovery.py::TestIgnoreRules`. Gap: only a root-level ignore file is read, not per-directory nested ignore files layered git-style down the tree (documented in `discovery.py`'s module docstring) |
| FR-011 | Symlinks not followed by default; followed symlinks must resolve within root | implemented | `ragledger.pipeline.discovery.discover_sources`/`SymlinkEscapesRootError`; `tests/pipeline/test_discovery.py::TestSymlinks` |
| FR-012 | Stable relative URI, POSIX-normalized, Unicode NFC | implemented | `ragledger.pipeline.discovery._walk_files`; `tests/pipeline/test_discovery.py::TestBasicDiscovery::test_uri_is_posix_normalized_and_unicode_nfc` |
| FR-013 | MIME sniff plus extension; unsupported files reported, never silently skipped | implemented | `ragledger.pipeline.discovery.sniff_media_type`; `tests/pipeline/test_discovery.py::TestMediaTypeSniffing`; a source whose media type has no registered parser still gets a `ParseRecord` with status `fail` and code `NO_PARSER_AVAILABLE` (`tests/pipeline/test_build.py::test_no_parser_available_marks_build_incomplete_not_a_crash`), never a silent skip |
| FR-014 | Max file size (100 MiB default) and PDF page (500 default) caps, admin-configurable | implemented | `ragledger.pipeline.discovery.DiscoveryConfig.max_file_bytes`/`FileTooLargeError`, `ragledger.pipeline.parsers.base.ParseLimits.max_pages`; `tests/pipeline/test_discovery.py::TestSizeCap`, `tests/pipeline/parsers/test_parser_pdf.py::test_page_count_over_cap_fails_explicitly` |
| FR-015 | Streaming source hash; no full-file in-memory read | implemented | `ragledger.pipeline.discovery._hash_streaming` (chunked `hashlib.sha256` update, 1 MiB blocks); exercised indirectly by every discovery test, not separately memory-profiled |
| FR-016 | Duplicate content across paths produces a relationship record, not auto-deletion | implemented | `ragledger.pipeline.discovery._attach_duplicate_relationships`; `tests/pipeline/test_discovery.py::TestDuplicateContent` |
| FR-017 | Deletion versus previous manifest produces a tombstone candidate | implemented | `ragledger.pipeline.discovery.compute_tombstones`, wired into `ragledger.pipeline.build.build_pipeline` via `BuildConfig.previous_sources`; `tests/pipeline/test_discovery.py::TestTombstones`, `tests/pipeline/test_build.py::test_tombstone_recorded_when_a_previously_seen_source_disappears` |

### 8.3 Parsing

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-020 | Docling primary parser for PDF/DOCX/HTML; native deterministic adapters for Markdown/TXT | implemented | Native deterministic adapters ship for text/markdown/HTML/JSON/CSV/PDF (`ragledger.pipeline.parsers.{text,markdown,html_parser,json_parser,csv_parser,pdf}`); `tests/pipeline/parsers/test_parser_*.py`. Gap: Docling itself (the spec's stated *primary* PDF/DOCX/HTML engine) and DOCX support are not implemented -- Docling is a heavy optional dependency with its own model downloads that this wave's dependency budget does not cover; the native `pypdf`/`html.parser`-backed adapters serve as the fallback adapters FR-020 also names |
| FR-021 | Exact parser version/config/model artifacts recorded | implemented | `ragledger.pipeline.parsers.base.ParserDescriptor`/`resolve_distribution_version` (real `importlib.metadata` versions, never guessed); `tests/pipeline/parsers/test_parser_pdf.py::test_descriptor_uses_real_installed_pypdf_version` |
| FR-022 | Parse success/partial/fail separated; partial warnings enter lineage | implemented | `ragledger.pipeline.parsers.base.ParseStatus`; success/fail covered throughout `tests/pipeline/parsers/`. Gap: no shipped native parser currently emits `status="partial"` (the type is modeled and would flow correctly through `ParseRecord`, but nothing produces it yet) |
| FR-023 | OCR on/off, language, engine/model, confidence config recorded | pending | `ParseOutcome.ocr`/`ragledger.core.models.OcrInfo` exist and round-trip correctly, but no parser performs OCR in this release; always `None` |
| FR-024 | Parsed structured document stored as canonical JSON artifact | implemented | `ragledger.pipeline.build._store_json_artifact` (RFC 8785 canonical bytes, content-addressed); `tests/pipeline/test_build.py::test_full_corpus_build_produces_a_schema_valid_manifest` |
| FR-025 | Parser makes no network calls; source-embedded external URLs not fetched | implemented | No parser module imports any networking library; verified by code inspection (`ragledger.pipeline.parsers.*` source), not a runtime network-blocking test harness |
| FR-026 | Embedded files/macros never executed | implemented | Every parser only extracts text (`pypdf.PdfReader.extract_text`, `html.parser` tokenization, `json`/`csv` stdlib parsing); `tests/pipeline/parsers/test_parser_html.py::test_script_and_style_content_never_emitted` |
| FR-027 | Password-protected/encrypted documents raise an explicit error | implemented | `ragledger.pipeline.parsers.pdf.PdfParser` (`PASSWORD_PROTECTED`, no blind empty-password attempt); `tests/pipeline/parsers/test_parser_pdf.py::test_encrypted_pdf_fails_explicitly_without_guessing_password` |

**Sandbox (PROJECT_SPEC.md section 8.3, 34.6, not its own FR row):** `ragledger.pipeline.parsers.sandbox.run_sandboxed` runs every parse in a subprocess with a timeout and an output-size cap; a hanging, crashing, or oversized-output parser degrades to a `status="fail"` `ParseOutcome`, never a raised exception or a hung host process. `tests/pipeline/parsers/test_parser_sandbox.py` covers timeout, oversized output, an unhandled exception, a hard `os._exit`, and a real broken PDF through the subprocess boundary.

### 8.4 Chunking

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-030 | Built-in `hierarchical`, `hybrid`, `line_based` chunking strategies | implemented | `ragledger.pipeline.chunkers.{hierarchical,hybrid,line_based}`; `tests/pipeline/chunkers/test_chunker_{hierarchical,hybrid,line_based}.py` |
| FR-031 | Exact tokenizer name/revision and max tokens/overlap/config hash recorded | implemented | `ragledger.pipeline.chunkers.base.WhitespaceTokenizer`/`ChunkRecord.tokenizer`, chunker config hash in `ragledger.pipeline.build._process_chunks`. The shipped tokenizer is ragledger's own honestly-named deterministic reference tokenizer (word-boundary splitting), not a real named tokenizer such as `cl100k_base`; `resolve_tokenizer` raises `TokenizerUnavailableError` rather than approximating for any other declared name (PROJECT_SPEC.md section 40), see `tests/pipeline/chunkers/test_chunker_base.py::TestTokenizer` |
| FR-032 | Deterministic chunk order, including under parallel parsing | implemented | Single-threaded, deterministic-by-construction chunk iteration (no parallel parsing is implemented in this release, so the "under parallel parsing" case does not yet arise); `tests/pipeline/chunkers/test_chunker_*.py::test_deterministic_across_two_runs`, `tests/pipeline/test_build.py::test_determinism_two_runs_are_byte_identical` |
| FR-033 | Declarative contextualization template; no arbitrary code execution | implemented | `ragledger.pipeline.chunkers.base.render_contextualization_template` (closed placeholder whitelist, plain regex substitution, never `str.format`); `tests/pipeline/chunkers/test_chunker_base.py::TestContextualizationTemplate` |
| FR-034 | Heading/table caption/page metadata preserved | implemented | `ragledger.pipeline.parsers.base.LedgerElement` (`heading_path`, `table_caption`, `page`); covered across every parser test file |
| FR-035 | Table header repetition included in the chunk hash input | implemented | `ragledger.pipeline.chunkers.base.build_candidate` (repeats `table_header` at the start of a continuation chunk's `raw_text`); `tests/pipeline/chunkers/test_chunker_hierarchical.py::test_table_header_repeated_when_table_split_across_chunks` |
| FR-036 | Oversized indivisible element policy (split/fail/configurable); no silent truncation | implemented | `ragledger.pipeline.chunkers.base.split_oversized`/`OversizedElementError`; `tests/pipeline/chunkers/test_chunker_hierarchical.py::test_oversized_single_element_{split,fail}_policy` |
| FR-037 | No empty/whitespace-only chunks; count reported as a warning | implemented | `ragledger.pipeline.chunkers.base.drop_empty_candidates`, surfaced as an `EMPTY_CHUNKS_DROPPED` `QUALITY` assertion in `ragledger.pipeline.build`; `tests/pipeline/chunkers/test_chunker_base.py::TestDropEmptyCandidates` |
| FR-038 | Duplicate chunk content reported (exact hash identity, optional near-duplicate via MinHash) | implemented | Exact-hash duplicate detection as a `DUPLICATE_CHUNK_CONTENT` `QUALITY` assertion in `ragledger.pipeline.build._process_chunks`; `tests/pipeline/test_build.py::test_duplicate_chunk_content_reported_as_a_quality_warning`. Gap: near-duplicate detection via MinHash (explicitly optional in the spec) is not implemented |

### 8.5 Embedding

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-040 | Metadata-only mode produces manifest/reconciliation lineage without vectors | implemented | `BuildConfig.embedding_provider=None` skips the embed stage entirely (`embeddings`/`index_bindings` empty, sources/chunks/assertions unaffected); `tests/pipeline/test_build.py::test_metadata_only_mode_produces_lineage_without_vectors` |
| FR-041 | Local Sentence Transformers adapter records model revision/digest, dimension, dtype, normalization | drafted | `ragledger.pipeline.embedding.SentenceTransformersEmbeddingProvider`/`SentenceTransformersConfig` implement the `EmbeddingProvider` protocol and accept real model/revision/dimension/device/batch-size config, but `embed()`/`tokenize()` raise `ProviderNotAvailableError` rather than making a live call -- the `sentence-transformers` library and its model download/cache are a documented gap, not a fake response; `tests/pipeline/test_embedding.py::TestUnwiredRealProviders` |
| FR-042 | Batch size and device config recorded as deterministic evidence | implemented | `DeterministicLocalEmbeddingProvider.embed` records `usage={"batch_size": ...}`; GPU nondeterminism policy is not applicable since no GPU-backed provider ships in this release |
| FR-043 | Vector NaN/Inf values rejected | implemented | `ragledger.pipeline.embedding.validate_finite`; `tests/pipeline/test_embedding.py::TestValidation` |
| FR-044 | Declared model dimension validated against the first produced vector | implemented | `ragledger.pipeline.embedding.validate_dimension`; `tests/pipeline/test_embedding.py::TestValidation::test_dimension_mismatch_rejected` |
| FR-045 | Canonical vector hash over float32 little-endian bytes, with dtype metadata for non-float32 inputs | implemented | `ragledger.pipeline.embedding.vector_hash`; `tests/pipeline/test_embedding.py::TestVectorHash` |
| FR-046 | Raw vectors excluded from the manifest by default | implemented | `ragledger.core.models.EmbeddingRecord` carries no vector field at all (core, unmodified); `ragledger.pipeline.build` never writes vector data anywhere outside the local stage cache |
| FR-047 | Externally imported embedding metadata marked unknown when not explicitly provided | implemented | `ragledger.pipeline.embedding.ExternalImportEmbeddingProvider` (unset provider/name/revision default to the literal `"unknown"`, never guessed); `tests/pipeline/test_embedding.py::TestExternalImportProvider` |

**Reference embedder (not its own FR row):** `ragledger.pipeline.embedding.DeterministicLocalEmbeddingProvider` is a seeded hash-projection embedder producing unit-L2-normalized float32 vectors of configurable dimension -- explicitly documented as a deterministic reference/testing embedder, not a semantic model. `OpenAiEmbeddingProvider` is a second config-plumbing-only stub (cloud embedding is explicitly non-mandatory for v1 per PROJECT_SPEC.md section 5.1) with the same never-fake-a-response guarantee as FR-041's Sentence Transformers stub.

### 8.6 PII

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-050 | Presidio analyzer plus deterministic regex/checksum recognizers | implemented | Deterministic recognizers implemented: email, phone, IBAN (MOD-97 checksum), credit card (Luhn), US SSN (structural plausibility), Turkish TCKN (official two-stage checksum) -- `ragledger.governance.pii.default_recognizers`; `tests/governance/test_pii.py::TestDetectors`. Gap: Presidio itself is not integrated (it pulls in a spaCy language model this environment does not vendor), documented in `pii.py`'s module docstring |
| FR-051 | Scanner language/config/version recorded | implemented | `ragledger.governance.pii.PiiScannerInfo` populated by `build_pii_scan_assertion`; `tests/governance/test_pii.py::TestAssertionConstruction::test_scanner_records_config_and_version` |
| FR-052 | No raw PII value written to any finding, database, or log | implemented | `ragledger.governance.pii.PiiFinding` never carries a raw value (only `masked_preview`/`value_hmac`); `tests/governance/test_pii.py::TestAssertionConstruction`, `tests/governance/test_pii_leak_canary.py` (full-pipeline canary, PROJECT_SPEC.md section 42.3) |
| FR-053 | PII scan runs separately over parsed source text and contextualized chunk text | implemented | `ragledger.pipeline.build._process_source` (source-version-level scan over parsed text) and `_process_chunks` (chunk-level scan over contextualized text); `tests/pipeline/test_build.py::test_pii_scan_finds_the_email_in_sample_txt`. Gap: a separate `raw_chunk`-text scan (distinct from `contextualized`, per PROJECT_SPEC.md section 40's edge case) is not implemented -- only source-level raw and chunk-level contextualized are covered |
| FR-054 | Allowlist/denylist custom recognizers in YAML, with bounded/timeout-safe regex | implemented | `ragledger.governance.pii.load_custom_recognizers` (YAML-configured custom entity types) and `_run_with_timeout` (`SIGALRM`-based regex timeout, proven against a catastrophic-backtracking pattern); `tests/governance/test_pii.py::TestCustomRecognizers`, `TestRegexTimeout`. Note: "allowlist/denylist" here is custom recognizer *definition*; entity-type allow/deny *policy* is `ragledger.pipeline.build.PiiPolicyConfig` (block-list only, see FR-056) |
| FR-055 | Zero findings reported as "no findings detected", never "guaranteed clean" | implemented | `ragledger.core.models.PiiScanStatus`; `tests/governance/test_pii.py::TestAssertionConstruction::test_no_findings_reported_as_no_findings_detected_not_guaranteed_clean` |
| FR-056 | Policy warn/block driven by entity type, confidence, and count | drafted | `ragledger.pipeline.build.PiiPolicyConfig` blocks embedding for a configured entity-type/confidence combination (`tests/pipeline/test_build.py::test_pii_policy_blocks_embedding_for_denied_entity_types`); count-based thresholds, a separate "warn" (non-blocking) tier, and full PASS/WARN/FAIL/INCONCLUSIVE policy verdicts are M6 scope and not implemented here |

### 8.7 License

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-060 | License source precedence: user assertion, frontmatter, sidecar, path rule; content text is never a v1 fact | implemented | `ragledger.governance.license.evaluate_license`/`gather_candidates` (user assertion, sidecar, frontmatter incl. `SPDX-License-Identifier` header, path rule, repository default; never scans body prose); `tests/governance/test_license.py::TestPrecedenceAndConflicts`. Precedence interpretation documented in `license.py`'s module docstring: PROJECT_SPEC.md section 12.2 does not explicitly rank `user_assertion`, so this module ranks it highest as the most direct operator override |
| FR-061 | SPDX identifier/expression validated; unrecognized value becomes `NOASSERTION` | implemented | `ragledger.governance.license.validate_spdx_expression`; `tests/governance/test_license.py::TestExpressionValidation`. Gap: `_KNOWN_SPDX_IDENTIFIERS` is a small, honestly-labeled hand-maintained subset (`_LICENSE_LIST_VERSION = "ragledger-embedded-subset-1"`), not the full network-fetched official SPDX license list |
| FR-062 | Multiple conflicting assertions produce a conflict finding | implemented | `ragledger.governance.license.evaluate_license` (every candidate cross-references every other via `conflicting_assertion_ids` on disagreement); `tests/governance/test_license.py::TestPrecedenceAndConflicts::test_disagreement_produces_cross_referenced_conflicts` |
| FR-063 | Policy allow/deny/unknown license behavior | pending | `evaluate_license` resolves the *effective* assertion; evaluating it against an allow/deny policy list to a PASS/WARN/FAIL verdict is M6 policy-engine scope |
| FR-064 | License evidence stored as source locator/hash, not a required full text copy | implemented | `ragledger.core.models.LicenseAssertion` never carries license body text, only the resolved SPDX expression/method (core, unmodified); `ragledger.governance.license` never reads or stores full license text |

### 8.8 ACL and tenant

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-070 | Expected ACL canonical set derived from source metadata/path policy/sidecar | implemented | `ragledger.governance.acl.expected_acl_entries`/`AclConfig` (direct per-source mapping and path-glob rules); `tests/governance/test_acl.py::TestExpectedAclResolution`. Gap: a dedicated ACL sidecar file format (distinct from the direct/path-rule config) is not implemented, only for license |
| FR-071 | Principal identifiers normalized; redaction policy applied on public export | implemented | `ragledger.governance.acl.normalize_acl_entries` (`case_normalize` opt-in, off by default per PROJECT_SPEC.md section 40); `tests/governance/test_acl.py::TestNormalization`. Gap: "public export" redaction is a reporting/export feature (M4+ CLI scope), not implemented |
| FR-072 | Expected tenant mandatory/optional policy | implemented | `ragledger.governance.acl.TenantConfig.required` drives a `TENANT_REQUIRED_BUT_MISSING` build warning in `ragledger.pipeline.build`; `tests/governance/test_acl.py::TestTenant` |
| FR-073 | Observed payload field mapping via target-configured JSONPath/column mapping | pending | Observed-side payload mapping is a connector/reconciliation concern (M5/M6), out of this wave's scope |
| FR-074 | Missing/broader/narrower/mismatched ACL reported as distinct finding types | pending | Comparing expected (this module's output) against an observed index payload is reconciliation's job (M6) |
| FR-075 | Tenant missing/mismatch/cross-tenant duplicate can be marked critical by policy | pending | Same as FR-074: cross-referencing expected tenant against observed index state is M6 reconciliation scope; this wave only builds the expected-side `TENANT` assertion |
| FR-076 | ACL canonical sort (order carries no semantics); wildcard is a distinct typed value | implemented | `ragledger.governance.acl.normalize_acl_entries` (deduplicated, sorted); `PUBLIC` is the typed wildcard value (section 12.3); `tests/governance/test_acl.py::TestValidation`, `TestNormalization` |

### 8.9 Build and manifest

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-080 | Build plan preview: source count, estimated parse/chunk/embed cost, resource caps | pending | Not implemented; no dry-run/plan-preview command exists (CLI is M4 scope) |
| FR-081 | Pipeline stage artifact caching keyed by content/config hash | implemented | `ragledger.pipeline.cache.StageCache`/`stage_cache_key` (stage + input hash + adapter name/version + config hash); wired into parse/chunk/embed in `ragledger.pipeline.build`. Governance-stage (PII/license/ACL) caching is not implemented, a documented gap noted in `build.py`'s module docstring. `tests/pipeline/test_cache.py`, `tests/pipeline/test_build.py::test_cache_hits_on_second_run_against_the_same_cache_directory` |
| FR-082 | Same input/config/reproducible epoch produces a byte-identical canonical manifest | implemented | `ragledger.core.manifest.build_manifest`/`canonical_manifest_bytes`; `tests/core/test_canonical.py`, `tests/core/test_manifest.py::TestCanonicalBytesAndRoundtrip`, `tests/core/test_golden_manifests.py`. Extended to the full pipeline: `ragledger.pipeline.build.build_pipeline` with `BuildConfig.reproducible=True` (which fixes `ParseRecord.duration_seconds` to `0.0` instead of real, necessarily run-varying wall-clock timing) produces byte-identical manifests across two runs; `tests/pipeline/test_build.py::test_determinism_two_runs_are_byte_identical` |
| FR-083 | Partial build manifest marked `incomplete`; policy fails on it by default | implemented | `ragledger.pipeline.build.build_pipeline` sets `build.status="incomplete"` whenever any source's parse run fails; `tests/pipeline/test_build.py::test_no_parser_available_marks_build_incomplete_not_a_crash`, `test_broken_pdf_source_fails_parse_without_crashing_the_build`. "Policy fails on it by default" (an actual CI/CLI gate decision) is M6 policy-engine scope |
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
| M2 | Source/parse/chunk pipeline: discovery, Docling/native parsers in a sandbox, structural artifacts, chunkers, caching | done (native parsers, not Docling; see FR-020) |
| M3 | Governance and embedding: local embeddings, PII, SPDX, ACL/tenant assertions, policy facts | done (deterministic reference embedder, not Sentence Transformers; see FR-041) |
| M4 | CLI build/report: standalone build, validate/sign/verify, JSON/HTML reporting | pending |
| M5 | Connectors/snapshot: Qdrant, pgvector, NDJSON, checkpointing, consistency, read-only enforcement | pending |
| M6 | Reconciliation/policy: external merge, taxonomy, ratios, history, remediation plan, CI outputs | pending |
| M7 | Persistence/API/auth/jobs: Postgres/Redis/S3, credentials, SSRF protection, SSE, audit trail (deferred beyond v0.1.0) | pending |
| M8 | Web: all screens, lineage explorer, policy views, accessibility (deferred beyond v0.1.0) | pending |
| M9 | Hardening/release: performance, security, backup/restore, documentation, v1.0 (spec's own v1.0 milestone; v0.1.0 release readiness is scoped to M0-M6) | pending |
