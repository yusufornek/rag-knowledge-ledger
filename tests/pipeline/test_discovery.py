"""Tests for `ragledger.pipeline.discovery` (FR-010..FR-017)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ragledger.pipeline.discovery import (
    DiscoveryConfig,
    FileTooLargeError,
    SymlinkEscapesRootError,
    compute_tombstones,
    discover_sources,
    sniff_media_type,
)


def _write(root: Path, relative: str, content: str = "content") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class TestBasicDiscovery:
    def test_discovers_files_recursively_in_deterministic_order(self, tmp_path: Path) -> None:
        _write(tmp_path, "b.txt")
        _write(tmp_path, "a.txt")
        _write(tmp_path, "sub/c.txt")
        sources = discover_sources(tmp_path, "ns")
        assert [s.uri for s in sources] == ["file:a.txt", "file:b.txt", "file:sub/c.txt"]

    def test_every_source_has_a_stable_content_derived_id(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.txt", "hello")
        first = discover_sources(tmp_path, "ns")
        second = discover_sources(tmp_path, "ns")
        assert first[0].id == second[0].id
        assert first[0].version_id == second[0].version_id

    def test_uri_is_never_an_absolute_path(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.txt")
        sources = discover_sources(tmp_path, "ns")
        assert sources[0].uri == "file:a.txt"
        assert not sources[0].uri.startswith("/")

    def test_uri_is_posix_normalized_and_unicode_nfc(self, tmp_path: Path) -> None:
        import unicodedata

        # NFD-decomposed "café" (e + combining acute accent)
        nfd_name = unicodedata.normalize("NFD", "café") + ".txt"
        _write(tmp_path, nfd_name)
        sources = discover_sources(tmp_path, "ns")
        assert len(sources) == 1
        assert sources[0].uri == unicodedata.normalize("NFC", f"file:{nfd_name}")
        assert sources[0].uri == "file:café.txt"
        assert "/" not in sources[0].uri.replace("file:", "")  # single path segment, POSIX-style

    def test_modified_at_is_populated_but_not_identity(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.txt", "same content")
        first = discover_sources(tmp_path, "ns")[0]
        os.utime(tmp_path / "a.txt", (1000000, 1000000))
        second = discover_sources(tmp_path, "ns")[0]
        assert first.modified_at != second.modified_at
        assert first.version_id == second.version_id  # mtime never enters content identity


class TestIgnoreRules:
    def test_gitignore_pattern_excludes_matching_files(self, tmp_path: Path) -> None:
        _write(tmp_path, ".gitignore", "*.log\n")
        _write(tmp_path, "keep.txt")
        _write(tmp_path, "drop.log")
        sources = discover_sources(tmp_path, "ns")
        assert [s.uri for s in sources] == ["file:.gitignore", "file:keep.txt"]

    def test_ragledgerignore_pattern_excludes_matching_files(self, tmp_path: Path) -> None:
        _write(tmp_path, ".ragledgerignore", "secret/\n")
        _write(tmp_path, "secret/data.txt")
        _write(tmp_path, "public.txt")
        sources = discover_sources(tmp_path, "ns")
        uris = [s.uri for s in sources]
        assert "file:secret/data.txt" not in uris
        assert "file:public.txt" in uris

    def test_negation_re_includes_a_file(self, tmp_path: Path) -> None:
        _write(tmp_path, ".gitignore", "*.log\n!important.log\n")
        _write(tmp_path, "drop.log")
        _write(tmp_path, "important.log")
        sources = discover_sources(tmp_path, "ns")
        uris = [s.uri for s in sources]
        assert "file:important.log" in uris
        assert "file:drop.log" not in uris

    def test_git_directory_always_pruned(self, tmp_path: Path) -> None:
        _write(tmp_path, ".git/config", "internal git file")
        _write(tmp_path, "readme.txt")
        sources = discover_sources(tmp_path, "ns")
        assert [s.uri for s in sources] == ["file:readme.txt"]


class TestSizeCap:
    def test_file_over_max_bytes_raises(self, tmp_path: Path) -> None:
        _write(tmp_path, "big.txt", "x" * 100)
        with pytest.raises(FileTooLargeError):
            discover_sources(tmp_path, "ns", DiscoveryConfig(max_file_bytes=10))


class TestSymlinks:
    def test_symlinks_not_followed_by_default(self, tmp_path: Path) -> None:
        target = _write(tmp_path, "real.txt", "actual content")
        (tmp_path / "link.txt").symlink_to(target)
        sources = discover_sources(tmp_path, "ns")
        assert [s.uri for s in sources] == ["file:real.txt"]

    def test_symlink_escaping_root_rejected_when_followed(self, tmp_path: Path) -> None:
        outside_dir = tmp_path.parent / "outside_target_dir"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("outside root")
        root = tmp_path / "root"
        root.mkdir()
        (root / "escape.txt").symlink_to(outside_file)
        with pytest.raises(SymlinkEscapesRootError):
            discover_sources(root, "ns", DiscoveryConfig(follow_symlinks=True))

    def test_symlink_within_root_followed_when_enabled(self, tmp_path: Path) -> None:
        _write(tmp_path, "real.txt", "actual content")
        (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
        sources = discover_sources(tmp_path, "ns", DiscoveryConfig(follow_symlinks=True))
        assert {s.uri for s in sources} == {"file:real.txt", "file:link.txt"}


class TestDuplicateContent:
    def test_duplicate_content_produces_relationship_not_deletion(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.txt", "identical content")
        _write(tmp_path, "b.txt", "identical content")
        sources = discover_sources(tmp_path, "ns")
        assert len(sources) == 2
        by_uri = {s.uri: s for s in sources}
        assert by_uri["file:a.txt"].relationships == []
        assert by_uri["file:b.txt"].relationships[0].type == "duplicate_of"
        assert (
            by_uri["file:b.txt"].relationships[0].target_version_id
            == by_uri["file:a.txt"].version_id
        )
        assert all(s.status == "active" for s in sources)


class TestMediaTypeSniffing:
    def test_pdf_magic_bytes_detected(self, tmp_path: Path) -> None:
        path = tmp_path / "doc.bin"
        path.write_bytes(b"%PDF-1.4\n...")
        assert sniff_media_type(path, path.read_bytes()[:4096]) == "application/pdf"

    def test_extension_overrides(self, tmp_path: Path) -> None:
        path = tmp_path / "doc.md"
        path.write_text("# hi")
        assert sniff_media_type(path, path.read_bytes()) == "text/markdown"

    def test_html_tag_sniffed_without_matching_extension(self, tmp_path: Path) -> None:
        path = tmp_path / "doc.page"
        path.write_bytes(b"<html><body>hi</body></html>")
        assert sniff_media_type(path, path.read_bytes()) == "text/html"

    def test_binary_fallback(self, tmp_path: Path) -> None:
        path = tmp_path / "doc.unknownext"
        path.write_bytes(bytes(range(256)))
        assert sniff_media_type(path, path.read_bytes()) == "application/octet-stream"

    def test_no_file_is_silently_unclassified(self, tmp_path: Path) -> None:
        _write(tmp_path, "mystery.xyz", "plain readable text")
        sources = discover_sources(tmp_path, "ns")
        assert sources[0].media_type  # always populated, never omitted


class TestTombstones:
    def test_missing_source_becomes_tombstone(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.txt")
        _write(tmp_path, "b.txt")
        previous = discover_sources(tmp_path, "ns")
        (tmp_path / "b.txt").unlink()
        current = discover_sources(tmp_path, "ns")
        tombstones = compute_tombstones(previous, [s.id for s in current])
        assert len(tombstones) == 1
        assert tombstones[0].uri == "file:b.txt"
        assert tombstones[0].status == "tombstone"

    def test_no_tombstones_when_nothing_removed(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.txt")
        previous = discover_sources(tmp_path, "ns")
        current = discover_sources(tmp_path, "ns")
        assert compute_tombstones(previous, [s.id for s in current]) == []

    def test_already_tombstoned_sources_not_duplicated(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.txt")
        previous = discover_sources(tmp_path, "ns")
        already_tombstoned = previous[0].model_copy(update={"status": "tombstone"})
        assert compute_tombstones([already_tombstoned], []) == []
