"""DB-backed job queue: enqueue, lease, complete, per the design specification section 21.

Section 21: "Dramatiq Redis broker, DB source of truth. Job message IDs
only. DB lease `FOR UPDATE SKIP LOCKED`." This module implements the DB
half of that design faithfully: the `jobs` row is the only authority on
a job's state, and a worker acquires work with a single
`SELECT ... FOR UPDATE SKIP LOCKED` so two workers can never lease the
same row. What this wave does *not* ship is the Dramatiq/Redis wakeup
channel itself -- `run_pending_jobs` is the execution entrypoint, and
the API layer invokes it via FastAPI background tasks after the
enqueueing request commits (single-process development/test execution).
A dedicated worker process would call the same function in a loop; when
the Dramatiq actor lands in a later slice, its actor body will also be
exactly this function, with the queue message carrying only the job id
per the spec. See the project status notes.

Retry policy (section 21: "Retry transient network/5xx/429; auth/config
no retry; parse deterministic crash no repeated unbounded retry"): a
handler failure marks the job `FAILED` after `MAX_ATTEMPTS` total
attempts, and re-queues it otherwise. Classifying transient-vs-permanent
error families is the handlers' job (a handler raises
`PermanentJobError` to skip retries entirely).

Cancellation in this slice covers `QUEUED` jobs only (they flip straight
to `CANCELLED` and never run). Cancelling a `RUNNING` job needs a
cooperative cancel-requested signal checked between pipeline stages;
that column and protocol are deferred with the Dramatiq wiring.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ragledger.server.db.models import Job
from ragledger.server.db.models.enums import JobStatus

__all__ = [
    "MAX_ATTEMPTS",
    "CancelOutcome",
    "JobCancelledError",
    "JobHandler",
    "PermanentJobError",
    "check_cancellation",
    "enqueue_job",
    "lease_next_job",
    "request_cancel",
    "run_pending_jobs",
]

logger = logging.getLogger("ragledger.server.jobs")

MAX_ATTEMPTS = 3
_DEFAULT_LEASE = timedelta(minutes=15)

JobHandler = Callable[[Session, Job], None]


class PermanentJobError(Exception):
    """A handler failure that must not be retried (auth/config/deterministic errors)."""


class JobCancelledError(Exception):
    """Raised by a handler when it observes its job's `cancel_requested` flag.

    `run_pending_jobs` rolls the handler's partial writes back, marks
    the job `CANCELLED`, and runs the job type's registered cancel
    finalizer (if any) so the related entity's own status can reflect
    the cancellation -- see the `finalizers` argument.
    """


class CancelOutcome:
    """What `request_cancel` did: cancelled outright, flagged, or nothing."""

    CANCELLED = "cancelled"
    REQUESTED = "requested"
    NOT_CANCELLABLE = "not_cancellable"


def request_cancel(session: Session, job: Job) -> str:
    """Cancel a queued job outright, or flag a leased/running one for cooperative stop.

    Returns a `CancelOutcome` value. A terminal job is left untouched
    (`NOT_CANCELLABLE`). The caller owns the transaction.
    """
    if job.status == JobStatus.QUEUED:
        job.status = JobStatus.CANCELLED
        job.completed_at = datetime.now(UTC)
        session.flush()
        return CancelOutcome.CANCELLED
    if job.status in (JobStatus.LEASED, JobStatus.RUNNING):
        job.cancel_requested = True
        session.flush()
        return CancelOutcome.REQUESTED
    return CancelOutcome.NOT_CANCELLABLE


def check_cancellation(session: Session, job: Job) -> None:
    """Raise `JobCancelledError` if this job's cancel flag was set by another transaction.

    Handlers call this between units of work (per page of points, per
    pipeline stage). The flag is re-read from the database, not from
    the session's identity-map copy, because the `:cancel` endpoint
    sets it in a different session/transaction.
    """
    session.expire(job, ["cancel_requested"])
    if job.cancel_requested:
        raise JobCancelledError(f"job {job.id} was cancelled by request")


def enqueue_job(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    job_type: str,
    payload: dict[str, Any] | None = None,
    related_entity_type: str | None = None,
    related_entity_id: str | None = None,
) -> Job:
    """Insert a `QUEUED` job row. The caller owns the transaction."""
    job = Job(
        workspace_id=workspace_id,
        job_type=job_type,
        payload=payload or {},
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
    )
    session.add(job)
    session.flush()
    return job


def lease_next_job(
    session: Session,
    *,
    worker_name: str,
    lease_for: timedelta = _DEFAULT_LEASE,
) -> Job | None:
    """Lease the oldest leasable job with `FOR UPDATE SKIP LOCKED`, or return `None`.

    Leasable means `QUEUED`, or `LEASED`/`RUNNING` with an expired lease
    (a worker that died mid-job; its lease lapsing is what makes the job
    visible again, per section 21's lease design). The returned row is
    already flipped to `LEASED` and flushed; the caller must commit
    promptly to release the row lock.
    """
    now = datetime.now(UTC)
    candidate = session.execute(
        select(Job)
        .where(
            (Job.status == JobStatus.QUEUED)
            | (
                Job.status.in_([JobStatus.LEASED, JobStatus.RUNNING])
                & (Job.lease_expires_at.is_not(None))
                & (Job.lease_expires_at < now)
            )
        )
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if candidate is None:
        return None
    candidate.status = JobStatus.LEASED
    candidate.lease_owner = worker_name
    candidate.lease_expires_at = now + lease_for
    candidate.attempt_count = candidate.attempt_count + 1
    session.flush()
    return candidate


def run_pending_jobs(
    session_factory: sessionmaker[Session],
    handlers: dict[str, JobHandler],
    *,
    worker_name: str = "inline",
    max_jobs: int | None = None,
    finalizers: dict[str, JobHandler] | None = None,
) -> int:
    """Lease and run queued jobs until the queue is empty (or ``max_jobs`` is hit).

    Each job runs in its own session/transaction pair: the lease commits
    immediately (so the row lock is not held for the duration of the
    work), then the handler runs and its session commits or rolls back
    as one unit with the job's terminal status update.
    """
    executed = 0
    while max_jobs is None or executed < max_jobs:
        with session_factory() as lease_session:
            job = lease_next_job(lease_session, worker_name=worker_name)
            if job is None:
                return executed
            job_id = job.id
            job_type = job.job_type
            lease_session.commit()

        with session_factory() as work_session:
            leased = work_session.get(Job, job_id)
            if leased is None:  # deleted out from under us; nothing to do
                continue
            handler = handlers.get(job_type)
            if handler is None:
                _finish(work_session, leased, JobStatus.FAILED, f"no handler for {job_type!r}")
                work_session.commit()
                executed += 1
                continue
            leased.status = JobStatus.RUNNING
            leased.started_at = datetime.now(UTC)
            work_session.flush()
            try:
                handler(work_session, leased)
            except JobCancelledError:
                work_session.rollback()
                cancelled = work_session.get(Job, job_id)
                if cancelled is not None:
                    _finish(work_session, cancelled, JobStatus.CANCELLED, None)
                    finalizer = (finalizers or {}).get(job_type)
                    if finalizer is not None:
                        finalizer(work_session, cancelled)
            except PermanentJobError as exc:
                work_session.rollback()
                _fail_in_fresh_row(work_session, job_id, str(exc), retry=False)
            except Exception as exc:  # noqa: BLE001 -- a job failure must never kill the worker
                logger.warning("job %s (%s) failed", job_id, job_type, exc_info=True)
                work_session.rollback()
                _fail_in_fresh_row(work_session, job_id, str(exc), retry=True)
            else:
                _finish(work_session, leased, JobStatus.COMPLETED, None)
            work_session.commit()
        executed += 1
    return executed


def _finish(session: Session, job: Job, status: JobStatus, error: str | None) -> None:
    job.status = status
    job.last_error = error[:2048] if error else None
    job.completed_at = datetime.now(UTC)
    job.lease_owner = None
    job.lease_expires_at = None
    session.flush()


def _fail_in_fresh_row(session: Session, job_id: uuid.UUID, error: str, *, retry: bool) -> None:
    """After a handler rollback, re-fetch the job row and record the failure.

    The handler's session was rolled back, so the in-memory `Job`
    instance is expired; failures are recorded against a freshly loaded
    row in the same (now clean) session. Retryable failures under the
    attempt budget go back to `QUEUED`; everything else is `FAILED`.
    """
    job = session.get(Job, job_id)
    if job is None:
        return
    if retry and job.attempt_count < MAX_ATTEMPTS:
        job.status = JobStatus.QUEUED
        job.last_error = error[:2048]
        job.lease_owner = None
        job.lease_expires_at = None
        session.flush()
        return
    _finish(session, job, JobStatus.FAILED, error)
