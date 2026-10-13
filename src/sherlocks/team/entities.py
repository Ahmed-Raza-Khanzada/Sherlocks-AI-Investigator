"""A5 Entity resolver: who is who.

Identifiers decide - CNIC, then a phone number, then the name together with the father's
name. A name alone never merges two people: it is a *maybe*, and a maybe is never used
to build a contradiction or a link. When a maybe matters, the officer is asked.

Code only (comparing identifiers and normalised names is not a judgment): the same
normalisers the graph uses (``name_key``, ``sounds_like`` across Urdu and Roman script).
"""

from __future__ import annotations

from typing import Any

from sherlocks.linkgraph.normalize import cnic13, mobile11, name_key, sounds_like

SAME, MAYBE, DIFFERENT, NEW = "same", "maybe", "different", "new"


# What a record writes where a name is missing - not a person to look for.
NOBODY = {"none", "unknown", "not known", "nil", "null", "na", "n a", "nobody", "نامعلوم", "نا معلوم"}


def _same_name(a: str, b: str) -> bool:
    ka, kb = name_key(a), name_key(b)
    return bool(ka and kb) and (ka == kb or sounds_like(a, b) and sounds_like(b, a))


def _father(net: Any, pid: str) -> str:
    d = net.data(pid)
    return str(d.get("father_name") or d.get("father") or "")


def resolve(net: Any, *, name: str = "", father: str = "", cnic: str = "", phone: str = "",
            decided: dict[str, Any] | None = None) -> dict[str, Any]:
    """``{state, pid, why}`` for one mention of a person in a record or a statement."""
    if decided:                                    # the officer said who it is
        return {"state": decided["state"], "pid": decided.get("pid"), "why": "the officer said so"}
    cnic, phone = cnic13(cnic) or "", mobile11(phone) or ""
    if cnic:
        for pid in net.people:
            if net.data(pid).get("cnic") == cnic:
                return {"state": SAME, "pid": pid, "why": "CNIC"}
    if phone:
        for pid in net.people:
            if any(str(p)[-10:] == phone[-10:] for p in net.data(pid).get("phones") or []):
                return {"state": SAME, "pid": pid, "why": "phone number"}
    if not name_key(name):
        return {"state": NEW, "pid": None, "why": "no name"}
    named = [pid for pid in net.people if _same_name(name, net.name(pid))]
    if not named:
        pid = net.resolve(name)
        named = [pid] if pid else []
    if not named:
        return {"state": NEW, "pid": None, "why": "not on the graph"}
    for pid in named:
        theirs = net.data(pid).get("cnic") or ""
        if cnic and theirs and cnic != theirs:
            continue                               # another person with the same name
        if father and _father(net, pid) and _same_name(father, _father(net, pid)):
            return {"state": SAME, "pid": pid, "why": "name and father's name"}
    clash = [pid for pid in named if (cnic and (net.data(pid).get("cnic") or cnic) != cnic)
             or (father and _father(net, pid) and not _same_name(father, _father(net, pid)))]
    open_ = [pid for pid in named if pid not in clash]
    if not open_:
        return {"state": DIFFERENT, "pid": clash[0], "why": "identifiers differ"}
    return {"state": MAYBE, "pid": open_[0], "why": "name only" if len(open_) == 1 else "name only, several people"}


def key_of(m: dict[str, Any]) -> str:
    return "|".join([name_key(m.get("name") or ""), name_key(m.get("father") or ""), cnic13(m.get("cnic")) or "",
                     mobile11(m.get("phone")) or ""])


def mentions(case: Any) -> list[dict[str, Any]]:
    """Every person named in the records, the recorded statements and the claims."""
    found: dict[str, dict[str, Any]] = {}

    def add(source: str, name: str, **ids: Any) -> None:
        m = {"name": name or "", "father": ids.get("father") or "", "cnic": ids.get("cnic") or "",
             "phone": ids.get("phone") or ""}
        if not name_key(m["name"]) or name_key(m["name"]) in NOBODY:
            return
        entry = found.setdefault(key_of(m), {**m, "sources": []})
        if source not in entry["sources"]:
            entry["sources"].append(source)

    for doc in case.documents.values():
        for person in doc.get("people") or []:
            if isinstance(person, dict):
                add(doc["id"], str(person.get("name") or ""), father=person.get("father") or person.get("father_name"),
                    cnic=person.get("cnic"), phone=person.get("phone") or person.get("contact"))
    for card in (case.register.get("cards") or {}).values():
        if card.get("record") == "system":
            continue                               # asked by identifier: it is that person's own record
        for f in card.get("fields") or []:
            add(card["doc"], f.get("person") or "", father=f.get("father"), cnic=f.get("cnic"))
    for w in case.statements.values():
        add(w["doc"], w["speaker"], father=w.get("father"))
    for c in case.claims.values():
        add((_doc_of(case, c["source"]) or {}).get("id") or c["source"], c["about"])
    return list(found.values())


