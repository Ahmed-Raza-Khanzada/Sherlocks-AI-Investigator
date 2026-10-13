"""O3 Statement recorder: what the officer tells us, written to the board as fields.

Keeping the officer's words and the turn says what was typed - not who the real source
is, when the event happened or how sure the officer is. The model reads the message and
breaks it into entries, one fact each:

* **who told us** (the officer, this turn) and the **original speaker** - "muddai ne
  bataya ke..." is two people, and a relayed statement gets the original speaker's
  trust, not the officer's;
* **about whom** (resolved to a person on the case), **what**, **where**;
* **when it happened** - "kal raat" becomes a date from the message time, the original
  words kept, with a tolerance ("10 baje ke qareeb" = 22:00 +/- 30 min) that S6 uses;
* **how the officer knows** (saw it, a record, heard it, an informant, thinks so) - this
  sets fact, inference or speculation - and **how sure**;
* whether it **corrects** something said before, which open question it **answers**,
  and what the officer wants of the desks (stop asking, hold the findings).

Code puts each entry where it belongs (the incident, a role, the ledger), and O1 reads
back in one line what was recorded, so a wrong date or name is caught at once.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from sherlocks.evidence.case_file import TRUST_OFFICER, TRUST_RECORDED, TRUST_UNVERIFIED
from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.team.runtime import Typed, ask, dumps

TIER = {"saw": "fact", "record": "fact", "heard": "inference", "informant": "inference", "thinks": "speculation",
        "not said": "fact"}
SPEAKER_TRUST = {"officer": TRUST_OFFICER, "record": TRUST_OFFICER, "complainant": TRUST_RECORDED,
                 "witness": TRUST_RECORDED, "accused": TRUST_RECORDED, "victim": TRUST_RECORDED,
                 "informant": TRUST_UNVERIFIED, "other": TRUST_RECORDED}
INCIDENT_FIELD = {"incident_place": "place", "incident_date": "date", "incident_time": "time",
                  "incident_offence": "offence", "incident_fir": "fir", "vehicle_or_weapon": "vehicle_or_weapon"}
TOPIC_KEY = {"incident_place": "incident:place", "incident_date": "incident:date", "incident_time": "incident:time",
             "incident_offence": "incident:offence", "incident_fir": "incident:fir",
             "vehicle_or_weapon": "incident:vehicle"}
CLOSES = {"incident_place": "incident:place", "incident_date": "incident:when", "incident_offence": "incident:what",
          "incident_fir": "incident:fir", "vehicle_or_weapon": "incident:vehicle"}
LANG = {"en": "English", "roman": "Roman Urdu (Urdu in Latin letters)", "ur": "Urdu (Urdu script)"}


class _Said(Typed):
    what: str = Field(description="ONE fact, one sentence in English, names and dates resolved.")
    quote: str = Field(description="The officer's exact words it comes from, in the original script.")
    topic: Literal["incident_place", "incident_date", "incident_time", "incident_offence", "incident_fir",
                   "vehicle_or_weapon", "role", "identity", "phone_owner", "event", "other"] = Field(
        description="What the entry is. incident_offence: the crime of the case, in a word or two - the act the "
                    "officer describes IS the offence ('killed' is murder, 'took my money and did not return it' is "
                    "fraud, 'snatched the phone' is robbery). incident_fir: ONLY an FIR number such as 121/25. "
                    "incident_place / incident_date / incident_time: where and when the crime happened. role: who a "
                    "person is in the case (did it, is suspected, victim, witness). event: anything else that "
                    "happened. A suspicion about who did it is 'role', never incident_fir.")
    value: str = Field(default="", description="The value itself: the place, the date (YYYY-MM-DD), the time (HH:MM), "
                                               "the offence, the FIR number, the vehicle or weapon.")
    about: str = Field(default="", description="The person it is about, as the officer named them.")
    role: Literal["", "main suspect", "suspect", "victim", "complainant", "witness", "informer", "cleared"] = Field(
        default="", description="For topic 'role': the role the officer gives that person in the new case.")
    original_speaker: str = Field(default="", description="Who really said it when the officer relays someone else "
                                                          "('the complainant told me...'); empty when it is the "
                                                          "officer's own knowledge.")
    speaker_role: Literal["officer", "complainant", "witness", "accused", "victim", "informant", "record", "other"] = "officer"
    how_known: Literal["saw", "record", "heard", "informant", "thinks", "not said"] = Field(
        default="not said", description="How the officer knows: saw it, read it in a record, heard it from someone, "
                                        "got it from an informant, or only thinks so ('I suspect', 'mujhe shak hai', "
                                        "'lagta hai' are 'thinks').")
    certainty: Literal["sure", "approximate", "unsure"] = Field(
        default="sure", description="approximate for 'about', 'qareeb', 'taqreeban'; unsure for a suspicion or a "
                                    "guess: 'I suspect', 'maybe', 'shayad', 'shak hai', 'lagta hai'.")
    when: str | None = Field(default=None, description="When the event happened, ISO (YYYY-MM-DD or "
                                                       "YYYY-MM-DDTHH:MM), worked out from words like 'kal raat' "
                                                       "using the message time given.")
    when_words: str = Field(default="", description="The officer's own words for the time.")
    tolerance_min: int = Field(default=0, description="How loose the time is in minutes: 30 for 'about 10 at night'.")
    where: str = ""
    corrects: bool = Field(default=False, description="True when this REPLACES something the officer said earlier on "
                                                      "the same topic ('nahi, 2 March tha').")


class _Record(Typed):
    entries: list[_Said] = Field(default_factory=list, description="One entry per fact stated. Empty when the "
                                                                   "message states nothing.")
    answers: str = Field(default="", description="The id (Q#) of the open question this message answers, or ''.")
    dont_know: bool = Field(default=False, description="True when the officer says they do not know the answer.")
    confirm: Literal["", "yes", "no"] = Field(default="", description="When Sherlock asked for a yes or no "
                                                                      "(to confirm a change or a lookup): the reply.")
    this_case: Literal["not said", "a new matter", "an FIR already on record"] = Field(
        default="not said", description="Which case the officer is working, worked out as an investigator would: "
                                        "compare what the officer describes with each FIR in FIRS_ON_FILE. 'an FIR "
                                        "already on record' when it is the SAME event as one of them (the same victim, "
                                        "the same act, the same people) - also when the officer now suspects someone "
                                        "the FIR does not name. 'a new matter' when no FIR on file describes it. 'not "
                                        "said' when the message does not let you tell.")
    this_case_fir: str = Field(default="", description="When the case is an FIR already on record: its number, as "
                                                       "'121/25', exactly as FIRS_ON_FILE gives it.")
    suspected: list[str] = Field(default_factory=list, description="The people the officer says did the crime of this "
                                 "case, or suspects of it - their names as on PEOPLE_ON_THE_CASE. Empty when nobody is.")
    target_in_custody: Literal["not said", "arrested", "not arrested", "on bail", "absconding"] = Field(
        default="not said", description="What the officer says about the target's custody in THIS case.")
    wish: Literal["none", "stop_asking", "pause_findings", "resume_findings"] = Field(
        default="none", description="stop_asking: the officer does not want the last question asked again; "
                                    "pause_findings: hold Sherlock's own findings until asked; resume_findings.")
    read_back: str = Field(description="ONE line spoken TO the officer, in their language, confirming what was noted, "
                                       "with the names and the case resolved: 'Noted: you suspect Kamran Aslam shot "
                                       "Naveed himself - the attack of FIR 488/25, where he is the complainant.' "
                                       "Never 'the officer stated', never a word about sources or how sure they are. "
                                       "A short thanks when nothing was stated.")


_SYSTEM = ("You are the statement recorder of a police investigation team. Read what the investigating officer wrote "
           "(Urdu, Roman Urdu or English) and record it. One entry per fact: 'yes, at 10, and Imran was there too' is "
           "two entries. Say who the real source is: when the officer relays what a complainant, witness or informant "
           "said, that person is the original speaker. Say how the officer knows it and how sure they are. Work out "
           "the date and time of the event from the message time given, and keep the officer's own words for it. "
           "Mark an entry as a correction only when it replaces something on file (ON_FILE shows what is). Say which "
           "open question the message answers - by what the answer IS, not by which question came last.\n"
           "WHICH CASE THIS IS - think as an investigator would, never by the wording. FIRS_ON_FILE tells you what "
           "each FIR on record is about (the offence, who complained, who was the victim, who is accused, when). "
           "Compare what the officer describes with them. The SAME event as an FIR on file - the same victim, the "
           "same act, the same people - is THAT case, whatever new the officer adds: an officer who says 'I suspect "
           "the complainant killed his daughter himself', when an FIR on file is that complainant's FIR for the "
           "murder of his daughter, is working that FIR's case with a new suspect - this_case 'an FIR already on "
           "record', its number, and the complainant as the one suspected. An event that no FIR on file describes "
           "('he took 5 lakh from me and never returned it', when the FIRs on file are robberies) is 'a new matter': "
           "the FIRs on file are then the person's history. When the message does not let you tell, 'not said' - "
           "never guess. Say whether the target has been arrested in this case only if the officer says so. Name in "
           "`suspected` everyone the officer says did it or suspects. Never record that the officer asked "
           "something - a question is not a fact. Record only "
           "what the officer states; a question the officer asks is not a statement.\n"
           "`read_back` is ONE short sentence in {lang}, spoken to the officer as 'you', starting with 'Noted:' (in "
           "their language): what you understood, with the person's full name and the case it belongs to. Nothing "
           "else - never 'the officer states', nothing about who the source is, how sure they are, or what the "
           "message did not mention. "
           + DATA_NOT_INSTRUCTIONS)


def read(case: Any, net: Any, message: str, llm: Any, language: str) -> _Record | None:
    """The model's reading of what the officer told us, or None (no model, or it failed)."""
    pending = case.dialog.get("pending_correction")
    return ask(llm, _Record, dumps({
        "message": message, "message_time": datetime.now(UTC).astimezone().isoformat(timespec="minutes"),
        "open_questions": [{"id": q["id"], "question": q["text"], "times_asked": q.get("times", 0)}
                           for q in case.open_questions()],
        "waiting_for_yes_or_no": (f"change {pending['topic']} from {pending['old']} to {pending['new']}?" if pending else None),
        "on_file": {"incident": case.incident, "roles": {net.name(p): r for p, r in case.roles.items() if p in net.people}},
        "firs_on_file": firs_on_file(case, net),
        "people_on_the_case": [net.name(p) for p in list(dict.fromkeys([*case.roles, *net.seeds(),
                                                                         *(case.dialog.get("focus") or [])]))[:30]],
        "recent_conversation": [{"officer": t.get("q", "")[:200], "sherlock": t.get("a", "")[:300]}
                                for t in case.conversation[-3:]]}),
        system=_SYSTEM.format(lang=LANG.get(language, "English")), kind="statement_recorder", max_tokens=1800)


