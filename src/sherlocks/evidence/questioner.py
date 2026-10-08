"""The Questioner: what the case still needs from the officer.

It looks at the case board for gaps and asks - at most two open questions at a time,
the most useful first, never the same question twice:

1. Where did the incident happen? (opens the map to pin it) - everything about who
   was near the scene depends on it.
2. When? (date and time) - the crime day in every CDR.
3. Who is the main suspect? - when there are targets and no roles yet.
4. Whose CDR is this file? - an uploaded CDR whose subscriber is not on the graph.
5. Upload the main suspect's CDR? - once the incident is known and no CDR covers them.

Each question carries an ``action`` the portal turns into a button: ``pin_location``,
``choose_person`` (with options) or ``text``.
"""

from __future__ import annotations

from typing import Any

from sherlocks.evidence.case_file import CaseFile

MAX_OPEN = 3
# How much the case needs each answer: red - the investigation is blocked without it (what,
# where, when, who); orange - important; green - helpful. Shown as the question's colour.
PRIORITY = {"incident:what": "red", "incident:when": "red", "incident:place": "red", "roles:main": "red",
            "cdr_owner": "red", "incident:pin": "orange", "incident:fir": "orange", "roles:victim": "orange",
            "cdr_of": "orange", "confirm_lookup": "orange", "incident:vehicle": "green", "link:why": "green",
            "suspects:other": "green"}


def priority_of(key: str) -> str:
    return PRIORITY.get(key) or PRIORITY.get(key.split(":", 1)[0]) or "orange"


def _people(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in graph.get("nodes") or [] if n.get("kind") == "person"}


def _options(graph: dict[str, Any], first: list[str] | None = None, limit: int = 6) -> list[dict[str, str]]:
    people = _people(graph)
    order = list(dict.fromkeys([*(first or []), *[p for p, n in people.items() if n["data"].get("seed")],
                                *sorted(people, key=lambda p: -len(people[p]["data"].get("records") or []))]))
    return [{"id": pid, "label": people[pid].get("label") or pid} for pid in order if pid in people][:limit]