def run(case: Any, net: Any) -> dict[str, int]:
    """A5 on the whole board: each mention resolved, kept as ``E#``."""
    counts = {SAME: 0, MAYBE: 0, DIFFERENT: 0, NEW: 0}
    with case.acting("A5 Entity resolver"):
        for m in mentions(case):
            key = key_of(m)
            old = next((e for e in case.entities.values() if e["key"] == key), None)
            got = resolve(net, **{k: m[k] for k in ("name", "father", "cnic", "phone")},
                          decided=(old or {}).get("decided"))
            if got["state"] == MAYBE:                 # the record itself may identify them
                for src in m["sources"]:
                    seen = lookup(case, net, m["name"], source=src, father=m["father"], cnic=m["cnic"])
                    if seen["state"] == SAME:
                        got = seen
                        break
            counts[got["state"]] += 1
            case.set_entity(key, {**m, **got, "name_on_graph": net.name(got["pid"]) if got["pid"] else None})
    return counts


def _doc_of(case: Any, source: str | None) -> dict[str, Any] | None:
    if not source:
        return None
    if source in case.statements:
        source = case.statements[source]["doc"]
    elif source in case.facts:
        source = case.facts[source]["doc"]
    return case.documents.get(source)


def lookup(case: Any, net: Any, name: str, *, source: str | None = None, father: str = "", cnic: str = "") -> dict[str, Any]:
    """The state of a name as the claims and the contradiction finder must treat it.
    ``source``: the entry it was read in - a person the record itself identifies (its
    CNIC or phone is in it, or the record was fetched from that person's own file) is the
    same person, not a maybe."""
    if not name_key(name):
        return {"state": NEW, "pid": None, "why": "no name"}
    for e in case.entities.values():
        if name_key(e.get("name") or "") == name_key(name) and e.get("decided"):
            return {"state": e["state"], "pid": e.get("pid"), "why": e.get("why")}
    got = resolve(net, name=name, father=father, cnic=cnic)
    doc = _doc_of(case, source)
    if got["state"] == MAYBE and got["pid"]:
        if str(source or "").startswith(f"sys:{got['pid']}:"):
            return {"state": SAME, "pid": got["pid"], "why": "named in their own police record"}
        if doc is not None and doc.get("kind") == "graph":
            return {"state": SAME, "pid": got["pid"], "why": "named from the case graph itself"}
    if got["state"] == MAYBE and doc is not None and got["pid"]:
        by_id = any(link["doc"] == doc["id"] and link["pid"] == got["pid"] and link.get("strength") == "identifier"
                    for link in case.links.values())
        if by_id or got["pid"] in (doc.get("owners") or []):
            return {"state": SAME, "pid": got["pid"], "why": f"identified in {doc['id']}"}
        # Named with identifiers elsewhere in the same record.
        for e in case.entities.values():
            if (e["state"] == SAME and e.get("pid") == got["pid"] and doc["id"] in (e.get("sources") or [])
                    and name_key(e.get("name") or "") == name_key(name)):
                return {"state": SAME, "pid": got["pid"], "why": f"{e['why']} in {doc['id']}"}
    return got


def decide(case: Any, ref: str, same: bool, pid: str | None = None) -> dict[str, Any] | None:
    """The officer answered "is this the same person?" - kept, and never asked again.
    Undoing a wrong match is the same call with ``same=False``."""
    e = case.entities.get(ref)
    if e is None:
        return None
    with case.acting("A5 Entity resolver"), case._lock:
        e["decided"] = {"state": SAME if same else DIFFERENT, "pid": pid or e.get("pid")}
        e["state"], e["why"] = e["decided"]["state"], "the officer said so"
        case._bump("entity", ref)
        # Everything built on the old reading follows: the claims that name them, and any
        # contradiction that only existed because two different people were read as one.
        changed = set()
        for c in case.claims.values():
            if name_key(c["about"]) == name_key(e["name"]) and ((_doc_of(case, c["source"]) or {}).get("id") in (
                    e.get("sources") or [])):
                c["about_pid"], c["about_state"] = (e["decided"]["pid"] if same else None), e["decided"]["state"]
                changed.add(c["id"])
    if not same:
        for x in list(case.contradictions.values()):
            if x["status"] == "open" and {x["a"].get("claim"), x["b"].get("claim")} & changed:
                case.move(x["id"], "resolved", f"{ref}: the officer said these records name different people")
    return e


def open_maybes(case: Any) -> list[dict[str, Any]]:
    """Maybes that matter: named by a claim the contradiction finder would compare."""
    used = {name_key(c["about"]) for c in case.claims.values()}

    def worth_asking(e: dict[str, Any]) -> bool:
        # The very same full name, held by one person on the case: asking "is X the same as X?"
        # helps nobody. (It still stays a maybe: nothing is merged on a name.)
        key = name_key(e.get("name") or "")
        return not (len(key.split()) >= 2 and key == name_key(e.get("name_on_graph") or "") and "several" not in (e.get("why") or ""))

    return [e for e in case.entities.values() if e["state"] == MAYBE and not e.get("decided")
            and name_key(e.get("name") or "") in used and worth_asking(e)][:2]
