"""One chat turn with Sherlock, worked by the agent team.

    officer's message
      -> Fact collector   records what the officer STATES (with the quote), sets roles / incident date
      -> Sherlock (lead)  plans and calls tools; the API, Document and CDR agents do the fetching
      -> Assessor         writes the conclusion from the tool results (investigator._llm_final)
      -> Validator        checks it against the officer's statements, the conversation and the
                          case file; sends it back once to fix, otherwise shows the doubt
      -> Questioner       asks what the case still needs (map pin, main suspect, whose CDR)
      -> answer, with every agent's step shown as it happened

The agents coordinate through the case board (``CaseFile``): each reads it and writes
its result back with a source. Every event carries ``agent`` so the portal can show who
did what. Without a model the same pipeline runs on rules.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from typing import Any, Literal

from pydantic import BaseModel, Field

from sherlocks.evidence.case_file import CITATION, CaseFile
from sherlocks.evidence.questioner import ask_gaps
from sherlocks.linkgraph.investigator import investigate
from sherlocks.linkgraph.network import PersonNetwork

logger = logging.getLogger(__name__)

_DATE = re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b|\b(\d{4})-(\d{2})-(\d{2})\b")
_INCIDENT_WORDS = ("incident", "occurred", "happened", "occurrence", "crime", "robbery", "murder", "snatch", "waqia",
                   "waqiya", "wardat", "hua", "hui", "واقعہ", "وقوعہ", "واردات")


def _agent(agent: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"type": "agent", "agent": agent, "text": text, **extra}


# --------------------------------------------------------------------------------------
# Fact collector
# --------------------------------------------------------------------------------------


class _Statement(BaseModel):
    fact: str = Field(description="One sentence in English: what the officer states.")
    quote: str = Field(description="The officer's exact words it comes from.")
    kind: Literal["incident_place", "incident_time", "role", "identifier", "event", "other"] = "other"
    person: str | None = Field(default=None, description="The person it is about, as named by the officer.")
    role: str | None = Field(default=None, description="For kind 'role': main suspect, suspect, victim, complainant, witness.")


class _Statements(BaseModel):
    statements: list[_Statement] = Field(default_factory=list, description="Only statements of fact, never questions.")


_COLLECT_SYSTEM = ("You record what a police officer STATES in a chat with the investigation assistant: facts about "
                   "the incident (place, date, time), people's roles (main suspect, victim, ...), numbers, vehicles, "
                   "events, answers to questions. The officer may write in Urdu, Roman Urdu or English: write `fact` "
                   "in English, and `quote` exactly as the officer wrote it, in the original script. Ignore pure "
                   "questions and requests. Return an empty list when nothing is stated.")


def _iso_date(text: str) -> str | None:
    m = _DATE.search(text)
    if not m:
        return None
    if m.group(4):
        return f"{m.group(4)}-{m.group(5)}-{m.group(6)}"
    d, mth, y = m.group(1), m.group(2), m.group(3)
    y = y if len(y) == 4 else f"20{y}"
    try:
        return f"{int(y):04d}-{int(mth):02d}-{int(d):02d}"
    except ValueError:
        return None


def collect(case: CaseFile, net: PersonNetwork, message: str, llm: Any = None,
            focus: list[str] | None = None) -> list[str]:
    """Record the officer's statements. Returns one line per thing recorded."""
    text = message.strip()
    if not text:
        return []
    doc_id, _line = case.officer_note(text)
    done: list[str] = []
    statements: list[_Statement] = []
    if llm is not None:
        try:
            out, _ = llm.generate_structured(prompt=f"Officer's message: {text}", schema=_Statements,
                                             system=_COLLECT_SYSTEM, cache_kind="officer_facts", prompt_version="v1")
            statements = out.statements[:6]
        except Exception as exc:  # noqa: BLE001 - the rules below still record the basics
            logger.info("Fact collector model failed: %s", exc)
    is_question = text.rstrip().endswith("?") and not statements
    low = text.lower()
    if not statements and not is_question:
        if any(w in low for w in ("main suspect", "prime suspect", "asal mulzim", "main mulzim", "مرکزی ملزم")):
            statements.append(_Statement(fact=text, quote=text, kind="role", role="main suspect"))
        elif re.search(r"\b(suspect|mulzim|accused)\b|ملزم", low):
            statements.append(_Statement(fact=text, quote=text, kind="role", role="suspect"))
        if _iso_date(text) and any(w in low for w in _INCIDENT_WORDS):
            statements.append(_Statement(fact=text, quote=text, kind="incident_time"))
    # "hamay kamran pr shaq ha": the officer suspects someone - recorded even if the model put it otherwise.
    if not is_question and _SUSPECT.search(text) and not any(st.kind == "role" for st in statements):
        main = "main suspect" not in case.roles.values()
        statements.append(_Statement(fact=text, quote=text, kind="role", role="main suspect" if main else "suspect"))
    place = _incident_place(text)
    if place and not is_question and not (case.incident or {}).get("place"):
        case.set_incident({"place": place})
        case.close_question("incident:place", place)
        done.append(f"incident place: {place}")
    for st in statements:
        fid = case.add_fact(doc_id, f"Stated by the officer: {st.fact}", st.quote, kind=f"officer_{st.kind}",
                            people=[st.person] if st.person else [], by="officer")
        if fid is None:
            continue
        done.append(f"recorded {fid}: {st.fact}")
        if st.kind == "role":
            pid = (net.resolve(st.person) if st.person else None) or _named(net, text) or (focus or [None])[0]
            if pid and not (case.roles.get(pid) == "main suspect" and (st.role or "suspect").lower() == "suspect"):
                case.set_role(pid, (st.role or "suspect").lower())
                if (st.role or "").lower() == "main suspect":
                    case.close_question("roles:main", net.name(pid))
                done.append(f"role: {net.name(pid)} = {st.role or 'suspect'}")
        if st.kind == "incident_time":
            day = _iso_date(st.quote) or _iso_date(text)
            if day:
                case.set_incident({"date": day})
                case.close_question("incident:when", day)
                done.append(f"incident date: {day}")
    return done


