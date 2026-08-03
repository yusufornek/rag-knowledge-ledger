"""M7 wave B slice 1: `/api/v1` bootstrap, tokens, targets, audit, and authz.

DB-backed (`requires_database`, same convention as the rest of
`tests/server/`): every test runs against a real Postgres through the
real FastAPI app, real auth dependency chain, and real problem
handlers -- nothing is monkeypatched except DNS-free target endpoints
(IP literals) and the environment the `Settings` under test reads.

The security-posture assertions to look for below: the bootstrap
endpoint permanently closes after first use; no response body ever
carries a credential or token secret except the two creation
responses; cross-workspace access renders the same 404 as nonexistence
(IDOR oracle prevention); scope enforcement distinguishes 401 from
403; and SSRF-blocked target URLs are a 422 problem, not a stored row.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ragledger.server.app import create_app
from ragledger.server.db.models import ApiToken, AuditEvent, VectorTarget, Workspace
from ragledger.server.security import issue_api_token
from ragledger.server.settings import Settings
from tests.server.conftest import TEST_DATABASE_URL, requires_database

pytestmark = requires_database

PROBLEM_CONTENT_TYPE = "application/problem+json"
_PUBLIC_QDRANT_URL = "http://8.8.8.8:6333"


@pytest.fixture
def client(db_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real app wired to the test database, with one encryption key configured."""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("APP_ENCRYPTION_KEY_V1", base64.b64encode(b"k" * 32).decode("ascii"))
    monkeypatch.delenv("ALLOW_PRIVATE_TARGETS", raising=False)
    monkeypatch.delenv("PRIVATE_TARGET_CIDRS", raising=False)
    app = create_app(Settings())
    with TestClient(app) as test_client:
        yield test_client


