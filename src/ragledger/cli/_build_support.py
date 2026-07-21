"""Maps a loaded `RagledgerConfig` + CLI flags to `ragledger.pipeline.build.BuildConfig`.

Two honest, documented simplifications versus a literal reading of
PROJECT_SPEC.md section 17.3 (see `docs/reviews/m4-status-notes.md` for
the full list):

- `parser:` config is validated for shape but never forwarded as
  `BuildConfig.parser_config`: every native parser this release ships
  (`ragledger.pipeline.parsers.*`) declares an empty allowed-config-key
  set (there is no `docling`/OCR adapter in this codebase), so passing
  the `parser.ocr` block through would only ever raise
  ``unknown parser config keys``.
- `embedding.mode: local` declares a real model name/revision (and is
  validated against `model-revisions.lock` accordingly), but this
  release's only working local embedding backend is
  `ragledger.pipeline.embedding.DeterministicLocalEmbeddingProvider` --
  a deterministic hash-projection reference embedder, not the named
  model. `SentenceTransformersEmbeddingProvider.embed()` raises
  `ProviderNotAvailableError` unconditionally in this release, so
  actually wiring it here would make every `local`-mode build crash.
  `make_embedding_provider` logs this substitution explicitly rather
  than silently pretending real inference happened.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ragledger.cli._config import ConfigError, RagledgerConfig, validate_model_revisions_lock
from ragledger.governance.acl import TenantConfig
from ragledger.governance.license import LicenseConfig
from ragledger.governance.pii import PiiScanConfig
from ragledger.pipeline.build import BuildConfig
from ragledger.pipeline.embedding import DeterministicLocalEmbeddingProvider, EmbeddingProvider

SUPPORTED_CHUNKERS = frozenset({"line_based", "hierarchical", "hybrid"})
SUPPORTED_EMBEDDING_MODES = frozenset({"none", "deterministic", "local"})

PII_SECRET_ENV_VAR = "RAGLEDGER_PII_HMAC_SECRET"
"""Optional workspace secret for `PiiScanConfig.workspace_secret` (section 12.1's
HMAC evidence). Never logged; read once, held only in memory for this process."""


def resolve_epoch(explicit_epoch: int | None) -> int | None:
    """Resolve a build/sign/snapshot timestamp epoch: `--epoch`, else `SOURCE_DATE_EPOCH`."""
    if explicit_epoch is not None:
        return explicit_epoch
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"SOURCE_DATE_EPOCH={raw!r} is not a valid integer") from exc


@dataclass(frozen=True)
class ResolvedTiming:
    created_at: datetime
    build_id: str
    reproducible: bool


def resolve_timing(epoch: int | None, *, force_reproducible: bool | None) -> ResolvedTiming:
    """Resolve `created_at`/`build_id`/`reproducible` for one build.

    An explicit epoch (`--epoch` or `SOURCE_DATE_EPOCH`) makes the build
    reproducible by default -- this is what PROJECT_SPEC.md section 7.2's
    "--reproducible mode" needs to actually produce a byte-identical
    manifest across runs, since `created_at` is otherwise real wall-clock
    time by construction (`ragledger.pipeline.build.BuildConfig`'s own
    documented default: real timing is honest telemetry, not a bug).
    `--reproducible`/`--no-reproducible` always overrides the epoch-based
    default when the caller passes it explicitly.
    """
    if epoch is not None:
        created_at = datetime.fromtimestamp(epoch, tz=UTC)
        reproducible = force_reproducible if force_reproducible is not None else True
    else:
        created_at = datetime.now(UTC)
        reproducible = bool(force_reproducible)
    build_id = "bld_" + created_at.strftime("%Y%m%dT%H%M%SZ")
    return ResolvedTiming(created_at=created_at, build_id=build_id, reproducible=reproducible)


def make_embedding_provider(
    config: RagledgerConfig, config_dir: Path, *, log: Callable[[str], None]
) -> EmbeddingProvider | None:
    """Resolve `embedding.mode` to a concrete provider, or `None` for metadata-only builds."""
    mode = config.embedding.mode
    if mode not in SUPPORTED_EMBEDDING_MODES:
        raise ConfigError(
            f"embedding.mode {mode!r} is not supported by this CLI release "
            f"(supported: {sorted(SUPPORTED_EMBEDDING_MODES)})"
        )
    if mode == "none":
        return None
    if mode == "local":
        if config.embedding.revision_file:
            lock_path = (config_dir / config.embedding.revision_file).resolve()
            validate_model_revisions_lock(lock_path, config.embedding.model)
        log(
            f"embedding.mode 'local' declares model {config.embedding.model!r}; this release's "
            "local embedding backend is the deterministic hash-projection reference embedder "
            "(ragledger.pipeline.embedding.DeterministicLocalEmbeddingProvider), not live "
            f"{config.embedding.model!r} inference -- see IMPLEMENTATION_STATUS.md"
        )
    return DeterministicLocalEmbeddingProvider(dimension=config.embedding.dimension, seed=0)


def build_config_from_ragledger_config(
    config: RagledgerConfig,
    *,
    root: Path,
    config_dir: Path,
    build_id: str,
    created_at: datetime,
    reproducible: bool,
    log: Callable[[str], None],
) -> BuildConfig:
    """Translate a validated `ragledger.yml` + resolved timing into a `BuildConfig`."""
    chunker_name = config.chunker.strategy
    if chunker_name not in SUPPORTED_CHUNKERS:
        raise ConfigError(
            f"chunker.strategy {chunker_name!r} is unknown "
            f"(supported: {sorted(SUPPORTED_CHUNKERS)})"
        )

    pii_secret = os.environ.get(PII_SECRET_ENV_VAR)
    pii_config = (
        PiiScanConfig(workspace_secret=pii_secret.encode("utf-8") if pii_secret else None)
        if config.governance.pii
        else None
    )

    if config.governance.acl_required:
        log(
            "governance.acl_required=true is recorded, but ragledger.yml has no ACL "
            "source-entry configuration surface in this release; no ACL assertions are "
            "produced from it (see docs/reviews/m4-status-notes.md)"
        )

    return BuildConfig(
        namespace=config.namespace,
        root=root,
        build_id=build_id,
        created_at=created_at,
        chunker_name=chunker_name,
        chunker_config={
            "max_tokens": config.chunker.max_tokens,
            "overlap_tokens": config.chunker.overlap_tokens,
        },
        embedding_provider=make_embedding_provider(config, config_dir, log=log),
        embedding_normalization="l2" if config.embedding.normalize else "none",
        pii_config=pii_config,
        license_config=LicenseConfig(repository_default=config.governance.license_default),
        acl_config=None,
        tenant_config=TenantConfig(required=config.governance.tenant_required),
        reproducible=reproducible,
    )
