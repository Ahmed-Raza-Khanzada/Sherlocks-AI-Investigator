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
from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.evidence.question_desk import wants_mute
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


_MONTH_NUM = {m: i for i, names in enumerate(
    [("jan", "january", "جنوری"), ("feb", "february", "فروری"), ("mar", "march", "مارچ"), ("apr", "april", "اپریل"),
     ("may", "مئی"), ("jun", "june", "جون"), ("jul", "july", "جولائی"), ("aug", "august", "اگست"),
     ("sep", "sept", "september", "ستمبر"), ("oct", "october", "اکتوبر"), ("nov", "november", "نومبر"),
     ("dec", "december", "دسمبر")], 1) for m in names}
_NAMED_DATE = re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s+([a-z]+|[؀-ۿ]+),?\s+(\d{4})\b", re.IGNORECASE)


def _iso_date(text: str) -> str | None:
    named = _NAMED_DATE.search(text)
    if named and named.group(2).lower() in _MONTH_NUM:
        try:
            from datetime import date

            return date(int(named.group(3)), _MONTH_NUM[named.group(2).lower()], int(named.group(1))).isoformat()
        except ValueError:
            pass
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
            focus: list[str] | None = None, skip_topics: set[str] | None = None) -> list[str]:
    """Record the officer's statements. Returns one line per thing recorded. A topic in
    ``skip_topics`` was just corrected (or waits for the officer to confirm a change), and
    a value already on file is never overwritten here - changing it is a correction."""
    skip_topics = skip_topics or set()
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
    if (not is_question and _SUSPECT.search(text) and not any(st.kind == "role" for st in statements)
            and "role:*" not in skip_topics):
        main = "main suspect" not in case.roles.values()
        statements.append(_Statement(fact=text, quote=text, kind="role", role="main suspect" if main else "suspect"))
    place = _incident_place(text)
    if place and not is_question and not (case.incident or {}).get("place") and "incident:place" not in skip_topics:
        case.set_incident({"place": place})
        case.close_question("incident:place", place)
        done.append(f"incident place: {place}")
    for st in statements:
        pid = ((net.resolve(st.person) if st.person else None) or _named(net, text) or (focus or [None])[0]
               if st.kind == "role" else None)
        topic = {"incident_time": "incident:date", "incident_place": "incident:place"}.get(st.kind) or (
            f"role:{pid}" if pid else None)
        if topic in skip_topics or (st.kind == "role" and "role:*" in skip_topics):
            continue
        fid = case.add_fact(doc_id, f"Stated by the officer: {st.fact}", st.quote, kind=f"officer_{st.kind}",
                            people=[st.person] if st.person else [], by="officer", topic=topic)
        if fid is None:
            continue
        done.append(f"recorded {fid}: {st.fact}")
        if st.kind == "role":
            if pid and not (case.roles.get(pid) == "main suspect" and (st.role or "suspect").lower() == "suspect"):
                case.set_role(pid, (st.role or "suspect").lower())
                if (st.role or "").lower() == "main suspect":
                    case.close_question("roles:main", net.name(pid))
                done.append(f"role: {net.name(pid)} = {st.role or 'suspect'}")
        if st.kind == "incident_time":
            day = _iso_date(st.quote) or _iso_date(text)
            if day and not (case.incident or {}).get("date"):
                case.set_incident({"date": day})
                case.close_question("incident:when", day)
                done.append(f"incident date: {day}")
    return done


_SUSPECT = re.compile(r"(?<![\w؀-ۿ])(shak|shaq|shuba|shubah|شک|شبہ)(?![\w؀-ۿ])", re.IGNORECASE)
_INCIDENT_WORD = (r"(?:wardaa*t|wardat|wardqaa*t|waqia|waqiya|waqa|vaqia|incident|crime|occurrence|jurm|dakait\w*|daka|"
                  r"dacoity|robbery|snatching|chheena\w*|qatl|murder|chori|theft|aghwa|kidnapping|firing|fire|"
                  r"واردات|واقعہ|جرم|ڈکیتی|ڈاکہ|قتل|چوری|اغوا|فائرنگ)")
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


_COUNTING = ("count_cases", "criminals_near", "criminals_all", "graph_stats", "fir_status")
_GAP_WORDS = re.compile(r"nothing|no record|not (?:found|known|recorded|in the records|been read)|none|could not|"
                        r"did not find|do(?:es)? not (?:give|say|show|hold|have|mention)|don'?t (?:give|say|have)|"
                        r"nahi mil|kuch nahi|koi nahi|record nahi|maloom nahi|نہیں ملا|کچھ نہیں|کوئی نہیں|ریکارڈ میں نہیں",
                        re.IGNORECASE)
_QUOTED = re.compile(r"[\"“«]([^\"”»]{6,300})[\"”»][^\[]{0,60}\[([DF]\d{1,4})\]")


def unverified_quotes(answer: str, case: CaseFile) -> list[str]:
    """Quoted text in the answer that is not in the document it cites - made up, or
    carried in from somewhere else. The Answer checker removes it."""
    from sherlocks.evidence.case_file import quote_in

    bad = []
    for m in _QUOTED.finditer(answer or ""):
        quote, ref = m.group(1), m.group(2)
        doc = case.documents.get(ref) if ref.startswith("D") else case.documents.get((case.facts.get(ref) or {}).get("doc", ""))
        fact = case.facts.get(ref)
        ok = doc is not None and quote_in(quote, doc.get("text", ""))
        ok = ok or (fact is not None and quote_in(quote, fact.get("quote", "")))
        if not ok:
            bad.append(quote)
    return bad


