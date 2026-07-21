"""Filesystem source discovery, per PROJECT_SPEC.md section 8.2 (FR-010..FR-017).

Recursively walks a root directory, applies `.gitignore`- and
`.ragledgerignore`-style ignore rules, streams a SHA-256 content hash
for every non-ignored file (FR-015: no full file held in memory at
once), sniffs its media type from magic bytes plus extension (FR-013),
and returns one `SourceRecord` per file in deterministic path-sorted
order (`discover_sources`).

Known simplification, documented rather than hidden: only a root-level
`.gitignore`/`.ragledgerignore` is read (not per-directory nested
ignore files layered git-style down the tree). This covers the common
single-ignore-file case exactly; full nested-directory gitignore
layering is tracked as a gap in `IMPLEMENTATION_STATUS.md`.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ragledger.core import ids
from ragledger.core.models import SourceRecord, SourceRelationship

_DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
"""FR-014's stated default: 100 MiB."""

_HASH_CHUNK_SIZE = 1024 * 1024
_SNIFF_SAMPLE_SIZE = 4096
_IGNORE_FILENAMES = (".gitignore", ".ragledgerignore")
_ALWAYS_IGNORED_DIR_NAMES = frozenset({".git"})
_HTML_TAG_RE = re.compile(rb"<\s*html", re.IGNORECASE)


class DiscoveryError(RuntimeError):
    """Base class for discovery-time errors."""


class SymlinkEscapesRootError(DiscoveryError):
    """A followed symlink resolves outside the discovery root (FR-011)."""


class SourceChangedDuringReadError(DiscoveryError):
    """A file's size/mtime changed between the pre- and post-hash stat,
    even after one retry (PROJECT_SPEC.md section 40).
    """


class FileTooLargeError(DiscoveryError):
    """A file exceeds `DiscoveryConfig.max_file_bytes` (FR-014)."""


@dataclass(frozen=True)
class DiscoveryConfig:
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES
    follow_symlinks: bool = False
    source_system: str = "local_fs"
    discovered_by: str = "filesystem_scan"


# --------------------------------------------------------------------------
# Ignore rules (a practical gitignore-syntax subset)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _IgnoreRule:
    regex: re.Pattern[str]
    negation: bool


def _glob_to_regex(pattern: str) -> str:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern[index : index + 3] == "**/":
            parts.append("(?:.*/)?")
            index += 3
            continue
        if pattern[index:] == "**":
            parts.append(".*")
            index += 2
            continue
        char = pattern[index]
        if char == "*":
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(char))
        index += 1
    return "".join(parts)


def _compile_ignore_pattern(pattern: str) -> _IgnoreRule:
    negation = pattern.startswith("!")
    if negation:
        pattern = pattern[1:]
    if pattern.endswith("/"):
        pattern = pattern[:-1]
    anchored = pattern.startswith("/")
    if anchored:
        pattern = pattern[1:]
    prefix = "^" if anchored else "^(?:.*/)?"
    return _IgnoreRule(
        regex=re.compile(prefix + _glob_to_regex(pattern) + "(?:/.*)?$"), negation=negation
    )


def _read_ignore_rules(root: Path) -> list[_IgnoreRule]:
    rules: list[_IgnoreRule] = []
    for filename in _IGNORE_FILENAMES:
        ignore_file = root / filename
        if not ignore_file.is_file():
            continue
        for line in ignore_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            rules.append(_compile_ignore_pattern(stripped))
    return rules


def _is_ignored(relative_posix: str, rules: Sequence[_IgnoreRule]) -> bool:
    matched = False
    for rule in rules:
        if rule.regex.match(relative_posix):
            matched = not rule.negation
    return matched


# --------------------------------------------------------------------------
# Media type sniffing (FR-013)
# --------------------------------------------------------------------------

_EXTENSION_OVERRIDES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
}


def sniff_media_type(path: Path, sample: bytes) -> str:
    """Detect a file's media type from magic bytes plus its extension (FR-013).

    Every file gets a media type -- there is no "unsupported, skip
    silently" outcome here; a file this cannot confidently classify
    still gets `application/octet-stream` or `text/plain`, and the
    parser stage is what reports "no parser available" for it later, as
    an explicit finding rather than a silent gap.
    """
    if sample.startswith(b"%PDF-"):
        return "application/pdf"
    suffix = path.suffix.lower()
    if suffix in _EXTENSION_OVERRIDES:
        return _EXTENSION_OVERRIDES[suffix]
    stripped = sample.lstrip()
    if stripped.startswith(b"<") and _HTML_TAG_RE.search(sample):
        return "text/html"
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed:
        return guessed
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    return "text/plain"


# --------------------------------------------------------------------------
# Streaming hash (FR-015)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _HashResult:
    content_hash: str
    size_bytes: int
    sample: bytes


def _hash_streaming(path: Path) -> _HashResult:
    hasher = hashlib.sha256()
    size = 0
    sample = b""
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK_SIZE)
            if not chunk:
                break
            if not sample:
                sample = chunk[:_SNIFF_SAMPLE_SIZE]
            hasher.update(chunk)
            size += len(chunk)
    return _HashResult(content_hash=hasher.hexdigest(), size_bytes=size, sample=sample)


# --------------------------------------------------------------------------
# Walking and record construction
# --------------------------------------------------------------------------


