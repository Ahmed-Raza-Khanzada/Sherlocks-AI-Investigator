"""Where the case board lives: stored on its own, by run, with a version guard.

A finished run leaves memory after a while (``runs._KEEP_FINISHED_SECONDS``), but the
agents keep working on its board - the officer chats, uploads, answers; the Sherlock
team re-assesses in the background. So the board is stored apart from the run
(``case_board``), and every save carries the board's version:

* a save **older** than the stored board is refused - a stale copy (an old portal save,
  a second process) can never overwrite newer work;
* a board posted back by the host portal (``graph["case"]``) that is older than the
  stored one does not replace it: only the officer's own edits in it (roles, the
  incident) are merged in, as new entries (:func:`merge_posted`).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Protocol

from sherlocks.evidence.case_file import CaseFile

logger = logging.getLogger(__name__)


class BoardStore(Protocol):
    def load(self, run_id: str) -> dict[str, Any] | None: ...

    def save(self, run_id: str, board: dict[str, Any]) -> bool: ...


class MemoryBoardStore:
    def __init__(self) -> None:
        self._boards: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def load(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._boards.get(run_id)

    def save(self, run_id: str, board: dict[str, Any]) -> bool:
        with self._lock:
            stored = self._boards.get(run_id)
            if stored is not None and int(stored.get("version") or 0) > int(board.get("version") or 0):
                return False
            self._boards[run_id] = board
            return True


class PostgresBoardStore:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def load(self, run_id: str) -> dict[str, Any] | None:
        from sherlocks.db.models import CaseBoard
        from sherlocks.db.session import session_scope

        try:
            with session_scope(self.settings) as session:
                row = session.get(CaseBoard, run_id)
                return dict(row.board) if row is not None else None
        except Exception:
            logger.exception("Could not load the case board of %s", run_id)
            return None

    def save(self, run_id: str, board: dict[str, Any]) -> bool:
        """Insert, or update only when not older than what is stored (one statement, so
        two writers cannot interleave)."""
        from sqlalchemy.dialects.postgresql import insert

        from sherlocks.db.models import CaseBoard
        from sherlocks.db.session import session_scope

        version = int(board.get("version") or 0)
        try:
            with session_scope(self.settings) as session:
                stmt = insert(CaseBoard).values(run_id=run_id, version=version, board=board)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[CaseBoard.run_id],
                    set_={"version": version, "board": board, "updated_at": stmt.excluded.updated_at},
                    where=CaseBoard.version <= version,
                )
                result = session.execute(stmt)
                return bool(result.rowcount)
        except Exception:
            logger.exception("Could not save the case board of %s", run_id)
            return False


def merge_posted(board: CaseFile, posted: dict[str, Any] | None) -> list[str]:
    """A board the host posted back, older than ``board``: keep ``board``, and add the
    officer's own edits from the posted copy (roles, incident details) that it lacks.
    Returns what was merged."""
    if not isinstance(posted, dict) or int(posted.get("version") or 0) >= board.version:
        return []
    merged: list[str] = []
    with board.batch("Portal save", "merge"):
        for pid, role in (posted.get("roles") or {}).items():
            if str(pid) not in board.roles:
                board.set_role(str(pid), str(role))
                merged.append(f"role {pid}={role}")
        inc = posted.get("incident") if isinstance(posted.get("incident"), dict) else {}
        missing = {k: v for k, v in inc.items() if k != "at" and v not in (None, "")
                   and (board.incident or {}).get(k) in (None, "")}
        if missing:
            board.set_incident(missing)
            merged += [f"incident {k}" for k in missing]
    return merged
