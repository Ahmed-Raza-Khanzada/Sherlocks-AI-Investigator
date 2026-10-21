"""What belongs to the case under investigation - and what does not go in the report.

The board holds far more than the case: every FIR any person on the graph appears in (a
police officer who is complainant in this case is a witness in fifty others), every
tenancy, every SIM registered on someone's CNIC. A senior investigator's brief keeps to the
case and the target. This module decides, from the structure of the records (FIR numbers,
the document a statement or fact comes from, the system a tie comes from), what is the
case's own material:

    this FIR        the FIR the officer said this case is
    linked FIRs     an FIR file that names this case's FIR (the recovery FIR of the weapon)
    case documents  those FIR files, the reports that name them, the officer's uploads and notes
    case people     the target, the people those FIRs name, anyone the officer gave a role
    the target's    FIRs that name the target himself - his record, not other people's cases
    own FIRs

and which ties around the target are his close circle (family, co-accused, the same
vehicle, travelling together, phone contact) - never a SIM registered on another CNIC, a
tenancy witness, a landlord or a PRVS guarantor: those are common and are not leads.
Nothing here reads free text for meaning; it compares identifiers and record kinds.
"""

from __future__ import annotations

import re
from typing import Any

# Graph patterns that are too common to be a lead or evidence about a crime: a SIM in
# another person's name (usually a relative or a friend), a tenancy witness, a PRVS guarantor.
NOT_EVIDENCE = ("graph_sim_front", "graph_tenancy_witness", "graph_recurring_guarantor")
# Ties that are not the target's circle: who a SIM is registered to, who witnessed a
# tenancy, who vouched in PRVS, a landlord, a number found on someone else's record.
_NOT_CIRCLE = re.compile(r"registered owner|sim \S+ on another|shares phone|shared phone|same number|same identifier|"
                         r"found in prvs|verification|vouched|guarantor|reference for|tenan|landlord|licence on the "
                         r"same|investigat|checked into a hotel on the same phone|nominee|same address|same locality", re.IGNORECASE)
# Ties that are: family, crime together, the same vehicle, travelling together, real phone contact.
_CIRCLE = (("family", re.compile(r"\b(family|relative|sibling|husband|wife|father|mother|son|daughter|brother|sister)\b",
                                 re.IGNORECASE)),
           ("crime", re.compile(r"co-accused|filed fir|in fir|accused|complainant", re.IGNORECASE)),
           ("vehicle", re.compile(r"vehicle|driver", re.IGNORECASE)),
           ("travel", re.compile(r"hotel|co-guest|room-mate", re.IGNORECASE)),
           ("contact", re.compile(r"phone contact|called", re.IGNORECASE)))
# Kinds of document that are reports about the case (medical, laboratory, scene).
REPORT_RECORDS = ("mlo", "postmortem", "dna", "chemical", "forensic", "recovery_memo", "site_plan")


def fir_number(text: Any) -> tuple[str, str] | None:
    m = re.search(r"(\d{1,5})\s*/\s*(\d{2,4})", str(text or ""))
    return (m.group(1).lstrip("0"), m.group(2)[-2:]) if m else None


def names(text: str, number: tuple[str, str]) -> bool:
    """Does the text give this FIR number (569/26, 569/2026, 0569/26)?"""
    no, yy = number
    return bool(re.search(rf"(?<!\d)0*{re.escape(no)}\s*/\s*(?:20)?{re.escape(yy)}(?!\d)", text or ""))


def _person_names(data: dict[str, Any]) -> list[tuple[str, str]]:
    """(name, role) of everyone an FIR file names."""
    out: list[tuple[str, str]] = []

    def add(who: Any, role: str) -> None:
        name = str((who.get("name") if isinstance(who, dict) else who) or "").strip()
        if name and not any(n == name for n, _ in out):
            out.append((name, role))

    add(data.get("complainant"), "complainant")
    for m in data.get("nominated_suspects") or []:
        add(m, "accused")
    for m in data.get("arrested_suspects") or []:
        add(m, "accused (arrested)")
    roster = data.get("roster") if isinstance(data.get("roster"), dict) else {}
    for role, members in roster.items():
        for m in members if isinstance(members, list) else []:
            add(m, "witness" if role == "witnesses" else str(role).removesuffix("s").replace("_", " "))
    for m in data.get("witnesses") or []:
        add(m, "witness")
    for m in data.get("investigating_officers") or []:
        add(m, "investigating officer")
    return out


def circle_kind(relation: str) -> str | None:
    """The kind of close tie a relation is (family, crime, vehicle, travel, contact), or None
    when it is not one (a SIM owner, a tenancy, a PRVS witness...)."""
    if not relation or _NOT_CIRCLE.search(relation):
        return None
    return next((kind for kind, pattern in _CIRCLE if pattern.search(relation)), None)


