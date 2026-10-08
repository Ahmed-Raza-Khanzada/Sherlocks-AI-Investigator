"""A4 Dossier agent: one card per person, everything the case holds on them.

Identity, the role the officer gave, FIRs, stated links, documents and facts that name
them, what the officer said about them, the CDRs they own and Sherlock's open hypotheses
about them. Code only. A card is cached on the board (``case.dossiers``) with a
signature of everything it was built from, and rebuilt only when something about that
person changed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _signature(case: Any, net: Any, pid: str) -> str:
    d = net.data(pid)
    mine = case.for_person(pid)
    parts = {
        "data": [d.get("cnic"), d.get("phones"), len(d.get("records") or []), len(d.get("firs") or [])],
        "role": case.roles.get(pid),
        "docs": sorted(x["id"] for x in mine["documents"]),
        "facts": sorted(f["id"] + str(bool(f.get("stale") or f.get("replaced_by"))) for f in mine["facts"]),
        "links": sorted(x["id"] for x in mine["links"]),
        "hyp": sorted(h["id"] + h["status"] for h in case.hypotheses.values() if pid in (h.get("people") or [])),
        "statements": len([f for f in case.facts.values() if f.get("by") == "officer"]),
        "edges": len(net.neighbours(pid)) if pid in net.G else 0,
    }
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def build_card(case: Any, net: Any, pid: str) -> dict[str, Any]:
    d = net.data(pid)
    name = net.name(pid)
    mine = case.for_person(pid)
    lower = name.lower()
    statements = [f for f in case.live_facts() if f.get("by") == "officer"
                  and (pid in (f.get("pids") or []) or f.get("topic") == f"role:{pid}" or lower in f["statement"].lower())]
    cdrs = [doc for doc in case.documents.values() if doc["kind"] == "cdr" and (
        pid in (doc.get("owners") or [])
        or ((doc.get("data") or {}).get("analysis") or {}).get("subject") in (d.get("phones") or []))]
    return {
        "pid": pid, "name": name, "cnic": d.get("cnic"), "phones": (d.get("phones") or [])[:6],
        "seed": bool(d.get("seed")), "flags": d.get("flags") or [],
        "role": case.roles.get(pid),
        "firs": [{"label": f.get("label"), "role": f.get("role"), "offence": f.get("offence"),
                  "status": f.get("status"), "system": f.get("system")} for f in (d.get("firs") or [])[:20]],
        "links": [{"to": c.get("name") or c.get("id"), "relation": c["relation"], "stated": c["stated"], "via": c["via"]}
                  for c in (net.neighbours(pid)[:15] if pid in net.G else [])],
        "documents": [{"id": x["id"], "kind": x["kind"], "title": x["title"]} for x in mine["documents"][:20]],
        "facts": [{"id": f["id"], "statement": f["statement"], "tier": f.get("tier", "fact")}
                  for f in mine["facts"] if not (f.get("stale") or f.get("replaced_by"))][:20],
        "statements": [{"id": f["id"], "statement": f["statement"]} for f in statements][-10:],
        "cdrs": [{"id": doc["id"], "title": doc["title"]} for doc in cdrs],
        "hypotheses": [{"id": h["id"], "statement": h["statement"], "status": h["status"]}
                       for h in case.hypotheses.values() if pid in (h.get("people") or []) and not h.get("stale")],
        "sig": _signature(case, net, pid),
    }


def dossier(case: Any, net: Any, pid: str) -> dict[str, Any]:
    """The person's card: the cached one if nothing about them changed, else rebuilt."""
    card = case.dossiers.get(pid)
    if card is not None and card.get("sig") == _signature(case, net, pid):
        return card
    with case.acting("A4 Dossier agent"):
        fresh = build_card(case, net, pid)
        case.set_dossier(pid, fresh)
    return case.dossiers[pid]


def refresh(case: Any, net: Any, pids: list[str] | None = None) -> list[str]:
    """Refresh the cards of ``pids`` (default: targets and anyone with a role); returns
    whose card changed."""
    want = pids if pids is not None else list(dict.fromkeys([*net.seeds(), *case.roles]))
    changed = []
    for pid in want:
        if pid not in net.people:
            continue
        before = (case.dossiers.get(pid) or {}).get("sig")
        if dossier(case, net, pid).get("sig") != before:
            changed.append(pid)
    return changed
