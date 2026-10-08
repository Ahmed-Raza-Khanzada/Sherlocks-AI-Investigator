"""The API router: which police system to ask, for what, about whom.

The model never picks a system freely. Every ask goes through the catalog:

1. **Topic** - the ask is mapped to a topic: identity, work, vehicle, phone, address,
   family, record, complaint, hotel, footprint, documents.
2. **Catalog** - each system lists the topics it answers, the identifier it needs, what
   it returns and how far it is trusted. Only systems from this list are called.
3. **Identifier check** - a CNIC-only system is called only when the person's CNIC is
   known; otherwise the lookup that finds it (SIMs, by phone) runs first.
4. **Memory** - systems already searched for that person are skipped, including the ones
   that found nothing ("nothing found" is a result too).
5. **Write back** - data comes back with the call recorded; nothing found is recorded too;
   a topic no system holds gets "no connected system holds this".

Every call itself goes through ``guard.py`` (people on the case only, the officer's
systems, limits per case, the audit log).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sherlocks.linkgraph.systems import SYSTEMS, system_label


@dataclass(frozen=True)
class CatalogEntry:
    system: str
    topics: tuple[str, ...]
    needs: str                 # cnic | phone | either
    output: str
    trust: str                 # verified | unverified


# Topic -> systems, in the order they are tried (most useful first).
TOPIC_SYSTEMS: dict[str, list[str]] = {
    "identity": ["nadra", "cfms"],
    "work": ["evs", "hope", "hrmis", "sbvs", "prvs"],
    "vehicle": ["excise", "avlc", "tracs", "dls"],
    "phone": ["simsdb", "subscriber", "caller_id"],
    "address": ["prvs", "old_tenant", "trust", "dls"],
    "family": ["prvs", "old_tenant", "trust", "dls"],
    "record": ["psrms", "cro", "watchlist", "arms"],
    "complaint": ["pfc", "igp_cms", "milap"],
    "hotel": ["hotel_eye"],
    "footprint": ["osint"],
}
# Documents are fetched by their own tools (FIR file, lab reports, CRO dossier).
DOCUMENT_TOPICS = {"fir_file": "fetch_fir", "lab": "fetch_lab_reports", "cro": "fetch_cro"}

_OUTPUT = {"nadra": "name, father, date of birth, address, photo", "cfms": "foreigner status",
           "evs": "employer", "hope": "employer, designation", "hrmis": "police posting", "sbvs": "employer, servant record",
           "prvs": "tenancy, landlord, family members", "excise": "registered vehicles", "avlc": "stolen / recovered vehicles",
           "tracs": "traffic challans", "dls": "driving licence, address", "simsdb": "owner CNIC, every SIM on it",
           "subscriber": "SIM owner (to 2020)", "caller_id": "name tags (unverified)", "old_tenant": "tenancy history",
           "trust": "property and tenancy", "psrms": "FIRs naming the person", "cro": "criminal record",
           "watchlist": "suspect watchlist", "arms": "arms licence profile", "pfc": "complaints", "igp_cms": "complaints",
           "milap": "lost persons / property", "hotel_eye": "hotel stays", "osint": "open-source footprint (unverified)"}

CATALOG: dict[str, CatalogEntry] = {}
for _topic, _systems in TOPIC_SYSTEMS.items():
    for _s in _systems:
        old = CATALOG.get(_s)
        info = SYSTEMS.get(_s)
        CATALOG[_s] = CatalogEntry(
            system=_s, topics=tuple(dict.fromkeys([*(old.topics if old else ()), _topic])),
            needs=info.needs if info else "either", output=_OUTPUT.get(_s, ""),
            trust="unverified" if _s in ("caller_id", "osint") else "verified")

# Knowledge-base concepts (what the question is about) -> router topics.
_CONCEPT_TOPIC = {"work": "work", "vehicle": "vehicle", "phone": "phone", "address": "address", "father": "family",
                  "family": "family", "property": "address", "hotel": "hotel", "accused": "record", "fir": "record",
                  "status": "record", "cnic": "identity", "cro": "record", "complainant": "complaint",
                  "complaints": "complaint"}
_QUERY_TOPIC = {"hotels": "hotel", "phones": "phone", "vehicles": "vehicle", "cases": "record", "count_cases": "record",
                "fir_status": "record", "serious_cases": "record", "criminals_near": "record", "profile": "identity"}


def topics_for(concepts: list[str], query_type: str | None = None) -> list[str]:
    out = [_CONCEPT_TOPIC[c] for c in concepts if c in _CONCEPT_TOPIC]
    if not out and query_type in _QUERY_TOPIC:
        out = [_QUERY_TOPIC[query_type]]
    return list(dict.fromkeys(out))


def searched(net: Any, case: Any, pid: str) -> dict[str, str]:
    """Systems already searched for ``pid``: by the graph run (its records) and by the
    agents since (``case.dialog['searched']``, including "nothing found")."""
    done = {str(r.get("system") or ""): "on the graph" for r in net.data(pid).get("records") or []}
    for key, status in ((case.dialog.get("searched") or {}) if case is not None else {}).items():
        who, _, system = key.partition("|")
        if who == pid:
            done[system] = status
    return done


def remember(case: Any, pid: str, system: str, status: str) -> None:
    if case is not None:
        case.dialog.setdefault("searched", {})[f"{pid}|{system}"] = status


def plan(net: Any, case: Any, pid: str, topics: list[str], *, allowed: list[str] | None = None,
         limit: int = 3) -> dict[str, Any]:
    """The calls to make for ``pid`` on ``topics``: ``{calls, skipped, unheld}``.

    ``calls`` - systems to ask, in order (a SIMs lookup first when a CNIC-only system
    needs the CNIC); ``skipped`` - systems already searched, with what they gave;
    ``unheld`` - topics no connected system holds."""
    d = net.data(pid)
    has_cnic, has_phone = bool(d.get("cnic")), bool(d.get("phones"))
    done = searched(net, case, pid)
    calls: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    unheld: list[str] = []
    for topic in topics:
        systems = [s for s in TOPIC_SYSTEMS.get(topic, []) if allowed is None or s in allowed]
        if not systems:
            unheld.append(topic)
            continue
        for system in systems:
            if system in done:
                skipped.append({"system": system, "why": done[system]})
                continue
            entry = CATALOG[system]
            if entry.needs == "cnic" and not has_cnic:
                if has_phone and "simsdb" not in done and not any(c["system"] == "simsdb" for c in calls):
                    calls.append({"system": "simsdb", "topic": "identity", "why": f"finds the CNIC {system_label(system)} needs"})
                    has_cnic = True        # resolved first; the CNIC-only call follows
                else:
                    skipped.append({"system": system, "why": "needs a CNIC, none known"})
                    continue
            if entry.needs == "phone" and not has_phone:
                skipped.append({"system": system, "why": "needs a phone number, none known"})
                continue
            if not any(c["system"] == system for c in calls):
                calls.append({"system": system, "topic": topic, "why": entry.output})
    return {"calls": calls[:limit], "skipped": skipped, "unheld": unheld}


def catalog_rows() -> list[dict[str, Any]]:
    """The catalog as data (for the portal and the docs)."""
    return [{"system": e.system, "label": system_label(e.system), "topics": list(e.topics), "needs": e.needs,
             "output": e.output, "trust": e.trust} for e in CATALOG.values()]
