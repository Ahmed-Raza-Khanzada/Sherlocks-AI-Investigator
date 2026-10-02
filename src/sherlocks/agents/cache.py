"""Postgres-backed memo for structured LLM output.

Address and name normalisation see the same strings repeatedly - the same office
address across a dozen colleagues, the same "Muhammad" spelling variants. Caching is
what keeps a small local model viable at case scale.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from sherlocks.db.models import LlmCache

logger = logging.getLogger(__name__)


class PostgresLlmCache:
    """``LlmCacheBackend`` over the ``llm_cache`` table.

    Holds a Session, so its lifetime should match the unit of work that owns it.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, input_hash: str) -> dict[str, Any] | None:
        row = self.session.execute(
            select(LlmCache).where(LlmCache.input_hash == input_hash)
        ).scalar_one_or_none()
        return row.output if row else None

    def set(
        self,
        input_hash: str,
        kind: str,
        model: str,
        prompt_version: str,
        input_text: str,
        output: dict[str, Any],
    ) -> None:
        if self.session.get(LlmCache, input_hash) is not None:
            return
        self.session.add(
            LlmCache(
                input_hash=input_hash,
                kind=kind,
                model=model,
                prompt_version=prompt_version,
                # The prompt can be long; only the hash is load-bearing, the text is
                # kept for debugging why a given parse was cached.
                input_text=input_text[:8000],
                output=output,
            )
        )
        self.session.flush()


class MemoryLlmCache:
    """In-process cache for the CLI and tests, where there may be no database."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    def get(self, input_hash: str) -> dict[str, Any] | None:
        return self._store.get(input_hash)

    def set(
        self, input_hash: str, kind: str, model: str, prompt_version: str,
        input_text: str, output: dict[str, Any],
    ) -> None:
        self._store[input_hash] = output
