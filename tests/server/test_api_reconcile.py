"""M7 wave B slice 4: policies, reconciliation execution, findings.

DB-backed, through the real app. The end-to-end test drives the whole
chain the server now supports: build a manifest from a corpus, snapshot
a (fixture-backed) target through the job queue, then reconcile the two
through the queue -- the real engine, real taxonomy findings persisted
to the `findings` table, the full report content-addressed on disk, and
a real policy gate producing a FAIL verdict on the drift it finds.
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

from ragledger.connectors.ndjson import NdjsonConnector
from ragledger.server import handlers
from ragledger.server.app import create_app
from ragledger.server.settings import Settings
from tests.server.conftest import TEST_DATABASE_URL, requires_database

pytestmark = requires_database

_FIXTURE_SNAPSHOT = (
    Path(__file__).parent.parent / "fixtures" / "snapshots" / "qdrant_support_kb.ndjson.zst"
)

_FAIL_ON_HIGH_POLICY = {
    "version": 1,
    "name": "fail-on-high",
    "requirements": {},
    "findings": {"fail_on_severity": ["critical", "high"]},
}


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
    with TestClient(create_app(Settings())) as test_client:
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


def _prepare_manifest_and_snapshot(
    client: TestClient,
    boot: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str]:
    """Run a build and a fixture-backed snapshot; return (manifest_id, snapshot_id)."""
    workspace = boot["workspace_id"]
    root = tmp_path / "corpus"
    root.mkdir(exist_ok=True)
    (root / "policy.md").write_text("# Policy\n\nRefunds within 14 days.\n")
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
    build = client.post(
        f"/api/v1/workspaces/{workspace}/builds",
        json={
            "source_collection_id": collection["id"],
            "pipeline_config_id": config["id"],
            "epoch": 1_700_000_000,
        },
        headers=_auth(boot),
    )
    assert build.status_code == 202, build.text
    manifest = client.get(f"/api/v1/workspaces/{workspace}/manifests", headers=_auth(boot)).json()
    assert len(manifest) == 1

    target = client.post(
        f"/api/v1/workspaces/{workspace}/targets",
        json={
            "name": "support-kb",
            "target_type": "qdrant",
            "endpoint_url": "http://8.8.8.8:6333",
            "credential": "api-key",
            "mapping_config": {"collection": "support_kb"},
        },
        headers=_auth(boot),
    ).json()
    monkeypatch.setattr(
        handlers,
        "_connector_for_target",
        lambda row, credential, settings: NdjsonConnector(_FIXTURE_SNAPSHOT),
    )
    snapshot = client.post(
        f"/api/v1/workspaces/{workspace}/targets/{target['id']}/snapshots",
        headers=_auth(boot),
    ).json()["snapshot"]
    detail = client.get(
        f"/api/v1/workspaces/{workspace}/snapshots/{snapshot['id']}", headers=_auth(boot)
    ).json()
    assert detail["status"] == "completed", detail
    return manifest[0]["id"], snapshot["id"]


class TestPolicies:
    def test_create_list_and_revise(self, client: TestClient, boot: dict[str, Any]) -> None:
        workspace = boot["workspace_id"]
        created = client.post(
            f"/api/v1/workspaces/{workspace}/policies",
            json={"name": "prod-gate", "document": _FAIL_ON_HIGH_POLICY},
            headers=_auth(boot),
        )
        assert created.status_code == 201, created.text
        assert created.json()["latest_revision"]["revision_number"] == 1

        listed = client.get(f"/api/v1/workspaces/{workspace}/policies", headers=_auth(boot))
        assert [p["name"] for p in listed.json()] == ["prod-gate"]

        revised_document = dict(_FAIL_ON_HIGH_POLICY, name="fail-on-critical-only")
        revised_document["findings"] = {"fail_on_severity": ["critical"]}
        revised = client.post(
            f"/api/v1/workspaces/{workspace}/policies/{created.json()['id']}/revisions",
            json={"document": revised_document},
            headers=_auth(boot),
        )
        assert revised.status_code == 201
        assert revised.json()["latest_revision"]["revision_number"] == 2

    def test_identical_revision_content_is_deduplicated(
        self, client: TestClient, boot: dict[str, Any]
    ) -> None:
        workspace = boot["workspace_id"]
        policy = client.post(
            f"/api/v1/workspaces/{workspace}/policies",
            json={"name": "gate", "document": _FAIL_ON_HIGH_POLICY},
            headers=_auth(boot),
        ).json()
        again = client.post(
            f"/api/v1/workspaces/{workspace}/policies/{policy['id']}/revisions",
            json={"document": _FAIL_ON_HIGH_POLICY},
            headers=_auth(boot),
        )
        assert again.status_code == 201
        assert again.json()["latest_revision"]["revision_number"] == 1

    def test_invalid_document_is_a_422_problem(
        self, client: TestClient, boot: dict[str, Any]
    ) -> None:
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/policies",
            json={"name": "bad", "document": {"version": 1, "unknown_key": True}},
            headers=_auth(boot),
        )
        assert response.status_code == 422
        assert response.json()["title"] == "Invalid policy document"

    def test_duplicate_name_is_a_409(self, client: TestClient, boot: dict[str, Any]) -> None:
        workspace = boot["workspace_id"]
        url = f"/api/v1/workspaces/{workspace}/policies"
        body = {"name": "gate", "document": _FAIL_ON_HIGH_POLICY}
        assert client.post(url, json=body, headers=_auth(boot)).status_code == 201
        assert client.post(url, json=body, headers=_auth(boot)).status_code == 409


class TestReconciliations:
    def test_end_to_end_reconciliation_produces_findings_and_a_report(
        self,
        client: TestClient,
        boot: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = boot["workspace_id"]
        manifest_id, snapshot_id = _prepare_manifest_and_snapshot(
            client, boot, tmp_path, monkeypatch
        )

        created = client.post(
            f"/api/v1/workspaces/{workspace}/reconciliations",
            json={"manifest_id": manifest_id, "snapshot_id": snapshot_id},
            headers=_auth(boot),
        )
        assert created.status_code == 202, created.text
        reconciliation_id = created.json()["reconciliation"]["id"]

        detail = client.get(
            f"/api/v1/workspaces/{workspace}/reconciliations/{reconciliation_id}",
            headers=_auth(boot),
        ).json()
        assert detail["state"] == "completed", detail
        assert detail["summary"]["verdict"] == "PASS"  # no policy attached
        assert detail["finding_count"] > 0  # unrelated corpus vs snapshot: drift expected

        findings = client.get(
            f"/api/v1/workspaces/{workspace}/reconciliations/{reconciliation_id}/findings",
            headers=_auth(boot),
        ).json()
        assert len(findings) == detail["finding_count"]
        # The fixture's 3 points exist in the target but not in the
        # manifest: every point-anchored finding is an orphan.
        assert "ORPHAN_IN_INDEX" in {finding["code"] for finding in findings}
        fingerprints = [finding["fingerprint"] for finding in findings]
        assert fingerprints == sorted(fingerprints)  # engine's stable order preserved

        # The full report exists as a content-addressed artifact.
        report_ref = detail["summary"]["report_artifact"]
        digest = report_ref.rpartition("/")[2]
        report_path = tmp_path / "artifacts" / "artifacts" / digest
        assert report_path.is_file()
        report = json.loads(report_path.read_bytes())
        assert report["policy"]["verdict"] == "PASS"

    def test_policy_gate_fails_on_high_severity_drift(
        self,
        client: TestClient,
        boot: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = boot["workspace_id"]
        manifest_id, snapshot_id = _prepare_manifest_and_snapshot(
            client, boot, tmp_path, monkeypatch
        )
        policy = client.post(
            f"/api/v1/workspaces/{workspace}/policies",
            json={"name": "prod-gate", "document": _FAIL_ON_HIGH_POLICY},
            headers=_auth(boot),
        ).json()

        created = client.post(
            f"/api/v1/workspaces/{workspace}/reconciliations",
            json={
                "manifest_id": manifest_id,
                "snapshot_id": snapshot_id,
                "policy_id": policy["id"],
            },
            headers=_auth(boot),
        )
        assert created.status_code == 202, created.text
        detail = client.get(
            f"/api/v1/workspaces/{workspace}/reconciliations/{created.json()['reconciliation']['id']}",
            headers=_auth(boot),
        ).json()
        assert detail["state"] == "completed"
        assert detail["policy_revision_id"] == policy["latest_revision"]["id"]
        assert detail["summary"]["verdict"] == "FAIL"  # orphans default to HIGH severity

    def test_findings_filter_by_severity_and_pagination(
        self,
        client: TestClient,
        boot: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = boot["workspace_id"]
        manifest_id, snapshot_id = _prepare_manifest_and_snapshot(
            client, boot, tmp_path, monkeypatch
        )
        reconciliation = client.post(
            f"/api/v1/workspaces/{workspace}/reconciliations",
            json={"manifest_id": manifest_id, "snapshot_id": snapshot_id},
            headers=_auth(boot),
        ).json()["reconciliation"]

        base = f"/api/v1/workspaces/{workspace}/reconciliations/{reconciliation['id']}/findings"
        everything = client.get(base, headers=_auth(boot)).json()
        assert len(everything) >= 2

        paged = client.get(f"{base}?limit=1", headers=_auth(boot)).json()
        assert len(paged) == 1
        assert paged[0]["fingerprint"] == everything[0]["fingerprint"]

        next_page = client.get(f"{base}?limit=1&offset=1", headers=_auth(boot)).json()
        assert next_page[0]["fingerprint"] == everything[1]["fingerprint"]

        orphans = client.get(f"{base}?code=ORPHAN_IN_INDEX", headers=_auth(boot)).json()
        assert orphans and all(f["code"] == "ORPHAN_IN_INDEX" for f in orphans)

    def test_reconciliation_against_pending_snapshot_is_a_409(
        self,
        client: TestClient,
        boot: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = boot["workspace_id"]
        manifest_id, _ = _prepare_manifest_and_snapshot(client, boot, tmp_path, monkeypatch)

        # A snapshot whose job fails leaves no artifact behind.
        monkeypatch.setattr(
            handlers,
            "_connector_for_target",
            lambda row, credential, settings: NdjsonConnector(tmp_path / "missing.ndjson.zst"),
        )
        target = client.get(f"/api/v1/workspaces/{workspace}/targets", headers=_auth(boot)).json()[
            0
        ]
        pending = client.post(
            f"/api/v1/workspaces/{workspace}/targets/{target['id']}/snapshots",
            headers=_auth(boot),
        ).json()["snapshot"]

        response = client.post(
            f"/api/v1/workspaces/{workspace}/reconciliations",
            json={"manifest_id": manifest_id, "snapshot_id": pending["id"]},
            headers=_auth(boot),
        )
        assert response.status_code == 409
        assert response.json()["title"] == "Snapshot not ready"
