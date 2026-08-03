"""A content-addressed local artifact store.

Stores arbitrary bytes under a caller-supplied root directory, addressed
by their SHA-256 digest, following the relative layout the design specification
section 33.5 describes for portable manifest exports
(``artifacts/<sha256>``). This is a plain local filesystem store: no
signed URLs, no bucket credentials, no remote calls -- those concerns
belong to a future object-storage backend, not this module.

Every hash a caller passes in is validated as a well-formed 64-character
lowercase hex SHA-256 digest before it is used to build a filesystem
path, which is both a basic path-traversal guard and, per the design specification
section 33.5's "ZIP traversal guard" note for portable exports, the
right layer to enforce it: a malformed or malicious digest string is
rejected before it ever reaches `pathlib.Path`.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ragledger.core.hashing import sha256_hex

_SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")


class InvalidArtifactHashError(ValueError):
    """Raised when a caller-supplied hash string is not a valid SHA-256 hex digest."""


@dataclass(frozen=True)
class ArtifactInfo:
    sha256: str
    size_bytes: int


class ArtifactStore:
    """A content-addressed store of artifact bytes rooted at a local directory."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._objects_dir = self._root / "artifacts"
        self._objects_dir.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, digest: str) -> Path:
        if not _SHA256_HEX_RE.match(digest):
            raise InvalidArtifactHashError(f"not a valid sha256 hex digest: {digest!r}")
        return self._objects_dir / digest

    def path_for(self, digest: str) -> Path:
        """Return the on-disk path for ``digest``, without requiring it to exist."""
        return self._path_for(digest)

    def put(self, data: bytes) -> ArtifactInfo:
        """Store ``data``, addressed by its SHA-256 digest, and return its info.

        Idempotent: storing the same bytes twice writes the file once.
        The write is atomic (write to a temporary file in the same
        directory, then ``os.replace``), so a reader never observes a
        partially written artifact.
        """
        digest = sha256_hex(data)
        target = self._path_for(digest)
        if not target.exists():
            descriptor, tmp_name = tempfile.mkstemp(dir=self._objects_dir, prefix=".tmp-")
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(data)
                os.replace(tmp_name, target)
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        return ArtifactInfo(sha256=digest, size_bytes=len(data))

    def exists(self, digest: str) -> bool:
        return self._path_for(digest).is_file()

    def get(self, digest: str) -> bytes:
        """Return the stored bytes for ``digest``.

        Raises `FileNotFoundError` if no artifact with that digest has
        been stored.
        """
        path = self._path_for(digest)
        if not path.is_file():
            raise FileNotFoundError(f"artifact not found: {digest}")
        return path.read_bytes()

    def verify(self, digest: str) -> bool:
        """Recompute the on-disk bytes' hash and compare it to ``digest``.

        Returns ``False`` (rather than raising) both when the artifact
        is missing and when its content no longer matches its claimed
        digest, since both are integrity findings a caller should
        handle the same way: do not trust this artifact.
        """
        path = self._path_for(digest)
        if not path.is_file():
            return False
        return sha256_hex(path.read_bytes()) == digest

    def list(self) -> list[str]:
        """Return the sorted SHA-256 digests of every artifact currently stored."""
        names = (entry.name for entry in self._objects_dir.iterdir() if entry.is_file())
        return sorted(name for name in names if _SHA256_HEX_RE.match(name))
