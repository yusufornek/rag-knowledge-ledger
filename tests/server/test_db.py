"""DB-backed tests for `ragledger.server.db.models`: CRUD, constraints, cross-workspace scoping.

Every test here is decorated `@requires_database` (see
`tests/server/conftest.py`): they skip cleanly when
`RAGLEDGER_TEST_DATABASE_URL` is unreachable (the default locally,
since `docker-compose.yml`'s `appdb` service is not started by
default), and run for real in CI, where
`.github/workflows/ci.yml` starts a Postgres service container.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ragledger.server.audit import AuditLog
from ragledger.server.db.models import (
    ApiToken,
    Build,
    BuildState,
    Manifest,
    ManifestStatus,
    Membership,
    MembershipRole,
    PipelineConfig,
    SourceCollection,
    User,
    Workspace,
)
from ragledger.server.security import issue_api_token
from tests.server.conftest import requires_database

pytestmark = requires_database


def _make_workspace(session: Session, slug: str = "acme") -> Workspace:
    workspace = Workspace(slug=slug, name="Acme Corp")
    session.add(workspace)
    session.flush()
    return workspace


class TestWorkspaceMembershipCrud:
    def test_create_workspace_user_and_membership(self, db_session: Session) -> None:
        workspace = _make_workspace(db_session)
        user = User(email="owner@example.com", display_name="Owner")
        db_session.add(user)
        db_session.flush()

        membership = Membership(
            workspace_id=workspace.id, user_id=user.id, role=MembershipRole.OWNER
        )
        db_session.add(membership)
        db_session.commit()

        fetched = db_session.get(Membership, membership.id)
        assert fetched is not None
        assert fetched.role == MembershipRole.OWNER
        assert fetched.workspace_id == workspace.id

    def test_duplicate_membership_violates_unique_constraint(self, db_session: Session) -> None:
        workspace = _make_workspace(db_session)
        user = User(email="dup@example.com")
        db_session.add(user)
        db_session.flush()

        db_session.add(
            Membership(workspace_id=workspace.id, user_id=user.id, role=MembershipRole.VIEWER)
        )
        db_session.commit()

        db_session.add(
            Membership(workspace_id=workspace.id, user_id=user.id, role=MembershipRole.EDITOR)
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()

    def test_duplicate_workspace_slug_violates_unique_constraint(self, db_session: Session) -> None:
        db_session.add(Workspace(slug="dup-slug", name="First"))
        db_session.commit()
        db_session.add(Workspace(slug="dup-slug", name="Second"))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


class TestApiTokenPersistence:
    def test_issued_token_persists_and_verifies_after_reload(self, db_session: Session) -> None:
        workspace = _make_workspace(db_session, slug="token-ws")
        issued = issue_api_token()
        row = ApiToken(
            workspace_id=workspace.id,
            name="ci token",
            prefix=issued.prefix,
            selector=issued.selector,
            salt=issued.salt,
            token_hash=issued.token_hash,
            scopes=["builds", "reconciliations"],
        )
        db_session.add(row)
        db_session.commit()

        db_session.expunge_all()
        reloaded = db_session.query(ApiToken).filter_by(selector=issued.selector).one()
        assert reloaded.scopes == ["builds", "reconciliations"]
        assert reloaded.salt == issued.salt

    def test_duplicate_selector_violates_unique_constraint(self, db_session: Session) -> None:
        workspace = _make_workspace(db_session, slug="dup-selector-ws")
        issued = issue_api_token()
        common_kwargs = {
            "workspace_id": workspace.id,
            "prefix": issued.prefix,
            "selector": issued.selector,
            "salt": issued.salt,
            "token_hash": issued.token_hash,
            "scopes": ["admin"],
        }
        db_session.add(ApiToken(name="first", **common_kwargs))
        db_session.commit()
        db_session.add(ApiToken(name="second", **common_kwargs))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


class TestBuildAndManifestLineage:
    def test_manifest_referencing_build_persists(self, db_session: Session) -> None:
        workspace = _make_workspace(db_session, slug="lineage-ws")
        collection = SourceCollection(
            workspace_id=workspace.id, name="docs", namespace="docs", root_config={}
        )
        config = PipelineConfig(
            workspace_id=workspace.id, config_hash="a" * 64, config_json={"parser": "text"}
        )
        db_session.add_all([collection, config])
        db_session.flush()

        build = Build(
            workspace_id=workspace.id,
            source_collection_id=collection.id,
            pipeline_config_id=config.id,
            state=BuildState.COMPLETED,
        )
        db_session.add(build)
        db_session.flush()

        manifest = Manifest(
            workspace_id=workspace.id,
            build_id=build.id,
            namespace="docs",
            manifest_hash="b" * 64,
            status=ManifestStatus.ACTIVE,
            artifact_ref="manifests/" + "b" * 64 + "/manifest.json",
        )
        db_session.add(manifest)
        db_session.commit()

        fetched = db_session.get(Manifest, manifest.id)
        assert fetched is not None
        assert fetched.build_id == build.id
        assert fetched.status == ManifestStatus.ACTIVE

    def test_duplicate_manifest_hash_in_same_workspace_violates_unique_constraint(
        self, db_session: Session
    ) -> None:
        workspace = _make_workspace(db_session, slug="dup-manifest-ws")
        common_kwargs = {
            "workspace_id": workspace.id,
            "namespace": "docs",
            "manifest_hash": "c" * 64,
            "artifact_ref": "manifests/" + "c" * 64 + "/manifest.json",
        }
        db_session.add(Manifest(**common_kwargs))
        db_session.commit()
        db_session.add(Manifest(**common_kwargs))
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()


class TestCrossWorkspaceIsolation:
    def test_same_portable_id_allowed_in_different_workspaces(self, db_session: Session) -> None:
        """`(workspace_id, portable_id)` is the unique key, not `portable_id` alone (15.3)."""
        workspace_a = _make_workspace(db_session, slug="ws-a")
        workspace_b = _make_workspace(db_session, slug="ws-b")
        common_kwargs = {
            "namespace": "docs",
            "manifest_hash": "d" * 64,
            "artifact_ref": "manifests/d/manifest.json",
        }
        db_session.add(Manifest(workspace_id=workspace_a.id, **common_kwargs))
        db_session.add(Manifest(workspace_id=workspace_b.id, **common_kwargs))
        db_session.commit()  # must not raise: different workspaces, same manifest_hash

        count = db_session.query(Manifest).filter_by(manifest_hash="d" * 64).count()
        assert count == 2


class TestAuditLogInsert:
    def test_record_persists_a_new_audit_event(self, db_session: Session) -> None:
        workspace = _make_workspace(db_session, slug="audit-ws")
        audit = AuditLog(db_session)
        event = audit.record(
            actor_type="user",
            actor_id=str(uuid.uuid4()),
            action="workspace.create",
            result="success",
            workspace_id=workspace.id,
            entity_type="workspace",
            entity_id=str(workspace.id),
            request_id="req-123",
            metadata={"slug": workspace.slug},
        )
        db_session.commit()

        fetched = db_session.get(type(event), event.id)
        assert fetched is not None
        assert fetched.action == "workspace.create"
        assert fetched.result == "success"
        assert fetched.metadata_json == {"slug": workspace.slug}