_SUSPECT = re.compile(r"(?<![\w؀-ۿ])(shak|shaq|shuba|shubah|شک|شبہ)(?![\w؀-ۿ])", re.IGNORECASE)
_INCIDENT_WORD = r"(?:wardaa*t|wardat|wardqaa*t|waqia|waqiya|waqa|vaqia|incident|crime|occurrence|jurm|واردات|واقعہ|جرم)"
_PLACE_SAID = [
    # "wardaat saddar mein hui", "waqia Gulshan block 5 par hua"
    re.compile(_INCIDENT_WORD + r"\s+(?P<p>[^.,?!؟]{2,60}?)\s+(?:ma|mein|main|me|mai|par|pr|pe|میں|پر)\s+"
               r"(?:hui|hua|huwa|hoi|howi|hoa|ہوئی|ہوا)", re.IGNORECASE),
    # "saddar mein wardaat hui", "incident happened at Saddar"
    re.compile(r"(?P<p>[^.,?!؟]{2,40}?)\s+(?:ma|mein|main|me|mai|میں)\s+" + _INCIDENT_WORD, re.IGNORECASE),
    re.compile(_INCIDENT_WORD + r"\s+(?:happened|occurred|took place)\s+(?:at|in|near)\s+(?P<p>[^.,?!]{2,60})",
               re.IGNORECASE),
]
_PLACE_FILLER = re.compile(r"^(?:okay|ok|ji|haan|han|sir|to|tou|ye|yeh|ke|ki|ka|jo|wo|woh|aur|or)\s+", re.IGNORECASE)


def _incident_place(text: str) -> str | None:
    for rx in _PLACE_SAID:
        m = rx.search(text)
        if m:
            place = m.group("p").strip()
            while _PLACE_FILLER.match(place):
                place = _PLACE_FILLER.sub("", place, count=1)
            if place and not re.search(r"\b(kahan|kaha|kidhar|where|kis)\b", place, re.IGNORECASE):
                return place.strip().title() if place.isascii() else place.strip()
    return None


def _named(net: PersonNetwork, text: str) -> str | None:
    from sherlocks.linkgraph.investigator import _mentioned

    found = _mentioned(net, text)
    return found[0] if found else None


# --------------------------------------------------------------------------------------
# Validator
# --------------------------------------------------------------------------------------


class _Check(BaseModel):
    consistent: bool = Field(description="True when the answer agrees with the officer's statements, the earlier "
                                         "conversation and the cited facts.")
    issues: list[str] = Field(default_factory=list, description="Each contradiction or unsupported claim, one sentence.")


class _Revision(BaseModel):
    answer: str = Field(description="The corrected answer, same sources in [brackets].")


_CHECK_SYSTEM = ("The draft may be in Urdu, Roman Urdu or English. You are the Validator of a police investigation team. Compare the draft answer with what the officer "
                 "has stated, the earlier conversation and the cited facts. Flag only real problems: a contradiction "
                 "(another date, place, person or role than stated), a claim with no support in the facts, a person "
                 "the facts do not mention. Do not flag style.")