def _bootstrap(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/v1/auth/bootstrap",
        json={
            "email": "admin@example.com",
            "workspace_slug": "primary",
            "workspace_name": "Primary Workspace",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_target(client: TestClient, boot: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "support-kb",
        "target_type": "qdrant",
        "endpoint_url": _PUBLIC_QDRANT_URL,
        "credential": "qdrant-api-key-plaintext",
        "mapping_config": {"source_id": "payload.source_id"},
    }
    payload.update(overrides)
    response = client.post(
        f"/api/v1/workspaces/{boot['workspace_id']}/targets",
        json=payload,
        headers=_auth(boot["token"]),
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


class TestBootstrap:
    def test_bootstrap_creates_workspace_and_working_admin_token(self, client: TestClient) -> None:
        boot = _bootstrap(client)
        assert boot["token_scopes"] == ["admin"]
        assert boot["token"].startswith("rlk_")

        response = client.get("/api/v1/workspaces", headers=_auth(boot["token"]))
        assert response.status_code == 200
        assert [w["slug"] for w in response.json()] == ["primary"]

    def test_second_bootstrap_is_a_409_problem(self, client: TestClient) -> None:
        _bootstrap(client)
        response = client.post(
            "/api/v1/auth/bootstrap",
            json={
                "email": "attacker@example.com",
                "workspace_slug": "second",
                "workspace_name": "Second",
            },
        )
        assert response.status_code == 409
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        assert response.json()["title"] == "Already bootstrapped"

    def test_bootstrap_rejects_invalid_email_and_slug(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/auth/bootstrap",
            json={"email": "not-an-email", "workspace_slug": "UPPER", "workspace_name": "X"},
        )
        assert response.status_code == 422
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
        # The problem body carries locations/messages but never echoes input values.
        assert "not-an-email" not in response.text

    def test_bootstrap_writes_an_audit_event(self, client: TestClient, db_session: Session) -> None:
        _bootstrap(client)
        actions = db_session.execute(select(AuditEvent.action)).scalars().all()
        assert "auth.bootstrap" in actions


class TestAuthentication:
    def test_missing_token_is_401_with_www_authenticate(self, client: TestClient) -> None:
        boot = _bootstrap(client)
        response = client.get(f"/api/v1/workspaces/{boot['workspace_id']}")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)

    def test_malformed_and_wrong_tokens_are_401(self, client: TestClient) -> None:
        boot = _bootstrap(client)
        url = f"/api/v1/workspaces/{boot['workspace_id']}"
        assert client.get(url, headers=_auth("garbage")).status_code == 401
        forged = issue_api_token().token  # valid shape, never persisted
        assert client.get(url, headers=_auth(forged)).status_code == 401

    def test_revoked_token_is_401(self, client: TestClient) -> None:
        boot = _bootstrap(client)
        url = f"/api/v1/workspaces/{boot['workspace_id']}"
        revoke = client.delete(
            f"/api/v1/workspaces/{boot['workspace_id']}/api-tokens/{boot['token_id']}",
            headers=_auth(boot["token"]),
        )
        assert revoke.status_code == 204
        response = client.get(url, headers=_auth(boot["token"]))
        assert response.status_code == 401
        assert "revoked" in response.json()["detail"]


class TestApiTokens:
    def test_create_and_list_tokens_without_secret_material(self, client: TestClient) -> None:
        boot = _bootstrap(client)
        created = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/api-tokens",
            json={"name": "targets-only", "scopes": ["targets"]},
            headers=_auth(boot["token"]),
        )
        assert created.status_code == 201
        body = created.json()
        assert body["token"].startswith("rlk_")
        assert body["scopes"] == ["targets"]

        listed = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/api-tokens",
            headers=_auth(boot["token"]),
        )
        assert listed.status_code == 200
        rows = listed.json()
        assert {row["name"] for row in rows} == {"bootstrap admin token", "targets-only"}
        for row in rows:
            assert "token" not in row
            assert "salt" not in row
            assert "token_hash" not in row

    def test_unknown_scope_is_a_422_problem(self, client: TestClient) -> None:
        boot = _bootstrap(client)
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/api-tokens",
            json={"name": "bad", "scopes": ["root"]},
            headers=_auth(boot["token"]),
        )
        assert response.status_code == 422

    def test_non_admin_scope_cannot_manage_tokens(self, client: TestClient) -> None:
        boot = _bootstrap(client)
        limited = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/api-tokens",
            json={"name": "targets-only", "scopes": ["targets"]},
            headers=_auth(boot["token"]),
        ).json()
        response = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/api-tokens",
            headers=_auth(limited["token"]),
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "this operation requires the 'admin' scope"


class TestTargets:
    def test_create_get_and_list_never_return_the_credential(
        self, client: TestClient, db_session: Session
    ) -> None:
        boot = _bootstrap(client)
        target = _create_target(client, boot)
        assert target["credential_configured"] is True
        assert target["credential_key_id"] == "v1"
        assert target["credential_version"] == 1
        assert target["allowlist_decision"] == "public"
        assert target["endpoint_redacted"] == _PUBLIC_QDRANT_URL
        assert "qdrant-api-key-plaintext" not in str(target)

        fetched = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/targets/{target['id']}",
            headers=_auth(boot["token"]),
        )
        assert fetched.status_code == 200
        assert "qdrant-api-key-plaintext" not in fetched.text

        # The credential is stored encrypted, never as plaintext bytes.
        row = db_session.execute(select(VectorTarget)).scalar_one()
        assert b"qdrant-api-key-plaintext" not in row.credential_ciphertext

    def test_private_endpoint_is_rejected_and_not_stored(
        self, client: TestClient, db_session: Session
    ) -> None:
        boot = _bootstrap(client)
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/targets",
            json={
                "name": "internal",
                "target_type": "qdrant",
                "endpoint_url": "http://10.0.0.5:6333",
                "credential": "secret",
            },
            headers=_auth(boot["token"]),
        )
        assert response.status_code == 422
        assert response.json()["title"] == "Target URL not allowed"
        assert db_session.execute(select(VectorTarget)).first() is None

    def test_metadata_endpoint_is_always_rejected(self, client: TestClient) -> None:
        boot = _bootstrap(client)
        response = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/targets",
            json={
                "name": "metadata",
                "target_type": "qdrant",
                "endpoint_url": "http://169.254.169.254/latest/meta-data/",
                "credential": "secret",
            },
            headers=_auth(boot["token"]),
        )
        assert response.status_code == 422

    def test_credential_rotation_bumps_version(self, client: TestClient) -> None:
        boot = _bootstrap(client)
        target = _create_target(client, boot)
        patched = client.patch(
            f"/api/v1/workspaces/{boot['workspace_id']}/targets/{target['id']}",
            json={"credential": "rotated-api-key"},
            headers=_auth(boot["token"]),
        )
        assert patched.status_code == 200
        assert patched.json()["credential_version"] == 2
        assert "rotated-api-key" not in patched.text

    def test_delete_target(self, client: TestClient) -> None:
        boot = _bootstrap(client)
        target = _create_target(client, boot)
        url = f"/api/v1/workspaces/{boot['workspace_id']}/targets/{target['id']}"
        assert client.delete(url, headers=_auth(boot["token"])).status_code == 204
        assert client.get(url, headers=_auth(boot["token"])).status_code == 404