def _walk_files(
    root: Path, config: DiscoveryConfig, rules: Sequence[_IgnoreRule]
) -> list[tuple[PurePosixPath, Path]]:
    # Pairs of (NFC-normalized identity path, original OS relative path). Identity,
    # ignore matching and ordering use the NFC form; file access must use the
    # original form because non-normalizing filesystems (ext4) store raw bytes.
    found: list[tuple[PurePosixPath, Path]] = []
    for current_dir, dirnames, filenames in os.walk(root, followlinks=config.follow_symlinks):
        dirnames[:] = sorted(name for name in dirnames if name not in _ALWAYS_IGNORED_DIR_NAMES)
        for filename in sorted(filenames):
            absolute = Path(current_dir) / filename
            os_relative = absolute.relative_to(root)
            relative_str = unicodedata.normalize("NFC", os_relative.as_posix())
            if _is_ignored(relative_str, rules):
                continue
            if absolute.is_symlink():
                if not config.follow_symlinks:
                    continue
                resolved = absolute.resolve()
                try:
                    resolved.relative_to(root)
                except ValueError as exc:
                    raise SymlinkEscapesRootError(
                        f"symlink {relative_str!r} resolves outside the discovery root"
                    ) from exc
            found.append((PurePosixPath(relative_str), os_relative))
    return sorted(found, key=lambda pair: str(pair[0]))


def _build_source_record(
    namespace: str, relative: PurePosixPath, absolute: Path, config: DiscoveryConfig
) -> SourceRecord:
    uri = f"file:{relative}"
    result: _HashResult | None = None
    for attempt in range(2):
        stat_before = absolute.stat()
        if stat_before.st_size > config.max_file_bytes:
            raise FileTooLargeError(
                f"{relative} is {stat_before.st_size} bytes, exceeding "
                f"max_file_bytes={config.max_file_bytes}"
            )
        result = _hash_streaming(absolute)
        stat_after = absolute.stat()
        if (
            stat_before.st_size == stat_after.st_size
            and stat_before.st_mtime_ns == stat_after.st_mtime_ns
        ):
            break
        if attempt == 1:
            raise SourceChangedDuringReadError(
                f"{relative} changed while being hashed (PROJECT_SPEC.md section 40)"
            )
    assert result is not None  # the loop always assigns or raises
    media_type = sniff_media_type(absolute, result.sample)
    source_id_value = ids.source_id(namespace, uri)
    version_id_value = ids.source_version_id(source_id_value, result.content_hash)
    return SourceRecord(
        id=source_id_value,
        version_id=version_id_value,
        namespace=namespace,
        uri=uri,
        media_type=media_type,
        size_bytes=result.size_bytes,
        content_hash=result.content_hash,
        modified_at=datetime.fromtimestamp(stat_before.st_mtime, tz=UTC),
        discovered_by=config.discovered_by,
        source_system=config.source_system,
        status="active",
    )


def _attach_duplicate_relationships(sources: Sequence[SourceRecord]) -> list[SourceRecord]:
    """Mark every source after the first with a matching `content_hash` (FR-016).

    ``sources`` must already be in deterministic order; the first
    occurrence of a given content hash (by that order) is treated as
    the canonical copy every later duplicate points at, so the result
    is stable across runs. Nothing is ever removed -- both records
    remain `active`.
    """
    canonical_by_hash: dict[str, SourceRecord] = {}
    result: list[SourceRecord] = []
    for source in sources:
        canonical = canonical_by_hash.get(source.content_hash)
        if canonical is None:
            canonical_by_hash[source.content_hash] = source
            result.append(source)
        else:
            relationship = SourceRelationship(
                type="duplicate_of", target_version_id=canonical.version_id
            )
            result.append(
                source.model_copy(update={"relationships": [*source.relationships, relationship]})
            )
    return result


def discover_sources(
    root: Path, namespace: str, config: DiscoveryConfig | None = None
) -> list[SourceRecord]:
    """Discover every non-ignored file under `root` as a `SourceRecord`.

    Returns records sorted deterministically by relative URI (FR-032's
    "deterministic order" requirement extends to discovery: the same
    directory tree always produces the same record order).
    """
    config = config if config is not None else DiscoveryConfig()
    root = Path(root).resolve()
    rules = _read_ignore_rules(root)
    pairs = _walk_files(root, config, rules)
    sources = [
        _build_source_record(namespace, relative, root / os_relative, config)
        for relative, os_relative in pairs
    ]
    return _attach_duplicate_relationships(sources)


def compute_tombstones(
    previous_sources: Iterable[SourceRecord], discovered_source_ids: Iterable[str]
) -> list[SourceRecord]:
    """Diff a previous manifest's active sources against a new discovery pass (FR-017).

    Any previously active source whose `id` was not rediscovered becomes
    a tombstone candidate: a copy of its last-known record with
    `status="tombstone"`. Pass only the most recent record per
    `source_id` from the previous manifest; this function does not
    deduplicate source history itself. Nothing is deleted -- the caller
    decides whether to include the returned tombstone records in the
    next manifest.
    """
    discovered = set(discovered_source_ids)
    seen: set[str] = set()
    tombstones: list[SourceRecord] = []
    for source in previous_sources:
        if source.id in discovered or source.id in seen or source.status == "tombstone":
            continue
        seen.add(source.id)
        tombstones.append(source.model_copy(update={"status": "tombstone"}))
    return sorted(tombstones, key=lambda item: item.uri)


__all__ = [
    "DiscoveryConfig",
    "DiscoveryError",
    "FileTooLargeError",
    "SourceChangedDuringReadError",
    "SymlinkEscapesRootError",
    "compute_tombstones",
    "discover_sources",
    "sniff_media_type",
]