def validate(case: CaseFile, net: PersonNetwork, final: dict[str, Any], llm: Any = None) -> dict[str, Any]:
    """``{ok, issues, checked}`` for a draft answer."""
    answer = final.get("answer") or ""
    issues: list[str] = []
    known = set(case.documents) | set(case.facts) | set(case.links)
    bad = [ref for ref in dict.fromkeys(CITATION.findall(answer)) if ref not in known]
    if bad:
        issues.append(f"Cites {', '.join(bad)}, which is not in the case file.")
    inc_date = (case.incident or {}).get("date")
    if inc_date and any(w in answer.lower() for w in ("incident", "occurrence", "crime")):
        from datetime import date

        for m in _DATE.finditer(answer):
            day = _iso_date(m.group(0))
            try:
                gap = abs((date.fromisoformat(day) - date.fromisoformat(str(inc_date)[:10])).days) if day else 0
            except ValueError:
                continue
            window = answer[max(0, m.start() - 60):m.end() + 60].lower()
            if gap > 1 and any(w in window for w in ("incident", "occurrence", "crime")):
                issues.append(f"Gives {m.group(0)} as the incident date, but the officer stated {inc_date}.")
                break
    checked = "rules"
    if llm is not None:
        notes = case.doc_for("notes:officer")
        cited = case.citations_in(" ".join([answer] + [e for h in final.get("hypotheses") or [] for e in h.get("evidence") or []]))
        prompt = json.dumps({
            "officer_statements": (notes or {}).get("text", "")[-3000:],
            "earlier_conversation": [{"q": t["q"][:300], "a": t["a"][:500]} for t in case.conversation[-4:]],
            "cited_facts": [{"id": c["id"], "text": c["title"], "quote": c.get("quote")} for c in cited][:20],
            "incident": case.incident, "roles": {net.name(p): r for p, r in case.roles.items()},
            "draft_answer": answer}, ensure_ascii=False, default=str)
        try:
            check, _ = llm.generate_structured(prompt=prompt, schema=_Check, system=_CHECK_SYSTEM,
                                               cache_kind="validator", prompt_version="v1")
            issues += [i for i in check.issues[:4] if i.strip()]
            checked = "rules + model"
        except Exception as exc:  # noqa: BLE001
            logger.info("Validator model failed: %s", exc)
    return {"ok": not issues, "issues": issues, "checked": checked}


def revise(final: dict[str, Any], issues: list[str], llm: Any) -> str | None:
    try:
        out, _ = llm.generate_structured(
            prompt=f"Draft answer: {final.get('answer')}\n\nProblems found: {issues}\n\nRewrite the answer fixing them. "
                   "Remove anything unsupported; keep the sources; keep the same language and tone as the draft.",
            schema=_Revision, system="You correct an investigation answer. Use only what the draft and the problems say.",
            cache_kind="revise", prompt_version="v1")
        return out.answer.strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.info("Revision failed: %s", exc)
        return None


# --------------------------------------------------------------------------------------
# The turn
# --------------------------------------------------------------------------------------


def _needs_research(query: dict[str, Any], facts: dict[str, Any] | None, hits: list[Any], case: CaseFile,
                    net: PersonNetwork) -> bool:
    """Nothing gathered answers the question, or it is about an FIR whose file is unread."""
    from sherlocks.linkgraph.case_queries import _firs, fir_document

    if query.get("firs") and any(fir_document(case, f) is None for f in query["firs"]):
        return True
    if query["type"] in ("fir_details", "fir_status", "io_report") and query["people"]:
        return any(not r.get("doc") for r in _firs(net, query["people"][0], case))
    if facts is not None:
        return bool(facts.get("empty")) and bool(query["people"] or query.get("firs"))
    return not hits and bool(query["people"])


def _case_summary(case: CaseFile, net: PersonNetwork) -> str:
    targets = ", ".join(net.name(p) for p in net.seeds()) or "no targets yet"
    inc = case.incident or {}
    where = (inc.get("place") or (f"{inc['lat']}, {inc['lon']}" if inc.get("lat") is not None else "")) or "not pinned yet"
    roles = ", ".join(f"{net.name(p)} = {r}" for p, r in case.roles.items() if p in net.people)
    return (f"targets {targets}; {len(net.people)} people on the graph; {len(case.documents)} case document(s); "
            f"incident: {where}{(' on ' + inc['date']) if inc.get('date') else ''}" + (f"; roles: {roles}" if roles else ""))


_NO = re.compile(r"^\s*(no|nope|none|not known|unknown|don'?t know|do not know|nahi|nahin|nai|pata nahi|maloom nahi|"
                 r"koi nahi|nhi|نہیں|معلوم نہیں|پتہ نہیں|کوئی نہیں)\b", re.IGNORECASE)


_CRIME = re.compile(r"murder|qatl|killing|killed|robbery|dakait|dakaiti|snatch|chheen|theft|chori|stolen|kidnap|aghwa|"
                    r"abduct|fraud|dhoka|cheat|rape|zina|extortion|bhatta|narcotic|drugs|charas|heroin|firing|fire kiya|"
                    r"injur|zakhmi|attack|hamla|burglary|nakab|dacoity|terror|blast|\b(302|324|365|376|379|380|382|392|"
                    r"395|397|406|420|489)\b|قتل|ڈکیتی|چوری|اغوا|فراڈ|زیادتی|فائرنگ|حملہ|بھتہ", re.IGNORECASE)