def drop_quotes(answer: str, quotes: list[str]) -> str:
    for q in quotes:
        answer = re.sub(r"[\"“«]" + re.escape(q) + r"[\"”»]", "(quote removed: not found in the source)", answer)
    return answer


def _first_part(answer: str) -> str:
    """The opening of a reply: its first sentence or line (what must answer the question)."""
    text = re.sub(r"\*\*", "", (answer or "").strip())
    first = re.split(r"(?<=[.!?؟۔])\s|\n", text, maxsplit=1)[0]
    return first[:300]


def validate(case: CaseFile, net: PersonNetwork, final: dict[str, Any], llm: Any = None) -> dict[str, Any]:
    """The Answer checker: ``{ok, issues, checked, rules}`` for a draft answer.

    Code first - answered what was asked, correct numbers, real and current sources,
    quotes that are in their documents, honest about gaps, the question after the answer,
    no contradiction of the officer's statements. The model is asked only about
    contradictions in claims no query computed (``llm`` given)."""
    answer = final.get("answer") or ""
    issues: list[str] = []
    rules = {"answered": True, "correct": True, "sourced": True, "consistent": True, "honest": True, "labelled": True}
    known = case.known_ids()
    bad = [ref for ref in dict.fromkeys(CITATION.findall(answer)) if ref not in known]
    if bad:
        issues.append(f"Cites {', '.join(bad)}, which is not in the case file.")
        rules["sourced"] = False
    old = [ref for ref in dict.fromkeys(CITATION.findall(answer)) if ref in case.facts and not case.usable(ref)]
    if old:
        issues.append(f"Cites {', '.join(old)}, replaced or out of date since a correction.")
        rules["sourced"] = False
    quotes = unverified_quotes(answer, case)
    if quotes:
        issues.append(f"Quotes text not found in its source: \"{quotes[0][:60]}\".")
        rules["sourced"] = False
    query, facts = final.get("query") or {}, final.get("facts")
    if facts is not None and facts.get("asked_role"):
        # Asked about one role ("FIR on him" = accused): yes or no for that role, first.
        opening = _first_part(answer).lower()
        said_yes = bool(re.match(r"^\W*(yes|haan|han|ji haan|جی ہاں|ہاں)\b", opening))
        said_no = bool(re.match(r"^\W*(no|nahi|nahin|نہیں)\b", opening)) or bool(re.search(
            r"\bno fir\b|\bnot (?:the )?" + re.escape(facts["asked_role"]) + r"|kisi fir|کسی ایف آئی آر", opening))
        if not facts.get("matching") and said_yes:
            issues.append(f"Says yes, but no FIR names {facts.get('subject')} as {facts['asked_role']}.")
            rules["answered"] = rules["correct"] = False
        elif facts.get("matching") and said_no:
            issues.append(f"Says no, but {facts.get('subject')} is {facts['asked_role']} in "
                          f"{len(facts['matching'])} FIR(s).")
            rules["answered"] = rules["correct"] = False
    elif (query.get("type") in _COUNTING and facts is not None and not facts.get("empty")
            and str(facts.get("count")) not in _first_part(answer)):
        issues.append(f"The reply does not open with the exact count ({facts['count']}).")
        rules["answered"] = rules["correct"] = False
    asking = (final.get("asking") or {}).get("asked_text")
    if asking and answer.strip()[:60] and asking.strip()[:40] in answer.strip()[:len(asking) + 5] and len(answer) > len(asking) + 20:
        issues.append("The reply opens with a question instead of the answer.")
        rules["answered"] = False
    if (final.get("intent") == "question" and facts is not None and facts.get("empty") and not final.get("investigation")
            and not _GAP_WORDS.search(answer)):
        issues.append("The records hold nothing on this, but the reply does not say so.")
        rules["honest"] = False
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
                rules["consistent"] = False
                break
    for h in final.get("hypotheses") or []:
        if h.get("tier") == "speculative" and h.get("statement") and h["statement"][:60] in answer:
            window = answer[max(0, answer.find(h["statement"][:60]) - 80):answer.find(h["statement"][:60])].lower()
            if not re.search(r"suspicion|speculat|not confirmed|andaza|tasdeeq|اندازہ|lead|view", window):
                issues.append("A speculative point is stated as fact - label it as Sherlock's view.")
                rules["labelled"] = False
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
            found = [i for i in check.issues[:4] if i.strip()]
            issues += found
            if found:
                rules["consistent"] = False
            checked = "rules + model"
        except Exception as exc:  # noqa: BLE001
            logger.info("Validator model failed: %s", exc)
    return {"ok": not issues, "issues": issues, "checked": checked, "rules": rules}


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


FOLLOW_HEAD = {"en": "More on your last question:", "roman": "Aap ke pichle sawal par mazeed:",
               "ur": "آپ کے پچھلے سوال پر مزید:"}
STILL_WORKING = {"en": "(Sherlock is still looking into part of this - I will send it as soon as it is ready.)",
                 "roman": "(Sherlock abhi is ke kuch hisse par kaam kar rahe hain - tayyar hote hi bhej dunga.)",
                 "ur": "(شرلاک ابھی اس کے کچھ حصے پر کام کر رہے ہیں - تیار ہوتے ہی بھیج دوں گا۔)"}


def QUESTIONS_TEXT(q: dict[str, Any], language: str) -> str:
    from sherlocks.linkgraph.conversation import QUESTIONS

    return QUESTIONS.get(q["key"], {}).get(language) or q["text"]


