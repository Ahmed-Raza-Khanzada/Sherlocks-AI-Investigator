"""Corrections: "Nahi, 2 March tha." "Kamran nahi, Imran ne fire kiya."

A correction is not new information. The old answer must stop being used, and
everything built on it has to be checked again:

1. **Spot it** - correction words ("nahi", "galat", "actually", "درست") with a value for
   a topic already on file. Without such words, a different value for a topic the
   officer already gave is asked about once: "Pehle aap ne 1 March bataya tha. 2 March
   kar dun?"
2. **Replace, never delete** - the new statement is a fact with ``replaces``; the old one
   stays, marked replaced, visible in the history.
3. **The ledger updates** - the question's answer changes; both turns are kept.
4. **Everything built on it is flagged** - every entry that rests on the old statement
   or the topic, directly or through other entries, is marked stale and leaves answers.
5. **Redo** - the work to redo is put on the board's agenda as one event (towers near the
   incident, re-filing a CDR's findings, dossiers, Sherlock's assessment).
6. **Tell the officer** - what was noted and what is being redone; if an earlier reply
   used the old value, say so.
"""

from __future__ import annotations

import re
from typing import Any

_MARKER = re.compile(r"(?<![\w؀-ۿ])(nahi|nahin|nai|nhi|galat|ghalat|ghalt|actually|correction|correct it|sorry|"
                     r"maaf|not\s+\S+\s*,?\s*(?:but|it was)|it was|i meant|matlab|dar ?asal|darasal|"
                     r"غلط|نہیں|درست|دراصل)(?![\w؀-ۿ])", re.IGNORECASE)
_YES = re.compile(r"^\s*(yes|y|haan|han|ji|jee|ok|okay|theek|thik|kar do|kardo|bilkul|ہاں|جی|ٹھیک)\b", re.IGNORECASE)
_NO = re.compile(r"^\s*(no|nahi|nahin|nai|mat|rehne do|نہیں)\b", re.IGNORECASE)
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
_ROLE_WORDS = {"main suspect": r"main suspect|prime suspect|asal mulzim|main mulzim|مرکزی ملزم",
               "victim": r"victim|mutasir|maqtool|متاثر|مقتول",
               "complainant": r"complainant|muddai|مدعی",
               "witness": r"witness|gawah|گواہ",
               "suspect": r"suspect|mulzim|accused|ملزم"}

CONFIRM = {
    "en": "Earlier you told me {old}. Shall I change it to {new}?",
    "roman": "Pehle aap ne {old} bataya tha. {new} kar dun?",
    "ur": "پہلے آپ نے {old} بتایا تھا۔ {new} کر دوں؟",
}
NOTED = {
    "en": "Corrected: {what} is now {new} (was {old}). {redo}",
    "roman": "Durust kar diya: {what} ab {new} hai (pehle {old}). {redo}",
    "ur": "درست کر دیا: {what} اب {new} ہے (پہلے {old})۔ {redo}",
}
REDO = {
    "en": "Re-checking what rested on it: {x}.",
    "roman": "Is par mabni cheezein dobara check ho rahi hain: {x}.",
    "ur": "اس پر مبنی چیزیں دوبارہ چیک ہو رہی ہیں: {x}۔",
}
EARLIER = {
    "en": "My earlier reply used {old}; it now reads {new}.",
    "roman": "Mere pichle jawab mein {old} tha; ab {new} hai.",
    "ur": "میرے پچھلے جواب میں {old} تھا؛ اب {new} ہے۔",
}
_WHAT = {"incident:date": {"en": "the incident date", "roman": "waqia ki tareekh", "ur": "واقعے کی تاریخ"},
         "incident:time": {"en": "the incident time", "roman": "waqia ka waqt", "ur": "واقعے کا وقت"},
         "incident:place": {"en": "the incident place", "roman": "waqia ki jagah", "ur": "واقعے کی جگہ"}}