_PLACE = re.compile(r"\b(road|rd|street|st|gali|block|sector|colony|town|nagar|abad|chowk|market|bazar|bazaar|plaza|"
                    r"near|opposite|behind|ke paas|ke qareeb|area|mohalla|goth|village|house|flat|phase|highway|bridge|"
                    r"station|karachi|hyderabad|sukkur|larkana)\b|روڈ|گلی|بلاک|سیکٹر|کالونی|ٹاؤن|چوک|بازار|نزد|محلہ|گوٹھ",
                    re.IGNORECASE)
_TIME = re.compile(r"\b\d{1,2}[:.]\d{2}\b|\b(raat|subah|shaam|dopehar|night|morning|evening|afternoon|kal|parson|"
                   r"yesterday|today|aaj|baje|am|pm)\b|رات|صبح|شام|بجے", re.IGNORECASE)
_PLATE = re.compile(r"\b(?!FIR\b|PS\b|PPC\b|CNIC\b|SIM\b)[A-Z]{2,4}-?\s?\d{2,4}(?!\s*/)\b")          # number plates are written in capitals
_VEHICLE_WORDS = re.compile(r"\b(car|bike|motorcycle|honda|suzuki|toyota|corolla|mehran|rickshaw|pistol|gun|"
                            r"kalashnikov|klashnikov|knife|chhuri|weapon|hathiyar)\b|گاڑی|موٹر سائیکل|پستول", re.IGNORECASE)


class _Vehicle:
    @staticmethod
    def search(text: str) -> re.Match[str] | None:
        return _PLATE.search(text) or _VEHICLE_WORDS.search(text)


_VEHICLE = _Vehicle()
_RELATION = re.compile(r"\b(brother|bhai|cousin|family|relative|rishtedar|friend|dost|partner|business|karobar|"
                       r"neighbour|parosi|gang|saathi|co-?accused)\b|بھائی|رشتہ|دوست|کاروبار|ساتھی", re.IGNORECASE)


def _fit_score(q: dict[str, Any], message: str, net: PersonNetwork) -> int:
    """How well ``message`` answers question ``q``: 3 clearly, 2 likely, 1 possibly, 0 no."""
    text = message.strip()
    key = q["key"]
    person = _named(net, text)
    if q["action"] == "choose_person" or key.startswith(("roles:", "cdr_owner:")):
        if person is None:
            return 2 if re.search(r"\b(other|someone else|koi aur)\b|کوئی اور", text, re.IGNORECASE) else 0
        if key == "roles:main" and re.search(r"suspect|mulzim|accused|main|asal|ملزم", text, re.IGNORECASE):
            return 3
        return 2 if len(text.split()) <= 6 else 1
    if key == "incident:when":
        return 3 if _iso_date(text) else (2 if _TIME.search(text) else 0)
    if key == "incident:what":
        return 3 if _CRIME.search(text) else 0
    if key == "incident:fir":
        return 3 if re.search(r"\d{1,5}\s*/\s*\d{2,4}", text) else 0
    if key == "incident:vehicle":
        return 3 if _VEHICLE.search(text) else 0
    if key == "suspects:other":
        return 3 if re.search(r"\d{5}-?\d{7}-?\d|(?:\+?92|0)3\d{2}[\s-]?\d{7}", text) else 0
    if key == "link:why":
        return 2 if _RELATION.search(text) else 0
    if key in ("incident:place", "incident:pin"):
        if _PLACE.search(text):
            return 3
        # Anything that reads as a crime, a person, a date or a vehicle is not a place.
        if _CRIME.search(text) or person or _iso_date(text) or _VEHICLE.search(text):
            return 0
        return 1
    return 1


def _incident_details(case: CaseFile, message: str, skip: str | None = None) -> list[str]:
    """Details of the incident mentioned in passing ("he is suspect in a murder case", "bike
    KDE-1234 thi"): fill the empty parts of the incident, so the Questioner does not ask
    for what the officer already said."""
    inc = case.incident or {}
    done: list[str] = []
    crime = _CRIME.search(message)
    if crime and not inc.get("offence") and skip != "incident:what":
        sentence = next((p for p in re.split(r"[.!؟?\n]", message) if _CRIME.search(p)), message).strip()
        case.set_incident({"offence": sentence[:200]})
        done.append(f"offence: {crime.group(0)}")
    vehicle = _VEHICLE.search(message)
    if vehicle and not inc.get("vehicle_or_weapon") and skip != "incident:vehicle":
        case.set_incident({"vehicle_or_weapon": message.strip()[:200]})
        done.append(f"vehicle / weapon: {vehicle.group(0)}")
    if done:
        case.officer_note(message, source="chat")
    return done