def _needs_research(query: dict[str, Any], facts: dict[str, Any] | None, hits: list[Any], case: CaseFile,
                    net: PersonNetwork) -> bool:
    """Nothing gathered answers the question, or it is about an FIR whose file is unread."""
    from sherlocks.linkgraph.case_queries import _firs, fir_document

    if query.get("firs") and any(fir_document(case, f) is None for f in query["firs"]):
        return True
    if query["type"] in ("fir_details", "fir_status", "io_report") and query["people"]:
        return any(not r.get("doc") for r in _firs(net, query["people"][0], case))
    if query["type"] == "hotels" and facts is not None and any(not m["day"] and not m.get("doc")
                                                                for m in facts.get("at_times") or []):
        return True                     # an FIR's date is only in its unread file

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
_MONTHS = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
           r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|جنوری|فروری|مارچ|اپریل|مئی|جون|جولائی|اگست|ستمبر|اکتوبر|نومبر|دسمبر")
_MONTH_DAY = re.compile(rf"\b\d{{1,2}}\s*(?:st|nd|rd|th)?\s*(?:{_MONTHS})\b|\b(?:{_MONTHS})\s+\d{{1,2}}\b", re.IGNORECASE)
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
        return 3 if (_iso_date(text) or _MONTH_DAY.search(text)) else (2 if _TIME.search(text) else 0)
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
    if key.startswith("confirm_lookup:"):
        return 3 if re.match(r"^\s*(yes|y|no|haan|han|ji|jee|nahi|nahin|ok|okay|theek|ہاں|جی|نہیں)\b", text,
                             re.IGNORECASE) else 0
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
    if _NO.match(text) and not key.startswith("confirm_lookup:"):
        # "Don't know" is an answer too: the question is closed, not asked again.
        case.answer(q["id"], "not known", status="dont_know")
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
    elif key.startswith("confirm_lookup:cdr:"):
        doc_id = key.split(":", 2)[2]
        yes = bool(re.match(r"^\s*(yes|y|haan|han|ji|jee|ok|okay|theek|kar ?do|کر دیں|ہاں|جی)\b", text, re.IGNORECASE))
        bucket = "confirmed_docs" if yes else "declined_docs"
        case.dialog[bucket] = list(dict.fromkeys([*(case.dialog.get(bucket) or []), doc_id]))
        if yes:
            case.plan(f"cdr:enrich:{doc_id}", "the officer agreed to look up the top contacts")
        done.append(f"top contacts of {doc_id}: {'will be looked up' if yes else 'not to be looked up'}")
    elif key.startswith("confirm_lookup:"):
        ident = key.split(":", 1)[1]
        yes = bool(re.match(r"^\s*(yes|y|haan|han|ji|jee|ok|okay|theek|kar ?do|کر دیں|ہاں|جی)\b", text, re.IGNORECASE))
        bucket = "confirmed_ids" if yes else "declined_ids"
        case.dialog[bucket] = list(dict.fromkeys([*(case.dialog.get(bucket) or []), ident]))
        case.dialog["unconfirmed_ids"] = [i for i in case.dialog.get("unconfirmed_ids") or [] if i != ident]
        done.append(f"{ident}: {'may be looked up' if yes else 'not to be looked up'}")
    elif key == "suspects:other":
        from sherlocks.linkgraph.normalize import cnic13, mobile11

        ids = [x for x in (cnic13(m) for m in re.findall(r"\d{5}-?\d{7}-?\d", text)) if x]
        ids += [x for x in (mobile11(m) for m in re.findall(r"(?:\+?92|0)3\d{2}[\s-]?\d{7}", text)) if x]
        case.dialog.setdefault("to_search", [])
        case.dialog["to_search"] = list(dict.fromkeys(case.dialog["to_search"] + ids))
        if ids:
            done.append(f"to search: {', '.join(ids)} - start a search with them to add them to the graph")
    case.answer(q["id"], text)
    note_id, line = case.officer_note(f"Answer to \"{q['text']}\": {text}", source="answer")
    topic = {"incident:when": "incident:date", "incident:place": "incident:place", "incident:what": "incident:offence",
             "incident:fir": "incident:fir", "incident:vehicle": "incident:vehicle"}.get(key)
    if key.startswith(("roles:", "cdr_owner:")):
        pid = _named(net, text)
        topic = (f"role:{pid}" if key.startswith("roles:") else f"owner:{key.split(':', 1)[1]}") if pid else None
    if topic:
        case.add_fact(note_id, f"Stated by the officer: {q['text']} - {text}", line, kind="officer_answer", by="officer",
                      topic=topic, rests_on=[q["id"]])
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
    "complainant, victim or witness as a charge against him. Lay the reply out for quick reading: the first line is the "
    "direct answer in one sentence; then short points, one fact per line, each line starting with '• ' and ending "
    "with its source in [brackets] (e.g. '• Charges: murder (302), rioting (148) [D1]'); no long paragraphs, no "
    "opening pleasantries. Put in bold (**like this**) the words that directly answer the question - the name, number, "
    "date, role or finding asked for. Do not add a separate evidence list: cite sources in [brackets] and the "
    "evidence is shown with the answer. When the officer asks for details, charges, an explanation or a summary, give them "
    "in full: every FIR with its sections named as crimes, the person's role, date, place, complainant, what the FIR "
    "and its documents say, and its status; for a summary, the targets, what connects them and what each document "
    "establishes. Never answer such a question with a count alone. "
    "When THE_ASK names asks_for (one specific fact - 'investigating officer', 'date of occurrence', 'witnesses'), "
    "answer exactly that in the first line (e.g. 'The investigating officer is SI Zahid Iqbal [D1]'), add only what "
    "makes it clear, and never answer with the whole case instead. "
    "THE_ASK says what the officer wants: with depth 'detailed', explain fully - for an FIR its crimes in words, his "
    "role, who filed it, when and where, the accused, what the complainant says happened, the evidence and the "
    "status; with depth 'brief', answer in a line or two. Answer about the role in THE_ASK only - do not add 'he is "
    "the complainant, not the accused' unless the officer asked whether he is accused. "
    "Read the role a question asks about: an FIR 'on' / 'against' someone ('par', 'ke khilaf') asks where he is the "
    "ACCUSED; 'did he file' / 'muddai' asks where he is the complainant; 'gawah' the witness. When COMPUTED has "
    "asked_role, answer yes or no for exactly that role first (matching = his FIRs in that role), then name his FIRs "
    "in other roles with his role and their accused - e.g. 'No, there is no FIR against him; he is the complainant "
    "in FIR 121/25, where the accused are ...'. "
    "Never invent anything. If the knowledge does not contain the answer, say what you do know and what is missing, "
    "and set needs_deeper_investigation only when routes between people, live lookups or new documents would answer it. "
    + DATA_NOT_INSTRUCTIONS
)