# What is redone when a topic changes, as (agenda task, what the officer is told).
REDO_TASKS = {
    "incident:date": [("cdr:location", "CDR incident window"), ("cdr:patterns", "silences around the incident"),
                      ("summary", "timeline"), ("sherlock", "Sherlock's assessment")],
    "incident:time": [("cdr:location", "CDR incident window"), ("summary", "timeline"), ("sherlock", "Sherlock's assessment")],
    "incident:place": [("cdr:location", "towers near the pin"), ("incident:ps", "nearest police station"),
                       ("sherlock", "Sherlock's assessment")],
    "role": [("dossiers", "dossiers"), ("summary", "summary"), ("gaps", "the questions still open"),
             ("sherlock", "Sherlock's assessment")],
    "owner": [("cdr:refile", "CDR findings under the right person"), ("cdr:cross", "cross-CDR links"),
              ("dossiers", "dossiers"), ("sherlock", "Sherlock's assessment")],
}


def is_marked(message: str) -> bool:
    return bool(_MARKER.search(message or ""))


def _role_in(text: str) -> str | None:
    for role, rx in _ROLE_WORDS.items():
        if re.search(rx, text, re.IGNORECASE):
            return role
    return None


def detect(case: Any, net: Any, message: str) -> list[dict[str, Any]]:
    """Corrections the message makes: ``{topic, old, new, sure, ...}``. ``sure`` when
    the officer used correction words; otherwise it is confirmed first."""
    from sherlocks.linkgraph.investigator import _mentioned
    from sherlocks.linkgraph.sherlock_team import _incident_place, _iso_date

    text = (message or "").strip()
    if not text or text.endswith(("?", "؟")):
        return []
    marked = is_marked(text)
    inc = case.incident or {}
    found: list[dict[str, Any]] = []
    day = _iso_date(text)
    if day and inc.get("date") and str(inc["date"])[:10] != day:
        found.append({"topic": "incident:date", "old": str(inc["date"])[:10], "new": day, "sure": marked})
    tm = _TIME.search(text)
    if tm and inc.get("time") and inc["time"] != f"{int(tm.group(1)):02d}:{tm.group(2)}":
        found.append({"topic": "incident:time", "old": inc["time"], "new": f"{int(tm.group(1)):02d}:{tm.group(2)}",
                      "sure": marked})
    place = _incident_place(text)
    if place and inc.get("place") and place.lower() not in str(inc["place"]).lower() \
            and str(inc["place"]).lower() not in place.lower():
        found.append({"topic": "incident:place", "old": inc["place"], "new": place, "sure": marked})
    people = _mentioned(net, text, sound=False) or _mentioned(net, text)
    role = _role_in(text)
    if marked and len(people) >= 2 and people[0] in case.roles:
        # "Kamran nahi, Imran main suspect hai": the role moves from the first to the second.
        moved = role or case.roles[people[0]]
        found.append({"topic": f"role:{people[1]}", "kind": "role_move", "pid": people[1], "from_pid": people[0],
                      "old": f"{net.name(people[0])} = {case.roles[people[0]]}", "new": f"{net.name(people[1])} = {moved}",
                      "role": moved, "sure": True})
    elif people and role and people[0] in case.roles and case.roles[people[0]] != role:
        found.append({"topic": f"role:{people[0]}", "kind": "role", "pid": people[0], "role": role,
                      "old": f"{net.name(people[0])} = {case.roles[people[0]]}", "new": f"{net.name(people[0])} = {role}",
                      "sure": marked})
    if people and re.search(r"\bcdr\b|سی ڈی آر", text, re.IGNORECASE) and marked:
        for doc in case.documents.values():
            owners = doc.get("owners") or []
            if doc["kind"] == "cdr" and owners and people[0] not in owners:
                found.append({"topic": f"owner:{doc['id']}", "kind": "owner", "doc": doc["id"], "pid": people[0],
                              "old": ", ".join(net.name(o) for o in owners), "new": net.name(people[0]), "sure": True})
                break
    return found


