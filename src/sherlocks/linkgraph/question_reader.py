"""O0 Question reader, and the Officer agent's plan: what is asked, and where to get it.

The Question reader reads every message once and hands the Officer agent an **ask**:

* ``act``   - question, request ("explain...", "tell me...", "batao"), statement, answer,
  greeting. A request is a question: it must be answered, not recorded.
* ``type``  - what is wanted: the Facts agent's query types (``cases``, ``fir_details``,
  ``fir_status``, ``io_report``, ``summary``, ``profile``, ``connection``...) or ``open``.
* ``depth`` - ``brief`` (a yes / no, a count, a list) or ``detailed`` ("explain", "what is
  the case", "tafseel", "what happened"). A detailed ask about FIRs gets each FIR's story.
* ``role``  - the role the question is about ("FIR on him" = accused, "filed by" =
  complainant, "gawah" = witness), ``yes_no``, the people, FIR numbers, crimes, and the
  person-detail topics asked (work, vehicle, phone, address, family, hotel, complaints).

With the model, one structured call reads it and **its reading is the ask** - it reads the
meaning, so any wording works; no keyword rule overrides it. Only structure is checked
(names must be on the graph, a list is not "one fact"). The keyword rules are the
fallback for when there is no model or the call fails.

The **plan** then decides the sources, so nothing is fetched that the ask does not need:
FIR questions are answered from the board and the FIR file (fetched only if it has not
been read); a person's job, vehicles or phones from the board, then the API router's
systems for exactly those topics; a judgment from Sherlock; a summary, a connection or
counts from the board alone.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class _Reading(BaseModel):
    act: Literal["question", "statement", "answer", "correction", "greeting"] = Field(
        description="question = anything asked or requested (incl. 'explain', 'tell me', 'batao'); statement = the "
                    "officer gives information; answer = replies to Sherlock's last question; correction = changes "
                    "something said before.")
    asks_something: bool = Field(default=False, description="True when the officer asks for information or wants "
                                 "something done - even if the message also names a person or replies in part to "
                                 "Sherlock's last question.")
    type: str = Field(default="open", description="What is wanted - one of: cases (which FIRs), count_cases, "
                      "fir_details (what each FIR / the case is about, its story, charges explained), fir_status, "
                      "serious_cases, io_report, criminals_near, criminals_all, profile, associates, connection, "
                      "hotels, phones, vehicles, documents, graph_stats, news, summary, open.")
    people: list[str] = Field(default_factory=list, description="Names of the people asked about, as written.")
    role: Literal["accused", "complainant", "witness", "victim", "any"] = Field(
        default="any", description="The role the question is about: an FIR 'on / against' someone = accused; "
                                   "'filed by', 'darj karwaya' = complainant; 'gawah' = witness; else any.")
    depth: Literal["brief", "detailed"] = Field(
        default="brief", description="detailed when the officer wants an explanation, the story, details, what the "
                                     "case is; brief for yes / no, a count or a list.")
    yes_no: bool = Field(default=False, description="True for a yes / no question.")
    opinion: bool = Field(default=False, description="True when the officer asks for a judgment (why, is he involved).")
    topics: list[str] = Field(default_factory=list, description="Details of a person asked about, from: work, "
                              "vehicle, phone, address, family, property, hotel, complaints.")
    about_new_case: bool = Field(default=False, description="True when the question is about the new case under "
                                 "investigation (the incident the officer described: 'this case'), not an old FIR.")
    scope: Literal["person", "case", "connection", "general"] = Field(
        default="case", description="person: about one person (who he is, his record, job, phones...); case: about "
                                    "the case, its FIRs, documents, evidence; connection: how people are linked; "
                                    "general: anything else.")
    crimes: list[str] = Field(default_factory=list, description="Crime types the question is about, in English "
                              "(murder, robbery, kidnapping...), if any.")
    asks_for: str = Field(default="", description="The single piece of information asked, in a few English words "
                          "(e.g. 'investigating officer', 'date of occurrence', 'place of occurrence', 'witnesses', "
                          "'weapon used', 'court', 'sections', 'complainant'), or '' when the officer wants a whole "
                          "picture (a summary, an explanation, a list).")
    uses_pronoun: bool = False


_READER_SYSTEM = (
    "You read one message a police officer sends to an investigation assistant (English, Urdu or Roman Urdu) and say "
    "exactly what is being asked, so the right information is fetched. Read the meaning, not keywords. A message that "
    "asks for anything is a question (act question, asks_something true), even if it names a person Sherlock asked "
    "about - 'answer' is only a reply to Sherlock's question that asks nothing back. Requests like "
    "'explain me the FIR filed by X' are questions; 'what is the case filed by X' wants the case's details (type "
    "fir_details, depth detailed, role complainant). Use the recent conversation to resolve who 'he / ye / wo' is "
    "and what 'that' refers to. When one specific fact is asked ('who is the IO of this case', 'kab hua', 'which "
    "weapon'), name it in asks_for, so exactly that is answered. When the officer wants what a document or case "
    "says, its story, an explanation or a summary ('what does the FIR say', 'what happened', 'tell me about the "
    "case'), it is a whole picture: depth detailed, asks_for ''. Never invent a narrower fact than the officer asked. "
    "scope person = about one person (his record, job, phones, family); case = the case, FIRs, documents, evidence; "
    "connection = how people are linked. about_new_case = the officer means the case being investigated now "
    "('this case', the incident he described), not one of the old FIRs.")

# A request is a question too: "explain me the FIR", "tell me about...", "tafseel batao".
REQUEST = re.compile(r"(?<![\w؀-ۿ])(explain\w*|describe|elaborate|tell me|tell us|show me|give me|list|summari[sz]e|"
                     r"details?|walk me through|batao|btao|bataen|bataein|bataiye|batayen|samjhao|samjha\w*|dikhao|"
                     r"tafs[ei]+l\w*|wazahat|بتائیں|بتاؤ|سمجھائیں|وضاحت|تفصیل|دکھائیں)(?![\w؀-ۿ])", re.IGNORECASE)
# How much is wanted: the story of the case, not just that it exists.
DETAILED = re.compile(r"(?<![\w؀-ۿ])(explain\w*|describe|elaborate|details?|in detail|tafs[ei]+l\w*|samjha\w*|wazahat|"
                      r"what (?:is|was|'s) (?:the |his |her |this |that )?(?:case|fir|matter)|"
                      r"what (?:happened|is it about|was it about)|about the (?:case|fir)|story|kahani|"
                      r"(?:case|fir|muqadma|parcha) (?:kya|kia) (?:hai|tha)|kya (?:hua|huwa) tha|"
                      r"کیا ہوا تھا|تفصیل|وضاحت|کیا معاملہ)(?![\w؀-ۿ])", re.IGNORECASE)
_FIR_TYPES = ("cases", "count_cases", "fir_details", "fir_status", "io_report", "serious_cases")
# Knowledge-base concepts that are a person's details the police systems hold.
_PERSON_TOPICS = {"work", "vehicle", "phone", "address", "father", "family", "property", "hotel", "cnic"}


# One specific fact asked (the rules' reading; the model names it in any wording).
_FIELD = re.compile(r"^\s*(?:who|what|which)\s+(?:is|was|are|were)\s+(?:the\s+)?(?P<x>[a-z][\w /-]{2,40}?)\s+"
                    r"(?:of|in|for|on|at|behind)\s+(?:this|that|the|his|her|their|its)\b", re.IGNORECASE)
_FIELD_ROMAN = re.compile(r"(?:is\s+(?:case|fir|muqadm\w*)\s+(?:ka|ki|ke)\s+)?(?P<x>[a-z][\w ]{1,30}?)\s+(?:kaun|kon)\s+"
                          r"(?:hai|ha|tha|hain|thi)\b", re.IGNORECASE)
_INVESTIGATOR = re.compile(r"who\s+(?:is|was|are)\s+(?:the\s+)?investigat\w*|\bi\.?o\b|investigat\w*\s+officer|"
                           r"tafteeshi\s+(?:officer|afsar)|تفتیشی افسر", re.IGNORECASE)
_WHO = re.compile(r"(?<![\w؀-ۿ])(who|whom|kaun|kon|کون)(?![\w؀-ۿ])", re.IGNORECASE)
_VERDICT = re.compile(r"report|guilty|innocent|declare|conclu|found (?:him|her)|challan|qasoor|gunah|بے گناہ|قصوروار",
                      re.IGNORECASE)
_WHEN = re.compile(r"^\s*when\b|(?<![\w؀-ۿ])(?:kab|کب)(?![\w؀-ۿ])", re.IGNORECASE)
_WHERE = re.compile(r"^\s*where\b|(?<![\w؀-ۿ])(?:kahan|kahaan|کہاں)(?![\w؀-ۿ])", re.IGNORECASE)
_HAPPEN = re.compile(r"happen|occur|hua|hui|huwa|waqia|wardat|ہوا|ہوئی", re.IGNORECASE)
_WHOLE = re.compile(r"target|suspect|summary|connect|all|everything|sab", re.IGNORECASE)


def field_of(message: str, net: Any) -> str:
    """The one fact a question asks for, by the rules ('' if it wants a whole picture)."""
    text = (message or "").strip()
    if _VERDICT.search(text):
        return ""                               # the IO's conclusion (io_report), not one fact
    if _INVESTIGATOR.search(text) and _WHO.search(text):
        return "investigating officer"
    if _WHEN.search(text) and _HAPPEN.search(text):
        return "date of occurrence"
    if _WHERE.search(text) and _HAPPEN.search(text):
        return "place of occurrence"
    for rx in (_FIELD, _FIELD_ROMAN):
        m = rx.search(text)
        if m:
            x = m.group("x").strip()
            if net.resolve(x) or _WHOLE.search(x) or len(x.split()) > 5:
                return ""                       # a person, or the whole picture: not one fact
            return x.lower()
    return ""


# Answers that are a whole picture: never narrowed to one fact.
_WHOLE_TYPES = {"summary", "connection", "criminals_near", "criminals_all", "graph_stats", "news", "associates",
                "profile", "documents", "fir_details"}


def one_fact(asks_for: str, kind: str, depth: str) -> bool:
    """Is this really one specific fact? Short, single, on a brief question that is not a
    whole picture ("summarise", "explain", "what does the FIR say" are not one fact)."""
    if not asks_for or depth == "detailed" or kind in _WHOLE_TYPES:
        return False
    if kind == "io_report" and not re.search(r"officer|investigator|\bi\.?o\b", asks_for):
        return False                    # the IO's conclusion is a whole answer; who the IO is, one fact
    return len(asks_for.split()) <= 4 and not re.search(r",|\band\b|&|/", asks_for)


def act_of(message: str, open_questions: list[dict[str, Any]]) -> str:
    """question | statement | answer | greeting - with requests counted as questions."""
    from sherlocks.linkgraph.conversation import route

    act = route(message, open_questions)
    if act in ("statement", "answer") and REQUEST.search(message or ""):
        return "question"
    return act


def depth_of(message: str, model_depth: str | None = None) -> str:
    if DETAILED.search(message or "") or model_depth == "detailed":
        return "detailed"
    return "brief"


def model_reading(message: str, case: Any, llm: Any) -> dict[str, Any] | None:
    """The model's reading of a message, or None (no model, or it failed)."""
    if llm is None or not (message or "").strip():
        return None
    recent = [{"officer": t.get("q", "")[:200], "sherlock": t.get("a", "")[:200]} for t in case.conversation[-3:]]
    asked = (case.conversation[-1].get("checks") or {}).get("asked") if case.conversation else None
    try:
        out, _ = llm.generate_structured(prompt=f"Recent conversation: {recent}\nSherlock last asked: {asked}\n"
                                                f"Message: {message}", schema=_Reading, system=_READER_SYSTEM,
                                         cache_kind="question_reader", prompt_version="v3", max_tokens=300)
        return out.model_dump()
    except Exception as exc:  # noqa: BLE001 - the rules read it instead
        logger.info("Question reader model failed: %s", exc)
        return None