def firs_on_file(case: Any, net: Any) -> list[dict[str, Any]]:
    """What each FIR on record is about, so a statement can be matched to the case it belongs to:
    the FIR files on the board (the offence, when, where, and what was read in them), and the
    targets' own FIR entries that have no file yet."""
    out, seen = [], set()
    for doc in case.documents.values():
        if doc.get("kind") != "fir":
            continue
        ref, data = doc.get("ref") or {}, doc.get("data") or {}
        number = f"{ref.get('fir_no')}/{ref.get('fir_year')}" if ref.get("fir_no") else doc["title"]
        seen.add(_fir_number(number))
        complainant = data.get("complainant")
        out.append({"fir": number, "police_station": data.get("police_station"), "sections": data.get("sections"),
                    "occurred": data.get("occurred"), "place": str(data.get("place") or "")[:120] or None,
                    "complainant": complainant.get("name") if isinstance(complainant, dict) else complainant,
                    # What was read in the file, in English: who is named, in what role, what happened.
                    "what_it_says": [case.facts[f]["statement"][:170] for f in doc.get("facts") or []
                                     if f in case.facts and case.usable(f)][:8]})
    for pid in net.seeds():
        for f in net.data(pid).get("firs") or []:
            if f.get("label") and _fir_number(str(f["label"])) not in seen:
                seen.add(_fir_number(str(f["label"])))
                out.append({"fir": f["label"], "police_station": f.get("ps"), "sections": f.get("offence"),
                            "the_target_is": f.get("role"), "what_it_says": []})
    return out[:6]