class TestCrossWorkspaceIsolation:
    @pytest.fixture
    def second_workspace_token(self, client: TestClient, db_session: Session) -> str:
        """A second workspace and admin token, created directly in the DB.

        Bootstrap only ever runs once, so the second workspace is
        seeded the way a future workspace-creation endpoint would.
        """
        workspace = Workspace(slug="other", name="Other Workspace")
        db_session.add(workspace)
        db_session.flush()
        issued = issue_api_token()
        db_session.add(
            ApiToken(
                workspace_id=workspace.id,
                name="other admin",
                prefix=issued.prefix,
                selector=issued.selector,
                salt=issued.salt,
                token_hash=issued.token_hash,
                scopes=["admin"],
            )
        )
        db_session.commit()
        return issued.token

    def test_foreign_workspace_path_is_404_not_403(
        self, client: TestClient, second_workspace_token: str
    ) -> None:
        boot = _bootstrap(client)
        response = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}",
            headers=_auth(second_workspace_token),
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "workspace not found"

    def test_foreign_target_id_under_own_workspace_is_404(
        self, client: TestClient, second_workspace_token: str, db_session: Session
    ) -> None:
        boot = _bootstrap(client)
        target = _create_target(client, boot)
        other_workspace_id = db_session.execute(
            select(Workspace.id).where(Workspace.slug == "other")
        ).scalar_one()
        response = client.get(
            f"/api/v1/workspaces/{other_workspace_id}/targets/{target['id']}",
            headers=_auth(second_workspace_token),
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "target not found"


class TestAuditEvents:
    def test_admin_can_list_workspace_audit_trail(self, client: TestClient) -> None:
        boot = _bootstrap(client)
        _create_target(client, boot)
        response = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/audit-events",
            headers=_auth(boot["token"]),
        )
        assert response.status_code == 200
        actions = [event["action"] for event in response.json()]
        assert "target.create" in actions
        assert "auth.bootstrap" in actions
        # Newest first.
        assert actions.index("target.create") < actions.index("auth.bootstrap")

    def test_non_admin_scope_cannot_read_audit_trail(self, client: TestClient) -> None:
        boot = _bootstrap(client)
        limited = client.post(
            f"/api/v1/workspaces/{boot['workspace_id']}/api-tokens",
            json={"name": "targets-only", "scopes": ["targets"]},
            headers=_auth(boot["token"]),
        ).json()
        response = client.get(
            f"/api/v1/workspaces/{boot['workspace_id']}/audit-events",
            headers=_auth(limited["token"]),
        )
        assert response.status_code == 403