def gaps(case: CaseFile, graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Every question the board currently calls for, most useful first. A question whose
    answer is already on the board (stated in chat, pinned, answered) is not a gap."""
    out: list[dict[str, Any]] = []
    inc = case.incident or {}
    people = _people(graph)
    seeds = [p for p, n in people.items() if n["data"].get("seed")]
    roles = set(case.roles.values())
    if inc.get("lat") is None and not inc.get("place"):
        out.append({"key": "incident:place", "action": "pin_location",
                    "text": "Where did the incident happen? Pin it on the map - I will then check whose phones were "
                            "near the scene and which police station covers it."})
    elif inc.get("lat") is None:
        out.append({"key": "incident:pin", "action": "pin_location",
                    "text": f"You told me the incident was at {inc['place'][:80]}. Can you pin it on the map? Then I "
                            "can check which phones were near it."})
    if not inc.get("offence"):
        out.append({"key": "incident:what", "action": "text",
                    "text": "What happened in this case - which crime (robbery, murder, kidnapping, fraud...)?"})
    if not inc.get("date"):
        out.append({"key": "incident:when", "action": "text",
                    "text": "When did the incident happen - date and time? I need it to read the crime day in the CDRs."})
    if seeds and "main suspect" not in roles:
        out.append({"key": "roles:main", "action": "choose_person", "options": _options(graph, seeds),
                    "text": "Who is the main suspect in this case?"})
    if not inc.get("fir"):
        out.append({"key": "incident:fir", "action": "text",
                    "text": "Is there an FIR for this incident? Tell me its number, year and police station "
                            "(e.g. 604/2025 Gulistan-e-Johar) and I will read its file."})
    for doc in case.documents.values():
        analysis = (doc.get("data") or {}).get("analysis") or {}
        if doc["kind"] != "cdr" or analysis.get("kind") != "cdr" or doc.get("owners"):
            continue
        subject = analysis.get("subject")
        on_graph = any(subject in (n["data"].get("phones") or []) for n in people.values())
        if not on_graph:
            out.append({"key": f"cdr_owner:{doc['id']}", "action": "choose_person", "about": doc["id"],
                        "options": _options(graph, seeds) + [{"id": "other", "label": "Someone not on the graph"}],
                        "text": f"Whose CDR is {doc['data'].get('name')} (subscriber {subject or 'unknown'})? "
                                "It is not a number on the graph yet."})
    if seeds and not roles & {"victim", "complainant"}:
        out.append({"key": "roles:victim", "action": "choose_person",
                    "options": _options(graph) + [{"id": "other", "label": "Someone not on the graph"}],
                    "text": "Who is the victim or the complainant in this incident?"})
    if not inc.get("vehicle_or_weapon"):
        out.append({"key": "incident:vehicle", "action": "text",
                    "text": "Was a vehicle or weapon used? A number plate or a description helps me search for it."})
    if len(seeds) >= 2:
        a, b = people[seeds[0]].get("label"), people[seeds[1]].get("label")
        out.append({"key": "link:why", "action": "text",
                    "text": f"What do you believe ties {a} and {b} together - family, business, a past crime?"})
    # A CNIC or number the agents wanted to look up that appears only in a document.
    confirmed = set(case.dialog.get("confirmed_ids") or []) | set(case.dialog.get("declined_ids") or [])
    for ident in (case.dialog.get("unconfirmed_ids") or [])[:2]:
        if ident not in confirmed:
            out.insert(0, {"key": f"confirm_lookup:{ident}", "action": "text",
                           "text": f"A document mentions {ident}, who is not on the case graph. Shall I look this "
                                   "number up in the police systems? (yes / no)"})
    # R2 Enricher: owners of a CDR's top contacts not on the graph - only if the officer agrees.
    decided = set(case.dialog.get("confirmed_docs") or []) | set(case.dialog.get("declined_docs") or [])
    for doc in case.documents.values():
        analysis = (doc.get("data") or {}).get("analysis") or {}
        if doc["kind"] != "cdr" or doc["id"] in decided or not analysis.get("top_contacts"):
            continue
        on_graph = {p for n in people.values() for p in n["data"].get("phones") or []}
        unknown = [c for c in analysis["top_contacts"][:5] if c not in on_graph]
        if unknown:
            out.append({"key": f"confirm_lookup:cdr:{doc['id']}", "action": "text", "about": doc["id"],
                        "text": f"{doc['data'].get('name') or doc['title']}: shall I look up who owns its top "
                                f"{len(unknown)} contact(s) ({', '.join(unknown[:3])}{'…' if len(unknown) > 3 else ''}) "
                                "in the SIMs database? (yes / no)"})
            break
    out.append({"key": "suspects:other", "action": "text",
                "text": "Is there anyone else you suspect who is not on the graph yet? Give me a name with a CNIC "
                        "or number and I will tell you what is known."})
    if inc.get("lat") is not None:
        main = next((p for p, r in case.roles.items() if r == "main suspect"), None)
        covered = {o for d in case.documents.values() if d["kind"] == "cdr" for o in d.get("owners") or []}
        covered |= {pid for d in case.documents.values() if d["kind"] == "cdr"
                    for pid, n in people.items()
                    if ((d.get("data") or {}).get("analysis") or {}).get("subject") in (n["data"].get("phones") or [])}
        if main and main in people and main not in covered:
            phone = (people[main]["data"].get("phones") or ["his number"])[0]
            out.insert(0, {"key": f"cdr_of:{main}", "action": "upload",
                           "text": f"Do you have the CDR of {people[main]['label']} ({phone}) around the incident date? "
                                   "Upload it (Excel) and I will place him against the scene."})
    return out


def next_question(case: CaseFile, *, recent: set[str]) -> dict[str, Any] | None:
    """The question to ask now: never one answered, at most twice each, not one asked in
    the last turns, and the least-asked first."""
    candidates = [q for q in case.open_questions() if q.get("times", 0) < 2 and q["key"] not in recent]
    if not candidates:
        return None
    order = {q["id"]: i for i, q in enumerate(case.questions)}
    return min(candidates, key=lambda q: (q.get("times", 0), order[q["id"]]))


def ask_gaps(case: CaseFile, graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Ask the most useful open gaps (keeping at most ``MAX_OPEN`` open). Returns the new
    questions. Open questions whose answer has reached the board another way (stated in
    chat, pinned on the map) are closed first."""
    from sherlocks.evidence.question_desk import Gatekeeper

    current = {g["key"] for g in gaps(case, graph)}
    for q in case.open_questions():
        if q["key"] not in current and not q["key"].startswith("free:"):
            case.close_question(q["key"], "known from the case board")
    asked = []
    desk = Gatekeeper(case, graph)
    for gap in gaps(case, graph):
        if len(case.open_questions()) >= MAX_OPEN:
            break
        if any(q["key"] == gap["key"] for q in case.questions):
            continue
        q = desk.admit({**gap, "priority": gap.get("priority") or priority_of(gap["key"])}, by="Questioner")
        if q:
            asked.append(q)
    return asked
