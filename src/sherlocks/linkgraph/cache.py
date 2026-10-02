"""Provider answer cache.

Two reasons, and the second matters more than the first:

1. Speed. One searched person is ~20 upstream calls at up to 60 s each.
2. **Load on police systems.** Those calls are logged upstream. Re-opening yesterday's
   graph, or two analysts searching overlapping networks, must not re-query everyone.

Only definite answers are cached - ``success``, ``partial`` and ``no_record``. An
``error`` (expired cookie, timeout) is not an answer about the person and is retried.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sherlocks.settings import Settings

logger = logging.getLogger(__name__)

CACHEABLE_STATUSES = {"success", "partial", "no_record"}


# Bump when the meaning of a cached payload changes, to retire stale entries. v2: empty
# envelopes ({"data":[{...all null...}]}) are now no_record, so old "hit" rows must not
# be replayed.
# v3: BOM-broken responses were wrongly cached as no_record before the transport fix.
# v4: "either" systems now ask by CNIC *and* by mobile; a cached no_record from the
# CNIC-only query is not an answer about the number, so those rows must be retired.
# v5: NADRA parser now tolerant of response shapes; a real hit was previously misread and
# cached as no_record. Retire those rows.
CACHE_VERSION = "v5"


def cache_key(backend: str, system: str, cnic: str | None, phone: str | None) -> str:
    return f"{CACHE_VERSION}|{backend}|{system}|{cnic or ''}|{phone or ''}"


class ProviderCache(Protocol):
    def get(self, key: str) -> dict[str, Any] | None: ...

    def put(self, key: str, system: str, payload: dict[str, Any]) -> None: ...


class MemoryProviderCache:
    def __init__(self, ttl_seconds: int = 86_400) -> None:
        self.ttl = ttl_seconds
        self._items: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(key)
        if not item or item[0] < time.time():
            return None
        return item[1]

    def put(self, key: str, system: str, payload: dict[str, Any]) -> None:
        if payload.get("status") not in CACHEABLE_STATUSES:
            return
        with self._lock:
            self._items[key] = (time.time() + self.ttl, payload)


class PostgresProviderCache:
    """``provider_cache`` table. Survives restarts; shared by every analyst."""

    def __init__(self, settings: Settings, ttl_seconds: int) -> None:
        self.settings = settings
        self.ttl = ttl_seconds

    def get(self, key: str) -> dict[str, Any] | None:
        from sherlocks.db.models import ProviderCacheEntry
        from sherlocks.db.session import session_scope

        try:
            with session_scope(self.settings) as session:
                row = session.get(ProviderCacheEntry, key)
                if row is None or row.expires_at < datetime.now(UTC):
                    return None
                return dict(row.payload)
        except Exception:
            logger.exception("Provider cache read failed; querying upstream instead")
            return None

    def put(self, key: str, system: str, payload: dict[str, Any]) -> None:
        if payload.get("status") not in CACHEABLE_STATUSES:
            return
        from sherlocks.db.models import ProviderCacheEntry
        from sherlocks.db.session import session_scope

        now = datetime.now(UTC)
        try:
            with session_scope(self.settings) as session:
                row = session.get(ProviderCacheEntry, key)
                if row is None:
                    row = ProviderCacheEntry(key=key)
                    session.add(row)
                row.system = system
                row.status = str(payload.get("status"))
                row.payload = payload
                row.fetched_at = now
                row.expires_at = now + timedelta(seconds=self.ttl)
        except Exception:
            # A cache write must never fail the lookup it was meant to save.
            logger.exception("Provider cache write failed")

    def prune(self) -> int:
        """Drop rows that can never be read again: expired ones, and answers cached
        under a superseded CACHE_VERSION (their meaning changed, so they are ignored)."""
        from sqlalchemy import delete, or_

        from sherlocks.db.models import ProviderCacheEntry
        from sherlocks.db.session import session_scope

        try:
            with session_scope(self.settings) as session:
                result = session.execute(delete(ProviderCacheEntry).where(or_(
                    ProviderCacheEntry.expires_at < datetime.now(UTC),
                    ProviderCacheEntry.key.notlike(f"{CACHE_VERSION}|%"),
                )))
                return int(result.rowcount or 0)
        except Exception:
            logger.exception("Provider cache prune failed")
            return 0