def _short(text: str, sentences: int = 2, limit: int = 320) -> str:
    """A read-back is a line, not a paragraph: its first sentences."""
    parts = re.split(r"(?<=[.!?؟۔])\s+", (text or "").strip())
    return " ".join(parts[:sentences])[:limit]


def case_changed(case: Any, before: tuple[Any, Any]) -> list[str]:
    """The officer has just said which case this is (or changed it). What the team worked out
    before knowing that - Sherlock's questions and his lines of inquiry - was about another
    picture of the case: the questions are withdrawn, the lines marked to be redone, and
    Sherlock is asked to think again."""
    now = (case.dialog.get("case_basis"), (case.incident or {}).get("fir"))
    if now == before or not now[0]:
        return []
    with case._lock:
        gone = [q for q in case.questions if q["status"] == "open" and q["key"].startswith("free:")]
        for q in gone:
            q["status"], q["withdrawn"] = "withdrawn", "written before the officer said which case this is"
            case._bump("question", q["id"])
    stale = case.mark_stale([h["id"] for h in case.hypotheses.values() if not h.get("stale")],
                            "the officer said which case this is")
    case.plan("sherlock", "the officer said which case this is")
    return ([f"{len(gone)} earlier question(s) of Sherlock withdrawn"] if gone else []) + (
        [f"{len(stale)} line(s) of inquiry to be thought through again"] if stale else [])


