"""Tests for job execution.

The start-method test is a regression guard, not a style check. Both failure modes it
prevents were observed live against a running API, and one of them left an orphaned
process behind that outlived the server.
"""

from __future__ import annotations

from sherlocks.services import job_worker


def test_jobs_are_never_forked() -> None:
    """Sherlocks starts jobs from a background thread of a running uvicorn process.

    ``fork`` there copies only the calling thread while keeping every mutex in whatever
    state it happened to be in, and copies the parent's live Postgres sockets. Observed:
    "server closed the connection unexpectedly" from a shared socket, and a child stuck
    permanently in ``futex_do_wait`` on a lock no surviving thread can release.
    """
    assert job_worker._start_method() == "spawn"


def test_the_child_entrypoint_drops_inherited_engines_first() -> None:
    """Belt-and-braces for the connection half of the same problem.

    Spawn makes this unnecessary, but a future change back to fork must not silently
    reintroduce a shared connection pool.
    """
    import inspect

    source = inspect.getsource(job_worker._osint_job_child)
    body = source[source.index("reset_engine()") :]
    assert "reset_engine()" in source
    # It must run before anything that could touch the database.
    assert "update_job" not in source[: source.index("reset_engine()")]
    assert body


def test_update_job_never_raises_for_an_unknown_job() -> None:
    """Status reporting is the only channel a child has. It must not be the thing that
    kills the child."""
    job_worker.update_job("no-such-job", status="processing", progress=10)