def _answering(case: CaseFile, net: PersonNetwork, message: str) -> dict[str, Any] | None:
    """The open question ``message`` answers best - by what the answer is, not by which was
    asked last. "Don't know" answers the question asked last."""
    open_qs = [q for q in case.open_questions() if q.get("times", 0) >= 1]
    if not open_qs:
        return None
    asked_order = [k for t in reversed(case.conversation[-6:]) if (k := (t.get("checks") or {}).get("asked"))]
    rank = {k: i for i, k in enumerate(dict.fromkeys(asked_order))}
    if _NO.match(message.strip()):
        return min(open_qs, key=lambda q: rank.get(q["key"], 99))
    scored = [(_fit_score(q, message, net), -rank.get(q["key"], 99), q) for q in open_qs]
    score, _, best = max(scored, key=lambda x: (x[0], x[1]))
    last = asked_order[0] if asked_order else None
    if score >= 2 or (score == 1 and best["key"] == last):
        return best
    return None


def _take_answer(case: CaseFile, net: PersonNetwork, message: str, q: dict[str, Any]) -> list[str]:
    """The officer answered question ``q`` (the one just asked). Remember the answer and put
    it where it belongs on the board."""
    key, text = q["key"], message.strip()
    done = [f"answer to \"{q['text'][:60]}…\": {text[:120]}"]
    if _NO.match(text):
        # "Don't know" is an answer too: the question is closed, not asked again.
        case.answer(q["id"], "not known")
        case.officer_note(f"Answer to \"{q['text']}\": not known", source="answer")
        return [f"not known: {q['text'][:60]}"]
    if q["action"] == "choose_person" or key.startswith(("roles:", "cdr_owner:")):
        pid = _named(net, text)
        if pid and key == "roles:main":
            case.set_role(pid, "main suspect")
            done.append(f"role: {net.name(pid)} = main suspect")
        elif pid and key == "roles:victim":
            case.set_role(pid, "victim")
            done.append(f"role: {net.name(pid)} = victim")
        elif pid and key.startswith("cdr_owner:"):
            doc = case.documents.get(key.split(":", 1)[1])
            if doc is not None and pid not in doc.setdefault("owners", []):
                doc["owners"].append(pid)
            done.append(f"{doc['title'] if doc else 'the CDR'} belongs to {net.name(pid)}")
    elif key == "incident:place":
        day = _iso_date(text)
        case.set_incident({"place": text[:160], **({"date": day} if day else {})})
        done.append(f"incident place: {text[:80]}")
        if day:
            case.close_question("incident:when", day)
    elif key == "incident:when":
        day = _iso_date(text)
        tm = re.search(r"\b(\d{1,2}:\d{2})\b", text)
        case.set_incident({"date": day or text[:40], **({"time": tm.group(1)} if tm else {})})
        done.append(f"incident date: {day or text[:40]}")
    elif key == "incident:what":
        case.set_incident({"offence": text[:200]})
        done.append(f"offence: {text[:80]}")
    elif key == "incident:fir":
        m = re.search(r"(\d{1,5})\s*/\s*(\d{2,4})", text)
        case.set_incident({"fir": f"{m.group(1)}/{m.group(2)}" if m else text[:60], "police_station": text[:120]})
        done.append(f"FIR: {m.group(0) if m else text[:60]}")
    elif key == "incident:vehicle":
        case.set_incident({"vehicle_or_weapon": text[:200]})
        done.append(f"vehicle / weapon: {text[:80]}")
    elif key == "suspects:other":
        from sherlocks.linkgraph.normalize import cnic13, mobile11

        ids = [x for x in (cnic13(m) for m in re.findall(r"\d{5}-?\d{7}-?\d", text)) if x]
        ids += [x for x in (mobile11(m) for m in re.findall(r"(?:\+?92|0)3\d{2}[\s-]?\d{7}", text)) if x]
        case.dialog.setdefault("to_search", [])
        case.dialog["to_search"] = list(dict.fromkeys(case.dialog["to_search"] + ids))
        if ids:
            done.append(f"to search: {', '.join(ids)} - start a search with them to add them to the graph")
    case.answer(q["id"], text)
    case.officer_note(f"Answer to \"{q['text']}\": {text}", source="answer")
    return done


class _KnowledgeAnswer(BaseModel):
    answer: str = Field(description="The reply to the officer, in the officer's language, citing sources in [brackets].")
    needs_deeper_investigation: bool = Field(
        default=False, description="True only if the knowledge given cannot answer and the investigation tools "
                                   "(routes, live lookups, fetching documents) are needed.")


_KNOWLEDGE_SYSTEM = (
    "You are Sherlock, an experienced police investigator chatting with a fellow officer about a case. You are given "
    "KNOWLEDGE: entries from everything the investigation has gathered - the people and every record about them, "
    "FIRs (complainant, accused, witnesses, sections, place, time, case positions, case diaries, results), lab / "
    "medical reports, CRO dossiers, uploaded files, links between people, and what the officer has said. Answer the "
    "officer's question - whatever it is - from that knowledge, like a colleague would: directly, naturally, in "
    "{lang}. Use exact numbers, names, dates and FIR numbers from the knowledge; put the source of each claim in "
    "[brackets] as given (e.g. [CRO], [D3], [F7]). If COMPUTED is given, its counts and lists are exact - use them. "
    "Speak of crimes by name (murder, attempted murder, rape, kidnapping, robbery, cheque bounce...), not only by "
    "section numbers, and always say the person's role in each FIR: never present an FIR where he is the "
    "complainant, victim or witness as a charge against him. Open with the direct answer in one or two sentences, "
    "then the supporting details. "
    "Never invent anything. If the knowledge does not contain the answer, say what you do know and what is missing, "
    "and set needs_deeper_investigation only when routes between people, live lookups or new documents would answer it."
)