def _fir_number(text: str) -> tuple[str, str]:
    m = re.search(r"(\d{1,5})\s*/\s*(\d{2,4})", text or "")
    return (m.group(1).lstrip("0"), m.group(2)[-2:]) if m else ("", "")


def adopt_fir(case: Any, net: Any, fir: str) -> list[str]:
    """The officer said this case IS an FIR already on file: what that FIR records about the
    offence, the day and the place now describes this case, so none of it is asked again.
    Only what the officer has not said themselves is taken, and it is marked as coming from
    the FIR."""
    want = _fir_number(fir)
    if not want[0]:
        return []
    found: dict[str, str] = {}
    for doc in case.documents.values():
        ref = doc.get("ref") or {}
        if doc.get("kind") != "fir" or (str(ref.get("fir_no") or "").lstrip("0"), str(ref.get("fir_year") or "")[-2:]) != want:
            continue
        data = doc.get("data") or {}
        occurred = str(data.get("occurred") or "").strip()
        when = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?:\s+(\d{1,2}):(\d{2})\s*([AP]M)?)?", occurred)
        iso = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{2}))?", occurred) if not when else None
        if iso:
            found["date"] = f"{iso.group(1)}-{int(iso.group(2)):02d}-{int(iso.group(3)):02d}"
            if iso.group(4):
                found["time"] = f"{int(iso.group(4)):02d}:{iso.group(5)}"
        elif occurred and not when:
            found["date_as_recorded"] = occurred[:120]        # the FIR says when, in words no date could be read from
        if when:
            found["date"] = f"{when.group(3)}-{int(when.group(2)):02d}-{int(when.group(1)):02d}"
            if when.group(4):
                hour = int(when.group(4)) % 12 + (12 if (when.group(6) or "").upper() == "PM" else 0) if when.group(6) \
                    else int(when.group(4))
                found["time"] = f"{hour:02d}:{when.group(5)}"
        found.update({k: str(v).strip()[:160] for k, v in (("offence", data.get("sections")), ("place", data.get("place")),
                                                             ("police_station", data.get("police_station"))) if v})
        found["fir_doc"] = doc["id"]
        break
    for pid in net.seeds():                           # no file on the board: the target's own FIR entry
        for f in net.data(pid).get("firs") or []:
            if _fir_number(str(f.get("label") or "")) == want:
                found.setdefault("offence", str(f.get("offence") or "").strip()[:160])
                found.setdefault("police_station", str(f.get("ps") or "").strip()[:80])
    have = case.incident or {}
    took = {k: v for k, v in found.items() if v and not have.get(k)}
    if not took:
        return []
    case.set_incident({**took, "from_fir": sorted(k for k in took if k != "fir_doc")})
    for field, key in (("offence", "incident:what"), ("date", "incident:when"), ("date_as_recorded", "incident:when"),
                       ("place", "incident:place")):
        if took.get(field):
            case.close_question(key, f"{took[field]} (from FIR {fir.strip()[:40]})")
    return [f"{k} from the FIR: {v}" for k, v in took.items() if k != "fir_doc"]


