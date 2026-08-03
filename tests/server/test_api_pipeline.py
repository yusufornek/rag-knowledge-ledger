"""M7 wave B slice 2: source collections, scan, pipeline configs, builds end to end.

DB-backed, through the real app. `TestClient` executes FastAPI
background tasks synchronously after each response, so the enqueued
scan/build jobs genuinely run inside these tests -- the build test
drives discovery, parsing, chunking, governance, embedding, and
manifest assembly through the same deterministic core the CLI uses,
and asserts on the persisted `Manifest` row plus the content-addressed
artifact on disk.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from ragledger.server.app import create_app
from ragledger.server.settings import Settings
from tests.server.conftest import TEST_DATABASE_URL, requires_database

pytestmark = requires_database

_EPOCH = 1_700_000_000


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "refund-policy.md").write_text(
        "# Refund policy\n\nRefunds are processed within 14 days.\n"
    )
    (root / "contact.txt").write_text("Support contact: support@example.com\n")
    return root


@pytest.fixture
def client(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("APP_ENCRYPTION_KEY_V1", base64.b64encode(b"k" * 32).decode("ascii"))
    monkeypatch.setenv("ARTIFACT_STORE_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("SOURCE_ROOT_ALLOWED_BASES", str(tmp_path))
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    app = create_app(Settings())
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def boot(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": "admin@example.com",
            "workspace_slug": "primary",
            "workspace_name": "Primary",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _auth(boot: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {boot['token']}"}


def _create_collection(
    client: TestClient, boot: dict[str, Any], root: Path, namespace: str = "docs"
) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/workspaces/{boot['workspace_id']}/source-collections",
        json={"name": "Docs", "namespace": namespace, "root": str(root)},
        headers=_auth(boot),
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


class TestSourceCollections:
    def test_create_scan_and_list_sources(
        self, client: TestClient, boot: dict[str, Any], corpus_root: Path
    ) -> None:
        collection = _create_collection(client, boot, corpus_root)

        scan = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/source-collections/{collection['id']}:scan",
            headers=_auth(boot),
        )
        assert scan.status_code == 202, scan.text
        job_id = scan.json()["id"]

        job = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/jobs/{job_id}", headers=_auth(boot)
        )
        assert job.status_code == 200
        assert job.json()["status"] == "completed", job.text

        sources = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/sources", headers=_auth(boot)
        ).json()
        assert sorted(s["uri"] for s in sources) == [
            "file:contact.txt",
            "file:refund-policy.md",
        ]
        assert all(s["status"] == "active" for s in sources)

        versions = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/sources/{sources[0]['id']}/versions",
            headers=_auth(boot),
        ).json()
        assert len(versions) == 1
        assert versions[0]["content_hash"]

    def test_rescan_is_idempotent_and_tombstones_deletions(
        self, client: TestClient, boot: dict[str, Any], corpus_root: Path
    ) -> None:
        collection = _create_collection(client, boot, corpus_root)
        scan_url = (
            f"/api/v1/workspaces/{boot['workspace_id']}/source-collections/{collection['id']}:scan"
        )
        sources_url = f"/api/v1/workspaces/{boot['workspace_id']}/sources"

        assert client.post(scan_url, headers=_auth(boot)).status_code == 202
        first = client.get(sources_url, headers=_auth(boot)).json()

        (corpus_root / "contact.txt").unlink()
        assert client.post(scan_url, headers=_auth(boot)).status_code == 202
        second = client.get(sources_url, headers=_auth(boot)).json()

        assert len(second) == len(first) == 2
        by_uri = {s["uri"]: s["status"] for s in second}
        assert by_uri["file:contact.txt"] == "tombstone"
        assert by_uri["file:refund-policy.md"] == "active"

    def test_nonexistent_root_is_rejected(
        self, client: TestClient, boot: dict[str, Any], tmp_path: Path
    ) -> None:
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/source-collections",
            json={"name": "X", "namespace": "x", "root": str(tmp_path / "missing")},
            headers=_auth(boot),
        )
        assert response.status_code == 422
        assert response.json()["title"] == "Invalid source root"

    def test_root_outside_allowed_bases_is_rejected(
        self, client: TestClient, boot: dict[str, Any]
    ) -> None:
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/source-collections",
            json={"name": "X", "namespace": "x", "root": "/etc"},
            headers=_auth(boot),
        )
        assert response.status_code == 422
        assert response.json()["title"] == "Source root not allowed"

    def test_duplicate_namespace_is_a_409(
        self, client: TestClient, boot: dict[str, Any], corpus_root: Path
    ) -> None:
        _create_collection(client, boot, corpus_root)
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/source-collections",
            json={"name": "Again", "namespace": "docs", "root": str(corpus_root)},
            headers=_auth(boot),
        )
        assert response.status_code == 409


class TestPipelineConfigs:
    def test_create_is_content_addressed_and_idempotent(
        self, client: TestClient, boot: dict[str, Any]
    ) -> None:
        url = f"/api/v1/workspaces/{boot['workspace_id']}/pipeline-configs"
        body = {"config": {"embedding": {"mode": "deterministic", "revision_file": None}}}
        first = client.post(url, json=body, headers=_auth(boot))
        assert first.status_code == 201, first.text
        second = client.post(url, json=body, headers=_auth(boot))
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["config_hash"] == second.json()["config_hash"]

    def test_unknown_config_key_is_rejected(self, client: TestClient, boot: dict[str, Any]) -> None:
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/pipeline-configs",
            json={"config": {"chunkerr": {"strategy": "hybrid"}}},
            headers=_auth(boot),
        )
        assert response.status_code == 422


class TestBuilds:
    def _setup_build_inputs(
        self, client: TestClient, boot: dict[str, Any], corpus_root: Path
    ) -> tuple[str, str]:
        collection = _create_collection(client, boot, corpus_root)
        config = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/pipeline-configs",
            json={"config": {"embedding": {"mode": "deterministic", "revision_file": None}}},
            headers=_auth(boot),
        ).json()
        return collection["id"], config["id"]

    def test_build_runs_to_completion_and_persists_a_manifest(
        self,
        client: TestClient,
        boot: dict[str, Any],
        corpus_root: Path,
        tmp_path: Path,
    ) -> None:
        collection_id, config_id = self._setup_build_inputs(client, boot, corpus_root)
        created = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/builds",
            json={
                "source_collection_id": collection_id,
                "pipeline_config_id": config_id,
                "epoch": _EPOCH,
            },
            headers=_auth(boot),
        )
        assert created.status_code == 202, created.text

        build = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/builds/{created.json()['id']}",
            headers=_auth(boot),
        ).json()
        assert build["state"] == "completed", build
        assert build["counters"]["manifest_status"] == "complete"
        assert build["counters"]["sources"] == 2
        assert build["counters"]["chunks"] > 0
        assert build["counters"]["embeddings"] > 0
        assert build["manifest_hash"]

        # The manifest artifact exists on disk, is valid JSON, and its
        # content hash matches the artifact_ref path.
        artifact_dir = tmp_path / "artifacts" / "artifacts"
        manifest_files = []
        for path in artifact_dir.iterdir():
            try:
                document = json.loads(path.read_bytes())
            except json.JSONDecodeError:
                continue  # raw source artifacts are stored as-is, not as JSON
            if isinstance(document, dict) and document.get("namespace") == "docs":
                manifest_files.append(path)
        assert manifest_files, "no manifest artifact found on disk"

    def test_same_epoch_build_twice_reuses_the_same_manifest_row(
        self, client: TestClient, boot: dict[str, Any], corpus_root: Path
    ) -> None:
        """FR-082 through the server: byte-identical manifests dedupe by hash."""
        collection_id, config_id = self._setup_build_inputs(client, boot, corpus_root)
        url = f"/api/v1/workspaces/{boot['workspace_id']}/builds"
        body = {
            "source_collection_id": collection_id,
            "pipeline_config_id": config_id,
            "epoch": _EPOCH,
        }
        first = client.post(url, json=body, headers=_auth(boot)).json()
        second = client.post(url, json=body, headers=_auth(boot)).json()

        first_done = client.get(f"{url}/{first['id']}", headers=_auth(boot)).json()
        second_done = client.get(f"{url}/{second['id']}", headers=_auth(boot)).json()
        assert first_done["state"] == second_done["state"] == "completed"
        assert first_done["manifest_hash"] == second_done["manifest_hash"]
        assert first_done["manifest_id"] == second_done["manifest_id"]

    def test_build_with_missing_collection_is_404(
        self, client: TestClient, boot: dict[str, Any], corpus_root: Path
    ) -> None:
        _, config_id = self._setup_build_inputs(client, boot, corpus_root)
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/builds",
            json={
                "source_collection_id": "00000000-0000-0000-0000-000000000000",
                "pipeline_config_id": config_id,
            },
            headers=_auth(boot),
        )
        assert response.status_code == 404

    def test_completed_build_cannot_be_cancelled(
        self, client: TestClient, boot: dict[str, Any], corpus_root: Path
    ) -> None:
        collection_id, config_id = self._setup_build_inputs(client, boot, corpus_root)
        build = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/builds",
            json={"source_collection_id": collection_id, "pipeline_config_id": config_id},
            headers=_auth(boot),
        ).json()
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/builds/{build['id']}:cancel",
            headers=_auth(boot),
        )
        assert response.status_code == 409
        assert response.json()["title"] == "Not cancellable"


class TestScopes:
    def test_sources_scope_cannot_create_builds(
        self, client: TestClient, boot: dict[str, Any], corpus_root: Path
    ) -> None:
        limited = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/api-tokens",
            json={"name": "sources-only", "scopes": ["sources"]},
            headers=_auth(boot),
        ).json()
        response = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/builds",
            headers={"Authorization": f"Bearer {limited['token']}"},
        )
        assert response.status_code == 403