def answer_from_knowledge(llm: Any, question: str, language: str, hits: list[Any], facts: dict[str, Any] | None,
                          history: list[dict[str, Any]], case: CaseFile, net: PersonNetwork) -> tuple[str | None, bool]:
    from sherlocks.linkgraph.conversation import LANG_NAME, render_facts

    payload = {
        "question": question,
        "knowledge": [e.line()[:400] for e in hits],
        "computed": None if facts is None else {k: v for k, v in facts.items() if k not in ("rows", "rest")},
        # The computed answer already in words: crime types, the person's role in each FIR.
        "computed_in_words": None if facts is None else render_facts(facts, language),
        "case": {"targets": [net.name(p) for p in net.seeds()], "people_on_graph": len(net.people),
                 "documents": len(case.documents), "incident": case.incident,
                 "roles": {net.name(p): r for p, r in case.roles.items() if p in net.people}},
        "recent_conversation": [{"officer": t.get("q", "")[:300], "sherlock": t.get("a", "")[:400]} for t in history[-4:]],
    }
    try:
        out, _ = llm.generate_structured(prompt=json.dumps(payload, ensure_ascii=False, default=str), schema=_KnowledgeAnswer,
                                         system=_KNOWLEDGE_SYSTEM.format(lang=LANG_NAME[language]),
                                         cache_kind="knowledge_answer", prompt_version="v2", max_tokens=1500)
        return (out.answer.strip() or None), bool(out.needs_deeper_investigation)
    except Exception as exc:  # noqa: BLE001 - the computed / knowledge answer below still works
        logger.info("Knowledge answer failed: %s", exc)
        return None, False