def _person(case: Any, net: Any, name: str, focus: list[str]) -> str | None:
    pid = net.resolve(name) if name else None
    return pid if pid in net.people else None


def write(case: Any, net: Any, message: str, record: _Record, *, language: str, focus: list[str] | None = None,
          crimes: list[str] | None = None) -> dict[str, Any]:
    """Put the reading on the board, as one event. Returns what was done:
    ``{recorded, corrected, answered, wish, read_back}``."""
    from sherlocks.linkgraph import corrections

    focus = focus or []
    out: dict[str, Any] = {"recorded": [], "corrected": [], "answered": None, "wish": record.wish,
                           "read_back": _short(record.read_back)}
    basis_before = (case.dialog.get("case_basis"), (case.incident or {}).get("fir"))
    with case.batch("O3 Statement recorder", "statement"):
        doc_id, line = case.officer_note(message)
        # Which case this is: a new matter, or an FIR already on file - only as the officer says.
        if record.this_case == "an FIR already on record" and record.this_case_fir.strip():
            case.dialog["case_basis"] = "fir_on_record"
            case.set_incident({"fir": record.this_case_fir.strip()[:40]})
            case.close_question("case:which", f"FIR {record.this_case_fir.strip()[:40]}")
            case.close_question("incident:fir", record.this_case_fir.strip()[:40])
            out["recorded"] += adopt_fir(case, net, record.this_case_fir)
            out["recorded"].append(f"this case is FIR {record.this_case_fir.strip()[:40]}, already on record")
        elif record.this_case == "a new matter":
            case.dialog["case_basis"] = "new_matter"
            case.close_question("case:which", "a new matter")
            out["recorded"].append("this is a new matter: the FIRs on file are earlier cases")
        if record.target_in_custody != "not said":
            case.set_incident({"target_status": record.target_in_custody})
            out["recorded"].append(f"target: {record.target_in_custody}")
        for name in record.suspected[:4]:
            # Who the officer says did it, or suspects: their role in this case - the officer's word, not evidence.
            pid = _person(case, net, name, focus)
            if pid and case.roles.get(pid) not in ("main suspect", "suspect"):
                role = "main suspect" if len(record.suspected) == 1 and "main suspect" not in case.roles.values() else "suspect"
                case.set_role(pid, role)
                case.close_question("roles:main", net.name(pid))
                out["recorded"].append(f"{net.name(pid)}: {role} (the officer's suspicion)")
        inc = case.incident or {}
        if crimes and inc.get("offence") and "offence" in (inc.get("from_fir") or []) and not inc.get("offence_said"):
            # The crime in the officer's word, with the sections the FIR gives: "murder (302, 114 ...)".
            case.set_incident({"offence": f"{', '.join(crimes[:2])[:60]} ({inc['offence']})"[:200], "offence_said": True})
        if crimes and not (case.incident or {}).get("offence") and record.this_case != "not said":
            # The crime as the message was read ("killed" is murder): nobody is asked "which crime?" after saying it.
            case.set_incident({"offence": ", ".join(crimes[:2])[:80]})
            case.close_question("incident:what", ", ".join(crimes[:2])[:80])
            out["recorded"].append(f"offence: {', '.join(crimes[:2])[:80]}")
        for e in record.entries[:10]:
            if e.topic == "incident_fir" and not _fir_number(e.value or e.what)[0]:
                e = e.model_copy(update={"topic": "event"})      # an FIR is a number: anything else is just what was said
            pid = _person(case, net, e.about, focus)
            field = INCIDENT_FIELD.get(e.topic)
            topic = TOPIC_KEY.get(e.topic) or (f"role:{pid}" if e.topic == "role" and pid else None)
            value = (e.value or e.what).strip()
            if e.topic in ("incident_date", "incident_time") and e.when:
                day, _, clock = e.when.strip().replace(" ", "T").partition("T")
                value = (clock[:5] if e.topic == "incident_time" else day) or value
            on_file = (case.incident or {}).get(field) if field else (case.roles.get(pid) if e.topic == "role" and pid else None)
            new = e.role if e.topic == "role" else value
            if e.corrects and on_file and topic and str(on_file) != str(new):
                # A correction is not new information: the old value stops being used, and
                # everything built on it is checked again.
                fix = {"topic": topic, "old": str(on_file), "new": new, "sure": True}
                if e.topic == "role":
                    fix.update({"kind": "role", "pid": pid, "role": e.role, "old": f"{net.name(pid)} = {on_file}",
                                "new": f"{net.name(pid)} = {e.role}"})
                done = corrections.apply(case, net, fix, language=language)
                out["corrected"].append({"topic": topic, "line": done["line"], "stale": done["stale"], "redo": done["redo"]})
                continue
            fid = case.add_fact(doc_id, f"Stated by the officer: {e.what}", e.quote, kind=f"officer_{e.topic}",
                                people=[e.about] if e.about else [], pids=[pid] if pid else [], by="officer",
                                topic=topic, tier=TIER[e.how_known], trust=SPEAKER_TRUST[e.speaker_role])
            if fid is None:                         # the quote is not in the message: recorded on the line itself
                fid = case.add_fact(doc_id, f"Stated by the officer: {e.what}", line, kind=f"officer_{e.topic}",
                                    people=[e.about] if e.about else [], pids=[pid] if pid else [], by="officer",
                                    topic=topic, tier=TIER[e.how_known], trust=SPEAKER_TRUST[e.speaker_role],
                                    check_quote=False)
            if fid is None:
                continue
            with case._lock:
                case.facts[fid]["said"] = {
                    "told_by": "officer", "turn": len(case.conversation) + 1, "original_speaker": e.original_speaker,
                    "speaker_role": e.speaker_role, "about": e.about, "about_pid": pid, "how_known": e.how_known,
                    "certainty": e.certainty, "when": e.when, "when_words": e.when_words,
                    "tolerance_min": max(0, min(e.tolerance_min, 720)), "where": e.where}
            out["recorded"].append(f"recorded {fid}: {e.what}")
            if field and value and not on_file:
                case.set_incident({field: value[:200]})
                if e.topic in CLOSES:
                    case.close_question(CLOSES[e.topic], value[:200])
                out["recorded"].append(f"incident {field}: {value[:80]}")
            elif e.topic == "role" and pid and e.role and not on_file:
                case.set_role(pid, e.role)
                if e.role == "main suspect":
                    case.close_question("roles:main", net.name(pid))
                out["recorded"].append(f"role: {net.name(pid)} = {e.role}")
    out["recorded"] += case_changed(case, basis_before)
    return out
