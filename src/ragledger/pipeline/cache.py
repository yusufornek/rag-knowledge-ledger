"""Content-addressed pipeline stage caching, per PROJECT_SPEC.md section 10.1.

A cache key is derived from the stage name, the input's content hash,
the adapter's identity and version, and the adapter config's canonical
hash: "Stage cache key input artifact hashes + tool exact version/config
hash." Because each stage's key is independent of every other stage's
config, changing PII/license/ACL policy invalidates only the governance
stage's cache entries and never forces parse or chunk to re-run (section
10.1: "Security policy/scanner değişimi assertion cache'i invalid eder
fakat parse/chunk tekrar gerekmez") -- `build.py` achieves that simply
by keying each stage independently through `stage_cache_key`, not
through any special-casing in this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ragledger.core.hashing import hash_canonical


def stage_cache_key(
    stage: str,
    input_hash: str,
    adapter_name: str,
    adapter_version: str,
    config_hash: str,
) -> str:
    """Derive a stage cache key from input identity and adapter identity/version/config."""
    return hash_canonical(
        {
            "stage": stage,
            "input_hash": input_hash,
            "adapter_name": adapter_name,
            "adapter_version": adapter_version,
            "config_hash": config_hash,
        }
    )


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0


class StageCache:
    """A local, content-addressed cache of JSON-serializable stage outputs.

    Not part of manifest identity: this is purely an execution-time
    optimization keyed by content, so cache presence/absence never
    changes a build's output, only how much work producing it takes.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self.stats = CacheStats()

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, key: str) -> Path:
        return self._root / f"{key}.json"

    def get(self, key: str) -> Any | None:
        path = self._path_for(key)
        if not path.is_file():
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, key: str, value: Any) -> None:
        path = self._path_for(key)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(value), encoding="utf-8")
        tmp_path.replace(path)

    def contains(self, key: str) -> bool:
        return self._path_for(key).is_file()
