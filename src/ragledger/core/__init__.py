"""Identity and manifest core: canonicalization, hashing, stable IDs,
manifest v1 models/assembly, Ed25519 signing, and content-addressed
artifact storage.

See the design specification sections 6, 7, 11, and 33 for the specification
this package implements, and the individual modules for the design
notes behind specific choices:

- `ragledger.core.canonical`: RFC 8785 canonical JSON.
- `ragledger.core.hashing`: SHA-256 content hashing.
- `ragledger.core.ids`: stable, content-derived record IDs.
- `ragledger.core.models`: pydantic v2 manifest v1 record types.
- `ragledger.core.manifest`: manifest assembly, schema validation,
  canonical serialization.
- `ragledger.core.signing`: Ed25519 manifest signing and verification.
- `ragledger.core.artifacts`: local content-addressed artifact store.
"""

from __future__ import annotations

from ragledger.core.artifacts import ArtifactInfo, ArtifactStore, InvalidArtifactHashError
from ragledger.core.canonical import JSONValue, canonical_bytes, canonicalize
from ragledger.core.hashing import hash_canonical, hash_raw_bytes, hash_text, sha256_hex
from ragledger.core.manifest import (
    build_manifest,
    canonical_manifest_bytes,
    compute_manifest_hash,
    load_manifest,
    manifest_to_dict,
    signing_view_bytes,
    validate_manifest_document,
    write_manifest,
)
from ragledger.core.models import (
    AclAssertion,
    ArtifactRef,
    Assertion,
    BuildEnvironment,
    BuildRecord,
    ChunkMetadata,
    ChunkRecord,
    EmbeddingModelInfo,
    EmbeddingRecord,
    IndexBinding,
    Integrity,
    LicenseAssertion,
    ManifestEnvelope,
    OcrInfo,
    ParseRecord,
    PiiFinding,
    PiiScanAssertion,
    PiiScannerInfo,
    QualityAssertion,
    SignatureRecord,
    SourceRecord,
    SourceRelationship,
    Statistics,
    StructuralLocator,
    TenantAssertion,
    Tokenizer,
    WarningRecord,
)
from ragledger.core.signing import (
    SignatureStatus,
    SignatureVerification,
    VerificationOverall,
    VerificationResult,
    fingerprint,
    generate_keypair,
    read_private_key,
    read_public_key,
    sign_manifest,
    verify_manifest,
    write_private_key,
    write_public_key,
)

__all__ = [
    "AclAssertion",
    "ArtifactInfo",
    "ArtifactRef",
    "ArtifactStore",
    "Assertion",
    "BuildEnvironment",
    "BuildRecord",
    "ChunkMetadata",
    "ChunkRecord",
    "EmbeddingModelInfo",
    "EmbeddingRecord",
    "IndexBinding",
    "Integrity",
    "InvalidArtifactHashError",
    "JSONValue",
    "LicenseAssertion",
    "ManifestEnvelope",
    "OcrInfo",
    "ParseRecord",
    "PiiFinding",
    "PiiScanAssertion",
    "PiiScannerInfo",
    "QualityAssertion",
    "SignatureRecord",
    "SignatureStatus",
    "SignatureVerification",
    "SourceRecord",
    "SourceRelationship",
    "Statistics",
    "StructuralLocator",
    "TenantAssertion",
    "Tokenizer",
    "VerificationOverall",
    "VerificationResult",
    "WarningRecord",
    "build_manifest",
    "canonical_bytes",
    "canonical_manifest_bytes",
    "canonicalize",
    "compute_manifest_hash",
    "fingerprint",
    "generate_keypair",
    "hash_canonical",
    "hash_raw_bytes",
    "hash_text",
    "load_manifest",
    "manifest_to_dict",
    "read_private_key",
    "read_public_key",
    "sha256_hex",
    "sign_manifest",
    "signing_view_bytes",
    "validate_manifest_document",
    "verify_manifest",
    "write_manifest",
    "write_private_key",
    "write_public_key",
]
