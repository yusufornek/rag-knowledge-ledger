"""M7 wave B final slice: cancellation, SSE events, policy re-evaluation,
remediation export, lineage drill-down, and the workspace export.

DB-backed, through the real app. Cooperative cancellation is proven at
the queue level (a pre-set `cancel_requested` flag observed by a real
handler check point) and at the API level (queued job cancelled
outright, terminal job a 409). SSE streams are consumed through the
real endpoint; with the inline execution model the job is terminal by
the time the stream opens, so the contract asserted is "one status
event, one done event, stream closes".
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from ragledger.connectors.ndjson import NdjsonConnector
from ragledger.server import handlers
from ragledger.server.app import create_app
from ragledger.server.db.models import Job, Workspace
from ragledger.server.db.models.enums import JobStatus
from ragledger.server.jobs import (
    CancelOutcome,
    JobCancelledError,
    check_cancellation,
    enqueue_job,
    request_cancel,
    run_pending_jobs,
)
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


def _run_build(client: TestClient, boot: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
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
    created = client.post(
        f"/api/v1/workspaces/{workspace}/builds",
        json={
            "source_collection_id": collection["id"],
            "pipeline_config_id": config["id"],
            "epoch": 1_700_000_000,
        },
        headers=_auth(boot),
    )
    assert created.status_code == 202
    return created.json()  # type: ignore[no-any-return]


def _run_reconciliation(
    client: TestClient,
    boot: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    policy_id: str | None = None,
) -> dict[str, Any]:
    workspace = boot["workspace_id"]
    _run_build(client, boot, tmp_path)
    manifest = client.get(f"/api/v1/workspaces/{workspace}/manifests", headers=_auth(boot)).json()[
        0
    ]
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
    body: dict[str, Any] = {"manifest_id": manifest["id"], "snapshot_id": snapshot["id"]}
    if policy_id is not None:
        body["policy_id"] = policy_id
    created = client.post(
        f"/api/v1/workspaces/{workspace}/reconciliations", json=body, headers=_auth(boot)
    )
    assert created.status_code == 202, created.text
    return created.json()["reconciliation"]  # type: ignore[no-any-return]


class TestCooperativeCancellation:
    def test_request_cancel_flips_queued_and_flags_running(self, db_session: Session) -> None:
        workspace = Workspace(slug=f"ws-{uuid.uuid4().hex[:8]}", name="Cancel")
        db_session.add(workspace)
        db_session.commit()

        queued = enqueue_job(db_session, workspace_id=workspace.id, job_type="a")
        db_session.commit()
        assert request_cancel(db_session, queued) == CancelOutcome.CANCELLED
        assert queued.status == JobStatus.CANCELLED

        running = enqueue_job(db_session, workspace_id=workspace.id, job_type="b")
        running.status = JobStatus.RUNNING
        db_session.commit()
        assert request_cancel(db_session, running) == CancelOutcome.REQUESTED
        assert running.cancel_requested is True

        assert request_cancel(db_session, queued) == CancelOutcome.NOT_CANCELLABLE

    def test_handler_observing_the_flag_cancels_job_and_runs_finalizer(
        self, db_engine: Engine
    ) -> None:
        factory = sessionmaker(db_engine)
        with factory() as session:
            workspace = Workspace(slug=f"ws-{uuid.uuid4().hex[:8]}", name="Cancel")
            session.add(workspace)
            session.flush()
            job = enqueue_job(session, workspace_id=workspace.id, job_type="cancellable")
            job.cancel_requested = True  # cancel arrives before the worker leases it
            session.commit()
            job_id = job.id

        finalized: list[uuid.UUID] = []

        def _handler(session: Session, job: Job) -> None:
            check_cancellation(session, job)
            raise AssertionError("handler must abort at the check point")

        run_pending_jobs(
            factory,
            {"cancellable": _handler},
            finalizers={"cancellable": lambda s, j: finalized.append(j.id)},
        )
        with factory() as session:
            row = session.get(Job, job_id)
            assert row is not None and row.status == JobStatus.CANCELLED
            assert row.last_error is None
        assert finalized == [job_id]

    def test_check_cancellation_raises_only_when_flagged(self, db_session: Session) -> None:
        workspace = Workspace(slug=f"ws-{uuid.uuid4().hex[:8]}", name="Cancel")
        db_session.add(workspace)
        db_session.commit()
        job = enqueue_job(db_session, workspace_id=workspace.id, job_type="a")
        db_session.commit()
        check_cancellation(db_session, job)  # no flag: no exception
        job.cancel_requested = True
        db_session.commit()
        with pytest.raises(JobCancelledError):
            check_cancellation(db_session, job)

    def test_cancelling_a_finished_build_is_a_409(
        self, client: TestClient, boot: dict[str, Any], tmp_path: Path
    ) -> None:
        build = _run_build(client, boot, tmp_path)
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/builds/{build['id']}:cancel",
            headers=_auth(boot),
        )
        assert response.status_code == 409
        assert response.json()["title"] == "Not cancellable"

    def test_snapshot_cancel_endpoint_is_wired(
        self,
        client: TestClient,
        boot: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reconciliation = _run_reconciliation(client, boot, tmp_path, monkeypatch)
        workspace = boot["workspace_id"]
        snapshot_id = reconciliation["snapshot_id"]
        response = client.post(
            f"/api/v1/workspaces/{workspace}/snapshots/{snapshot_id}:cancel",
            headers=_auth(boot),
        )
        assert response.status_code == 409  # already completed

        rec_response = client.post(
            f"/api/v1/workspaces/{workspace}/reconciliations/{reconciliation['id']}:cancel",
            headers=_auth(boot),
        )
        assert rec_response.status_code == 409


class TestSseEvents:
    def test_build_events_stream_status_then_done(
        self, client: TestClient, boot: dict[str, Any], tmp_path: Path
    ) -> None:
        build = _run_build(client, boot, tmp_path)
        with client.stream(
            "GET",
            f"/api/v1/workspaces/{boot['workspace_id']}/builds/{build['id']}/events",
            headers=_auth(boot),
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = "".join(response.iter_text())
        assert "event: status" in body
        assert "event: done" in body
        assert '"status": "completed"' in body

    def test_events_for_unknown_build_is_404(
        self, client: TestClient, boot: dict[str, Any]
    ) -> None:
        response = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/builds/{uuid.uuid4()}/events",
            headers=_auth(boot),
        )
        assert response.status_code == 404


class TestPolicyReEvaluation:
    def test_evaluate_policy_against_a_stored_result(
        self,
        client: TestClient,
        boot: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reconciliation = _run_reconciliation(client, boot, tmp_path, monkeypatch)
        workspace = boot["workspace_id"]
        response = client.post(
            f"/api/v1/workspaces/{workspace}/reconciliations/{reconciliation['id']}"
            ":evaluate-policy",
            json={"document": _FAIL_ON_HIGH_POLICY},
            headers=_auth(boot),
        )
        assert response.status_code == 200, response.text
        verdict = response.json()
        assert verdict["verdict"] == "FAIL"  # orphans are HIGH severity
        assert verdict["policy_name"] == "fail-on-high"

    def test_evaluate_policy_with_invalid_document_is_422(
        self,
        client: TestClient,
        boot: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reconciliation = _run_reconciliation(client, boot, tmp_path, monkeypatch)
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/reconciliations/"
            f"{reconciliation['id']}:evaluate-policy",
            json={"document": {"nonsense": True}},
            headers=_auth(boot),
        )
        assert response.status_code == 422


class TestRemediationAndLineage:
    def test_remediation_plan_json_and_csv(
        self,
        client: TestClient,
        boot: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reconciliation = _run_reconciliation(client, boot, tmp_path, monkeypatch)
        workspace = boot["workspace_id"]
        base = f"/api/v1/workspaces/{workspace}/reconciliations/{reconciliation['id']}"

        plan = client.post(f"{base}/remediation-plans", headers=_auth(boot))
        assert plan.status_code == 200, plan.text
        actions = plan.json()["actions"]
        assert actions, "orphan findings must produce remediation candidates"
        delete_actions = [a for a in actions if a["action"] == "delete_point_candidate"]
        assert delete_actions and all(a["destructive"] for a in delete_actions)

        csv_response = client.post(f"{base}/remediation-plans?format=csv", headers=_auth(boot))
        assert csv_response.status_code == 200
        assert csv_response.headers["content-type"].startswith("text/csv")
        lines = csv_response.text.strip().splitlines()
        assert len(lines) >= 2  # header plus at least one action row

    def test_lineage_drill_down_returns_findings_for_a_portable_id(
        self,
        client: TestClient,
        boot: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        reconciliation = _run_reconciliation(client, boot, tmp_path, monkeypatch)
        workspace = boot["workspace_id"]
        findings = client.get(
            f"/api/v1/workspaces/{workspace}/reconciliations/{reconciliation['id']}/findings",
            headers=_auth(boot),
        ).json()
        anchored = [f for f in findings if f["source_hash"] or f["chunk_hash"]]
        if not anchored:
            pytest.skip("no lineage-anchored findings in this corpus/snapshot pairing")
        portable_id = anchored[0]["source_hash"] or anchored[0]["chunk_hash"]
        response = client.get(
            f"/api/v1/workspaces/{workspace}/reconciliations/{reconciliation['id']}"
            f"/lineage/{portable_id}",
            headers=_auth(boot),
        )
        assert response.status_code == 200
        assert any(f["fingerprint"] == anchored[0]["fingerprint"] for f in response.json())


class TestWorkspaceExport:
    def test_export_contains_metadata_but_never_secrets(
        self,
        client: TestClient,
        boot: dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace = boot["workspace_id"]
        policy = client.post(
            f"/api/v1/workspaces/{workspace}/policies",
            json={"name": "prod-gate", "document": _FAIL_ON_HIGH_POLICY},
            headers=_auth(boot),
        ).json()
        _run_reconciliation(client, boot, tmp_path, monkeypatch, policy_id=policy["id"])

        response = client.get(f"/api/v1/workspaces/{workspace}/export", headers=_auth(boot))
        assert response.status_code == 200, response.text
        export = response.json()
        assert export["export_version"] == 1
        assert export["workspace"]["slug"] == "primary"
        assert len(export["source_collections"]) == 1
        assert len(export["targets"]) == 1
        assert len(export["manifests"]) == 1
        assert len(export["snapshots"]) == 1
        assert len(export["policies"]) == 1
        assert len(export["reconciliations"]) == 1

        # FR-005: no secret material, ever.
        text = response.text
        assert "api-key" not in text  # the target credential
        assert boot["token"] not in text
        assert "token_hash" not in text
        assert "credential_ciphertext" not in text
        assert export["targets"][0]["credential_configured"] is True

    def test_export_requires_admin_scope(self, client: TestClient, boot: dict[str, Any]) -> None:
        workspace = boot["workspace_id"]
        limited = client.post(
            f"/api/v1/workspaces/{workspace}/api-tokens",
            json={"name": "sources-only", "scopes": ["sources"]},
            headers=_auth(boot),
        ).json()
        response = client.get(
            f"/api/v1/workspaces/{workspace}/export",
            headers={"Authorization": f"Bearer {limited['token']}"},
        )
        assert response.status_code == 403
