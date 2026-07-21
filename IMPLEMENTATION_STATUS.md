# Implementation Status

This document tracks the status of every functional requirement (`FR-*`)
defined in `PROJECT_SPEC.md` section 8, and the milestone plan from
section 27. It is updated as work lands; it is not a plan of intent.
It is the merged result of `docs/reviews/m4-status-notes.md`,
`m5-status-notes.md`, and `m6-status-notes.md`, which remain in place as
the underlying per-milestone review artifacts this table summarizes.

Status values:

- `pending`: not started.
- `drafted`: a design artifact exists (schema, ADR, doc) but no code
  implements or enforces the requirement yet.
- `partial`: real, tested code exists but covers only some of what the
  requirement describes (for example, some but not all of a list of
  required export formats); see the row's evidence for exactly what is
  and is not covered.
- `implemented`: working code exists and is covered by tests. A row can
  still carry a documented gap alongside `implemented` when the gap is
  narrow (a single edge case, a metadata field not yet populated) rather
  than a missing capability.

## Release scope

v0.1.0 covers the standalone deterministic core: identity and manifest
core (M1), the source/parse/chunk pipeline (M2), governance and embedding
(M3), the CLI (M4), vector database connectors and snapshotting (M5),
and reconciliation and policy evaluation, including its CLI wiring (M6).
The server API, persistence, job orchestration (M7), and the web UI (M8)
described in the project specification are planned for a later release
and are intentionally not part of v0.1.0. Requirements below that belong
to M7/M8 (workspace auth/tokens, credential storage, web lineage
navigation, SSE progress, and similar) remain `pending` for v0.1.0 and
are tracked here for completeness against the full specification, not as
a v0.1.0 commitment.

Within that scope, this release is a standalone deterministic core, not a
hosted service: everything runs as a local CLI (`ragledger ...`) or a
Python library import against local files, with no server process,
database, or authentication layer. Two provider integrations are
intentionally stubbed rather than faked: the Sentence Transformers/OpenAI
embedding providers (FR-041) and Presidio PII analysis (FR-050) both
raise an explicit "not available" error instead of ever returning a
fabricated response; the deterministic reference embedder and the
regex/checksum PII recognizers are what this release actually runs on.

## Functional requirements