def answer_from_knowledge(llm: Any, question: str, language: str, hits: list[Any], facts: dict[str, Any] | None,
                          history: list[dict[str, Any]], case: CaseFile, net: PersonNetwork,
                          pack: dict[str, Any] | None = None,
                          ask: dict[str, Any] | None = None) -> tuple[str | None, bool]:
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
    if ask:
        # O0's reading: how much the officer wants, and about which role - answer exactly that.
        payload["the_ask"] = {"wants": ask.get("type"), "asks_for": ask.get("asks_for") or "",
                              "depth": ask.get("depth"), "role": ask.get("role") or "any",
                              "yes_no": bool(ask.get("yes_no")), "about": [net.name(p) for p in ask.get("people") or []]}
    if pack:
        # O4's context pack: the case summary, Sherlock's view, what the officer said (all of
        # the chat, older turns summarised), sources in conflict, and what the board lacks.
        payload["context"] = {k: pack.get(k) for k in ("summary", "sherlock_view", "statements", "chat_older",
                                                       "conflicts", "missing", "focus")}
        if pack.get("dossiers"):
            # A4's card of each person asked about: everything the case holds on them.
            payload["dossiers"] = [{k: v for k, v in card.items() if k != "sig"} for card in pack["dossiers"]]
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
             live_calls: int = 0, officer: dict[str, Any] | None = None,
             on_late: Any = None, settings: Any = None) -> Iterator[dict[str, Any]]:
    """One chat turn, worked by the Officer team (docs/SHERLOCK_AGENTS.md):

    corrections and statements (O3) -> O1 understands -> O4 Case briefer's context pack ->
    the toolbox (board, Facts agent, API router, research) and Sherlock's quick view, in
    parallel -> O1 drafts -> O2 Answer checker -> O5 Presenter; the Question desk adds at
    most one approved question. Every step has a time budget; what finishes late is sent
    as a follow-up (``case.followups``; ``on_late()`` is called so the board is saved)."""
    import time as _time

    from sherlocks.linkgraph import briefer, case_queries, quick_view
    from sherlocks.linkgraph.budget import Budget, CountingLlm
    from sherlocks.linkgraph.conversation import (
        NEWS_LINE,
        briefing,
        compose,
        detect_language,
        status_text,
    )
    from sherlocks.settings import ChatSettings

    case = case if case is not None else CaseFile()
    net = PersonNetwork(graph)
    message = (question or "").strip()
    language = detect_language(message)
    cfg = getattr(settings or getattr(agents, "settings", None), "chat", None) or ChatSettings()
    budget = Budget(cfg.reply_budget_s)
    llm = CountingLlm(llm) if llm is not None else None
    turn_no = len(case.conversation) + 1
    pack: dict[str, Any] | None = None
    view: dict[str, Any] | None = None

    def follow(text: str, kind: str = "followup") -> None:
        """Something finished after the reply went out: a follow-up in the same chat."""
        if text and text.strip():
            case.add_followup(text.strip(), turn=turn_no, kind=kind)
            if on_late is not None:
                on_late()
    # O0 Question reader: what is asked (any wording - the model reads it; rules without one).
    from sherlocks.linkgraph import question_reader

    ask = question_reader.read_message(message, net, case, [p for p in case.dialog.get("focus") or []], llm) \
        if message else {"act": "greeting"}
    intent = ask["act"]
    focus = [p for p in case.dialog.get("focus") or [] if p in net.people]
    yield {"type": "start", "question": message, "people": len(net.people), "targets": [net.name(p) for p in net.seeds()],
           "documents": len(case.documents), "language": language, "intent": intent,
           "model": getattr(llm, "model", None) if llm else None, "findings": 0, "live_calls": live_calls}
    lang_name = {"ur": "Urdu", "roman": "Roman Urdu", "en": "English"}[language]

    def status(key: str, tool: str | None = None, on: str = "") -> dict[str, Any]:
        # The officer's waiting line: what runs next, in their language.
        return {"type": "status", "key": key, "tool": tool, "text": status_text(key, language, tool, on)}

    yield status("start")
    yield _agent("Conversation agent", f"{lang_name}; discussing {', '.join(net.name(p) for p in focus) or 'the case'}")

    # Fact collector: what the officer states.
    # Which of my recent questions does a non-question answer? The latest still open whose
    # kind of answer it fits (a date for "when", a person for "who"...).
    # Corrections first: "nahi, 2 March tha" replaces what was said; it is not new information.
    from sherlocks.linkgraph import corrections as corr_mod

    corrected: list[str] = []
    skip_topics: set[str] = set()
    confirm_ask: str | None = None
    consumed = False
    waiting_fix = case.dialog.get("pending_correction")
    if waiting_fix and message:
        yes = corr_mod.reply_to_pending(message)
        if yes is not None:
            case.dialog.pop("pending_correction", None)
            consumed, intent = True, "answer"
            if yes:
                done = corr_mod.apply(case, net, waiting_fix, language=language)
                corrected.append(done["line"])
                skip_topics.add(waiting_fix["topic"])
                yield _agent("Statement recorder", f"correction confirmed: {waiting_fix['topic']} = {waiting_fix['new']}; "
                                                   f"{len(done['stale'])} entr(ies) marked stale")
    if message and not consumed and intent != "question":
        for fix in corr_mod.detect(case, net, message):
            if fix["sure"]:
                done = corr_mod.apply(case, net, fix, language=language)
                corrected.append(done["line"])
                skip_topics.add(fix["topic"])
                if fix["topic"].startswith("role:"):
                    skip_topics.add("role:*")         # the correction said who is who
                yield _agent("Statement recorder", f"correction: {fix['topic']} {fix['old']} -> {fix['new']}; "
                                                   f"{len(done['stale'])} entr(ies) marked stale, redo: {', '.join(done['redo'])}")
            elif confirm_ask is None:
                case.dialog["pending_correction"] = fix
                confirm_ask = corr_mod.confirm_text(fix, language)
                skip_topics.add(fix["topic"])
    if intent in ("statement", "answer"):
        yield status("record")
    # A question gets the collector's rules only: O1's one model call understands it.
    recorded = (collect(case, net, message, llm if intent != "question" else None, focus=focus, skip_topics=skip_topics)
                if message and not consumed else [])
    pending = (_answering(case, net, message) if intent in ("statement", "answer") and not (corrected or consumed
                                                                                          or confirm_ask) else None)
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

        yield status("understand")
        query = ask if "type" in ask else question_reader.read(message, net, case, focus)
        names = ", ".join(net.name(p) for p in query["people"]) or "the whole case"
        plan = question_reader.plan(query, case, net)
        yield _agent("Question reader", f"{query['depth']} {query['type'].replace('_', ' ')} - about {names}"
                     + (f"; role asked: {query['role']}" if query.get("role") else "")
                     + (" (yes / no)" if query.get("yes_no") else "") + f" [{query.get('by')}]")
        # O1 asks his agents in turn: the Dossier agent (a person), the case board and the
        # graph, then the API agent at run time - and says plainly when none of them has it.
        from sherlocks.linkgraph import officer as chain

        checked: list[str] = []
        cards: list[dict[str, Any]] = []
        if chain.about_person(query):
            plan["sources"].insert(0, "dossier")
        yield _agent("Officer agent", "plan: " + " + ".join(plan["sources"]))
        if chain.about_person(query):
            yield status("dossier", on=names)
            cards = chain.dossier_step(case, net, query)
            if cards:
                checked.append(chain.checked_item("dossier", language, x=names))
                yield _agent("Dossier agent", "; ".join(f"{c['name']}: {len(c.get('firs') or [])} FIR(s), "
                                                        f"{len(c.get('links') or [])} link(s), "
                                                        f"{len(c.get('facts') or [])} fact(s) on the board"
                                                        for c in cards))
        # 1. Everything known that bears on the question (the live knowledge base).
        yield status("board")
        kb = knowledge(graph, case, net)
        hits = kb.search(message, people=query["people"], k=30)
        checked.append(chain.checked_item("board", language, f=len(case.facts), d=len(case.documents)))
        checked.append(chain.checked_item("graph", language, n=len(net.people)))
        yield _agent("Knowledge base", f"{len(hits)} relevant entr(ies) of {len(kb.entries)}")
        # O4 Case briefer: the context pack, cut at one board version.
        yield status("brief")
        started = _time.monotonic()
        pack = briefer.build(case, graph, net, message, people=query["people"], kb=kb, chars=cfg.pack_chars,
                             recent=cfg.recent_turns)
        budget.record("brief", started)
        if cards:
            pack["dossiers"] = cards
        yield _agent("Case briefer", f"context pack at board v{pack['board_version']}: "
                                     f"{len(pack['matched_facts'])} matched fact(s)"
                                     + (f"; missing: {'; '.join(pack['missing'][:2])}" if pack["missing"] else ""))
        # 2. An exact computed answer, when the question is one the Facts agent knows.
        if query["type"] == "news":
            facts = {"type": "news", "subject": None, "count": len(news_all),
                     "items": [{"text": line, "source": ""} for line in news_all], "empty": not news_all}
            case.seen = seen_now
        elif query.get("asks_for") and not (
                found := case_queries.fact_answer(net, case, kb, query["asks_for"], query["people"], query.get("firs"))
        )["empty"]:
            # One fact asked: exactly that, from the case's FIR files - not the whole case.
            yield status("facts")
            facts = found
            from sherlocks.linkgraph.relevance import new_case

            if query.get("this_case") and new_case(case)["known"] and not (case.incident or {}).get("fir"):
                facts["new_case_unknown"] = True    # "this case" is the new one: its FIR is not in yet
        elif query["type"] not in ("open",):
            query["asks_for"] = ""          # not found as one fact: the full answer instead
            yield status("facts")
            facts = case_queries.run(query, net, case)
        if facts is not None:
            yield _agent("Facts agent", f"{facts['count']} result(s) for {facts['type'].replace('_', ' ')}")
        # Sherlock's quick view runs alongside the toolbox: one model call, no tools.
        view_future = None
        if llm is not None and pack is not None:     # Sherlock weighs in on every question
            yield status("view")
            view_started = _time.monotonic()
            view_future = budget.submit(lambda: quick_view.ask(llm, message, language, pack))
        found_nothing_on_board = facts is None or chain.found_nothing(facts, hits)
        if found_nothing_on_board and cards and llm is None and not query.get("topics"):
            # The Dossier agent's card is the answer when the board search had none.
            items = [it for card in cards for it in chain.card_items(card)]
            if items:
                facts = {"type": "knowledge", "subject": names, "count": len(items), "items": items, "empty": False}
                found_nothing_on_board = False
        # 2b. API agent: the board and the graph do not answer it (or an FIR's file is unread)
        #     - the police systems and the FIR files are called now.
        can_call = live_calls > 0 and (agents is not None or backend is not None)
        api_tools: Any = None

        def call_apis(unanswered: bool) -> list[dict[str, Any]]:
            nonlocal api_tools
            from sherlocks.linkgraph.api_router import topics_for
            from sherlocks.linkgraph.investigator import _Tools
            from sherlocks.linkgraph.researcher import facts_from, research

            if api_tools is None:
                api_tools = _Tools(graph, case, agents, backend, live_calls, officer=officer, team="O1 Officer agent",
                                   reason=f"officer asked: {message[:120]}")
            rq = chain.research_query(query, net, unanswered)
            topics = list(query.get("topics") or [])
            if unanswered and not topics_for(topics, rq.get("type")):
                topics = ["identity"]       # nothing asked in particular: who he is, in the systems
            files = plan["fir_files"] or unanswered
            systems = plan["systems"] or (unanswered and bool(rq["people"]))

            def late_research(found: list[dict[str, Any]]) -> None:
                from sherlocks.linkgraph.conversation import render_facts

                useful = [d for d in found or [] if d.get("document") or any(
                    not re.search(r"no record|nothing found|failed|not found", line, re.IGNORECASE)
                    for line in d.get("lines") or [])]
                if useful:                  # a follow-up only when it adds something
                    found = useful
                    follow(FOLLOW_HEAD[language] + "\n" + render_facts(facts_from(found, names), language), "research")

            def fetch() -> list[dict[str, Any]]:
                # One event: what the research brings onto the board wakes the Sherlock team once.
                with case.batch("O1 Officer agent", "research"):
                    return research(api_tools, rq, message, topics, files=files, systems=systems)

            return budget.run("tools", fetch, cfg.tools_budget_s, fallback=[], on_late=late_research) or []

        def take_research(done: list[dict[str, Any]]) -> None:
            nonlocal kb, hits, facts
            from sherlocks.linkgraph.researcher import facts_from

            researched.extend(d["what"] for d in done)
            checked.extend(d["what"] for d in done)
            kb = knowledge(graph, case, net)
            hits = kb.search(message, people=query["people"], k=30)
            if query["type"] not in ("open", "news"):
                facts = case_queries.run(query, net, case)
            if facts is None or facts.get("empty"):
                found = facts_from(done, names)
                if not found["empty"]:
                    facts = found

        if can_call and ((plan["fir_files"] or plan["systems"]) and _needs_research(query, facts, hits, case, net)
                         or found_nothing_on_board):
            yield status("research")
            done = call_apis(found_nothing_on_board)
            if done:
                take_research(done)
                yield _agent("API agent", "called at run time: " + ", ".join(d["what"] for d in done))
            elif "tools" not in budget.late:
                checked.append(chain.checked_item("no_call", language))
        elif found_nothing_on_board and not can_call:
            checked.append(chain.checked_item("offline", language))
        # 3. With the model: answer ANY question from the knowledge (and the exact facts);
        #    dig deeper with the investigation tools only when that is not enough.
        if llm is not None:
            yield status("think")

            def late_draft(out: tuple[str | None, bool]) -> None:
                answer = out[0] if out else None
                if answer and validate(case, net, {"answer": answer})["ok"]:
                    from sherlocks.linkgraph.presenter import present

                    follow(FOLLOW_HEAD[language] + "\n" + present(answer, language=language, case=case), "draft")

            drafted = budget.run("draft", lambda: answer_from_knowledge(llm, message, language, hits, facts, history,
                                                                        case, net, pack=pack, ask=query),
                                 cfg.draft_budget_s, fallback=None, on_late=late_draft)
            knowledge_answer, deeper = drafted if drafted else (None, False)
            if knowledge_answer:
                yield _agent("Conversation agent", "answered from the knowledge base")
            if drafted and (deeper or not knowledge_answer) and can_call and not researched and budget.left() > 2.0:
                # The board did not answer it: the API agent fetches at run time, then O1 redrafts.
                yield status("research")
                done = call_apis(True)
                if done:
                    take_research(done)
                    yield _agent("API agent", "called at run time: " + ", ".join(d["what"] for d in done))
                    yield status("think")
                    redrafted = budget.run("draft", lambda: answer_from_knowledge(
                        llm, message, language, hits, facts, history, case, net, pack=pack, ask=query),
                        cfg.draft_budget_s, fallback=None, on_late=late_draft)
                    if redrafted and redrafted[0]:
                        knowledge_answer, deeper = redrafted
            if drafted and (deeper or not knowledge_answer) and budget.left() > 1.0:
                ask = message
                if query["people"] and not _mentioned_any(net, message):
                    ask = f"{message} (about {', '.join(net.name(p) for p in query['people'])})"

                def late_investigation(final_event: dict[str, Any] | None) -> None:
                    if final_event and final_event.get("answer"):
                        case.set_assessment({"answer": final_event.get("answer"),
                                             "hypotheses": final_event.get("hypotheses") or [], "question": message})
                        from sherlocks.linkgraph.presenter import present

                        follow(FOLLOW_HEAD[language] + "\n" + present(final_event["answer"], language=language, case=case),
                               "investigation")

                for event in budget.stream("investigate",
                                           lambda: investigate(graph, ask, llm=llm, history=history, case=case,
                                                               agents=agents, backend=backend, live_calls=live_calls,
                                                               status=True, officer=officer),
                                           cfg.investigate_budget_s, on_late=late_investigation):
                    if event.get("type") == "final":
                        result = event
                        break
                    if event.get("type") == "status":
                        yield status(event.get("key") or "think", event.get("tool"), event.get("on") or "")
                    elif event.get("type") != "start":
                        yield event
                if result is not None:
                    yield _agent("Investigator", f"concluded ({len(result.get('hypotheses') or [])} hypothesis(es))")
                    case.set_assessment({"answer": result.get("answer"), "hypotheses": result.get("hypotheses") or [],
                                         "question": message})
                    knowledge_answer = None if deeper else knowledge_answer
                elif "investigate" in budget.late:
                    yield _agent("Investigator", "still working - his conclusion will follow")
        # 4. Without the model: the exact answer if there is one, else what the knowledge holds.
        elif facts is None:
            if hits:
                unique = list({(e.title, e.text): e for e in hits}.values())[:10]     # the same entry once
                facts = {"type": "knowledge", "subject": names, "count": len(unique), "empty": False,
                         "items": [{"text": e.text if e.title in e.text else f"{e.title}: {e.text}",
                                    "source": e.source, "pid": (e.people or [None])[0]} for e in unique]}
            elif query["people"] and kb.topics(message):
                # A person's job / relatives... that no record holds: say so, don't guess.
                facts = {"type": "knowledge", "subject": names, "count": 0, "items": [], "empty": True,
                         "topic": kb.topics(message)[0]}
            else:
                facts = {"type": "not_understood", "subject": None, "count": 0, "items": [], "empty": True}
        # 4. None of the agents has it: say so plainly, with what was checked - never a guess.
        late_steps = {"tools", "draft", "investigate"} & set(budget.late)
        if (not knowledge_answer and not (result or {}).get("answer") and not late_steps
                and (facts is None or (facts.get("empty") and facts.get("type") in chain._SEARCHES))):
            from sherlocks.linkgraph.conversation import render_facts

            said = render_facts(facts, language) if facts is not None and facts.get("type") != "not_understood" else ""
            facts = chain.not_found(checked, names, said=said)
            yield _agent("Officer agent", "no agent found an answer - checked: " + chain.checked_line(checked))
        if view_future is not None:
            def late_view(v: dict[str, Any] | None) -> None:
                if v:
                    case.add_view(message, v["view"], turn=turn_no, people=v.get("people"))
                    from sherlocks.linkgraph.presenter import VIEW_LABEL

                    follow(f"{VIEW_LABEL[language]}: {v['view']}", "view")

            view = budget.collect("view", view_future, cfg.view_budget_s, fallback=None, on_late=late_view,
                                  started=view_started)
            if view:
                case.add_view(message, view["view"], turn=turn_no, people=view.get("people"))
                yield _agent("Sherlock", "quick view given, labelled as his view")
            elif "view" in budget.late:
                yield _agent("Sherlock", "still thinking - his view will follow")
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

    # Question desk: the Questioner proposes, the Gatekeeper decides - one question per reply,
    # never one already answered (anywhere in the chat), at most twice, never twice in a row,
    # none while the officer is busy asking about something else.
    from sherlocks.evidence.question_desk import Gatekeeper

    desk = Gatekeeper(case, graph)
    last_asked = (case.conversation[-1].get("checks") or {}).get("asked") if case.conversation else None
    if message and last_asked and wants_mute(message):
        desk.mute(last_asked)
        recorded.append(f"will not ask again: {last_asked}")
    ask_gaps(case, graph)
    to_ask = desk.pick(intent=intent)
    rule_view = None
    if intent == "question" and not to_ask and not (view or {}).get("view"):
        # Every question gets Sherlock's input: his assessment for the new case - or, when
        # he has none yet, the Questioner's most needed question.
        rule_view = quick_view.take(case, net, query, language)
        if rule_view is None:
            to_ask = desk.pick(intent="statement")
    if to_ask:
        yield _agent("Questioner", to_ask["text"])
        yield _agent("Gatekeeper", f"approved {to_ask['key']} (checked again before asking)")

    if researched:
        from sherlocks.linkgraph.conversation import RESEARCH_LINE

        news_line = " ".join(x for x in (RESEARCH_LINE[language].format(x=", ".join(researched)), news_line) if x)
    # Sources that disagree on what is being discussed: shown, never hidden (order of trust).
    from sherlocks.evidence.trust import conflict_lines, conflicts

    about = {net.name(p) for p in (query or {}).get("people") or case.dialog.get("focus") or [] if p in net.people}
    incident_talk = bool(re.search(r"incident|waqi|wardat|occur|date|tareekh|kab|واقعہ|تاریخ", message, re.IGNORECASE))
    shown = [c for c in conflicts(case, net)
             if (c["topic"].startswith("incident") and incident_talk) or c.get("person") in about]
    if shown:
        lines = conflict_lines(shown, language)
        news_line = "\n".join(x for x in (news_line, *lines) if x)
        yield _agent("Answer checker", f"{len(shown)} conflict(s) between sources shown, not hidden")
    if corrected:
        news_line = "\n".join(x for x in (*corrected, news_line) if x)
    if confirm_ask:          # one thing at a time: confirm the change before any other question
        to_ask = None
        news_line = "\n".join(x for x in (news_line, confirm_ask) if x)
    if knowledge_answer:
        reply, writer = knowledge_answer, "model"
        extras = [x for x in (("\n".join(["", *relevant]) if relevant else ""), news_line or "") if x]
        if extras:
            reply = reply + "\n\n" + "\n\n".join(x.strip() for x in extras)
    else:
        yield status("write")
        if llm is not None and pack is None:
            pack = briefer.build(case, graph, net, message, people=case.dialog.get("focus"), chars=cfg.pack_chars,
                                 recent=cfg.recent_turns)
        if any(step in budget.late for step in ("draft", "view", "investigate", "tools")):
            news_line = "\n".join(x for x in (news_line, STILL_WORKING[language]) if x)
        started = _time.monotonic()
        reply, writer = compose(language=language, intent=intent, message=message, recorded=recorded, result=result,
                                news=relevant, question=None, case_summary=_case_summary(case, net), history=history,
                                llm=llm if (budget.left() > 1.0 and "draft" not in budget.late) else None, memory=pack,
                                facts=facts, news_line=news_line)
        budget.record("write", started)
    yield _agent("Conversation agent", "reply written by the model" if writer == "model" else "reply from templates (no model)")

    final = dict(result or {"type": "final", "hypotheses": [], "key_people": [], "next_steps": [], "suggestions": [],
                            "findings": [], "confident": True, "model": getattr(llm, "model", None) if llm else None})
    final.update({"type": "final", "answer": reply, "language": language, "intent": intent, "query": query,
                  "facts": facts, "investigation": (result or {}).get("answer"),
                  "sherlock_view": (view or {}).get("view") or rule_view})
    if facts is not None and not final.get("key_people"):
        final["key_people"] = [{"id": i["pid"], "name": net.name(i["pid"])} for i in facts["items"] if i.get("pid")][:12] \
            or [{"id": p, "name": net.name(p)} for p in (query or {}).get("people") or []]

    # O2 Answer checker: code first; the model only for claims no query computed.
    yield status("check")
    if to_ask:
        final["asking"] = {**to_ask, "asked_text": QUESTIONS_TEXT(to_ask, language)}
    computed = facts is not None and not facts.get("empty") and writer != "model"
    check = budget.run("check", lambda: validate(case, net, final, None if computed else llm), cfg.check_budget_s,
                       fallback=None)
    if check is None:          # the model check ran over: the rules alone decide this time
        check = {**validate(case, net, final, None), "checked": "rules (model check over budget)"}
    if writer != "model" and not check["ok"]:
        # A template reply carries the records as they are: only real source problems count.
        check["issues"] = [i for i in check["issues"] if i.startswith(("Cites", "Quotes"))]
        check["ok"] = not check["issues"]
    if not check["ok"] and llm is not None:
        fixed = None
        if facts is not None:
            fixed, _ = compose(language=language, intent=intent, message=message, recorded=recorded, result=result,
                               news=relevant, question=to_ask, case_summary="", history=[], llm=None, facts=facts,
                               news_line=news_line)
        else:
            fixed = budget.run("revise", lambda: revise(final, check["issues"], llm), cfg.revise_budget_s,
                               fallback=None)
        if fixed:
            final["answer"] = fixed
            yield _agent("Validator", "sent the reply back once: " + "; ".join(check["issues"]))
            check = {**validate(case, net, final, None), "revised": True, "first_issues": check["issues"]}
    bad_quotes = unverified_quotes(final.get("answer") or "", case)
    if bad_quotes:      # never shown as evidence: dropped, whatever else the check found
        final["answer"] = drop_quotes(final["answer"], bad_quotes)
    yield _agent("Validator", "checked: " + ("consistent with the records and your statements" if check["ok"]
                                             else "doubts remain - " + "; ".join(check["issues"])), check=check)
    # O5 Presenter: answer first, layout that fits, evidence quoted, Sherlock's view apart,
    # the question last - and a format guard so nothing changes in transit.
    from sherlocks.linkgraph.presenter import asked_terms, present

    yield status("present")
    ask_line = None
    if to_ask:             # the Questioner's question: always its own last line, in its colour
        from sherlocks.linkgraph.conversation import _REASK

        ask_line = QUESTIONS_TEXT(to_ask, language)
        if to_ask.get("times", 0) >= 1:
            ask_line = _REASK[language] + ask_line
    final["answer"] = present(final["answer"], language=language, facts=facts, question=ask_line,
                              question_priority=(to_ask or {}).get("priority") or "orange",
                              view=final.pop("sherlock_view", None), case=case,
                              llm=llm if writer == "model" else None, answer_first=intent == "question",
                              highlight_terms=asked_terms(query, net))
    yield _agent("Presenter", f"laid out in {lang_name}")
    check["asked"] = to_ask["key"] if to_ask else None
    if to_ask:
        desk.asked(to_ask)
    check["intent"] = intent
    final["checks"] = check
    final["questions"] = case.open_questions()
    final["asking"] = to_ask
    final["citations"] = case.citations_in(" ".join([final.get("answer") or ""] + [
        e for h in final.get("hypotheses") or [] for e in h.get("evidence") or []]))
    final["timing"] = {**budget.timing, "total": int((_time.monotonic() - budget.started) * 1000)}
    final["model_calls"] = llm.calls if llm is not None else 0
    final["late"] = list(dict.fromkeys(budget.late))
    final["followup_pending"] = bool(budget.late)
    final["board_version"] = (pack or {}).get("board_version", case.version)
    logger.info("Chat turn: %s ms, %d model call(s), steps %s%s", final["timing"]["total"], final["model_calls"],
                budget.timing, f", late: {final['late']}" if final["late"] else "")
    case.add_turn(message, final["answer"], check)
    yield final


def _mentioned_any(net: PersonNetwork, message: str) -> bool:
    from sherlocks.linkgraph.investigator import _mentioned

    return bool(_mentioned(net, message))
