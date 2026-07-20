# ADR 0002: Manifest signing algorithm

## Status

Accepted

## Context

Section 11 of the project specification requires manifests to carry a
content hash and an optional cryptographic signature so that a consumer
can detect tampering and, if they choose to trust a given signer's key,
verify authenticity. Key management for v1 is deliberately narrow: a
CLI-managed encrypted local key file, an environment/secret-mounted raw
key for CI, and a separate public-key verification path. There is no KMS
or HSM integration in v1, and the web UI does not accept private key
upload. The specification explicitly names Ed25519 as the intended
algorithm and defines the signing procedure: canonicalize the manifest
with `signatures` empty and `integrity.manifest_hash` omitted, produce
RFC 8785 canonical JSON bytes, take a SHA-256 digest, sign the digest
together with a fixed domain separator (`RAGLEDGER-MANIFEST-V1\0`) using
Ed25519, then attach `manifest_hash` and the signature record.

## Decision

Manifest signing uses Ed25519, implemented with the `cryptography`
package (`cryptography.hazmat.primitives.asymmetric.ed25519`).

Reasons:

- Ed25519 keys and signatures are small and fixed-size, which keeps the
  manifest's `signatures` array compact regardless of manifest size.
- Ed25519 signing is deterministic (no per-signature random nonce to get
  wrong) and fast enough that signing large manifests is not a
  bottleneck, which matters given manifests can contain very large
  numbers of chunk/embedding/index-binding records.
- `cryptography` is a widely used, actively maintained Python package
  with a stable Ed25519 API, avoiding a bespoke or less-audited
  cryptographic implementation.
- Ed25519 does not require parameter/curve choices the way RSA or
  classical ECDSA configurations do, which reduces the chance of a
  misconfigured signing setup and keeps verification logic simple:
  clients only need the signer's 32-byte public key.
- The key id in a signature record is defined as the SHA-256 fingerprint
  of the public key, which composes cleanly with Ed25519's small,
  fixed-size public keys and lets a trust store be a simple mapping from
  fingerprint to public key, with no certificate chain required for v1.

## Consequences

- Verifying a manifest requires only the signer's Ed25519 public key
  (or its fingerprint, resolved through a locally configured trust
  store); there is no built-in PKI/CA-based trust path in v1. An
  unrecognized key produces a cryptographically valid but explicitly
  `untrusted` verification result, per the specification's trust model.
- Key rotation is handled by keeping previously used public keys in the
  trust store as long as manifests signed with them need to remain
  verifiable, and by tracking revocation status separately from
  cryptographic validity; there is no automatic key-rotation protocol
  in v1.
- Because Ed25519 signing is deterministic and canonicalization is
  fixed by RFC 8785, signing the same manifest content with the same
  key always produces the same signature bytes, which is useful for
  reproducible-build verification but means signature bytes alone must
  never be treated as a nonce or freshness indicator; `signed_at` in the
  signature record carries that information instead.
- Post-quantum signature migration (if ever needed) would be a new
  signature algorithm value alongside `Ed25519` in the signature record,
  not a breaking change to the manifest envelope itself.