def read_message(message: str, net: Any, case: Any, focus: list[str], llm: Any = None) -> dict[str, Any]:
    """O0: the whole ask. With the model, its reading is the reading - it understands any
    wording; the keyword rules are only the fallback when there is no model or it fails."""
    reading = model_reading(message, case, llm)
    if reading and reading.get("act"):
        said = reading["act"]
        # A message that asks is a question - also when it names someone Sherlock asked about
        # ("did Zameer stay in a hotel then?" does not answer "who is the main suspect?").
        asking = said == "question" or bool(reading.get("asks_something")) or (message or "").rstrip().endswith(("?", "؟"))
        if asking:
            act = "question"
        elif said == "greeting":
            act = "greeting"
        else:                       # information: an answer when Sherlock has a question open
            act = "answer" if said == "answer" and case.open_questions() else "statement"
        ask = {"act": act, "reading": reading}
        if act == "question":
            ask.update(from_reading(message, net, case, focus, reading))
        return ask
    act = act_of(message, case.open_questions()) if message else "greeting"
    ask = {"act": act, "reading": None}
    if act == "question":
        ask.update(read(message, net, case, focus))
    return ask


def from_reading(message: str, net: Any, case: Any, focus: list[str], reading: dict[str, Any]) -> dict[str, Any]:
    """The ask, from the model's reading alone (no keywords). Only structure is checked:
    the names must be on the graph, a list of things is not one fact, FIR numbers are
    taken as written."""
    from sherlocks.linkgraph.investigator import _mentioned
    from sherlocks.linkgraph.understanding import FIR_NO, QueryType

    people: list[str] = []
    for name in reading.get("people") or []:
        pid = net.resolve(name)
        if pid and pid not in people:
            people.append(pid)
    for pid in _mentioned(net, message, sound=False):      # a name written in full is on the graph
        if pid not in people:
            people.append(pid)
    kind = reading.get("type") if reading.get("type") in set(QueryType.__args__) else "open"
    if not people and focus and (reading.get("uses_pronoun") or reading.get("scope") == "person"
                                 or kind in ("cases", "count_cases", "fir_status", "serious_cases", "fir_details",
                                             "io_report", "profile", "associates", "hotels", "phones", "vehicles",
                                             "documents")):
        people = [p for p in focus if p in net.people][:1]      # "he / ye / wo": the person being discussed
    depth = reading.get("depth") or "brief"
    asks_for = (reading.get("asks_for") or "").strip().lower()
    role = reading.get("role")
    return {"type": kind, "people": people, "pronoun": bool(reading.get("uses_pronoun")), "by": "model",
            "crimes": [c.lower() for c in reading.get("crimes") or []],
            "firs": [m.group(0) for m in FIR_NO.finditer(message)],
            "role": role if role in ("accused", "complainant", "witness", "victim") else None,
            "yes_no": bool(reading.get("yes_no")), "opinion": bool(reading.get("opinion")), "depth": depth,
            "asks_for": asks_for if one_fact(asks_for, kind, depth) else "",
            "this_case": bool(reading.get("about_new_case")), "scope": reading.get("scope") or "case",
            "topics": [t for t in reading.get("topics") or [] if t in _PERSON_TOPICS or t == "complaints"]}