def build(case: Any, net: Any) -> dict[str, Any]:
    """The case's own material, as ids and names (kept on the report job as ``scope``)."""
    inc = case.incident or {}
    basis = (case.dialog or {}).get("case_basis")
    seeds = list(net.seeds())
    this = fir_number(inc.get("fir")) if inc.get("fir") and basis == "fir_on_record" else None
    firs: dict[tuple[str, str], dict[str, Any]] = {}
    for doc in case.documents.values():
        ref = doc.get("ref") or {}
        if doc.get("kind") == "fir" and ref.get("fir_no"):
            firs[(str(ref["fir_no"]).lstrip("0"), str(ref.get("fir_year") or "")[-2:])] = doc
    main = firs.get(this) if this else None
    linked = [doc for number, doc in firs.items() if this and number != this
              and names(f"{doc.get('text') or ''} {doc.get('summary') or ''}", this)]
    numbers = {this} if this else set()
    numbers |= {(str(d["ref"]["fir_no"]).lstrip("0"), str(d["ref"].get("fir_year") or "")[-2:]) for d in linked}
    cards = (case.register or {}).get("cards") or {}
    docs: list[str] = [d["id"] for d in ([main] if main else []) + linked]
    for doc in case.documents.values():
        if doc["id"] in docs:
            continue
        record = (cards.get(doc["id"]) or {}).get("record")
        if doc.get("kind") in ("upload", "cdr", "notes"):
            docs.append(doc["id"])                      # what the officer brought or said is this case's
        elif doc.get("kind") == "lab" or record in REPORT_RECORDS:
            named = fir_number((doc.get("ref") or {}).get("fir")) or fir_number(doc.get("title"))
            if (named and named in numbers) or (not named and numbers and any(
                    names((doc.get("text") or "")[:2000], n) for n in numbers)):
                docs.append(doc["id"])
            elif not firs and not named:
                docs.append(doc["id"])                  # no FIR on the board: a report is the case's own
    people: list[dict[str, str]] = []
    for doc in ([main] if main else []) + linked:
        fir = f"{doc['ref']['fir_no']}/{str(doc['ref'].get('fir_year') or '')[-2:]}"
        for name, role in _person_names(doc.get("data") or {}):
            pid = net.resolve(name)
            if not any(p["name"] == name for p in people):
                people.append({"name": name, "role": role, "fir": fir, "pid": pid if pid in net.people else ""})
    pids = list(dict.fromkeys([*seeds, *[p for p in case.roles if p in net.people],
                               *[p["pid"] for p in people if p["pid"]]]))
    statements = [w["id"] for w in case.statements.values() if w["doc"] in docs]
    facts = [f["id"] for f in case.facts.values() if f["doc"] in docs or f.get("by") == "officer"]
    own_firs = []
    for pid in seeds:
        for f in net.data(pid).get("firs") or []:
            number = fir_number(f.get("label"))
            if number and number != this:
                own_firs.append({**f, "of": net.name(pid), "pid": pid, "number": number})
    return {"this_fir": "/".join(this) if this else None, "main": main["id"] if main else None,
            "linked": [d["id"] for d in linked], "fir_numbers": sorted("/".join(n) for n in numbers),
            "documents": docs, "statements": statements, "facts": facts, "people": people, "pids": pids,
            "targets": seeds, "own_firs": own_firs}


def source_doc(case: Any, ref: str) -> str | None:
    """The document an entry comes from (a statement's or a fact's document, or the document itself)."""
    if ref in case.statements:
        return case.statements[ref]["doc"]
    if ref in case.facts:
        return case.facts[ref]["doc"]
    return ref if ref in case.documents else None


def in_scope(case: Any, scope: dict[str, Any], ref: str) -> bool:
    """Is this entry the case's own material - from a case document, or said by the officer?"""
    if ref in case.facts and (case.facts[ref].get("by") == "officer" or case.facts[ref].get("kind") in NOT_EVIDENCE):
        return case.facts[ref].get("by") == "officer"
    return source_doc(case, ref) in set(scope["documents"])


def weak(case: Any, ref: str) -> bool:
    """A fact that states only a common pattern (a SIM in another's name, a tenancy witness, a guarantor)."""
    return (case.facts.get(ref) or {}).get("kind") in NOT_EVIDENCE


def close_circle(net: Any, scope: dict[str, Any], most: int = 8) -> list[dict[str, Any]]:
    """The target's close circle: one line per person tied to a target by family, crime,
    the same vehicle, travel or phone contact - the strongest tie of each, with its source."""
    from sherlocks.linkgraph.systems import system_label

    rank = {"crime": 0, "family": 1, "vehicle": 2, "travel": 3, "contact": 4}
    out: dict[str, dict[str, Any]] = {}
    for seed in scope["targets"]:
        if seed not in net.G:
            continue
        for other in net.G.neighbors(seed):
            if other not in net.people or other in scope["targets"] or "police_officer" in (net.data(other).get("flags") or []):
                continue                                    # an officer on someone's FIR is not their circle
            for link in sorted(net.G[seed][other]["links"], key=lambda link: link.cost):
                text = f"{link.label or ''} {link.sentence or ''}"
                kind = circle_kind(text)
                if kind is None:
                    continue
                row = {"pid": other, "name": net.name(other), "kind": kind,
                       "how": re.sub(r"\s*[—-]\s*inferred(?: \d\.\d+)?", "", link.sentence or link.label or "").strip(),
                       "source": system_label(link.system or "") or "record", "stated": bool(net.G[seed][other]["stated"]),
                       "criminal": bool(net.criminal(other)), "in_case": other in scope["pids"]}
                best = out.get(other)
                if best is None or rank[kind] < rank[best["kind"]]:
                    out[other] = row
                break
    rows = sorted(out.values(), key=lambda r: (not r["in_case"], rank[r["kind"]], not r["stated"], not r["criminal"]))
    return [r for r in rows if r["stated"]][:most]
