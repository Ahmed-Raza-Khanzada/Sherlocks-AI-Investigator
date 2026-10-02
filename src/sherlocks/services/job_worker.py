"""Fork-per-job execution.

Inherited from cdr_report_app, and for the same reason: an OSINT run imports OpenOSINT,
spawns external binaries and drives a local LLM, and none of that reliably returns its
memory to the allocator. Running it in a child process means the operating system does
the cleanup when the child exits.

There is a second reason here that cdr_report_app did not have. ``OsintClient`` already
isolates each *tool*; this isolates the whole *job*, so a wedged run can be abandoned
without touching the API process.

The parent thread updates the job row itself if the child dies without doing so - a job
stuck at "processing" forever is worse than one that reports a crash.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import sys
from datetime import UTC, datetime

from sherlocks.db.models import Job
from sherlocks.db.session import session_scope
from sherlocks.settings import Settings, load_settings

logger = logging.getLogger(__name__)


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    progress: int | None = None,
    message: str | None = None,
    result_file_path: str | None = None,
) -> None:
    """Best-effort job row update. Never raises into the caller's control flow."""
    try:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is None:
                logger.warning("update_job called for unknown job %s", job_id)
                return
            if status is not None:
                job.status = status
            if progress is not None:
                job.progress_pct = progress
            if message is not None:
                job.message = message[:500]
            if result_file_path is not None:
                job.result_file_path = result_file_path
            job.updated_at = datetime.now(UTC)
    except Exception:
        logger.exception("Failed to update job %s", job_id)


def _start_method() -> str:
    """Always spawn. This is not the cheap option, and it is not negotiable.

    cdr_report_app forks, and gets away with it because it forks from a request handler.
    Sherlocks starts jobs from a background *thread* of a running uvicorn process, and
    ``fork`` in a multithreaded process copies only the calling thread while keeping
    every lock in whatever state it was in. Two things were observed here before this
    changed:

    * ``psycopg2.OperationalError: server closed the connection unexpectedly`` - parent
      and child writing into the same inherited TCP socket.
    * A child stuck forever in ``futex_do_wait`` - it inherited a mutex that was held by
      a parent thread that does not exist on this side of the fork, so nothing will ever
      release it. It outlived the API process that started it.

    Spawn costs roughly half a second of interpreter startup. An OSINT scan takes
    seconds to minutes. Do not trade the correctness back for that.
    """
    return "spawn"


def run_osint_job(job_id: str, subject_payload: dict, settings: Settings | None = None) -> None:
    """Run one OSINT job in a child process and wait for it.

    Call this from a background thread; it blocks for the length of the scan.
    """
    settings = settings or load_settings()
    context = mp.get_context(_start_method())
    process = context.Process(
        target=_osint_job_child,
        args=(job_id, subject_payload, settings.model_dump(mode="python")),
        daemon=False,
    )
    process.start()
    process.join()

    if process.exitcode not in (0, None):
        logger.error("OSINT job %s child exited with %s", job_id, process.exitcode)
        # The child sets "failed" itself on a normal exception. This covers the cases it
        # cannot report: a kill, an OOM, a segfault in a native dependency.
        _fail_if_unfinished(job_id, f"Worker exited with code {process.exitcode}")


def _fail_if_unfinished(job_id: str, message: str) -> None:
    try:
        with session_scope() as session:
            job = session.get(Job, job_id)
            if job is None or job.status in ("completed", "failed"):
                return
            job.status = "failed"
            job.message = message[:500]
            job.updated_at = datetime.now(UTC)
    except Exception:
        logger.exception("Could not mark job %s failed", job_id)


def _osint_job_child(job_id: str, subject_payload: dict, settings_payload: dict) -> None:
    # Imported inside the child so a spawn start method does not pay for them twice.
    from sherlocks.db.session import reset_engine
    from sherlocks.logging_config import configure_logging
    from sherlocks.osint.models import OsintSubject
    from sherlocks.services.osint_service import run_osint

    # First statement that runs in the child, before any query. With a fork start
    # method this process holds a copy of the parent's connection pool, pointing at the
    # parent's sockets; using one corrupts the wire protocol for both and Postgres
    # closes it. Abandon them and build fresh connections.
    reset_engine()

    settings = Settings.model_validate(settings_payload)
    # force=True: with a fork start method the child inherits the parent's
    # already-configured flag and would otherwise log nowhere.
    configure_logging(settings, force=True)

    try:
        update_job(job_id, status="processing", progress=10, message="Planning the scan...")
        subject = OsintSubject.model_validate(subject_payload)

        update_job(job_id, status="processing", progress=30, message="Running OSINT tools...")
        outcome = run_osint(subject, settings=settings, job_id=job_id)

        update_job(job_id, status="processing", progress=90, message="Rendering report...")
        counts = outcome.report.counts
        update_job(
            job_id,
            status="completed",
            progress=100,
            message=(
                f"Scan complete. {counts.get('ok', 0)} of {len(outcome.report.results)} "
                "tools returned results."
            ),
            result_file_path=str(outcome.pdf_path) if outcome.pdf_path else None,
        )
    except Exception as exc:
        logger.exception("OSINT job %s failed", job_id)
        update_job(job_id, status="failed", progress=0, message=f"{type(exc).__name__}: {exc}")
        sys.exit(1)