def apply(case: Any, net: Any, corr: dict[str, Any], *, language: str = "en") -> dict[str, Any]:
    """Make the correction on the board, as one event. Returns ``{line, stale, redo}``."""
    topic = corr["topic"]
    family = topic.split(":", 1)[0] if topic.startswith(("role:", "owner:")) else topic
    with case.batch("O3 Statement recorder", "correction", topic=topic):
        old_fact = case.statement_for(topic)
        if corr.get("kind") == "role_move":
            old_fact = old_fact or case.statement_for(f"role:{corr['from_pid']}")
        note, line = case.officer_note(f"Correction: {topic} was {corr['old']}, now {corr['new']}", source="correction")
        new_fact = case.add_fact(note, f"Stated by the officer (correction): {topic} is {corr['new']} (was {corr['old']})",
                                 line, kind="officer_correction", by="officer", topic=topic,
                                 replaces=old_fact["id"] if old_fact else None)
        if topic == "incident:date":
            case.set_incident({"date": corr["new"]})
            key = "incident:when"
        elif topic == "incident:time":
            case.set_incident({"time": corr["new"]})
            key = "incident:when"
        elif topic == "incident:place":
            case.set_incident({"place": corr["new"], "lat": None, "lon": None})
            if case.incident is not None:          # the old pin is for the old place
                case.incident.pop("lat", None)
                case.incident.pop("lon", None)
            key = "incident:place"
        elif corr.get("kind") == "role_move":
            case.roles.pop(corr["from_pid"], None)
            case.set_role(corr["pid"], corr["role"])
            key = "roles:main" if corr["role"] == "main suspect" else "roles:victim" if corr["role"] in (
                "victim", "complainant") else ""
        elif corr.get("kind") == "role":
            case.set_role(corr["pid"], corr["role"])
            key = ""
        else:                                       # owner of a CDR
            doc = case.documents[corr["doc"]]
            doc["owners"] = [corr["pid"]]
            key = f"cdr_owner:{corr['doc']}"
        for q in case.questions:
            if key and q["key"] == key and q["status"] != "open":
                case.answer(q["id"], corr["new"])
        roots = [x for x in (old_fact["id"] if old_fact else None, topic) if x]
        if corr.get("kind") == "role_move":
            roots.append(f"role:{corr['from_pid']}")
        stale = case.mark_stale([x for x in case.dependents(roots) if x != new_fact], f"{topic} corrected")
        redo = REDO_TASKS.get(family, [("sherlock", "Sherlock's assessment")])
        for task, _what in redo:
            case.plan(task if task != "cdr:refile" else f"cdr:refile:{corr.get('doc')}", f"{topic} corrected")
    what = _WHAT.get(topic, {}).get(language) or topic.split(":", 1)[0]
    redo_text = REDO.get(language, REDO["en"]).format(x=", ".join(w for _t, w in redo))
    line = NOTED.get(language, NOTED["en"]).format(what=what, new=corr["new"], old=corr["old"], redo=redo_text)
    earlier = [t for t in case.conversation[-6:] if str(corr["old"]) in str(t.get("a") or "")]
    if earlier:
        line += " " + EARLIER.get(language, EARLIER["en"]).format(old=corr["old"], new=corr["new"])
    return {"line": line, "stale": stale, "redo": [t for t, _w in redo], "fact": new_fact}


def confirm_text(corr: dict[str, Any], language: str) -> str:
    return CONFIRM.get(language, CONFIRM["en"]).format(old=corr["old"], new=corr["new"])


def reply_to_pending(message: str) -> bool | None:
    """True / False for a yes / no to "shall I change it?", None if neither."""
    if _YES.match(message or ""):
        return True
    if _NO.match(message or ""):
        return False
    return None
