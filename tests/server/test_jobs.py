"""The DB job queue: enqueue, `FOR UPDATE SKIP LOCKED` leasing, retry, failure.

DB-backed; the leasing tests use two concurrent sessions against the
same Postgres to prove the skip-locked contract for real, not via
mocks.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from ragledger.server.db.models import Job, Workspace
from ragledger.server.db.models.enums import JobStatus
from ragledger.server.jobs import (
    MAX_ATTEMPTS,
    PermanentJobError,
    enqueue_job,
    lease_next_job,
    run_pending_jobs,
)
from tests.server.conftest import requires_database

pytestmark = requires_database


def _workspace(session: Session) -> uuid.UUID:
    workspace = Workspace(slug=f"ws-{uuid.uuid4().hex[:8]}", name="Jobs Test")
    session.add(workspace)
    session.commit()
    return workspace.id


class TestLeasing:
    def test_lease_returns_oldest_queued_job_and_marks_it_leased(self, db_session: Session) -> None:
        workspace_id = _workspace(db_session)
        first = enqueue_job(db_session, workspace_id=workspace_id, job_type="a")
        enqueue_job(db_session, workspace_id=workspace_id, job_type="b")
        db_session.commit()

        leased = lease_next_job(db_session, worker_name="w1")
        assert leased is not None
        assert leased.id == first.id
        assert leased.status == JobStatus.LEASED
        assert leased.lease_owner == "w1"
        assert leased.attempt_count == 1
        db_session.commit()

    def test_second_worker_skips_a_locked_row(self, db_engine: Engine) -> None:
        """While session A holds the row lock, session B leases the *other* job."""
        factory = sessionmaker(db_engine)
        with factory() as setup:
            workspace_id = _workspace(setup)
            enqueue_job(setup, workspace_id=workspace_id, job_type="a")
            enqueue_job(setup, workspace_id=workspace_id, job_type="b")
            setup.commit()

        with factory() as session_a, factory() as session_b:
            leased_a = lease_next_job(session_a, worker_name="wa")  # uncommitted: lock held
            leased_b = lease_next_job(session_b, worker_name="wb")
            assert leased_a is not None and leased_b is not None
            assert leased_a.id != leased_b.id
            session_a.commit()
            session_b.commit()

    def test_expired_lease_becomes_leasable_again(self, db_session: Session) -> None:
        workspace_id = _workspace(db_session)
        job = enqueue_job(db_session, workspace_id=workspace_id, job_type="a")
        job.status = JobStatus.RUNNING
        job.lease_owner = "dead-worker"
        job.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db_session.commit()

        leased = lease_next_job(db_session, worker_name="w2")
        assert leased is not None
        assert leased.id == job.id
        assert leased.lease_owner == "w2"
        db_session.commit()


class TestRunPendingJobs:
    def test_runs_handler_and_completes(self, db_engine: Engine) -> None:
        factory = sessionmaker(db_engine)
        with factory() as session:
            workspace_id = _workspace(session)
            job = enqueue_job(session, workspace_id=workspace_id, job_type="noop")
            session.commit()
            job_id = job.id

        ran: list[uuid.UUID] = []
        executed = run_pending_jobs(factory, {"noop": lambda s, j: ran.append(j.id)})
        assert executed == 1
        assert ran == [job_id]
        with factory() as session:
            row = session.get(Job, job_id)
            assert row is not None and row.status == JobStatus.COMPLETED
            assert row.lease_owner is None

    def test_transient_failure_requeues_then_fails_at_attempt_budget(
        self, db_engine: Engine
    ) -> None:
        factory = sessionmaker(db_engine)
        with factory() as session:
            workspace_id = _workspace(session)
            job = enqueue_job(session, workspace_id=workspace_id, job_type="flaky")
            session.commit()
            job_id = job.id

        def _always_fails(session: Session, job: Job) -> None:
            raise RuntimeError("transient network error")

        total = run_pending_jobs(factory, {"flaky": _always_fails})
        assert total == MAX_ATTEMPTS  # requeued until the attempt budget is spent
        with factory() as session:
            row = session.get(Job, job_id)
            assert row is not None and row.status == JobStatus.FAILED
            assert row.attempt_count == MAX_ATTEMPTS
            assert "transient network error" in (row.last_error or "")

    def test_permanent_failure_never_retries(self, db_engine: Engine) -> None:
        factory = sessionmaker(db_engine)
        with factory() as session:
            workspace_id = _workspace(session)
            job = enqueue_job(session, workspace_id=workspace_id, job_type="misconfigured")
            session.commit()
            job_id = job.id

        calls: list[int] = []

        def _permanent(session: Session, job: Job) -> None:
            calls.append(1)
            raise PermanentJobError("bad config")

        executed = run_pending_jobs(factory, {"misconfigured": _permanent})
        assert executed == 1
        assert len(calls) == 1
        with factory() as session:
            row = session.get(Job, job_id)
            assert row is not None and row.status == JobStatus.FAILED

    def test_handler_exception_rolls_back_partial_writes(self, db_engine: Engine) -> None:
        """A failed handler's DB writes must not survive the failure."""
        factory = sessionmaker(db_engine)
        with factory() as session:
            workspace_id = _workspace(session)
            enqueue_job(session, workspace_id=workspace_id, job_type="partial")
            session.commit()

        def _writes_then_fails(session: Session, job: Job) -> None:
            session.add(Workspace(slug="never-committed", name="Partial"))
            session.flush()
            raise PermanentJobError("failed after writing")

        run_pending_jobs(factory, {"partial": _writes_then_fails})
        with factory() as session:
            from sqlalchemy import select

            leaked = session.execute(
                select(Workspace).where(Workspace.slug == "never-committed")
            ).first()
            assert leaked is None

    def test_unknown_job_type_fails_cleanly(self, db_engine: Engine) -> None:
        factory = sessionmaker(db_engine)
        with factory() as session:
            workspace_id = _workspace(session)
            job = enqueue_job(session, workspace_id=workspace_id, job_type="mystery")
            session.commit()
            job_id = job.id

        run_pending_jobs(factory, {})
        with factory() as session:
            row = session.get(Job, job_id)
            assert row is not None and row.status == JobStatus.FAILED
            assert "no handler" in (row.last_error or "")
