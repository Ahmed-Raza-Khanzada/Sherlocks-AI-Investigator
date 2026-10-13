"""The presence matrix: for each person, everything that says where they were in the
incident window - CDR rows, hotel records, what they and others stated, the records.

Built in code. A phone is not a person and a hotel record is not a person either, so
every cell says which kind of evidence it is; the matrix never concludes "was there".
S6 takes each person's row and looks for an account that the other cells do not fit.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sherlocks.team.claims import when

KIND_NOTE = {"cdr": "a phone on a tower, not the person", "hotel": "a hotel record, not the person",
             "statement": "what was stated, not proof", "vehicle": "a vehicle record, not the person", "officer": "the officer's word", "record": "an official record",
             "analysis": "our own inference"}


def window(case: Any, minutes: int = 60) -> dict[str, Any] | None:
    """The incident window: an hour either side of the stated time, or the whole day."""
    inc = case.incident or {}
    day = str(inc.get("date") or "")[:10]
    try:
        base = datetime.fromisoformat(day)
    except ValueError:
        return None
    clock = str(inc.get("time") or "").strip()
    try:
        hh, mm = (int(x) for x in clock.split(":")[:2]) if clock else (None, None)
    except ValueError:
        hh = mm = None
    if hh is None:
        return {"from": base, "to": base + timedelta(hours=23, minutes=59), "day": day, "exact": False}
    at = base.replace(hour=hh % 24, minute=mm % 60)
    return {"from": at - timedelta(minutes=minutes), "to": at + timedelta(minutes=minutes), "day": day, "exact": True}


def _kind(case: Any, source: str) -> str:
    if source.startswith("W"):
        return "statement"
    f = case.facts.get(source)
    if f is not None:
        return {"cdr": "cdr", "officer": "officer"}.get(f.get("by"), "analysis" if f.get("tier") != "fact" else "record")
    return "record"


def _overlaps(c: dict[str, Any], win: dict[str, Any]) -> bool:
    start = when(c.get("when_from"))
    if start is None:
        return False
    if len(str(c.get("when_from"))) <= 10:                 # a day only: the same day
        return start.strftime("%Y-%m-%d") == win["day"]
    end = when(c.get("when_to")) or start
    slack = timedelta(minutes=int(c.get("tolerance_min") or 0))
    return start - slack <= win["to"] and end + slack >= win["from"]


def build(case: Any, net: Any, *, minutes: int = 60) -> dict[str, Any] | None:
    """One row per person on the case. None while the incident has no date."""
    from sherlocks.linkgraph.case_queries import stays_at

    win = window(case, minutes)
    if win is None:
        return None
    pids = list(dict.fromkeys([*case.roles, *net.seeds(),
                               *[c["about_pid"] for c in case.claims.values() if c.get("about_pid")]]))
    out = []
    for pid in [p for p in pids if p in net.people][:40]:
        cells: list[dict[str, Any]] = []
        for c in case.claims.values():
            if c.get("about_pid") == pid and c["topic"] in ("presence", "place", "phone", "vehicle") and _overlaps(c, win):
                kind = "vehicle" if c["topic"] == "vehicle" else _kind(case, c["source"])
                cells.append({"kind": kind, "note": KIND_NOTE[kind], "text": f"{c['value']}" + (f" at {c['where']}" if c.get("where") else ""),
                              "when": c.get("when_from"), "source": c["source"], "claim": c["id"], "said_by": c["said_by"]})
        for s in stays_at(net.data(pid).get("stays") or [], win["day"]):
            cells.append({"kind": "hotel", "note": KIND_NOTE["hotel"], "source": "Hotel Eye",
                          "text": f"{s.get('hotel')} ({s.get('district') or '-'}), {s.get('check_in') or '-'} to "
                                  f"{s.get('check_out') or '-'}", "when": str(s.get("check_in") or "")})
        for doc in case.documents.values():
            if doc["kind"] != "cdr" or pid not in (doc.get("owners") or []):
                continue
            for r in ((doc.get("data") or {}).get("analysis") or {}).get("near_incident") or []:
                t = when(r.get("when"))
                if t is not None and win["from"] <= t <= win["to"]:
                    cells.append({"kind": "cdr", "note": KIND_NOTE["cdr"], "when": r.get("when"),
                                  "text": f"{r.get('number') or 'phone'} on a tower {r.get('km')} km from the pin"
                                          + (f" ({r['place']})" if r.get("place") else ""),
                                  "source": f"{doc['id']} row {r.get('row')}"})
        kinds = sorted({c["kind"] for c in cells})
        out.append({"pid": pid, "name": net.name(pid), "role": case.roles.get(pid), "cells": cells[:12],
                    "kinds": kinds, "covered": bool(cells)})
    return {"window": {"from": win["from"].isoformat(timespec="minutes"), "to": win["to"].isoformat(timespec="minutes"),
                       "exact": win["exact"]}, "rows": out}


def run(case: Any, net: Any) -> dict[str, Any] | None:
    matrix = build(case, net)
    if matrix is not None:
        with case.acting("Presence matrix"):
            if (case.presence or {}).get("rows") != matrix["rows"] or (case.presence or {}).get("window") != matrix["window"]:
                case.set_presence(matrix)
    return matrix


def lookup(case: Any, net: Any, who: str | None = None) -> dict[str, Any]:
    """The Presence matrix tool."""
    matrix = build(case, net)
    if matrix is None:
        return {"empty": True, "why": "the incident has no date on record, so there is no window to check"}
    pid = net.resolve(who) if who else None
    rows = [r for r in matrix["rows"] if pid is None or r["pid"] == pid]
    return {"window": matrix["window"], "rows": rows, "empty": not any(r["cells"] for r in rows),
            "note": "Each cell is one kind of evidence. A phone or a hotel record is not the person."}
