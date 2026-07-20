"""Tests for `ragledger.pipeline.cache`."""

from __future__ import annotations

from pathlib import Path

from ragledger.pipeline.cache import StageCache, stage_cache_key


class TestStageCacheKey:
    def test_deterministic(self) -> None:
        key_a = stage_cache_key("parse", "h1", "adapter", "1.0", "cfg1")
        key_b = stage_cache_key("parse", "h1", "adapter", "1.0", "cfg1")
        assert key_a == key_b

    def test_different_stage_never_collides(self) -> None:
        parse_key = stage_cache_key("parse", "h1", "adapter", "1.0", "cfg1")
        chunk_key = stage_cache_key("chunk", "h1", "adapter", "1.0", "cfg1")
        assert parse_key != chunk_key

    def test_config_change_invalidates_key(self) -> None:
        key_a = stage_cache_key("embed", "h1", "adapter", "1.0", "cfg1")
        key_b = stage_cache_key("embed", "h1", "adapter", "1.0", "cfg2")
        assert key_a != key_b

    def test_adapter_version_change_invalidates_key(self) -> None:
        key_a = stage_cache_key("parse", "h1", "adapter", "1.0", "cfg1")
        key_b = stage_cache_key("parse", "h1", "adapter", "2.0", "cfg1")
        assert key_a != key_b

    def test_is_a_sha256_hex_digest(self) -> None:
        key = stage_cache_key("parse", "h1", "adapter", "1.0", "cfg1")
        assert len(key) == 64
        int(key, 16)


class TestStageCache:
    def test_miss_then_hit(self, tmp_path: Path) -> None:
        cache = StageCache(tmp_path)
        key = stage_cache_key("parse", "h1", "adapter", "1.0", "cfg1")
        assert cache.get(key) is None
        assert cache.stats.misses == 1
        cache.put(key, {"status": "success"})
        assert cache.get(key) == {"status": "success"}
        assert cache.stats.hits == 1

    def test_contains(self, tmp_path: Path) -> None:
        cache = StageCache(tmp_path)
        key = stage_cache_key("parse", "h1", "adapter", "1.0", "cfg1")
        assert not cache.contains(key)
        cache.put(key, [1, 2, 3])
        assert cache.contains(key)

    def test_persists_across_instances_on_the_same_directory(self, tmp_path: Path) -> None:
        key = stage_cache_key("embed", "h1", "adapter", "1.0", "cfg1")
        StageCache(tmp_path).put(key, {"vector": [0.1, 0.2]})
        second = StageCache(tmp_path)
        assert second.get(key) == {"vector": [0.1, 0.2]}

    def test_creates_root_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "nested" / "cache"
        StageCache(root)
        assert root.is_dir()