def run_turn(graph: dict[str, Any], question: str, *, llm: Any = None, history: list[dict] | None = None,
             case: CaseFile | None = None, agents: Any = None, backend: Any = None,
             live_calls: int = 0) -> Iterator[dict[str, Any]]:
    """One chat turn, as planned in docs/SHERLOCK_CONVERSATION_PLAN.md:

    Conversation agent (language, dialogue state) -> Fact collector -> Understanding agent
    -> Facts agent (exact answers) or Investigator (open questions) -> Conversation agent
    (reply) -> Validator; the Briefing agent and the Questioner speak only when useful."""
    from sherlocks.linkgraph import case_queries
    from sherlocks.linkgraph.conversation import (
        NEWS_LINE,
        briefing,
        case_memory,
        compose,
        detect_language,
        route,
    )
    from sherlocks.linkgraph.understanding import understand

    case = case if case is not None else CaseFile()
    net = PersonNetwork(graph)
    message = (question or "").strip()
    language = detect_language(message)
    intent = route(message, case.open_questions()) if message else "greeting"
    focus = [p for p in case.dialog.get("focus") or [] if p in net.people]
    yield {"type": "start", "question": message, "people": len(net.people), "targets": [net.name(p) for p in net.seeds()],
           "documents": len(case.documents), "language": language, "intent": intent,
           "model": getattr(llm, "model", None) if llm else None, "findings": 0, "live_calls": live_calls}
    lang_name = {"ur": "Urdu", "roman": "Roman Urdu", "en": "English"}[language]
    yield _agent("Conversation agent", f"{lang_name}; discussing {', '.join(net.name(p) for p in focus) or 'the case'}")

    # Fact collector: what the officer states.
    # Which of my recent questions does a non-question answer? The latest still open whose
    # kind of answer it fits (a date for "when", a person for "who"...).
    recorded = collect(case, net, message, llm, focus=focus) if message else []
    pending = _answering(case, net, message) if intent in ("statement", "answer") else None
    if intent != "question" and message:
        recorded += _incident_details(case, message, skip=pending["key"] if pending else None)
    if pending is not None:
        intent = "answer"
        recorded += _take_answer(case, net, message, pending)
    elif intent == "answer":
        intent = "statement"
    if recorded:
        yield _agent("Fact collector", "; ".join(recorded), recorded=recorded)

    history = history or [{"q": t["q"], "a": t["a"]} for t in case.conversation[-4:]]
    query: dict[str, Any] | None = None
    facts: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    news_all, seen_now = briefing(case, case.seen)
    knowledge_answer: str | None = None
    researched: list[str] = []
    if intent == "question":
        from sherlocks.linkgraph.knowledge import knowledge

        query = understand(message, net, focus, llm)
        names = ", ".join(net.name(p) for p in query["people"]) or "the whole case"
        yield _agent("Understanding agent", f"{query['type'].replace('_', ' ')} - about {names}")
        # 1. Everything known that bears on the question (the live knowledge base).
        kb = knowledge(graph, case, net)
        hits = kb.search(message, people=query["people"], k=30)
        yield _agent("Knowledge base", f"{len(hits)} relevant entr(ies) of {len(kb.entries)}")
        # 2. An exact computed answer, when the question is one the Facts agent knows.
        if query["type"] == "news":
            facts = {"type": "news", "subject": None, "count": len(news_all),
                     "items": [{"text": line, "source": ""} for line in news_all], "empty": not news_all}
            case.seen = seen_now
        elif query["type"] not in ("open",):
            facts = case_queries.run(query, net, case)
        if facts is not None:
            yield _agent("Facts agent", f"{facts['count']} result(s) for {facts['type'].replace('_', ' ')}")
        # 2b. Research agent: nothing gathered answers it (or an FIR's file is unread) - call the APIs.
        if live_calls > 0 and (agents is not None or backend is not None) and _needs_research(query, facts, hits, case, net):
            from sherlocks.linkgraph.investigator import _Tools
            from sherlocks.linkgraph.researcher import facts_from, research

            tools = _Tools(graph, case, agents, backend, live_calls)
            done = research(tools, query, message, kb.topics(message))
            if done:
                researched = [d["what"] for d in done]
                yield _agent("Research agent", "called: " + ", ".join(researched))
                kb = knowledge(graph, case, net)
                hits = kb.search(message, people=query["people"], k=30)
                if query["type"] not in ("open", "news"):
                    facts = case_queries.run(query, net, case)
                if facts is None or facts.get("empty"):
                    found = facts_from(done, names)
                    if not found["empty"]:
                        facts = found
        # 3. With the model: answer ANY question from the knowledge (and the exact facts);
        #    dig deeper with the investigation tools only when that is not enough.
        if llm is not None:
            knowledge_answer, deeper = answer_from_knowledge(llm, message, language, hits, facts, history, case, net)
            if knowledge_answer:
                yield _agent("Conversation agent", "answered from the knowledge base")
            if deeper or not knowledge_answer:
                ask = message
                if query["people"] and not _mentioned_any(net, message):
                    ask = f"{message} (about {', '.join(net.name(p) for p in query['people'])})"
                for event in investigate(graph, ask, llm=llm, history=history, case=case, agents=agents,
                                         backend=backend, live_calls=live_calls):
                    if event.get("type") == "final":
                        result = event
                        break
                    if event.get("type") != "start":
                        yield event
                if result is not None:
                    yield _agent("Investigator", f"concluded ({len(result.get('hypotheses') or [])} hypothesis(es))")
                    case.set_assessment({"answer": result.get("answer"), "hypotheses": result.get("hypotheses") or [],
                                         "question": message})
                    knowledge_answer = None if deeper else knowledge_answer
        # 4. Without the model: the exact answer if there is one, else what the knowledge holds.
        elif facts is None:
            if hits:
                facts = {"type": "knowledge", "subject": names, "count": len(hits[:10]), "empty": False,
                         "items": [{"text": e.text if e.title in e.text else f"{e.title}: {e.text}",
                                    "source": e.source, "pid": (e.people or [None])[0]} for e in hits[:10]]}
            elif query["people"] and kb.topics(message):
                # A person's job / relatives... that no record holds: say so, don't guess.
                facts = {"type": "knowledge", "subject": names, "count": 0, "items": [], "empty": True,
                         "topic": kb.topics(message)[0]}
            else:
                facts = {"type": "not_understood", "subject": None, "count": 0, "items": [], "empty": True}
        if query["people"]:
            case.dialog["focus"] = query["people"][:2]
        case.dialog["last_type"] = query["type"]
    else:
        from sherlocks.linkgraph.investigator import _mentioned

        named = _mentioned(net, message, sound=False) or _mentioned(net, message)
        if named:
            case.dialog["focus"] = named[:2]
            if intent == "statement":
                # Relate what the officer said to what the records hold on that person - briefly.
                facts = case_queries.cases(net, named[0], case)
                facts["brief"] = True

    # Briefing agent: details only when they concern the person discussed; otherwise one line, once.
    focus_names = [net.name(p) for p in case.dialog.get("focus") or [] if p in net.people]
    relevant: list[str] = []
    news_line = None
    if not (query and query["type"] == "news"):
        told = set(case.dialog.get("told") or [])
        relevant = [line for line in news_all if any(n and n in line for n in focus_names)
                    and not set(re.findall(r"\[([DFL]\d+)\]", line)) & told][:2]
        case.dialog["told"] = sorted(told | {i for line in relevant for i in re.findall(r"\[([DFL]\d+)\]", line)})
        if len(news_all) > len(relevant) and len(news_all) != case.dialog.get("announced"):
            news_line = NEWS_LINE[language].format(n=len(news_all))
            case.dialog["announced"] = len(news_all)
        if relevant:
            yield _agent("Briefing agent", f"{len(relevant)} new finding(s) about {', '.join(focus_names)}")

    # Questioner: one question per reply, throughout the chat. Answered questions never come
    # back; one left unanswered waits while the officer asks something else, and is asked at
    # most twice in all.
    from sherlocks.evidence.questioner import next_question

    ask_gaps(case, graph)
    recent = {(t.get("checks") or {}).get("asked") for t in case.conversation[-3:]}
    # A question asked in the last reply and not answered yet: let the officer answer it first.
    last_asked = (case.conversation[-1].get("checks") or {}).get("asked") if case.conversation else None
    waiting = last_asked in {q["key"] for q in case.open_questions()} and intent == "question"
    to_ask = None if waiting else next_question(case, recent=recent)
    if to_ask and intent == "question" and to_ask.get("times", 0) >= 1:
        to_ask = None     # a question already asked once comes back only when the officer is telling, not asking
    if to_ask:
        yield _agent("Questioner", to_ask["text"])

    if researched:
        from sherlocks.linkgraph.conversation import RESEARCH_LINE

        news_line = " ".join(x for x in (RESEARCH_LINE[language].format(x=", ".join(researched)), news_line) if x)
    if knowledge_answer:
        reply, writer = knowledge_answer, "model"
        extras = [x for x in (("\n".join(["", *relevant]) if relevant else ""), news_line or "") if x]
        if to_ask:
            from sherlocks.linkgraph.conversation import QUESTIONS

            extras.append(QUESTIONS.get(to_ask["key"], {}).get(language) or to_ask["text"])
        if extras:
            reply = reply + "\n\n" + "\n\n".join(x.strip() for x in extras)
    else:
        reply, writer = compose(language=language, intent=intent, message=message, recorded=recorded, result=result,
                                news=relevant, question=to_ask, case_summary=_case_summary(case, net), history=history,
                                llm=llm, memory=case_memory(case, graph, net) if llm is not None else None,
                                facts=facts, news_line=news_line)
    yield _agent("Conversation agent", "reply written by the model" if writer == "model" else "reply from templates (no model)")

    final = dict(result or {"type": "final", "hypotheses": [], "key_people": [], "next_steps": [], "suggestions": [],
                            "findings": [], "confident": True, "model": getattr(llm, "model", None) if llm else None})
    final.update({"type": "final", "answer": reply, "language": language, "intent": intent, "query": query,
                  "facts": facts, "investigation": (result or {}).get("answer")})
    if facts is not None and not final.get("key_people"):
        final["key_people"] = [{"id": i["pid"], "name": net.name(i["pid"])} for i in facts["items"] if i.get("pid")][:12] \
            or [{"id": p, "name": net.name(p)} for p in (query or {}).get("people") or []]

    # Validator: the numbers must be the Facts agent's; no contradiction with the board.
    check = validate(case, net, final, llm)
    counting = (query or {}).get("type") in ("count_cases", "criminals_near", "criminals_all", "graph_stats", "fir_status")
    if counting and facts is not None and not facts.get("empty") and writer == "model" and str(facts["count"]) not in reply:
        check["issues"].append(f"The reply does not give the exact count ({facts['count']}).")
        check["ok"] = False
    if not check["ok"] and llm is not None:
        fixed = None
        if facts is not None:
            fixed, _ = compose(language=language, intent=intent, message=message, recorded=recorded, result=result,
                               news=relevant, question=to_ask, case_summary="", history=[], llm=None, facts=facts,
                               news_line=news_line)
        else:
            fixed = revise(final, check["issues"], llm)
        if fixed:
            final["answer"] = fixed
            yield _agent("Validator", "sent the reply back once: " + "; ".join(check["issues"]))
            check = {**validate(case, net, final, None), "revised": True, "first_issues": check["issues"]}
    yield _agent("Validator", "checked: " + ("consistent with the records and your statements" if check["ok"]
                                             else "doubts remain - " + "; ".join(check["issues"])), check=check)
    check["asked"] = to_ask["key"] if to_ask else None
    if to_ask:
        to_ask["times"] = to_ask.get("times", 0) + 1
    check["intent"] = intent
    final["checks"] = check
    final["questions"] = case.open_questions()
    final["asking"] = to_ask
    final["citations"] = case.citations_in(" ".join([final.get("answer") or ""] + [
        e for h in final.get("hypotheses") or [] for e in h.get("evidence") or []]))
    case.add_turn(message, final["answer"], check)
    yield final


def _mentioned_any(net: PersonNetwork, message: str) -> bool:
    from sherlocks.linkgraph.investigator import _mentioned

    return bool(_mentioned(net, message))