def read(message: str, net: Any, case: Any, focus: list[str], llm: Any = None,
         reading: dict[str, Any] | None = None) -> dict[str, Any]:
    """The ask for one question by the rules - the fallback when there is no model. (With
    a ``reading``, it defers to :func:`from_reading`.)"""
    if reading:
        return from_reading(message, net, case, focus, reading)
    from sherlocks.linkgraph.knowledge import KnowledgeBase
    from sherlocks.linkgraph.understanding import YES_NO, asked_role, understand

    q = understand(message, net, focus, None, roles=case.roles, last_type=case.dialog.get("last_type"))
    q["role"] = q.get("role") or asked_role(message)
    q["yes_no"] = q.get("yes_no", bool(YES_NO.search(message)))
    depth = depth_of(message)
    kind = q["type"]
    asks_for = field_of(message, net)
    q["asks_for"] = asks_for if one_fact(asks_for, kind, depth) else ""
    # "this case" / "is case" / "naye case": the new case under investigation, not an old FIR.
    q["this_case"] = bool(re.search(r"(?<![\w؀-ۿ])(?:this|the new|new|is|naye|naya|isi)\s+(?:case|muqadm\w*|waqi\w*|"
                                    r"incident|matter)(?![\w؀-ۿ])|اس کیس|اس مقدمے|نئے کیس", message, re.IGNORECASE))
    if depth == "detailed" and kind in ("cases", "count_cases", "serious_cases") and q["people"] \
            and not re.search(r"how many|kitn|کتن", message, re.IGNORECASE):
        kind = "fir_details"            # "what is the case filed by him?": its story, not that it exists
    topics = [t for t in KnowledgeBase.topics(message) if t in _PERSON_TOPICS]
    scope = "connection" if kind == "connection" else "person" if q["people"] and kind in (
        "profile", "hotels", "phones", "vehicles", "associates") or topics else "case"
    return {**q, "type": kind, "depth": depth, "topics": topics, "scope": scope}


