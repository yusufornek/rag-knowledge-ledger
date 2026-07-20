"""Tests for `ragledger.core.artifacts`: the content-addressed local artifact store."""

from __future__ import annotations

from pathlib import Path

import pytest

from ragledger.core.artifacts import ArtifactStore, InvalidArtifactHashError
from ragledger.core.hashing import sha256_hex


class TestPutAndGet:
    def test_put_then_get_roundtrips_bytes(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        data = b"hello, artifact store"
        info = store.put(data)
        assert info.sha256 == sha256_hex(data)
        assert info.size_bytes == len(data)
        assert store.get(info.sha256) == data

    def test_put_is_idempotent(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        data = b"same content twice"
        first = store.put(data)
        second = store.put(data)
        assert first == second
        assert len(store.list()) == 1

    def test_get_missing_artifact_raises(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        with pytest.raises(FileNotFoundError):
            store.get(sha256_hex(b"never stored"))

    def test_layout_matches_spec_relative_artifacts_path(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        info = store.put(b"content")
        path = store.path_for(info.sha256)
        assert path == tmp_path / "artifacts" / info.sha256

    def test_failed_write_cleans_up_the_temporary_file_and_reraises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        store = ArtifactStore(tmp_path)

        def _failing_replace(*_args: object, **_kwargs: object) -> None:
            raise OSError("simulated disk failure")

        monkeypatch.setattr(os, "replace", _failing_replace)

        with pytest.raises(OSError, match="simulated disk failure"):
            store.put(b"content that will never land")

        leftover_temp_files = [
            entry for entry in (tmp_path / "artifacts").iterdir() if entry.name.startswith(".tmp-")
        ]
        assert leftover_temp_files == []


class TestExists:
    def test_exists_true_after_put(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        info = store.put(b"data")
        assert store.exists(info.sha256) is True

    def test_exists_false_for_unstored_digest(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        assert store.exists(sha256_hex(b"nope")) is False


class TestVerify:
    def test_verify_true_for_untouched_artifact(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        info = store.put(b"trustworthy bytes")
        assert store.verify(info.sha256) is True

    def test_verify_false_for_missing_artifact(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        assert store.verify(sha256_hex(b"missing")) is False

    def test_verify_false_for_corrupted_artifact(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        info = store.put(b"original content")
        on_disk_path = store.path_for(info.sha256)
        on_disk_path.write_bytes(b"corrupted content")
        assert store.verify(info.sha256) is False


class TestList:
    def test_list_is_sorted_and_complete(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        infos = [store.put(payload) for payload in (b"one", b"two", b"three")]
        expected = sorted(info.sha256 for info in infos)
        assert store.list() == expected

    def test_list_empty_store(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        assert store.list() == []

    def test_list_ignores_non_digest_named_files(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        store.put(b"real artifact")
        (tmp_path / "artifacts" / "not-a-digest.txt").write_text("stray file")
        assert all(len(name) == 64 for name in store.list())


class TestInvalidHashGuard:
    @pytest.mark.parametrize(
        "bad_digest",
        [
            "not-hex-at-all",
            "abc",  # too short
            "a" * 63,  # one short of 64
            "a" * 65,  # one too many
            "A" * 64,  # uppercase not accepted (only lowercase per schema pattern)
            "../../etc/passwd",
            "../" * 20 + "a" * 64,
        ],
    )
    def test_malformed_hash_is_rejected(self, tmp_path: Path, bad_digest: str) -> None:
        store = ArtifactStore(tmp_path)
        with pytest.raises(InvalidArtifactHashError):
            store.path_for(bad_digest)

    def test_path_traversal_digest_never_escapes_the_store_root(self, tmp_path: Path) -> None:
        store = ArtifactStore(tmp_path)
        traversal_attempt = "../" * 10 + "etc/passwd" + "a" * 54
        with pytest.raises(InvalidArtifactHashError):
            store.get(traversal_attempt)


class TestRoot:
    def test_root_property_and_directory_creation(self, tmp_path: Path) -> None:
        root = tmp_path / "nested" / "store"
        store = ArtifactStore(root)
        assert store.root == root
        assert (root / "artifacts").is_dir()