### 8.1 Workspace, auth and targets

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-001 | Local admin bootstrap, workspace and roles (owner/editor/viewer) | pending | M7 (server/API) scope; not part of v0.1.0 |
| FR-002 | API token scopes (sources, builds, targets, snapshots, reconciliations, policies, admin) | pending | M7 scope |
| FR-003 | Target credentials AES-256-GCM encrypted, write-only, versioned | pending | M7 scope. Target configs in this release are local YAML files the CLI reads directly (`ragledger.connectors.config`); there is no credential store to encrypt |
| FR-004 | Target URL SSRF-safe validation with explicit private-host allowlist | pending | M7 scope (a server accepting operator-supplied target URLs). The CLI's target configs are trusted local files, not a network-facing input surface |
| FR-005 | Workspace export excludes secrets and raw documents by default | pending | M7 scope; no workspace/export concept exists in this release |

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
| FR-020 | Docling primary parser for PDF/DOCX/HTML; native deterministic adapters for Markdown/TXT | implemented | Native deterministic adapters ship for text/markdown/HTML/JSON/CSV/PDF (`ragledger.pipeline.parsers.{text,markdown,html_parser,json_parser,csv_parser,pdf}`); `tests/pipeline/parsers/test_parser_*.py`. Gap: Docling itself (the spec's stated *primary* PDF/DOCX/HTML engine) and DOCX support are not implemented -- Docling is a heavy optional dependency with its own model downloads that this project's dependency budget does not cover; the native `pypdf`/`html.parser`-backed adapters serve as the fallback adapters FR-020 also names |
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
| FR-041 | Local Sentence Transformers adapter records model revision/digest, dimension, dtype, normalization | drafted | `ragledger.pipeline.embedding.SentenceTransformersEmbeddingProvider`/`SentenceTransformersConfig` implement the `EmbeddingProvider` protocol and accept real model/revision/dimension/device/batch-size config, but `embed()`/`tokenize()` raise `ProviderNotAvailableError` rather than making a live call -- the `sentence-transformers` library and its model download/cache are a documented gap, not a fake response; `tests/pipeline/test_embedding.py::TestUnwiredRealProviders`. `ragledger.yml`'s `embedding.mode: local` in this release resolves to the deterministic reference provider (see below), not this adapter; see M4's interpretation decision 3 |
| FR-042 | Batch size and device config recorded as deterministic evidence | implemented | `DeterministicLocalEmbeddingProvider.embed` records `usage={"batch_size": ...}`; GPU nondeterminism policy is not applicable since no GPU-backed provider ships in this release |
| FR-043 | Vector NaN/Inf values rejected | implemented | `ragledger.pipeline.embedding.validate_finite`; `tests/pipeline/test_embedding.py::TestValidation` |
| FR-044 | Declared model dimension validated against the first produced vector | implemented | `ragledger.pipeline.embedding.validate_dimension`; `tests/pipeline/test_embedding.py::TestValidation::test_dimension_mismatch_rejected` |
| FR-045 | Canonical vector hash over float32 little-endian bytes, with dtype metadata for non-float32 inputs | implemented | `ragledger.pipeline.embedding.vector_hash`; `tests/pipeline/test_embedding.py::TestVectorHash`. The observed-side counterpart (a connector-computed vector hash over an already-canonical-JSON float array, not raw IEEE-754 bytes) is `ragledger.connectors.base.hash_vector`; `ragledger.reconcile.engine` compares the two via `VECTOR_HASH_MISMATCH` |
| FR-046 | Raw vectors excluded from the manifest by default | implemented | `ragledger.core.models.EmbeddingRecord` carries no vector field at all (core, unmodified); `ragledger.pipeline.build` never writes vector data anywhere outside the local stage cache |
| FR-047 | Externally imported embedding metadata marked unknown when not explicitly provided | implemented | `ragledger.pipeline.embedding.ExternalImportEmbeddingProvider` (unset provider/name/revision default to the literal `"unknown"`, never guessed); `tests/pipeline/test_embedding.py::TestExternalImportProvider` |

**Reference embedder (not its own FR row):** `ragledger.pipeline.embedding.DeterministicLocalEmbeddingProvider` is a seeded hash-projection embedder producing unit-L2-normalized float32 vectors of configurable dimension -- explicitly documented as a deterministic reference/testing embedder, not a semantic model. `OpenAiEmbeddingProvider` is a second config-plumbing-only stub (cloud embedding is explicitly non-mandatory for v1 per PROJECT_SPEC.md section 5.1) with the same never-fake-a-response guarantee as FR-041's Sentence Transformers stub. This is the release's one honest limitation to call out loudest: nothing in v0.1.0 produces a semantically meaningful embedding on its own.

### 8.6 PII

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-050 | Presidio analyzer plus deterministic regex/checksum recognizers | implemented | Deterministic recognizers implemented: email, phone, IBAN (MOD-97 checksum), credit card (Luhn), US SSN (structural plausibility), Turkish TCKN (official two-stage checksum) -- `ragledger.governance.pii.default_recognizers`; `tests/governance/test_pii.py::TestDetectors`. Gap: Presidio itself is not integrated (it pulls in a spaCy language model this project does not vendor), documented in `pii.py`'s module docstring |
| FR-051 | Scanner language/config/version recorded | implemented | `ragledger.governance.pii.PiiScannerInfo` populated by `build_pii_scan_assertion`; `tests/governance/test_pii.py::TestAssertionConstruction::test_scanner_records_config_and_version` |
| FR-052 | No raw PII value written to any finding, database, or log | implemented | `ragledger.governance.pii.PiiFinding` never carries a raw value (only `masked_preview`/`value_hmac`); `ragledger.reconcile.taxonomy`/`engine` carry the same guarantee into reconciliation findings (`PII_POLICY_VIOLATION` evidence is `entity_type`/`confidence`/`masked_preview` only). Canary tests across both layers: `tests/governance/test_pii_leak_canary.py` (full pipeline), `tests/cli/test_report.py::test_report_no_canary_pii_value_leaks_into_json_or_html`, `tests/reconcile/test_pii_masking_canary.py`, `tests/cli/test_reconcile.py::test_reconcile_masked_evidence_canary_json_html_and_stdout` |
| FR-053 | PII scan runs separately over parsed source text and contextualized chunk text | implemented | `ragledger.pipeline.build._process_source` (source-version-level scan over parsed text) and `_process_chunks` (chunk-level scan over contextualized text); `tests/pipeline/test_build.py::test_pii_scan_finds_the_email_in_sample_txt`. Gap: a separate `raw_chunk`-text scan (distinct from `contextualized`, per PROJECT_SPEC.md section 40's edge case) is not implemented -- only source-level raw and chunk-level contextualized are covered |
| FR-054 | Allowlist/denylist custom recognizers in YAML, with bounded/timeout-safe regex | implemented | `ragledger.governance.pii.load_custom_recognizers` (YAML-configured custom entity types) and `_run_with_timeout` (`SIGALRM`-based regex timeout, proven against a catastrophic-backtracking pattern); `tests/governance/test_pii.py::TestCustomRecognizers`, `TestRegexTimeout`. Note: "allowlist/denylist" here is custom recognizer *definition*; entity-type allow/deny *policy* is `ragledger.pipeline.build.PiiPolicyConfig` (build time, block-list only) and `ragledger.reconcile.policy.PiiPolicy` (reconciliation time, deny/allow plus a confidence ceiling; see FR-056) |
| FR-055 | Zero findings reported as "no findings detected", never "guaranteed clean" | implemented | `ragledger.core.models.PiiScanStatus`; `tests/governance/test_pii.py::TestAssertionConstruction::test_no_findings_reported_as_no_findings_detected_not_guaranteed_clean` |
| FR-056 | Policy warn/block driven by entity type, confidence, and count | implemented | Two layers: (1) build time, `ragledger.pipeline.build.PiiPolicyConfig` blocks embedding for a configured entity-type/confidence combination (`tests/pipeline/test_build.py::test_pii_policy_blocks_embedding_for_denied_entity_types`); (2) reconciliation time, `ragledger.reconcile.policy.PiiPolicy` (`deny`/`allow`/`max_confidence_allowed`) drives `PII_POLICY_VIOLATION` findings and a full PASS/WARN/FAIL/INCONCLUSIVE verdict via `findings.fail_on_severity`/`warn_on_severity` (`tests/reconcile/test_policy.py`, `tests/reconcile/test_pii_masking_canary.py`). Gap: neither layer has a *count*-based threshold (e.g. "warn only if more than N findings of this type"); both are per-finding entity-type/confidence gates |

### 8.7 License

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-060 | License source precedence: user assertion, frontmatter, sidecar, path rule; content text is never a v1 fact | implemented | `ragledger.governance.license.evaluate_license`/`gather_candidates` (user assertion, sidecar, frontmatter incl. `SPDX-License-Identifier` header, path rule, repository default; never scans body prose); `tests/governance/test_license.py::TestPrecedenceAndConflicts`. Precedence interpretation documented in `license.py`'s module docstring: PROJECT_SPEC.md section 12.2 does not explicitly rank `user_assertion`, so this module ranks it highest as the most direct operator override |
| FR-061 | SPDX identifier/expression validated; unrecognized value becomes `NOASSERTION` | implemented | `ragledger.governance.license.validate_spdx_expression`; `tests/governance/test_license.py::TestExpressionValidation`. Gap: `_KNOWN_SPDX_IDENTIFIERS` is a small, honestly-labeled hand-maintained subset (`_LICENSE_LIST_VERSION = "ragledger-embedded-subset-1"`), not the full network-fetched official SPDX license list |
| FR-062 | Multiple conflicting assertions produce a conflict finding | implemented | `ragledger.governance.license.evaluate_license` (every candidate cross-references every other via `conflicting_assertion_ids` on disagreement); `tests/governance/test_license.py::TestPrecedenceAndConflicts::test_disagreement_produces_cross_referenced_conflicts` |
| FR-063 | Policy allow/deny/unknown license behavior | implemented | `ragledger.reconcile.policy.LicensesPolicy` (`allow`/`deny`/`unknown: fail\|warn\|allow`) evaluated by `_evaluate_licenses`/`_license_policy_findings`, producing `LICENSE_UNKNOWN`/`LICENSE_POLICY_VIOLATION` findings and folding into the overall PASS/WARN/FAIL verdict; `tests/reconcile/test_policy.py`, `tests/reconcile/test_engine.py` |
| FR-064 | License evidence stored as source locator/hash, not a required full text copy | implemented | `ragledger.core.models.LicenseAssertion` never carries license body text, only the resolved SPDX expression/method (core, unmodified); `ragledger.governance.license` never reads or stores full license text |

### 8.8 ACL and tenant

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-070 | Expected ACL canonical set derived from source metadata/path policy/sidecar | implemented | `ragledger.governance.acl.expected_acl_entries`/`AclConfig` (direct per-source mapping and path-glob rules); `tests/governance/test_acl.py::TestExpectedAclResolution`. Gap: a dedicated ACL sidecar file format (distinct from the direct/path-rule config) is not implemented, only for license |
| FR-071 | Principal identifiers normalized; redaction policy applied on public export | implemented | Expected-side: `ragledger.governance.acl.normalize_acl_entries` (`case_normalize` opt-in, off by default per PROJECT_SPEC.md section 40); `tests/governance/test_acl.py::TestNormalization`. Reconciliation-report redaction: `ragledger.reconcile.taxonomy.mask_acl_entry`/`mask_acl_entries` hash every ACL principal identifier (HMAC-keyed when a workspace secret is supplied, else plain SHA-256) before it reaches a finding's evidence, unconditionally -- not a configurable "policy" toggle, see M6's documented gap under FR-131/PolicyVerdict.principal_masking; `tests/reconcile/test_pii_masking_canary.py`, `tests/cli/test_reconcile.py::test_reconcile_masked_evidence_canary_json_html_and_stdout`. Gap: the manifest/`report manifest` command itself still surfaces raw ACL entry strings as recorded (no redaction on that path; only reconciliation findings are masked) |
| FR-072 | Expected tenant mandatory/optional policy | implemented | `ragledger.governance.acl.TenantConfig.required` drives a `TENANT_REQUIRED_BUT_MISSING` build warning in `ragledger.pipeline.build`; `tests/governance/test_acl.py::TestTenant` |
| FR-073 | Observed payload field mapping via target-configured JSONPath/column mapping | implemented | `ragledger.connectors.base.NormalizedPoint.payload_projection` plus each connector's configurable field mapping (`connectors/qdrant.py`, `connectors/pgvector.py` payload-path/column config); unresolved mapped fields are recorded as `missing_mapped_field:<name>` rather than fabricated. `tests/connectors/test_qdrant.py`, `test_pgvector.py` |
| FR-074 | Missing/broader/narrower/mismatched ACL reported as distinct finding types | implemented | `ragledger.reconcile.engine._compare_matched_pair` classifies an ACL mismatch into `ACL_MISSING` (expected non-empty, observed empty), `ACL_BROADER_THAN_SOURCE` (observed has entries expected does not, e.g. a `PUBLIC` leak), or `ACL_MISMATCH` (any other disagreement, which is where a narrower observed set also lands); `tests/reconcile/test_engine.py`, `test_pii_masking_canary.py`. Gap: "narrower" has no separate taxonomy code of its own -- it is the same `ACL_MISMATCH` code as any other non-broadening disagreement, since the 23-code taxonomy (verified against `docs/spec/policy-v1.schema.json`'s enum) does not define a fourth ACL code |
| FR-075 | Tenant missing/mismatch/cross-tenant duplicate can be marked critical by policy | implemented | `TENANT_MISSING`/`TENANT_MISMATCH` default to `FindingSeverity.CRITICAL` in `ragledger.reconcile.taxonomy.DEFAULT_SEVERITY`, and severity feeds `findings.fail_on_severity`/`warn_on_severity` policy gating like any other finding; `tests/reconcile/test_engine.py`, `test_policy.py`. Gaps: no distinct "cross-tenant duplicate" finding code exists (a duplicate point id/content across two different declared tenants is reported as `DUPLICATE_POINT_ID`/`DUPLICATE_CONTENT`, without a tenant-aware variant); and `PolicyDocument.access.acl_required`/`tenant_required` are accepted, schema-valid fields that `evaluate_policy`/`_evaluate_access` does not actually read (only `access.acl_compliance_min` is evaluated) |
| FR-076 | ACL canonical sort (order carries no semantics); wildcard is a distinct typed value | implemented | `ragledger.governance.acl.normalize_acl_entries` (deduplicated, sorted); `PUBLIC` is the typed wildcard value (section 12.3); `tests/governance/test_acl.py::TestValidation`, `TestNormalization` |

### 8.9 Build and manifest

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-080 | Build plan preview: source count, estimated parse/chunk/embed cost, resource caps | pending | Not implemented; no dry-run/plan-preview command exists |
| FR-081 | Pipeline stage artifact caching keyed by content/config hash | implemented | `ragledger.pipeline.cache.StageCache`/`stage_cache_key` (stage + input hash + adapter name/version + config hash); wired into parse/chunk/embed in `ragledger.pipeline.build`. Governance-stage (PII/license/ACL) caching is not implemented, a documented gap noted in `build.py`'s module docstring. `tests/pipeline/test_cache.py`, `tests/pipeline/test_build.py::test_cache_hits_on_second_run_against_the_same_cache_directory` |
| FR-082 | Same input/config/reproducible epoch produces a byte-identical canonical manifest | implemented | `ragledger.core.manifest.build_manifest`/`canonical_manifest_bytes`; `tests/core/test_canonical.py`, `tests/core/test_manifest.py::TestCanonicalBytesAndRoundtrip`, `tests/core/test_golden_manifests.py`. Extended to the full pipeline: `ragledger.pipeline.build.build_pipeline` with `BuildConfig.reproducible=True` produces byte-identical manifests across two runs, and this is exercised end to end through the CLI too: `tests/pipeline/test_build.py::test_determinism_two_runs_are_byte_identical`, `tests/cli/test_build.py::test_build_twice_with_the_same_epoch_is_byte_identical` |
| FR-083 | Partial build manifest marked `incomplete`; policy fails on it by default | implemented | `ragledger.pipeline.build.build_pipeline` sets `build.status="incomplete"` whenever any source's parse run fails; `tests/pipeline/test_build.py::test_no_parser_available_marks_build_incomplete_not_a_crash`, `test_broken_pdf_source_fails_parse_without_crashing_the_build`. The CLI enforces the "policy fails by default" half: `ragledger build` exits `3` on an incomplete manifest unless `--allow-incomplete` is passed (exit `0`, manifest still written either way); `tests/cli/test_build.py` |
| FR-084 | Manifest validated against its JSON Schema | implemented | `ragledger.core.manifest.validate_manifest_document`; `tests/core/test_manifest.py`, `tests/core/test_models.py`, `tests/core/test_golden_manifests.py`; wired into every CLI command that loads a manifest (`ragledger manifest validate`, `report manifest`, `reconcile`) |
| FR-085 | Manifest supports detached/embedded Ed25519 signature | implemented | `ragledger.core.signing.sign_manifest` attaches to the embedded `signatures[]` array (the `SignatureRecord` model also serializes standalone for a detached file); CLI: `ragledger manifest sign --key-file`; `tests/core/test_signing.py`, `tests/cli/test_manifest.py` |
| FR-086 | Signature key id is the public key fingerprint; private key never in the manifest | implemented | `ragledger.core.signing.fingerprint`; `tests/core/test_signing.py::TestRfc8032Vector`, `tests/core/test_signing.py::TestSignAndVerifyRoundtrip` |
| FR-087 | Verify command checks hash, schema, signature, and optional deep artifact checksums | implemented | `ragledger manifest verify MANIFEST [--public-key PATH]... [--deep] [--artifacts DIR]` (`ragledger.cli.commands.manifest_cmd.verify`) composes `ragledger.core.signing.verify_manifest` (hash/signature), schema validation on load, and `ragledger.core.artifacts.ArtifactStore.verify` for `--deep`; exit codes documented in the command's own docstring (`0` trusted, `2` valid-but-untrusted-key, `5` invalid/incomplete or a `--deep` mismatch); `tests/cli/test_manifest.py` covers all four `VerificationOverall` outcomes plus `--deep` pass/fail |

### 8.10 Index target and snapshot

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-090 | Target types: Qdrant, pgvector, NDJSON | implemented | `ragledger.connectors.{qdrant,pgvector,ndjson}`; `tests/connectors/test_{qdrant,pgvector,ndjson}.py`; CLI: `ragledger target add {qdrant\|pgvector}`, `ragledger snapshot` |
| FR-091 | Connectors use read-only credentials; no mutation API/SQL is ever issued | implemented | Interface has no mutation method (`ragledger.connectors.base.VectorTargetConnector`); transport-level guards: `qdrant.py::_guard_request`/`_is_allowed_request` (httpx request event hook allowlisting exactly `GET /collections/{name}`, `GET /aliases`, `POST .../points/scroll`), `pgvector.py::_assert_read_only_statement`/`_GuardedCursor` (statement allowlists only `SELECT`/`SHOW`) plus `connection.read_only = True` at the database level. Guard tests: `test_qdrant.py::test_guard_*` (parametrized over PUT/DELETE/PATCH/POST-elsewhere), `test_pgvector.py::test_assert_read_only_statement_blocks_mutations` (parametrized over INSERT/UPDATE/DELETE/DROP/ALTER/TRUNCATE/CREATE/GRANT/COPY), `test_guarded_cursor_*`. Live guard proof (skipped by default, `RAGLEDGER_IT=1`): `test_integration.py::test_qdrant_connector_read_only_guard_blocks_live_mutation`, `test_pgvector_connection_is_read_only_at_database_level` |
| FR-092 | Full snapshot uses cursor/scroll streaming with a resumable checkpoint | implemented | Qdrant: scroll `next_page_offset` chaining. pgvector: server-side named cursor + keyset `WHERE (pk...) > (...)`. NDJSON: sequential replay with canonical-key checkpoint. Library: `iterate_points(checkpoint=...)` on each connector. CLI: `ragledger snapshot --checkpoint/--resume`, writing a `<output>.checkpoint.json` sidecar. Tests: `test_iterate_points_resumes_from_checkpoint` in each connector test file, `tests/cli/test_snapshot.py` |
| FR-093 | Sample snapshot records explicit method/seed/rate; completeness-dependent policies become `INCONCLUSIVE` | partial | `ndjson.py::SnapshotHeader` carries `snapshot_kind`, `sample_method`, `sample_seed`, `sample_rate` fields for a writer to populate, and `ragledger.reconcile.engine`/`report.ConsistencyCaveat` correctly propagate `snapshot_kind` into a reconciliation report and the `requirements.full_snapshot` policy gate. No connector in this release actually performs sampling (only full iteration) -- the fields exist but nothing populates a non-`"full"` value in practice |
| FR-094 | Snapshot records target metadata (collection/table, dimension/distance, schema/index config, timestamp, connector version) | implemented | `TargetSchema` (`connectors/base.py`) from `inspect_target_schema()`; `SnapshotHeader` (`connectors/ndjson.py`) carries target_id/scope/target_type/vector_names/vector_dimensions/started_at/connector_version/consistency_mode/scope_filter. Tests: `test_ndjson.py::test_write_and_read_roundtrip`, fixture tests |
| FR-095 | Observed points normalized to a common field set | implemented | `NormalizedPoint` (`connectors/base.py`) implements the exact section 13.2 field list. Tests: `test_base.py` |
| FR-096 | Raw payload retention policy defaults to mapped fields only | implemented | `payload_projection` only ever contains the configured logical mapping fields (`source_id`/`source_version_id`/`chunk_id`/`embedding_id`/`tenant`/`acl`); no raw/unmapped payload is ever retained by either live connector. `apply_projection()` further restricts on request |
| FR-097 | Snapshots are immutable and content-hashed | implemented | `ndjson.py::SnapshotTrailer.content_hash` = SHA-256 over canonical point-line bytes, verified on every read; zstd frame checksum (`write_checksum=True`) as a second layer. Tests: `test_ndjson.py::test_tampered_*`, `test_corrupted_zstd_bytes_are_detected`, `test_missing_trailer_is_detected` |

### 8.11 Qdrant connector

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-100 | Collection config, named vector config, dimension/distance, payload index inventory | implemented | `qdrant.py::inspect_target_schema` parses `config.params.vectors` (named and unnamed) and `payload_schema`. Test: `test_qdrant.py::test_inspect_target_schema_*` |
| FR-101 | Scroll API pagination visits all points exactly once on a best-effort basis | implemented | `qdrant.py::iterate_points`; `test_iterate_points_streams_all_pages_in_order` |
| FR-102 | Vector retrieval defaults to false; enabling vector hashing surfaces a resource warning | implemented | `include_vectors` defaults `False` everywhere; when true, both connectors' `normalize_point` append a `vector_retrieval_enabled` warning to every point regardless of outcome |
| FR-103 | Payload mapping is configurable; missing fields become unknown | implemented | Unresolved mapped fields are omitted from `payload_projection` and recorded as `missing_mapped_field:<name>` in `normalization_warnings`, never fabricated |
| FR-104 | Qdrant point id string/number type preserved | implemented | `qdrant.py::_coerce_point_id` passes through `str`/`int` as-is. Test: `test_iterate_points_streams_all_pages_in_order` asserts `int` point ids round-trip |
| FR-105 | Collection aliases resolved to actual collection metadata | implemented | `qdrant.py::_resolve_alias` (best-effort `GET /aliases`); `TargetSchema.resolved_scope`. Test: `test_resolve_alias_returns_resolved_collection_name` |

### 8.12 pgvector connector

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-110 | Table/view, primary key, vector column, and mapped metadata columns explicitly configured | implemented | `config.py::PgvectorTargetConfig` |
| FR-111 | Identifiers are SQLAlchemy-quoted; no raw user SQL execution path | implemented (with a documented substitution) | SQLAlchemy is not in this project's permitted dependency list; `psycopg.sql.Identifier`/`sql.SQL` is used instead for the same safe-quoting guarantee, plus config-time identifier allowlisting (`config.py::_IDENTIFIER_PATTERN`). There is no code path that accepts or executes caller-supplied raw SQL |
| FR-112 | Read-only transaction, statement timeout, server-side cursor/keyset pagination | implemented | `pgvector.py::_configure_connection` (`read_only=True`, `IsolationLevel.REPEATABLE_READ`), `psycopg.connect(..., options="-c statement_timeout=...")`, named server-side cursor + `fetchmany` streaming, keyset `ORDER BY`/`WHERE (pk) > (...)` |
| FR-113 | Vector dimension/type/index metadata sourced from PostgreSQL and pgvector catalogs | implemented | `_fetch_vector_dimension` (`pg_attribute.atttypmod`), `_fetch_index_names` (`pg_index`/`pg_class`/`pg_am` join). Gap: distance/opclass decoding is not implemented -- `VectorFieldSchema.distance` is always `None` for pgvector (Qdrant's is populated, since Qdrant's collection config states distance directly) |
| FR-114 | Vector data not fetched by default; hash mode uses chunked queries | implemented | `vector_column` is only added to the `SELECT` list when `include_vectors=True`; streaming is always `fetchmany`-batched regardless |
| FR-115 | Composite primary keys produce a canonical JSON point id | implemented | `pgvector.py::normalize_point` builds a `dict` point id for multi-column primary keys. Test: `test_composite_primary_key_produces_object_point_id` |
| FR-116 | Row-level tenant filtering only via explicit parameterized configuration | implemented | `PgvectorTargetConfig.where`: allowlisted column names, parameterized `=`/`ANY(...)` only, no operators/raw fragments. `SnapshotHeader.scope_filter` records it. Test: `test_iterate_points_applies_row_level_where_filter`, `test_where_list_value_generates_any_predicate` |

**Section 13.3/13.4 consistency (not its own FR row):** Qdrant is always `ConsistencyMode.BEST_EFFORT_LIVE`, with a before/after `points_count` probe driving `SnapshotCompleteness`. pgvector's default `consistency: repeatable_read` opens one `REPEATABLE READ` read-only transaction per pass (`STRICT_CONSISTENT`, always `COMPLETE` by construction); `consistency: best_effort_paged` re-probes the row count after streaming and reports drift. `tests/connectors/test_qdrant.py::test_iterate_points_marks_snapshot_incomplete_on_count_drift`, `test_pgvector.py::test_repeatable_read_consistency_is_always_complete`, `test_best_effort_paged_marks_incomplete_on_drift`.

**Fixtures:** `tests/fixtures/snapshots/qdrant_support_kb.ndjson.zst` and `pgvector_document_chunks.ndjson.zst` are small (3-point), synthetic, committed snapshots (integer Qdrant point id, composite pgvector point id) used across `tests/connectors/`, `tests/cli/test_snapshot.py`, and `tests/cli/test_report.py`.

### 8.13 Reconciliation

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-120 | Expected manifest and observed snapshot scope compatibility check | implemented | `ragledger.reconcile.engine._preflight_schema_check` (acceptance scenario C: a target-schema dimension mismatch short-circuits before any point is streamed, emitting `EMBEDDING_DIMENSION_MISMATCH` rather than running a full, doomed scan); `tests/reconcile/test_acceptance_scenarios.py`, `test_engine.py` |
| FR-121 | Streaming hash-join reconciliation bounded to 1 GiB memory at 1M points | implemented | `ragledger.reconcile.engine.reconcile_big_data` (section 14.2 external sort/merge: both expected bindings and the observed connector stream are spilled to chunk-size-bounded sorted run files and k-way merged via `heapq.merge` at each closing matching level; leftovers stream directly into the next round's spill writer, never fully materialized). Verified functionally at 100k+ points with correct findings under a 30-second budget (`tests/reconcile/test_engine_big_data.py`) and by construction (chunk-size-bounded buffers); not verified by an explicit peak-RSS measurement at the literal 1M-point/1-GiB figure in CI. `reconcile_small_data`'s `max_in_memory_points` guard (used by `ragledger reconcile --auto`, the CLI default) is a correctness guard rail, not itself a streaming bound -- it fully materializes the observed stream before checking the count, then falls back to `reconcile_big_data` |
| FR-122 | Complete finding taxonomy | implemented | 23 codes (`ragledger.reconcile.taxonomy.FindingCode`), verified to match `docs/spec/policy-v1.schema.json`'s `$defs.taxonomyCode` enum member-for-member; `tests/reconcile/test_taxonomy.py::test_taxonomy_matches_policy_schema_enum` |
| FR-123 | Findings carry expected/observed evidence refs, severity, confidence, remediation | implemented | `ragledger.reconcile.taxonomy.Finding` (`locator`, `affected_lineage`, `evidence`, `severity`, `match_level`, `confidence`); remediation is a separate, joined artifact (`ragledger.reconcile.remediation.build_remediation_plan`, see FR-133); `tests/reconcile/test_taxonomy.py`, `test_engine.py` |
| FR-124 | Summary ratios reported with denominator and sample completeness | implemented | `ragledger.reconcile.report.Ratios`/`ratio()` (zero denominator is `None`, "not applicable" -- never coerced to 0.0 or 1.0) and `ConsistencyCaveat` (`completeness`, `snapshot_kind`, `degraded_confidence`); `tests/reconcile/test_report.py::test_ratio_zero_denominator_is_not_applicable` |
| FR-125 | Identical reconciliation inputs produce an idempotent cached result | pending | `Finding.fingerprint` is stable across reruns of the same logical input (the primitive a cache layer would need), but no caching/persistence layer exists in this release |
| FR-126 | Reconciliation history diff (new/resolved/persistent findings) | pending | Same underlying primitive (`Finding.fingerprint`) as FR-125; no history storage or diff computation is implemented |

### 8.14 Policy and remediation

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-130 | Typed YAML/JSON policy schema; unknown keys are a hard error | implemented | `docs/spec/policy-v1.schema.json` (design artifact, drafted in M0) is now load-bearing: `ragledger.reconcile.policy.load_policy_document` validates every policy document against it before parsing into the typed `PolicyDocument` model, whose own `extra="forbid"` fields are a second, independent unknown-key guard; `tests/reconcile/test_policy.py` |
| FR-131 | Rule categories: count/ratio, severity, source path/media/license/PII/ACL/tenant, age, completeness | partial | Two evaluation surfaces exist: (1) dedicated top-level policy blocks with real, category-specific semantics -- `findings.fail_on_severity`/`warn_on_severity` (severity), `pii`/`licenses` (PII, license), `access.acl_compliance_min` (a slice of ACL), `drift.{stale,orphan,missing}_ratio_max` (ratio) -- all implemented and tested (`tests/reconcile/test_policy.py`). (2) the generic `rules: []` list accepts all eleven schema categories, but `_evaluate_rule` only gives `count` and `severity` real distinct semantics; `ratio`/`source_path`/`media_type`/`license`/`pii`/`acl`/`tenant`/`age`/`completeness` inside `rules[]` are schema-valid but fall back to the same generic "count of matching findings" evaluation, since the schema's own `$defs.rule` has no field naming which ratio a `category: "ratio"` rule checks, and `Finding` does not carry a source URI or an age to compare `pattern`/`max_age_days` against -- documented in `policy.py`'s module docstring as a real schema gap, not silently worked around |
| FR-132 | Policy verdicts: PASS/WARN/FAIL/INCONCLUSIVE | implemented | `ragledger.reconcile.policy.evaluate_policy` (worst-of-all-rules ranking, `FAIL > INCONCLUSIVE > WARN > PASS`); `ragledger.reconcile.report.exit_code_for`/`render_ci_summary` map the verdict to a CI exit code and a plain-text summary line; `tests/reconcile/test_policy.py`, `test_report.py`, `tests/cli/test_reconcile.py` |
| FR-133 | Remediation plan lists read-only candidate operations only | implemented | `ragledger.reconcile.remediation.build_remediation_plan` groups findings into `RemediationAction`s (`reindex_source`, `delete_point_candidate`, `update_payload_candidate`, `full_rebuild_required`, `review_required`), each naming candidates, never an executable operation; `tests/reconcile/test_remediation.py` |
| FR-134 | Remediation plan never executes any action | implemented | Nothing in `ragledger.reconcile.remediation` imports or calls a connector; verified by module inspection and by `tests/reconcile/test_remediation.py` never constructing one |
| FR-135 | Remediation plan exportable as JSON/CSV, destructive candidates explicitly flagged | partial | JSON: part of the full reconciliation report (`ragledger.reconcile.report.to_json_bytes`, `ragledger reconcile --output`). `RemediationAction.destructive` plus an explicit `caution` string are set for delete/full-rebuild actions (`tests/reconcile/test_remediation.py::test_orphan_in_index_suggests_delete_candidate_and_is_destructive_with_caution`). `RemediationPlan.to_csv_rows()` (header-first row list) exists as a tested library method (`tests/reconcile/test_remediation.py::test_to_csv_rows_has_header_and_one_row_per_action`) but is not yet exposed as a `ragledger reconcile --format csv`/similar CLI option -- CSV export today requires calling the Python API directly |

### 8.15 Reporting and web

| ID | Title | Status | Evidence |
|---|---|---|---|
| FR-140 | JSON/NDJSON/CSV/HTML/SARIF/JUnit export formats | partial | JSON and HTML implemented for all three report kinds: manifest (`ragledger report manifest`), snapshot (`ragledger report snapshot`), and reconciliation (`ragledger reconcile --output/--html`) -- `ragledger.reports.{manifest_report,snapshot_report,reconciliation_report}`; `tests/cli/test_report.py`, `test_reconcile.py`. CSV exists only as `RemediationPlan.to_csv_rows()` (see FR-135), not CLI-wired. NDJSON, SARIF, and JUnit export are not implemented for any report kind |
| FR-141 | Web lineage navigation source -> chunk -> embedding -> point and reverse | pending | M8 (web UI) scope; not part of v0.1.0. The underlying lineage links (`AffectedLineage`, `Finding.locator`) exist in the reconciliation data model for a future UI to consume |
| FR-142 | Web findings filter, history, diff, policy, target health views | pending | M8 scope |
| FR-143 | Raw sensitive artifact reveal/download is audited | pending | M7/M8 scope (audit trail requires the persistence/API layer); no equivalent exists in the standalone CLI |
| FR-144 | SSE progress, cancel, and retry-failed-stage support | pending | M7/M8 scope. The CLI itself has no long-running/background job model to report progress for |

## Milestones

| Milestone | Scope | Status |
|---|---|---|
| M0 | Foundation: repository scaffolding, CI, Compose, base docs, schema skeletons, threat model and test strategy | done. `.github/workflows/ci.yml`, `docker-compose.yml`, `docs/architecture/threat-model.md`, `docs/architecture/adr/`, `docs/spec/{manifest-v1,policy-v1}.schema.json`, `docs/testing/test-matrix.md` |
| M1 | Identity and manifest core: canonicalization, stable IDs, manifest schema, artifacts, signing/verify | done |
| M2 | Source/parse/chunk pipeline: discovery, Docling/native parsers in a sandbox, structural artifacts, chunkers, caching | done (native parsers, not Docling; see FR-020) |
| M3 | Governance and embedding: local embeddings, PII, SPDX, ACL/tenant assertions, policy facts | done (deterministic reference embedder, not Sentence Transformers; see FR-041) |
| M4 | CLI build/report: standalone build, validate/sign/verify, JSON/HTML reporting | done. `ragledger {init,build,manifest validate\|sign\|verify,key generate,target add,snapshot,report manifest\|snapshot}`; `src/ragledger/cli/`, `src/ragledger/reports/{manifest_report,snapshot_report,_html}.py`; `tests/cli/`, `tests/reports/`; see `docs/reviews/m4-status-notes.md` for full command signatures and interpretation decisions |
| M5 | Connectors/snapshot: Qdrant, pgvector, NDJSON, checkpointing, consistency, read-only enforcement | done. `src/ragledger/connectors/{qdrant,pgvector,ndjson,base,config}.py`; 162 passed, 4 skipped (live-integration, `RAGLEDGER_IT=1`) in `tests/connectors/`; see `docs/reviews/m5-status-notes.md` for the full FR table and mutation-guard evidence |
| M6 | Reconciliation/policy: external merge, taxonomy, ratios, history, remediation plan, CI outputs | done. `src/ragledger/reconcile/{matching,taxonomy,engine,policy,remediation,report}.py`, plus its CLI wiring (`ragledger reconcile MANIFEST SNAPSHOT [--policy] [--output] [--html] [--work-dir] [--big-data\|--auto]`, `src/ragledger/cli/commands/reconcile_cmd.py`) and HTML report (`src/ragledger/reports/reconciliation_report.py`); `tests/reconcile/` (115 passed) plus `tests/cli/test_reconcile.py`; see `docs/reviews/m6-status-notes.md` for taxonomy/matching/policy design decisions. History diff (FR-126) and cached idempotent results (FR-125) remain pending, out of scope per those notes |
| M7 | Persistence/API/auth/jobs: Postgres/Redis/S3, credentials, SSRF protection, SSE, audit trail (deferred beyond v0.1.0) | planned |
| M8 | Web: all screens, lineage explorer, policy views, accessibility (deferred beyond v0.1.0) | planned |
| M9 | Hardening/release: performance, security, backup/restore, documentation, v1.0 (spec's own v1.0 milestone) | partial. v0.1.0's own release-readiness slice of M9 -- documentation (this file, `README.md`, `CHANGELOG.md`), a security self-review (`docs/reviews/v0.1-security.md`), and full-suite verification (`uv run ruff check .`, `ruff format --check .`, `mypy src`, `pytest -q`) -- is done for the M0-M6 scope; performance benchmarking beyond M6's own 100k-point test, a formal backup/restore story, and v1.0 itself (which needs M7/M8) remain pending |