def plan(ask: dict[str, Any], case: Any, net: Any) -> dict[str, Any]:
    """The Officer agent's sources for the ask: ``{sources, fir_files, systems, why}``."""
    from sherlocks.linkgraph.case_queries import _firs

    kind, people = ask.get("type"), ask.get("people") or []
    sources = ["board"]
    fir_files = systems = False
    if ask.get("asks_for"):
        # One fact: from the case's FIR files and records; the files fetched if not read.
        sources.append(f"fact: {ask['asks_for']}")
        unread = [r for r in _firs(net, people[0], case) if not r.get("doc")] if people else []
        fir_files = bool(unread)
        if fir_files:
            sources.append("fir_file")
    elif kind in _FIR_TYPES and people:
        sources.append("facts")
        unread = [r for r in _firs(net, people[0], case) if not r.get("doc")]
        if unread and kind in ("fir_details", "fir_status", "io_report"):
            fir_files = True            # the story is in the FIR file: fetch it if not read
            sources.append("fir_file")
    elif kind in ("summary", "connection", "criminals_near", "criminals_all", "graph_stats", "news", "associates",
                  "documents"):
        sources.append("facts")
    elif kind in ("profile", "hotels", "phones", "vehicles"):
        sources.append("facts")
        systems = bool(ask.get("topics")) or kind in ("hotels", "phones", "vehicles")
        if kind == "hotels" and people and any(not r.get("doc") and not r.get("occurred")
                                               for r in _firs(net, people[0], case)):
            fir_files = True            # stays are checked against each FIR's date: read the files that hold it
            sources.append("fir_file")
    else:                               # open: the knowledge base; systems only for a person's details
        systems = bool(ask.get("topics")) and bool(people)
    if ask.get("firs"):
        fir_files = True
    if systems:
        sources.append("api_router")
    if ask.get("opinion"):
        sources.append("sherlock")
    why = (f"{ask.get('depth')} {kind}" + (f" about {', '.join(net.name(p) for p in people)}" if people else "")
           + (f", role {ask['role']}" if ask.get("role") else "") + (f", topics {', '.join(ask['topics'])}"
                                                                      if ask.get("topics") else ""))
    return {"sources": sources, "fir_files": fir_files, "systems": systems, "why": why}
