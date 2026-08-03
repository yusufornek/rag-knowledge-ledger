"""M7 wave B slice 3: manifest list/sign/verify and snapshot execution.

DB-backed, through the real app. The signing tests use a real Ed25519
keypair generated on the fly: sign attaches a real signature, verify
checks it against a trust-store directory, and the three
`VerificationOverall` outcomes (INCOMPLETE, VALID_UNTRUSTED,
VALID_TRUSTED) are each exercised. The snapshot tests replace only the
connector construction (`_connector_for_target`) with the NDJSON
replay connector reading a committed fixture -- header, streaming,
trailer hashing, artifact storage, and row bookkeeping all run for
real.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from ragledger.connectors.ndjson import NdjsonConnector
from ragledger.core.signing import generate_keypair, write_private_key, write_public_key
from ragledger.server import handlers
from ragledger.server.app import create_app
from ragledger.server.db.models import VectorTarget
from ragledger.server.settings import Settings
from tests.server.conftest import TEST_DATABASE_URL, requires_database

pytestmark = requires_database

_FIXTURE_SNAPSHOT = (
    Path(__file__).parent.parent / "fixtures" / "snapshots" / "qdrant_support_kb.ndjson.zst"
)
_CREDENTIAL = "qdrant-api-key-plaintext"


@pytest.fixture
def signing_keys(tmp_path: Path) -> dict[str, Path]:
    private_key, public_key = generate_keypair()
    key_file = tmp_path / "signing.key"
    trust_dir = tmp_path / "trust-store"
    trust_dir.mkdir()
    write_private_key(private_key, key_file)
    write_public_key(public_key, trust_dir / "server.pub")
    return {"key_file": key_file, "trust_dir": trust_dir}


def _make_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra_env: dict[str, str]
) -> TestClient:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("APP_ENCRYPTION_KEY_V1", base64.b64encode(b"k" * 32).decode("ascii"))
    monkeypatch.setenv("ARTIFACT_STORE_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("SOURCE_ROOT_ALLOWED_BASES", str(tmp_path))
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    monkeypatch.delenv("MANIFEST_SIGNING_KEY_FILE", raising=False)
    monkeypatch.delenv("MANIFEST_TRUST_STORE_PATH", raising=False)
    for name, value in extra_env.items():
        monkeypatch.setenv(name, value)
    return TestClient(create_app(Settings()))


@pytest.fixture
def client(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[TestClient]:
    with _make_client(monkeypatch, tmp_path, {}) as test_client:
        yield test_client


@pytest.fixture
def signing_client(
    db_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    signing_keys: dict[str, Path],
) -> Iterator[TestClient]:
    env = {
        "MANIFEST_SIGNING_KEY_FILE": str(signing_keys["key_file"]),
        "MANIFEST_SIGNING_KEY_ID": "server-key-1",
        "MANIFEST_TRUST_STORE_PATH": str(signing_keys["trust_dir"]),
    }
    with _make_client(monkeypatch, tmp_path, env) as test_client:
        yield test_client


def _bootstrap(client: TestClient) -> dict[str, Any]:
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


def _run_build(client: TestClient, boot: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    """Create a corpus, collection, config, and a completed build; return the build."""
    root = tmp_path / "corpus"
    root.mkdir(exist_ok=True)
    (root / "policy.md").write_text("# Policy\n\nRefunds within 14 days.\n")
    workspace = boot["workspace_id"]
    collection = client.post(
        f"/api/v1/workspaces/{workspace}/source-collections",
        json={"name": "Docs", "namespace": "docs", "root": str(root)},
        headers=_auth(boot),
    ).json()
    config = client.post(
        f"/api/v1/workspaces/{workspace}/pipeline-configs",
        json={"config": {"embedding": {"mode": "deterministic", "revision_file": None}}},
        headers=_auth(boot),
    ).json()
    created = client.post(
        f"/api/v1/workspaces/{workspace}/builds",
        json={
            "source_collection_id": collection["id"],
            "pipeline_config_id": config["id"],
            "epoch": 1_700_000_000,
        },
        headers=_auth(boot),
    )
    assert created.status_code == 202, created.text
    build = client.get(
        f"/api/v1/workspaces/{workspace}/builds/{created.json()['id']}", headers=_auth(boot)
    ).json()
    assert build["state"] == "completed", build
    return build  # type: ignore[no-any-return]


class TestManifests:
    def test_list_and_get_after_a_build(self, client: TestClient, tmp_path: Path) -> None:
        boot = _bootstrap(client)
        build = _run_build(client, boot, tmp_path)
        listed = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/manifests", headers=_auth(boot)
        )
        assert listed.status_code == 200
        manifests = listed.json()
        assert len(manifests) == 1
        assert manifests[0]["manifest_hash"] == build["manifest_hash"]
        assert manifests[0]["namespace"] == "docs"
        assert manifests[0]["signed"] is False
        assert manifests[0]["source_count"] == 1

        detail = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/manifests/{manifests[0]['id']}",
            headers=_auth(boot),
        )
        assert detail.status_code == 200

    def test_sign_without_a_key_is_a_503_problem(self, client: TestClient, tmp_path: Path) -> None:
        boot = _bootstrap(client)
        _run_build(client, boot, tmp_path)
        manifest = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/manifests", headers=_auth(boot)
        ).json()[0]
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/manifests/{manifest['id']}:sign",
            headers=_auth(boot),
        )
        assert response.status_code == 503
        assert response.json()["title"] == "Signing not enabled"

    def test_unsigned_manifest_verifies_incomplete(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        boot = _bootstrap(client)
        _run_build(client, boot, tmp_path)
        manifest = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/manifests", headers=_auth(boot)
        ).json()[0]
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/manifests/{manifest['id']}:verify",
            headers=_auth(boot),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["hash_valid"] is True
        assert body["overall"] == "INCOMPLETE"

    def test_sign_then_verify_trusted(self, signing_client: TestClient, tmp_path: Path) -> None:
        boot = _bootstrap(signing_client)
        _run_build(signing_client, boot, tmp_path)
        workspace = boot["workspace_id"]
        manifest = signing_client.get(
            f"/api/v1/workspaces/{workspace}/manifests", headers=_auth(boot)
        ).json()[0]

        signed = signing_client.post(
            f"/api/v1/workspaces/{workspace}/manifests/{manifest['id']}:sign",
            headers=_auth(boot),
        )
        assert signed.status_code == 200, signed.text
        body = signed.json()
        assert body["signed"] is True
        assert len(body["signatures"]) == 1
        assert body["signatures"][0]["issuer"] == "server-key-1"
        # The signing view excludes signatures: the portable identity is stable.
        assert body["manifest_hash"] == manifest["manifest_hash"]

        verified = signing_client.post(
            f"/api/v1/workspaces/{workspace}/manifests/{manifest['id']}:verify",
            headers=_auth(boot),
        ).json()
        assert verified["overall"] == "VALID_TRUSTED"
        assert verified["signatures"][0]["status"] == "valid"

    def test_signed_manifest_without_trust_store_is_valid_untrusted(
        self,
        db_engine: Engine,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        signing_keys: dict[str, Path],
    ) -> None:
        """Signing key configured, but no trust store: the signature is real yet untrusted."""
        env = {"MANIFEST_SIGNING_KEY_FILE": str(signing_keys["key_file"])}
        with _make_client(monkeypatch, tmp_path, env) as client:
            boot = _bootstrap(client)
            _run_build(client, boot, tmp_path)
            workspace = boot["workspace_id"]
            manifest = client.get(
                f"/api/v1/workspaces/{workspace}/manifests", headers=_auth(boot)
            ).json()[0]
            assert (
                client.post(
                    f"/api/v1/workspaces/{workspace}/manifests/{manifest['id']}:sign",
                    headers=_auth(boot),
                ).status_code
                == 200
            )
            verified = client.post(
                f"/api/v1/workspaces/{workspace}/manifests/{manifest['id']}:verify",
                headers=_auth(boot),
            ).json()
            assert verified["overall"] == "VALID_UNTRUSTED"
            assert verified["signatures"][0]["status"] == "unknown_key"


class TestSnapshots:
    def _create_target(self, client: TestClient, boot: dict[str, Any]) -> dict[str, Any]:
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/targets",
            json={
                "name": "support-kb",
                "target_type": "qdrant",
                "endpoint_url": "http://8.8.8.8:6333",
                "credential": _CREDENTIAL,
                "mapping_config": {"collection": "support_kb"},
            },
            headers=_auth(boot),
        )
        assert response.status_code == 201, response.text
        return response.json()  # type: ignore[no-any-return]

    def test_snapshot_runs_through_the_queue_and_stores_an_artifact(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        boot = _bootstrap(client)
        target = self._create_target(client, boot)
        seen_credentials: list[str] = []

        def _fixture_connector(
            row: VectorTarget, credential: str, settings: Settings
        ) -> NdjsonConnector:
            seen_credentials.append(credential)
            return NdjsonConnector(_FIXTURE_SNAPSHOT)

        monkeypatch.setattr(handlers, "_connector_for_target", _fixture_connector)

        workspace = boot["workspace_id"]
        created = client.post(
            f"/api/v1/workspaces/{workspace}/targets/{target['id']}/snapshots",
            headers=_auth(boot),
        )
        assert created.status_code == 202, created.text
        snapshot_id = created.json()["snapshot"]["id"]

        # The credential reached the connector factory decrypted, intact.
        assert seen_credentials == [_CREDENTIAL]

        detail = client.get(
            f"/api/v1/workspaces/{workspace}/snapshots/{snapshot_id}", headers=_auth(boot)
        ).json()
        assert detail["status"] == "completed", detail
        assert detail["point_count"] == 3
        assert detail["content_hash"]
        assert detail["schema_hash"]

        listed = client.get(
            f"/api/v1/workspaces/{workspace}/targets/{target['id']}/snapshots",
            headers=_auth(boot),
        ).json()
        assert [row["id"] for row in listed] == [snapshot_id]

        # The snapshot artifact is on disk and content-addressed.
        artifact_dir = tmp_path / "artifacts" / "artifacts"
        assert any(path.stat().st_size > 0 for path in artifact_dir.iterdir())

    def test_broken_target_fails_the_job_and_rolls_the_snapshot_back(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A connector configuration failure fails the job; no phantom snapshot state survives."""
        boot = _bootstrap(client)
        target = self._create_target(client, boot)

        def _broken_connector(
            row: VectorTarget, credential: str, settings: Settings
        ) -> NdjsonConnector:
            return NdjsonConnector(tmp_path / "does-not-exist.ndjson.zst")

        monkeypatch.setattr(handlers, "_connector_for_target", _broken_connector)

        workspace = boot["workspace_id"]
        created = client.post(
            f"/api/v1/workspaces/{workspace}/targets/{target['id']}/snapshots",
            headers=_auth(boot),
        )
        assert created.status_code == 202
        snapshot_id = created.json()["snapshot"]["id"]
        job_id = created.json()["job"]["id"]

        job = client.get(
            f"/api/v1/workspaces/{workspace}/jobs/{job_id}", headers=_auth(boot)
        ).json()
        assert job["status"] == "failed", job
        snapshot = client.get(
            f"/api/v1/workspaces/{workspace}/snapshots/{snapshot_id}", headers=_auth(boot)
        ).json()
        # The handler's RUNNING flip rolled back with the failure.
        assert snapshot["status"] == "pending"
        assert snapshot["point_count"] is None


class TestConnectorFactory:
    def test_qdrant_factory_builds_a_validated_connector_and_cleans_the_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        monkeypatch.setenv("APP_ENCRYPTION_KEY_V1", base64.b64encode(b"k" * 32).decode("ascii"))
        settings = Settings()
        target = VectorTarget(
            name="kb",
            target_type="qdrant",
            endpoint_redacted="http://8.8.8.8:6333",
            mapping_config={"collection": "support_kb"},
            credential_ciphertext=b"unused",
            credential_key_id="v1",
        )
        before = set(os.environ)
        connector = handlers._connector_for_target(target, "the-api-key", settings)
        try:
            connector.validate_configuration()
        finally:
            connector.close()
        leaked = {name for name in set(os.environ) - before if "RAGLEDGER_TARGET" in name}
        assert leaked == set()
