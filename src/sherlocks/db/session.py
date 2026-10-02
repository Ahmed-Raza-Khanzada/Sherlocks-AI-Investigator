"""Engine and session management.

The engine is built lazily from settings and memoised per database URL, so importing
this module never opens a connection - which keeps the CLI and the tests usable with
no Postgres running.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from sherlocks.db.models import SCHEMA, Base
from sherlocks.settings import Settings, load_settings

logger = logging.getLogger(__name__)


# Every engine handed out, so a forked child can abandon the ones it inherited.
_ENGINES: list[Engine] = []


@lru_cache(maxsize=4)
def _build_engine(url: str, pool_size: int, max_overflow: int, echo: bool) -> Engine:
    engine = create_engine(
        url,
        echo=echo,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=3600,
        pool_pre_ping=True,
    )
    _ENGINES.append(engine)
    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    settings = settings or load_settings()
    db = settings.database
    return _build_engine(db.url, db.pool_size, db.max_overflow, db.echo)


def reset_engine() -> None:
    """Drop every memoised engine without closing its sockets.

    Call this **first thing in a forked child**. A fork copies the parent's connection
    pool, so parent and child end up writing into the same TCP sockets and Postgres
    tears them down - it surfaces as "server closed the connection unexpectedly" in
    whichever process loses the race, which is a confusing way to learn about fork
    semantics.

    ``dispose()`` is deliberately not called: it would close connections the parent is
    still using. ``dispose(close=False)`` abandons them instead, which is exactly what a
    child should do. Clearing the cache then forces a fresh engine on next use.
    """
    for engine in tuple(_ENGINES):
        try:
            engine.dispose(close=False)
        except Exception:  # a child that cannot tidy up must still run
            logger.debug("Could not dispose inherited engine", exc_info=True)
    _ENGINES.clear()
    _build_engine.cache_clear()


def get_sessionmaker(settings: Settings | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(settings), autocommit=False, autoflush=False)


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """Transactional scope. Commits on clean exit, rolls back on any exception."""
    factory = get_sessionmaker(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ensure_schema(settings: Settings | None = None) -> None:
    """Create the Postgres schema if it is missing.

    Sherlocks lives in its own schema so it can share a database with
    cdr_report_app's ``report_jobs`` without colliding, and so it can be deployed by
    a role that has no CREATEDB privilege.
    """
    settings = settings or load_settings()
    engine = get_engine(settings)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"'))


def init_db(settings: Settings | None = None) -> None:
    """Create any missing tables.

    Alembic owns schema changes; this exists for first-run bootstrap and for tests.
    It creates missing tables but never alters existing ones.
    """
    ensure_schema(settings)
    Base.metadata.create_all(bind=get_engine(settings))
