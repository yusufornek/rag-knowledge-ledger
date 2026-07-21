"""`ragledger.yml` project configuration, per PROJECT_SPEC.md sections 17.3 and 41.

`RagledgerConfig` mirrors section 17.3's example field-for-field, plus
one additive, optional field (`embedding.dimension`) this CLI needs to
size its deterministic embedder -- see `docs/reviews/m4-status-notes.md`
for why. Every model here is `extra="forbid"` (`_StrictModel`),
matching section 41's "app config file unknown key hard error": a
typo'd or stale config key is a load-time `ConfigError`, never a
silently ignored field.

`validate_model_revisions_lock` implements section 17.3's model-lock
requirement literally: "model-revisions.lock ... Dosya eksikse veya
model entry'si mutable alias ise config validation build'i reddeder"
(a missing lock file, or a model entry pinned to a mutable alias,
rejects the build at config-validation time). This is enforced whenever
`embedding.mode: local` is configured, independent of which embedding
backend this release actually runs (see `ragledger.cli._build_support`
for that honest gap).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

_MUTABLE_ALIASES = frozenset({"main", "master", "latest", "head", "trunk", "hf-latest"})


class ConfigError(ValueError):
    """Raised for a malformed, missing, or schema-invalid `ragledger.yml` (or lock file)."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourcesConfig(_StrictModel):
    root: str = Field(min_length=1)
    include: list[str] = Field(default_factory=lambda: ["**/*"])


class OcrConfig(_StrictModel):
    enabled: bool = False
    languages: list[str] = Field(default_factory=lambda: ["eng"])


class ParserConfig(_StrictModel):
    name: str = "docling"
    ocr: OcrConfig = Field(default_factory=OcrConfig)


class ChunkerConfig(_StrictModel):
    strategy: str = "hybrid"
    max_tokens: int = Field(default=700, gt=0)
    overlap_tokens: int = Field(default=100, ge=0)
    tokenizer: str = "sentence-transformers/all-MiniLM-L6-v2"


class EmbeddingConfig(_StrictModel):
    mode: str = "deterministic"
    model: str = "sentence-transformers/all-MiniLM-L6-v2"
    revision_file: str | None = "./model-revisions.lock"
    normalize: bool = True
    dimension: int = Field(default=32, gt=0)
    """Not in PROJECT_SPEC.md section 17.3's literal example: sizes this
    release's deterministic reference embedder (see
    `ragledger.cli._build_support`). Optional, additive, defaults to
    `DeterministicLocalEmbeddingProvider`'s own default dimension."""


class GovernanceConfig(_StrictModel):
    pii: bool = True
    license_default: str = "NOASSERTION"
    acl_required: bool = True
    tenant_required: bool = True


class ManifestSectionConfig(_StrictModel):
    reproducible: bool = True


class RagledgerConfig(_StrictModel):
    version: int = 1
    namespace: str = Field(min_length=1)
    sources: SourcesConfig
    parser: ParserConfig = Field(default_factory=ParserConfig)
    chunker: ChunkerConfig = Field(default_factory=ChunkerConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    governance: GovernanceConfig = Field(default_factory=GovernanceConfig)
    manifest: ManifestSectionConfig = Field(default_factory=ManifestSectionConfig)


def load_config(path: Path) -> RagledgerConfig:
    """Load and strictly validate a `ragledger.yml` file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        data: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    try:
        return RagledgerConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{path} failed validation:\n{exc}") from exc


def validate_model_revisions_lock(path: Path, model_name: str) -> None:
    """Reject a build whose declared embedding model has no pinned, immutable revision.

    Per PROJECT_SPEC.md section 17.3. Expected shape::

        models:
          sentence-transformers/all-MiniLM-L6-v2:
            revision: 8b3219a92973c328a8e22fadcfa821b5dc75636
            files:
              config.json: sha256:...

    Only ``revision`` is required; ``files`` (per-file checksums) is
    optional and, when present, only checked for shape (non-empty
    strings), not resolved against any actual downloaded model file --
    this CLI never fetches a model.
    """
    if not path.is_file():
        raise ConfigError(
            f"model-revisions.lock not found at {path}: embedding model {model_name!r} has no "
            "pinned revision (PROJECT_SPEC.md section 17.3)"
        )
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    models = raw.get("models")
    if not isinstance(models, dict) or model_name not in models:
        raise ConfigError(f"{path} has no 'models' entry for embedding model {model_name!r}")
    entry = models[model_name]
    revision = entry.get("revision") if isinstance(entry, dict) else None
    if not isinstance(revision, str) or not revision.strip():
        raise ConfigError(f"{path} entry for {model_name!r} has no non-empty 'revision'")
    if revision.strip().lower() in _MUTABLE_ALIASES:
        raise ConfigError(
            f"{path} pins {model_name!r} to mutable alias {revision!r}; an immutable commit SHA "
            "or fixed version tag is required (PROJECT_SPEC.md section 17.3)"
        )
    files = entry.get("files") if isinstance(entry, dict) else None
    if files is not None and (
        not isinstance(files, dict)
        or not all(
            isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in files.items()
        )
    ):
        raise ConfigError(f"{path} entry for {model_name!r} has a malformed 'files' mapping")
