"""`ragledger server serve|worker|migrate`: run the API, a job worker, or migrations.

The server design (the design specification section 21) makes the `jobs` table
the source of truth, so a worker needs nothing but database access:
`ragledger server worker` leases and executes queued jobs in a polling
loop. The API process also executes jobs it enqueues (via background
tasks), so a dedicated worker is optional in a single-process
deployment and becomes useful when builds/snapshots should not share a
process with request handling. A Redis-backed wakeup channel (Dramatiq)
can replace the poll without any schema change; the poll interval is
the only cost of not having it.

`serve` runs the FastAPI app under uvicorn on `APP_HOST`/`APP_PORT`.
`migrate` runs `alembic upgrade head` against `DATABASE_URL` -- the
same entrypoint the Alembic CLI uses, packaged so a deployment does
not need the `alembic` binary on PATH.
"""

from __future__ import annotations

import logging
import time

import typer

app = typer.Typer(
    help="Run the ragledger API server, a job worker, or database migrations.",
    no_args_is_help=True,
)

logger = logging.getLogger("ragledger.server.cli")


@app.command()
def serve(
    host: str | None = typer.Option(None, "--host", help="Bind address (default: APP_HOST)."),
    port: int | None = typer.Option(None, "--port", help="Bind port (default: APP_PORT)."),
) -> None:
    """Run the API server under uvicorn."""
    import uvicorn

    from ragledger.server.app import create_app
    from ragledger.server.settings import get_settings

    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host=host if host is not None else settings.app_host,
        port=port if port is not None else settings.app_port,
        log_config=None,  # keep ragledger's own JSON logging configuration
    )


@app.command()
def worker(
    poll_interval: float = typer.Option(
        2.0, "--poll-interval", min=0.1, help="Seconds to sleep when the queue is empty."
    ),
    worker_name: str = typer.Option(
        "worker", "--name", help="Lease-owner name recorded on jobs this worker runs."
    ),
    max_jobs: int | None = typer.Option(
        None, "--max-jobs", help="Exit after executing this many jobs (default: run forever)."
    ),
) -> None:
    """Run a job worker: lease and execute queued jobs until interrupted."""
    from ragledger.server.app import configure_logging
    from ragledger.server.db.session import make_engine, make_session_factory
    from ragledger.server.handlers import make_cancel_finalizers, make_handlers
    from ragledger.server.jobs import run_pending_jobs
    from ragledger.server.settings import get_settings

    settings = get_settings()
    configure_logging(settings)
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    handlers = make_handlers(settings)
    finalizers = make_cancel_finalizers()

    executed_total = 0
    logger.info("worker %r started (poll interval %.1fs)", worker_name, poll_interval)
    try:
        while True:
            budget = None if max_jobs is None else max_jobs - executed_total
            executed = run_pending_jobs(
                session_factory,
                handlers,
                worker_name=worker_name,
                max_jobs=budget,
                finalizers=finalizers,
            )
            executed_total += executed
            if max_jobs is not None and executed_total >= max_jobs:
                logger.info("worker %r exiting after %d jobs", worker_name, executed_total)
                return
            if executed == 0:
                time.sleep(poll_interval)
    except KeyboardInterrupt:
        logger.info("worker %r interrupted after %d jobs", worker_name, executed_total)
    finally:
        engine.dispose()


@app.command()
def migrate() -> None:
    """Run `alembic upgrade head` against DATABASE_URL."""
    # alembic.ini sits at the repository/installation root, two levels
    # above this package (src/ragledger/cli/commands -> repo root when
    # running from a checkout). For an installed package, ALEMBIC_CONFIG
    # may point at it explicitly.
    import os
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    config_path = os.environ.get("ALEMBIC_CONFIG")
    if config_path is None:
        candidate = Path(__file__).resolve().parents[4] / "alembic.ini"
        if not candidate.is_file():
            raise typer.BadParameter("alembic.ini not found; set ALEMBIC_CONFIG to its location")
        config_path = str(candidate)
    command.upgrade(Config(config_path), "head")
    typer.echo("database migrated to head")
