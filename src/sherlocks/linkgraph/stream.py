"""Live graph deltas for a viewer.

A run's graph grows in place while it executes. Resending the whole snapshot on every
tick costs a megabyte a second on a large graph, so each viewer gets only what changed
since its last message: nodes and edges that are new or different, and the ids of any
that disappeared (two person nodes merged into one when a record revealed they share a
CNIC).

The diff is taken here, per viewer, rather than recorded inside ``GraphBuilder``:
node data is mutated in many places and edge wording (``relation``) is derived at
snapshot time, so comparing what was sent with what is there now is the only way to
be sure nothing is missed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _digest(item: dict[str, Any]) -> str:
    raw = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.blake2b(raw.encode(), digest_size=12).hexdigest()


class GraphDiffer:
    """Remembers what one viewer has been sent and returns only the changes."""

    def __init__(self) -> None:
        self._nodes: dict[str, str] = {}
        self._edges: dict[str, str] = {}
        self.sent_any = False

    def diff(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        """Changes since the previous call, or ``None`` when there are none.

        The first call returns everything with ``full: true``, which tells the client
        to replace what it holds - so a reconnecting viewer can never keep a node that
        was merged away while it was disconnected.
        """
        full = not self.sent_any
        out: dict[str, Any] = {"version": snapshot.get("version", 0), "full": full,
                               "nodes": [], "edges": [], "removed_nodes": [], "removed_edges": []}
        for kind, seen in (("nodes", self._nodes), ("edges", self._edges)):
            current: dict[str, str] = {}
            for item in snapshot.get(kind, []):
                digest = _digest(item)
                current[item["id"]] = digest
                if seen.get(item["id"]) != digest:
                    out[kind].append(item)
            if not full:
                out[f"removed_{kind}"] = [i for i in seen if i not in current]
            seen.clear()
            seen.update(current)
        self.sent_any = True
        if not full and not any(out[k] for k in ("nodes", "edges", "removed_nodes", "removed_edges")):
            return None
        return out
