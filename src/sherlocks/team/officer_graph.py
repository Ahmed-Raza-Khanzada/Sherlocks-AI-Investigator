"""One chat turn of the Officer team, as a LangGraph graph.

    read (O0 Question reader: the model reads the message - act, people, shape, depth)
      |-- no model, or it does not answer --> rules (the rule-based turn) --> END
      |-- the officer tells us something  --> record (O3) --> desk
      '-- the officer asks                --> brief (A4 dossiers, O4 context pack, the Facts
                                              agent, the sources the answer shape needs;
                                              Sherlock's quick view starts alongside)
            --> draft (O1 fills the shape; names the lookups he still needs)
                  |-- lookups, a round left, time left --> lookup (toolbox / API agent) --> draft
                  '-- else --> desk
    desk (Sherlock's view collected; Q3 Insight desk and Q1 Gatekeeper: one extra item)
      --> check (O2: sources, quotes, numbers, completeness; one revision) --> present (O5) --> END

What decides the route is the model's reading and the draft's own ``lookups`` - never
words matched in the officer's message. The keyword rules live in one node, ``rules``,
which runs only when there is no model to read with.

Every node sends its events (``status``, ``agent``, and at the end ``final``) to the
graph's stream; ``stream_turn`` yields them to the chat as they happen. Each step has a
time budget; what finishes late is sent as a follow-up (``case.followups``).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.team import case_reader, insight_desk, officer_agent, recorder, shapes
from sherlocks.team.runtime import emit

logger = logging.getLogger(__name__)
LANG_LABEL = {"ur": "Urdu", "roman": "Roman Urdu", "en": "English"}
IN_DOUBT = {"en": "Part of this answer could not be verified against the records - check it against the sources.",
            "roman": "Is jawab ka kuch hissa records se tasdeeq nahi ho saka - sources se mila lein.",
            "ur": "اس جواب کا کچھ حصہ ریکارڈ سے تصدیق نہیں ہو سکا - ذرائع سے ملا لیں۔"}
LOOKING_INTO = {"en": "Sherlock is looking into this - what he finds will follow.",
                "roman": "Sherlock is par kaam kar rahe hain - jo milega woh bata diya jayega.",
                "ur": "شرلاک اس پر کام کر رہے ہیں - جو ملے گا وہ بتا دیا جائے گا۔"}


@dataclass
class Turn:
    """What the nodes of one turn share. The case board is the memory; this carries the turn."""

    graph: dict[str, Any]
    message: str
    case: CaseFile
    net: PersonNetwork
    llm: Any
    raw_llm: Any
    history: list[dict[str, Any]]
    agents: Any
    backend: Any
    live_calls: int
    officer: dict[str, Any] | None
    on_late: Any
    settings: Any
    cfg: Any
    budget: Any
    language: str
    turn_no: int
    route: str = "rules"
    rules_llm: Any = None
    announced: bool = False
    asked: bool = False                                    # the message asks something (it may also tell)
    ask: dict[str, Any] = field(default_factory=dict)
    intent: str = "greeting"
    query: dict[str, Any] | None = None
    names: str = ""
    facts: dict[str, Any] | None = None
    pack: dict[str, Any] | None = None
    cards: list[dict[str, Any]] = field(default_factory=list)
    hits: list[Any] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)
    sources: dict[str, Any] = field(default_factory=dict)
    tools: Any = None
    registry: dict[str, Any] = field(default_factory=dict)
    draft: dict[str, Any] | None = None
    rounds: int = 0
    looked: list[dict[str, Any]] = field(default_factory=list)
    researched: list[str] = field(default_factory=list)
    view_future: Any = None
    sweep_future: Any = None
    view_started: float = 0.0
    view: dict[str, Any] | None = None
    recorded: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)       # shown after the answer: corrections, conflicts, news
    reply: str = ""
    writer: str = "model"
    not_found: bool = False
    dropped: list[str] = field(default_factory=list)
    given: dict[str, Any] = field(default_factory=dict)       # what O1 was given for his last draft
    deeper: bool = False
    planned: bool = False                                     # the API agent was asked once this turn
    to_ask: dict[str, Any] | None = None
    finding: dict[str, Any] | None = None
    final: dict[str, Any] = field(default_factory=dict)
    check: dict[str, Any] = field(default_factory=dict)
    trace: Any = None                                         # team/trace.py: how the agents worked on this turn

    def status(self, key: str, tool: str | None = None, on: str = "") -> None:
        from sherlocks.linkgraph.conversation import status_text

        emit({"type": "status", "key": key, "tool": tool, "text": status_text(key, self.language, tool, on)})

    def agent(self, name: str, text: str, **extra: Any) -> None:
        emit({"type": "agent", "agent": name, "text": text, **extra})
        if self.trace is not None:
            self.trace.note(name, text, **extra)

    def follow(self, text: str, kind: str = "followup") -> None:
        """Something finished after the reply went out: a follow-up in the same chat."""
        if text and text.strip() and self.trace is not None:
            self.trace.note("Follow-up", f"{kind}: sent after the reply - {text.strip()[:400]}")
        if text and text.strip():
            self.case.add_followup(text.strip(), turn=self.turn_no, kind=kind)
            if self.on_late is not None:
                self.on_late()


class TurnState(TypedDict, total=False):
    turn: Turn


# -- O0 ------------------------------------------------------------------------------------


def _asked(t: Turn) -> str:
    """The question to answer: the officer's words - or, when they point their last question at
    someone ("Y, not X!"), that question with their words."""
    return str((t.ask or {}).get("question_text") or t.message)


def _read(state: TurnState) -> dict[str, Any]:
    from sherlocks.linkgraph import question_reader

    t = state["turn"]
    if t.llm is None or not t.message:
        t.route, t.rules_llm = "rules", t.raw_llm           # no model to read with (or nothing said)
        return {}
    focus = [p for p in t.case.dialog.get("focus") or [] if p in t.net.people]
    t.ask = question_reader.read_message(t.message, t.net, t.case, focus, t.llm)
    if not t.ask.get("reading"):
        t.route, t.rules_llm = "rules", None                # the model is not answering: rules, no retries
        return {}
    t.intent = t.ask["act"]
    # The script is a fact (Urdu letters or not); between English and Roman Urdu the model's reading decides.
    said = t.ask["reading"].get("language")
    if said == "roman" and not roman_words(t.message):
        said = "en"         # typos are not Urdu: "who is kamran aslam hwo herelates" has no Roman-Urdu word in it
    if t.language != "ur" and said in ("en", "roman"):
        t.language = said
    t.case.dialog["language"] = t.language
    if t.case.conversation and t.case.open_questions():
        # Q1 reads the whole chat alongside: an open question the officer already answered is never asked.
        from sherlocks.evidence.question_desk import sweep

        t.sweep_future = t.budget.submit(lambda: sweep(t.case, t.llm, t.message))
    emit({"type": "start", "question": t.message, "people": len(t.net.people),
          "targets": [t.net.name(p) for p in t.net.seeds()], "documents": len(t.case.documents), "language": t.language,
          "intent": t.intent, "model": getattr(t.llm, "model", None), "findings": 0, "live_calls": t.live_calls})
    t.announced = True
    t.status("start")
    t.agent("Conversation agent", f"{LANG_LABEL[t.language]}; discussing "
                                  f"{', '.join(t.net.name(p) for p in focus) or 'the case'}")
    # A message that asks AND tells ("naya mamla hai, usne fraud kiya - uska record kya hai?"):
    # what it tells is recorded first, then the question is answered.
    t.asked = t.intent == "question"
    t.route = "record" if (not t.asked or t.ask.get("tells")) else "brief"
    return {}


def _rules(state: TurnState) -> dict[str, Any]:
    """The rule-based turn: the whole fallback, in one node."""
    from sherlocks.linkgraph.sherlock_team import rule_turn

    t = state["turn"]
    for event in rule_turn(t.graph, t.message, llm=t.rules_llm, history=t.history or None, case=t.case, agents=t.agents,
                           backend=t.backend, live_calls=t.live_calls, officer=t.officer, on_late=t.on_late,
                           settings=t.settings, ask=t.ask if t.ask.get("reading") else None):
        if event.get("type") == "start" and t.announced:
            continue
        if t.trace is not None and event.get("type") in ("agent", "step"):
            t.trace.note(event.get("agent") or "Rule answer", event.get("text") or event.get("summary")
                         or f"{event.get('tool')}({event.get('args_text') or ''})")
        if event.get("type") == "final":
            t.final = event
        if event.get("type") == "final" and "draft" in t.budget.late and event.get("answer"):
            # The model's answer is still being written and follows: say so first, and keep what the board holds short.
            lines = str(event["answer"]).split("\n")
            bullets = [x for x in lines if x.lstrip().startswith(("•", "-", "*"))]
            head = [x for x in lines if x.strip() and x not in bullets][:1]
            event["answer"] = "\n".join([HOLDING[t.language], *head, *bullets[:5]]
                                        + ([MORE_ON_BOARD[t.language].format(n=len(bullets) - 5)] if len(bullets) > 5 else []))
        if event.get("type") == "final" and t.announced:
            # The graph's own steps before the fallback count too (a slow draft, the model calls made).
            event["late"] = list(dict.fromkeys([*t.budget.late, *(event.get("late") or [])]))
            event["followup_pending"] = bool(event["late"])
            event["model_calls"] = (event.get("model_calls") or 0) + (t.llm.calls if t.llm is not None else 0)
            event["timing"] = {**t.budget.timing, **(event.get("timing") or {})}
        emit(event)
    return {}


# -- O3 ------------------------------------------------------------------------------------


def _record(state: TurnState) -> dict[str, Any]:
    from sherlocks.evidence.question_desk import Gatekeeper
    from sherlocks.linkgraph import corrections
    from sherlocks.linkgraph.sherlock_team import _take_answer

    t = state["turn"]
    case, net = t.case, t.net
    if t.intent == "greeting":
        t.route = "desk"
        return {}
    t.status("record")
    focus = [p for p in case.dialog.get("focus") or [] if p in net.people]
    reading = t.budget.run("record", lambda: recorder.read(case, net, t.message, t.llm, t.language),
                           t.cfg.draft_budget_s, fallback=None)
    if reading is None:
        t.route, t.rules_llm = "rules", None
        return {}
    # A change waiting for a yes or no ("2 March kar dun?").
    waiting = case.dialog.get("pending_correction")
    if waiting and reading.confirm:
        case.dialog.pop("pending_correction", None)
        if reading.confirm == "yes":
            done = corrections.apply(case, net, waiting, language=t.language)
            t.lines.append(done["line"])
            t.agent("Statement recorder", f"correction confirmed: {waiting['topic']} = {waiting['new']}; "
                                          f"{len(done['stale'])} entr(ies) marked stale")
    answered = next((q for q in case.open_questions() if q["id"] == reading.answers), None)
    if recorder.no_case(reading):
        answered = None             # "there is no case" answers no question about one
    if answered is not None and answered["key"] == "case:which":
        # Which case this is, is the recorder's own reading (`this_case`): written below when it
        # could tell, and left open when it could not - never settled by a rule on the wording.
        answered = None
    understood = (t.ask.get("reading") or {})
    wrote = recorder.write(case, net, t.message, reading, language=t.language, focus=focus,
                           crimes=[str(c) for c in understood.get("crimes") or [] if c] if understood.get("about_new_case")
                           else None)
    t.recorded += wrote["recorded"]
    for fix in wrote["corrected"]:
        t.lines.append(fix["line"])
        t.agent("Statement recorder", f"correction: {fix['topic']}; {len(fix['stale'])} entr(ies) marked stale, "
                                      f"redo: {', '.join(fix['redo'])}")
    if answered is not None:
        t.intent = "answer"
        if reading.dont_know:
            case.answer(answered["id"], "not known", status="dont_know")     # an answer too: never asked again
            case.officer_note(f"Answer to \"{answered['text']}\": not known", source="answer")
            t.recorded.append(f"not known: {answered['text'][:60]}")
        elif answered["key"].startswith("confirm_lookup:") and reading.confirm:
            _confirm_lookup(case, answered, reading.confirm == "yes")
            t.recorded.append(f"{answered['key'].split(':', 1)[1]}: {'may' if reading.confirm == 'yes' else 'not to'} be looked up")
        else:
            given = _answer_given(reading, answered["key"], t.message)
            if given is None:
                # A question or a request ("check in the FIR what the IO says"), not a value for this field: the
                # question stays open - the whole message once became the case's place.
                t.intent = "statement"
                t.agent("Statement recorder", f"not an answer to \"{answered['text'][:60]}\": no value given for it")
            else:
                with case.acting("O3 Statement recorder"):
                    t.recorded += _take_answer(case, net, given, answered)
    else:
        t.intent = "statement"
    if wrote["wish"] == "stop_asking":
        last = (case.conversation[-1].get("checks") or {}).get("asked") if case.conversation else None
        if last:
            Gatekeeper(case, t.graph).mute(last)
            t.recorded.append(f"will not ask again: {last}")
    elif wrote["wish"] in ("pause_findings", "resume_findings"):
        insight_desk.pause(case, wrote["wish"] == "pause_findings")
        t.recorded.append("Sherlock's findings: " + ("held until asked" if wrote["wish"] == "pause_findings" else "on again"))
    if t.recorded:
        t.agent("Statement recorder", "; ".join(t.recorded), recorded=t.recorded)
    named = [net.resolve(e.about) for e in reading.entries if e.about]
    named = [p for p in dict.fromkeys(named) if p in net.people]
    if named:
        case.dialog["focus"] = named[:2]
    if t.asked:                                        # it also asked something: the answer follows
        t.intent = "question"
        if t.recorded and wrote["read_back"]:
            t.lines.append(wrote["read_back"])
        t.route = "brief"
        return {}
    t.reply = wrote["read_back"]
    t.route = "desk"
    return {}


# The recorder's entries that give the value an incident question asks for.
_FIELD_TOPICS = {"incident:place": ("incident_place",), "incident:when": ("incident_date", "incident_time"),
                 "incident:what": ("incident_offence",), "incident:fir": ("incident_fir",),
                 "incident:vehicle": ("vehicle_or_weapon",)}


def _answer_given(reading: Any, key: str, message: str) -> str | None:
    """What the officer's message gives as the answer to the question ``key``: for a question about the incident
    (its place, time, offence, FIR) only the value the recorder read out of the message - the place, not the whole
    sentence; None when the message gives no such value (it asks something, or asks to look in a record). Any
    other question takes the message as it is."""
    topics = _FIELD_TOPICS.get(key)
    if not topics:
        return message
    values = []
    for e in reading.entries:
        if e.topic not in topics:
            continue
        value = (e.where if e.topic == "incident_place" and e.where and len(e.where.split()) <= 15 else "") \
            or e.value or ""
        if value.strip() and len(value.split()) <= 15:
            values.append(value.strip())
    return " ".join(dict.fromkeys(values)) or None


def _confirm_lookup(case: CaseFile, q: dict[str, Any], yes: bool) -> None:
    """The officer's yes / no to looking up an identifier (or a CDR's contacts) found in a document."""
    key = q["key"]
    if key.startswith("confirm_lookup:cdr:"):
        doc_id = key.split(":", 2)[2]
        bucket = "confirmed_docs" if yes else "declined_docs"
        case.dialog[bucket] = list(dict.fromkeys([*(case.dialog.get(bucket) or []), doc_id]))
        if yes:
            case.plan(f"cdr:enrich:{doc_id}", "the officer agreed to look up the top contacts")
    else:
        ident = key.split(":", 1)[1]
        bucket = "confirmed_ids" if yes else "declined_ids"
        case.dialog[bucket] = list(dict.fromkeys([*(case.dialog.get(bucket) or []), ident]))
        case.dialog["unconfirmed_ids"] = [i for i in case.dialog.get("unconfirmed_ids") or [] if i != ident]
    case.answer(q["id"], "yes" if yes else "no")


# -- O4 ------------------------------------------------------------------------------------


def _people_of_the_case(case: Any) -> dict[str, Any] | None:
    """Everyone the case connects to the target, as the latest report laid it out: the direct connections and how,
    the people one step on who matter and through whom, and the people the case's own records name with their part."""
    parts = ((case.report or {}).get("parts") or {})
    network, story = parts.get("network") or {}, parts.get("case_story") or {}
    if not (network.get("first") or story.get("people")):
        return None
    return {
        "direct_connections": [{"name": r.get("name"), "how": r.get("how"), "part_in_this_case": r.get("part") or "",
                                "criminal_record": r.get("record") or "", "to": r.get("to")}
                               for r in network.get("first") or []],
        "one_step_further": [{"name": r.get("name"), "how": r.get("how"), "through": r.get("through"),
                              "part_in_this_case": r.get("part") or "", "criminal_record": r.get("record") or ""}
                             for r in network.get("second") or []],
        "named_in_the_case_records": [{"name": p.get("name"), "part": p.get("part"), "status": p.get("status"),
                                       "did_or_said": str(p.get("did") or "")[:200], "sources": p.get("sources")}
                                      for p in story.get("people") or []][:30],
        "as_of": "the case report built at board version " + str((case.report or {}).get("report_board_version") or "?")}


def roman_words(text: str) -> bool:
    """Does the message have a Roman-Urdu word in it at all?"""
    from sherlocks.linkgraph.conversation import _ROMAN

    return any(w in _ROMAN for w in re.findall(r"[a-z]+", str(text or "").lower()))


def _targets_together(case: Any, net: Any, every: bool = False) -> list[str]:
    """The targets and their direct connections in plain lines, each citing the board's link-analysis record - with
    one target (``every``), every direct connection."""
    from sherlocks.team import case_scope, group_view
    from sherlocks.team.report_graph import _graph_doc

    try:
        group = group_view.build(case, net, case_scope.build(case, net), least=1)
        if not group:
            return []
        ref = _graph_doc(case)
        out = group_view.lines(group, ref) if len(group["targets"]) > 1 else []
        # Each target's contacts to look for them through - those tied to other targets or with a record first -
        # and who is named with them only as complainant, witness or police (not a lead).
        for c in group["contacts"]:
            for r in c["rows"][:group_view.MOST_CONTACTS if every else 4]:
                out.append(f"Direct connection of {c['target']}: {r['name']} - {r['how']}"
                           + (f"; also tied to {', '.join(r['also_tied_to'])}" if r["also_tied_to"] else "")
                           + (f"; phones {', '.join(r['phones'])}" if r["phones"] else "")
                           + (f"; address {r['address']}" if r["address"] else "")
                           + (f"; record: {r['record']}" if r["record"] else "") + (f" [{ref}]" if ref else ""))
            if c.get("named"):
                out.append(f"Also tied to {c['target']} as complainant, witness or police in an FIR (with how): "
                           f"{'; '.join(c['named'][:12 if every else 6])}")
            if every and c.get("more"):
                out.append(f"{c['more']} more direct connection(s) of {c['target']} on the graph")
        return out
    except Exception:          # the answer goes on without it
        logger.exception("Group view for the chat failed")
        return []


FILE_WAIT_S = 20.0


def _wait_for_files(t: Any, most: float = FILE_WAIT_S) -> None:
    """An FIR file or report being fetched right now (the background found the FIR a moment ago) is waited for,
    up to ``most`` seconds: answered without it, the reply said the FIR's story was not on record."""
    def fetching() -> list[str]:
        return [k for k in list(t.case.pending) if k.startswith(("fir:", "labs:", "labpdf:"))]

    if not fetching():
        return
    t.agent("Case briefer", f"waiting for {len(fetching())} document(s) being fetched")
    end = time.monotonic() + most
    while fetching() and time.monotonic() < end:
        time.sleep(0.5)


ARCHIVE_WAIT_S = 30.0


def _no_number(graph: dict[str, Any]) -> list[str] | None:
    from sherlocks.evidence import cdr_archive

    try:
        return cdr_archive.without_a_number(graph)[:12] or None
    except Exception:
        logger.exception("CDR archive: people with no number")
        return None


def _search_archive_again(t: Any) -> None:
    """Asked about the call records: the numbers of the targets, of the people tied to them and the ones the officer
    wrote, not searched in the CDR archive lately, are searched now (officers keep extracting CDRs into it) - waited
    for up to ``ARCHIVE_WAIT_S``; each CDR found is then read onto the board in the background."""
    import threading

    from sherlocks.evidence import cdr_archive
    from sherlocks.linkgraph.investigator import _Tools

    if t.case is None or (t.agents is None and t.backend is None):
        return
    try:
        if not (cdr_archive.Archive().ready and cdr_archive.live(t.tools)):
            return
        todo = cdr_archive.to_search(t.case, t.graph, again_after_h=cdr_archive.AGAIN_AFTER_H)
    except Exception:
        logger.exception("CDR archive: which numbers to search")
        return
    if not todo:
        return
    tools = _Tools(t.graph, t.case, t.agents, t.backend, len(todo), officer=t.officer, team="CDR archive",
                   reason=f"officer asked: {t.message[:120]}")
    searched = threading.Event()

    def run() -> None:
        try:
            cdr_archive.sweep(t.case, t.graph, tools, again_after_h=cdr_archive.AGAIN_AFTER_H, searched=searched)
        except Exception:
            logger.exception("CDR archive search from the chat failed")
            searched.set()

    t.agent("CDR archive", f"searching the iCOP CDR archive now for {len(todo)} number(s) of "
                           f"{len({x['pid'] or x['number'] for x in todo})} person(s)")
    threading.Thread(target=run, daemon=True, name="cdr-archive-chat").start()
    if not searched.wait(ARCHIVE_WAIT_S):
        t.agent("CDR archive", "still searching - the answer says what was searched so far")


def _call_records(t: Any) -> None:
    """What the CDRs on the board say about the people of the case, computed over every record: the target's own
    CDR (profile, top contacts with who is behind each number, places) and each CDR he is only a contact in (the
    contact history). The full tables are on the case board."""
    from sherlocks.evidence import cdr_profile

    try:
        calls = cdr_profile.for_report(t.case, t.net)
    except Exception:
        logger.exception("Call records for the chat")
        return
    if not (calls["own"] or calls["contact"]):
        return
    keep = ("number", "owner", "records", "calls_out", "calls_in", "sms_out", "sms_in", "talk_seconds", "first", "last",
            "days", "night", "hours", "where", "rows")
    t.sources["call_records_analysed"] = {
        "targets_own_cdrs": [{
            "whose": f"{o['name']} ({o['subject']})", "record": o["doc"],
            "profile": {k: o["profile"].get(k) for k in ("records", "first", "last", "active_days", "per_day", "contacts",
                                                         "services", "busiest_hours", "night", "weekdays", "home", "work",
                                                         "devices", "silences", "longest", "only_around_incident")},
            "top_contacts": [{k: c.get(k) for k in keep} for c in o["contacts"] if not c.get("service")][:25],
            "top_places": [{k: x.get(k) for k in ("place", "records", "calls", "sms", "contacts", "night", "day", "first",
                                                  "last", "rows")} for x in o["places"]][:12],
            "all_contacts": len(o["contacts"]), "all_places": len(o["places"])} for o in calls["own"]],
        "cdrs_the_target_is_a_contact_in": [{
            "subscriber": h["subject"] + (f" ({h['subject_name']})" if h.get("subject_name") else ""),
            "target": f"{h.get('target_name')} ({h['other']})", "record": h["doc"],
            **{k: h.get(k) for k in ("records", "calls", "sms", "from_subject", "to_subject", "talk_seconds", "first",
                                     "last", "days", "hours", "longest")},
            "every_contact": (h.get("events") or [])[:40]} for h in calls["contact"]],
        "note": "Computed in code over every record. 'owner' is who the number is registered to, as the graph holds it "
                "(empty: not known yet). A tower is where the phone connected, not where the person was. Every unique "
                "contact and place is on the case board (the 'Call records' documents)."}
    t.agent("CDR team", f"call records analysed: {len(calls['own'])} own CDR(s) of the target, "
                        f"{len(calls['contact'])} CDR(s) he is a contact in")


def _brief(state: TurnState) -> dict[str, Any]:
    from sherlocks.linkgraph import briefer, case_queries, quick_view
    from sherlocks.linkgraph import officer as chain
    from sherlocks.linkgraph.investigator import _Tools
    from sherlocks.linkgraph.knowledge import knowledge

    t = state["turn"]
    case, net, q = t.case, t.net, t.ask
    t.query = q
    t.status("understand")
    from sherlocks.team import case_rematch

    if case_rematch.unmatched(case):
        # A FIR file read after the officer described the case: is it this case? Asked once, before answering.
        fir = t.budget.run("rematch", lambda: case_rematch.rematch(case, net, t.llm), 30.0, fallback=None)
        if fir:
            t.agent("Case matcher", f"this case is FIR {fir} (read after the officer described the case)")
    t.names = ", ".join(net.name(p) for p in q["people"]) or "the whole case"
    shape = q.get("shape") or "other"
    t.agent("Question reader", f"{q['depth']} {shape} answer ({q['type'].replace('_', ' ')}) - about {t.names}"
            + (f"; role asked: {q['role']}" if q.get("role") else "") + (" (yes / no)" if q.get("yes_no") else "")
            + f" [{q.get('by')}]")
    # A4: the card of each person asked about.
    if chain.about_person(q):
        t.status("dossier", on=t.names)
        t.cards = chain.dossier_step(case, net, q)
        if t.cards:
            t.checked.append(chain.checked_item("dossier", t.language, x=t.names))
            t.agent("Dossier agent", "; ".join(f"{c['name']}: {len(c.get('firs') or [])} FIR(s), "
                                               f"{len(c.get('links') or [])} link(s), {len(c.get('facts') or [])} fact(s)"
                                               for c in t.cards))
    # The case board and the graph.
    t.status("board")
    kb = knowledge(t.graph, case, net)
    t.hits = kb.search(_asked(t), people=q["people"], k=30)
    t.checked.append(chain.checked_item("board", t.language, f=len(case.facts), d=len(case.documents)))
    t.checked.append(chain.checked_item("graph", t.language, n=len(net.people)))
    t.status("brief")
    started = time.monotonic()
    t.pack = briefer.build(case, t.graph, net, _asked(t), people=q["people"], kb=kb, chars=t.cfg.pack_chars,
                           recent=t.cfg.recent_turns)
    t.budget.record("brief", started)
    # The Facts agent: exact counts and lists, computed - never left to the model to count.
    if q.get("asks_for"):
        found = case_queries.fact_answer(net, case, kb, q["asks_for"], q["people"], q.get("firs"))
        t.facts = None if found["empty"] else found
    if t.facts is None and q["type"] != "open":
        t.status("facts")
        t.facts = case_queries.run(q, net, case)
    if t.facts is not None:
        t.agent("Facts agent", f"{t.facts['count']} result(s) for {t.facts['type'].replace('_', ' ')}")
    # The shared toolbox, and every source this answer shape needs.
    t.tools = _Tools(t.graph, case, t.agents, t.backend, t.live_calls, officer=t.officer, team="O1 Officer agent",
                     reason=f"officer asked: {t.message[:120]}")
    t.registry = t.tools.registry()
    who = net.name(q["people"][0]) if q["people"] else None
    for tool, arg in shapes.SOURCES.get(shape) or []:
        if tool not in t.registry or (arg and not who) or (tool == "dossier" and t.cards):
            continue
        t.sources[tool] = _call(t, tool, {arg: who} if arg and who else {})
    if shape == "fir" or q.get("firs"):
        # The FIR file itself, in its own words - its first information, every case diary and its investigation
        # result, and what the full reader took out of it - so the answer is read from the document even when
        # the board's facts missed something. A file still being fetched is waited for, a little.
        _wait_for_files(t)
        named = [d for d in (case_queries.fir_document(case, f) for f in q.get("firs") or []) if d]
        theirs = [case.documents[d["id"]] for p in (q["people"] or net.seeds())
                  for d in case.for_person(p)["documents"] if (case.documents.get(d["id"]) or {}).get("kind") == "fir"]
        files = list({d["id"]: d for d in (named or theirs or [d for d in case.documents.values() if d["kind"] == "fir"])}.values())[:2]
        for doc in files:
            t.sources[f"FIR file {doc['id']}"] = t.tools._digest(doc["id"], text_chars=12000 if len(files) == 1 else 7000)
    if q.get("call_records") or shape == "phone":
        _search_archive_again(t)
    searched = (case.dialog or {}).get("cdr_archive") or {}
    if (q.get("call_records") or shape == "phone") and searched.get("searched"):
        # What the search of the CDR archive came to, in one place: how many numbers of whom, whose CDR it has.
        t.sources["cdr_archive_search"] = {
            "state": searched.get("state"), "numbers_searched": searched.get("all_numbers") or searched.get("searched"),
            "targets": searched.get("all_targets") or searched.get("targets"),
            "people_tied_to_them": searched.get("all_contacts") or searched.get("contacts"),
            "cdr_found_for": [f"{f['name']} ({f['number']})" for f in searched.get("all_found") or searched.get("found") or []]
            or "nobody",
            "numbers_the_officer_gave": searched.get("all_given") or 0,
            "tied_to_them_with_no_number_on_record": _no_number(t.graph),
            "files_read_onto_the_board": searched.get("read") or 0, "record": searched.get("doc"),
            "last_searched": searched.get("last_searched") or searched.get("finished"),
            "now": searched.get("state"),
            "note": "Searched by calling the archive: every number of the targets, of the people tied to them and the "
                    "numbers the officer gave; searched again when asked if the last search is old. A number not in the "
                    "archive has no CDR extracted by any officer yet - the CDR must be requested from the operator. "
                    "Say when it was last searched. A person with no number on record could not be searched - say so."}
    if q.get("call_records") or shape == "phone":
        _call_records(t)
    if q.get("call_records") and "cdr_analysis" in t.registry and officer_agent._cdrs(case):
        # A question about the call records: computed over every record before the draft, never read from text.
        t.status("think", "cdr_analysis", _asked(t)[:80])
        t.sources["cdr_analysis"] = t.budget.run("cdr", lambda: _call(t, "cdr_analysis", {"question": _asked(t)}),
                                                 t.cfg.cdr_budget_s, fallback={"error": "the computation did not finish "
                                                                                       "in time - ask again"})
        t.agent("R8 CDR analyst", "computed over the CDRs on the board" if isinstance(t.sources["cdr_analysis"], dict)
                and not t.sources["cdr_analysis"].get("error") else "could not compute it")
    seeds = net.seeds()
    together = len(seeds) >= 2 and (shape in ("link", "what_next", "phone") or q.get("scope") == "connection"
                                    or q.get("whole_picture") or len(set(q["people"]) & set(seeds)) >= 2)
    circle = seeds and (shape in ("link", "count", "what_next") or q.get("scope") == "connection"
                        or q.get("type") in ("associates", "connection"))
    if circle:
        people = _people_of_the_case(case)
        if people:
            t.sources["people_of_the_case"] = people
    if together or circle:
        # How the records tie the targets to each other (several searched together) and each target's direct
        # connections with how they are related - every one of them, not the few the board's facts name.
        group = _targets_together(case, net, every=len(seeds) == 1)
        if group:
            t.sources["how_the_targets_connect" if len(seeds) >= 2 else "direct_connections_of_the_target"] = group
    if len(q["people"]) >= 2 and (shape == "link" or q.get("scope") == "connection"):
        # How two people are connected: the routes between them and what they share.
        pair = {"a": net.name(q["people"][0]), "b": net.name(q["people"][1])}
        for tool in ("paths", "compare"):
            if tool in t.registry:
                t.sources[tool] = _call(t, tool, pair)
    t.agent("Case briefer", f"context pack at board v{t.pack['board_version']}: {len(t.pack['matched_facts'])} matched "
                            f"fact(s), {len(t.hits)} knowledge entr(ies), sources: {', '.join(t.sources) or 'none'}"
            + (f"; missing: {'; '.join(t.pack['missing'][:2])}" if t.pack["missing"] else ""))
    # Sherlock's quick view runs alongside, when his judgment helps (the model's reading says so).
    if q.get("wants_view") or q.get("opinion") or shape in ("person", "what_next"):
        t.status("view")
        t.view_started = time.monotonic()
        read = case_reader.ensure(t.case, t.net)
        t.view_future = t.budget.submit(lambda: quick_view.ask(t.llm, _asked(t), t.language, t.pack, case=t.case,
                                                                  people=[*(q.get("people") or []), *t.net.seeds()],
                                                                  read=read))
    return {}


def _call(t: Turn, tool: str, args: dict[str, str]) -> Any:
    """One toolbox call. A bad argument is the caller's to correct, never a crash."""
    from sherlocks.linkgraph.investigator import _trim

    try:
        with t.case.batch("O1 Officer agent", "research"):       # what it brings onto the board is one event
            got = t.registry[tool][1]({k: str(v) for k, v in args.items() if v not in (None, "")})
            # A computed CDR answer is a table the officer asked for, a document is read whole: both keep more room.
            return _trim(got, 9000 if tool == "cdr_analysis" else 12000 if tool == "read_document" or tool.startswith("fetch_")
                         else 2500)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


# -- O1 ------------------------------------------------------------------------------------


HOLDING = {"en": "**This needs more time than usual - my full answer will follow here in a moment.** Meanwhile, from the "
                  "case board:",
           "roman": "**Is mein mamool se zyada waqt lag raha hai - poora jawab thori der mein yahin aayega.** Tab tak case "
                    "board se:",
           "ur": "**اس میں معمول سے زیادہ وقت لگ رہا ہے - مکمل جواب تھوڑی دیر میں یہیں آئے گا۔** تب تک کیس بورڈ سے:"}
MORE_ON_BOARD = {"en": "(+{n} more on the case board)", "roman": "(+{n} mazeed case board par)", "ur": "(+{n} مزید کیس بورڈ پر)"}
NOT_FINISHED = {"en": "I could not finish this answer in time - please ask it again.",
                "roman": "Main yeh jawab waqt par mukammal nahi kar saka - baraye meharbani dobara poochein.",
                "ur": "میں یہ جواب وقت پر مکمل نہیں کر سکا - براہ کرم دوبارہ پوچھیں۔"}
VERIFY = {"en": "Please verify:", "roman": "Baraye meharbani tasdeeq karein:", "ur": "براہ کرم تصدیق کریں:"}


def _draft(state: TurnState) -> dict[str, Any]:
    from sherlocks.linkgraph.conversation import render_facts
    from sherlocks.linkgraph.presenter import present
    from sherlocks.linkgraph.sherlock_team import FOLLOW_HEAD, validate

    t = state["turn"]
    q = t.query or {}
    t.status("think")
    last = t.rounds + 1 >= officer_agent.ROUNDS or t.budget.left() < t.cfg.draft_budget_s + 2.0
    data = officer_agent.payload(
        message=_asked(t), ask_=q, net=t.net, case=t.case, pack=t.pack, cards=t.cards, facts=t.facts,
        computed_words=render_facts(t.facts, t.language) if t.facts is not None else None, hits=t.hits,
        sources=t.sources, findings=officer_agent.findings_about(t.case, q.get("people") or []),
        tools={name: desc for name, (desc, _) in t.registry.items()}, looked=t.looked, last_round=last,
        patterns=officer_agent.graph_patterns(t.tools, t.net, q.get("people") or []))

    def late(d: dict[str, Any] | None) -> None:
        # The officer was told a fuller answer follows: one always does - the answer, with what the checker found
        # flagged as in any reply, or a line saying it could not be finished. Never silence.
        if not officer_agent.filled(d):
            t.follow(FOLLOW_HEAD[t.language] + "\n" + NOT_FINISHED[t.language], "draft")
            return
        d = officer_agent.normalise(d, t.case.known_ids(), officer_agent.systems_in(data))
        text = shapes.render(shapes.fill_missing(d, q.get("shape") or "other"), q.get("shape") or "other", t.language,
                             depth=q.get("depth") or "detailed")
        check = validate(t.case, t.net, {"answer": text}, drafted_from=data)
        flagged = "" if check["ok"] else "\n⚠ " + VERIFY[t.language] + " " + " · ".join(check["issues"][:3])
        t.follow(FOLLOW_HEAD[t.language] + "\n" + present(text, language=t.language, case=t.case, answer_first=False)
                 + flagged, "draft")

    def drafted() -> dict[str, Any] | None:
        try:
            return officer_agent.draft(t.llm, t.language, data)
        except Exception as exc:  # noqa: BLE001 - a failed late draft still tells the officer
            logger.info("Officer agent draft failed: %s", exc)
            return None

    t.given = data
    got = t.budget.run("draft", drafted, t.cfg.draft_budget_s, fallback=None, on_late=late)
    t.rounds += 1
    if got is None and t.draft is not None and (officer_agent.filled(t.draft) or t.draft.get("answered")):
        # The draft after the lookups is late: the first draft already answered, so it goes
        # out now and the fuller one follows.
        t.draft["lookups"] = []
        t.route = "desk"
        return {}
    if got is None:
        # No draft in time: the rules answer from the board now, and the model's answer follows
        # when it is ready - a first draft that said "not known" before the lookups came back
        # is never sent as the answer.
        t.route, t.rules_llm = "rules", None
        return {}
    t.draft = officer_agent.normalise(got, t.case.known_ids(), officer_agent.systems_in(data))
    wanted = [c for c in (t.draft.get("lookups") or []) if c["tool"] in t.registry][:officer_agent.MAX_LOOKUPS]
    if not wanted and not last and not t.planned and officer_agent.needs_live(t.draft) \
            and t.budget.left() > t.cfg.draft_budget_s:
        wanted = _plan_live(t)                         # the board did not hold it: the API agent picks the calls
    t.draft["lookups"] = wanted
    more = bool(wanted) and got is not None and not last and t.budget.left() > 4.0
    t.agent("Officer agent", ("answered from the board" if t.draft.get("answered") else "the board does not hold it all")
            + (f"; needs: {', '.join(c['tool'] for c in wanted)}" if more else ""))
    t.route = "lookup" if more else "desk"
    return {}


PLAN_BUDGET_S = 30.0


def _plan_live(t: Any) -> list[dict[str, Any]]:
    """The draft could not answer all of it from the board and named no lookup: the API agent picks the live calls
    to the police systems that would bring what is missing (one short model call), from the people and FIRs of
    this case."""
    from sherlocks.linkgraph.investigator import _LIVE_TOOLS

    live = {name: desc for name, (desc, _) in t.registry.items() if name in _LIVE_TOOLS}
    if not live or t.tools is None or t.tools.live_left <= 0 or t.llm is None:
        return []
    t.planned = True
    net, q = t.net, t.query or {}
    asked = [p for p in q.get("people") or [] if p in net.people]
    around = [o for s in net.seeds() for o in net.G.neighbors(s)]
    pids = list(dict.fromkeys([*asked, *net.seeds(), *around]))[:12]
    people = [{"name": net.name(p), "phones": (net.data(p).get("phones") or [])[:3], "cnic": net.data(p).get("cnic"),
               "role": t.case.roles.get(p) or ("target" if net.data(p).get("seed") else "")} for p in pids]
    firs = list({str(f.get("label")): {"fir": f.get("label"), "ps_id": f.get("ps_id"), "role": f.get("role")}
                 for p in pids for f in net.data(p).get("firs") or [] if f.get("label")}.values())
    called = [{"tool": d["tool"], "args": d["args"]} for d in t.looked]
    got = t.budget.run("plan", lambda: officer_agent.plan_live(t.llm, message=_asked(t), draft=t.draft, tools=live,
                                                               people=people, firs=firs, called=called),
                       PLAN_BUDGET_S, fallback=[]) or []
    t.agent("API agent", ("the board does not hold it all - calling " + ", ".join(c["tool"] for c in got)) if got
            else "the board does not hold it all; no police system would hold the rest")
    return got


def _lookup(state: TurnState) -> dict[str, Any]:
    """The lookups O1 named: any tool of the shared toolbox; the live ones are the API agent."""
    from sherlocks.linkgraph import officer as chain
    from sherlocks.linkgraph.investigator import _LIVE_TOOLS, AGENT_OF, _args_text, _summary
    from sherlocks.linkgraph.knowledge import knowledge

    t = state["turn"]
    calls = t.draft.get("lookups") or []
    live = [c for c in calls if c["tool"] in _LIVE_TOOLS]
    t.status("research" if live else "board")
    for c in calls:
        t.status("think", c["tool"], _args_text(t.net, c.get("args") or {}))

    whole_case = not (t.query or {}).get("people")       # the reading: the question is not about one person

    def one(c: dict[str, Any]) -> dict[str, Any]:
        args = dict(c.get("args") or {})
        result = _call(t, c["tool"], args)
        if whole_case and args.get("who") and _nothing(result):
            wider = _call(t, c["tool"], {k: v for k, v in args.items() if k != "who"})
            if not _nothing(wider):                    # asked for the whole case, so looked up for the whole case
                args, result = {k: v for k, v in args.items() if k != "who"}, wider
        return {"tool": c["tool"], "args": args, "result": result}

    def run_all() -> list[dict[str, Any]]:
        return [f.result() for f in [t.budget.submit(lambda c=c: one(c)) for c in calls]]

    def late(done: list[dict[str, Any]] | None) -> None:
        useful = [d for d in done or [] if not (isinstance(d["result"], dict) and (d["result"].get("error")
                                                                             or d["result"].get("empty")))]
        if useful and t.on_late is not None:
            t.on_late()                                # what it brought is on the board for the next question

    done = t.budget.run("tools", run_all, t.cfg.tools_budget_s, fallback=[], on_late=late) or []
    for d in done:
        t.looked.append(d)
        label = chain.checked_tool(d["tool"], _args_text(t.net, d["args"]), t.language)
        t.checked.append(label)
        if d["tool"] in _LIVE_TOOLS:
            t.researched.append(label)
        emit({"type": "step", "tool": d["tool"], "agent": AGENT_OF.get(d["tool"], "Officer agent"), "args": d["args"],
              "args_text": _args_text(t.net, d["args"]), "thought": next((c.get("why") or "" for c in calls
                                                                         if c["tool"] == d["tool"]), ""),
              "summary": _summary(d["tool"], d["result"]) if isinstance(d["result"], dict) else "",
              "result": d["result"], "live": d["tool"] in _LIVE_TOOLS})
    if live and done:
        t.agent("API agent", "called at run time: " + ", ".join(d["tool"] for d in done if d["tool"] in _LIVE_TOOLS))
    if not done and "tools" not in t.budget.late:
        t.checked.append(chain.checked_item("no_call", t.language))
    if done:                                           # the board changed: the knowledge is read again
        kb = knowledge(t.graph, t.case, t.net)
        t.hits = kb.search(_asked(t), people=(t.query or {}).get("people"), k=30)
    t.route = "draft"
    return {}


def _nothing(result: Any) -> bool:
    """A tool result that holds nothing: an error, an explicit empty, a zero count."""
    if not isinstance(result, dict):
        return not result
    return bool(result.get("error") or result.get("empty")) or result.get("count") == 0


# -- the desks, O2, O5 ---------------------------------------------------------------------


def _desk(state: TurnState) -> dict[str, Any]:
    """Sherlock's view, then at most one extra item: a finding from the Insight desk or
    the top approved question from the Gatekeeper. Critical findings go first."""
    from sherlocks.evidence.question_desk import Gatekeeper
    from sherlocks.evidence.questioner import ask_gaps
    from sherlocks.evidence.trust import conflict_lines, conflicts
    from sherlocks.linkgraph import officer as chain
    from sherlocks.linkgraph.conversation import (
        NEWS_LINE,
        RESEARCH_LINE,
        briefing,
        compose,
        render_facts,
    )
    from sherlocks.linkgraph.presenter import VIEW_LABEL
    from sherlocks.linkgraph.sherlock_team import STILL_WORKING, _case_summary

    t = state["turn"]
    case, net, q = t.case, t.net, t.query
    if t.intent == "question":
        shape = q.get("shape") or "other"
        d = t.draft or {}
        if officer_agent.filled(d) or d.get("answered"):
            t.dropped = shapes.complete(d, shape)       # put back below, marked - never silently gone
            t.draft = shapes.fill_missing(d, shape)
            t.reply = shapes.render(t.draft, shape, t.language, depth=q.get("depth") or "detailed")
        else:
            # None of the agents has it: said plainly, with every place checked - never a guess.
            if not (t.agents is not None or t.backend is not None) or t.live_calls <= 0:
                t.checked.append(chain.checked_item("offline", t.language))
            t.facts = chain.not_found(t.checked, t.names, said=(d.get("first_line") or "").strip())
            t.reply, t.not_found = render_facts(t.facts, t.language), True
            t.agent("Officer agent", "no agent found an answer - checked: " + chain.checked_line(t.checked))
        if (d.get("deeper") and not d.get("answered")) or (t.not_found and q.get("opinion")):
            # The deep investigation never runs inside a reply: Sherlock takes it up in the background.
            t.deeper = True
            asked = [*(case.dialog.get("asked_of_sherlock") or [])[-9:], {"turn": t.turn_no, "question": t.message[:300]}]
            case.dialog["asked_of_sherlock"] = asked
            case.plan("sherlock", f"the officer asked: {t.message[:120]}")
            t.lines.append(LOOKING_INTO[t.language])
        if t.view_future is not None:
            def late_view(v: dict[str, Any] | None) -> None:
                if v:
                    case.add_view(t.message, v["view"], turn=t.turn_no, people=v.get("people"))
                    t.follow(f"{VIEW_LABEL[t.language]}: {v['view']}", "view")

            t.view = t.budget.collect("view", t.view_future, t.cfg.view_budget_s, fallback=None, on_late=late_view,
                                      started=t.view_started)
            if t.view:
                case.add_view(t.message, t.view["view"], turn=t.turn_no, people=t.view.get("people"))
                t.agent("Sherlock", "quick view given, labelled as his view")
            elif "view" in t.budget.late:
                t.agent("Sherlock", "still thinking - his view will follow")
        if q["people"]:
            case.dialog["focus"] = q["people"][:2]
        case.dialog["last_type"] = q["type"]
        if t.researched:
            t.lines.append(RESEARCH_LINE[t.language].format(x=", ".join(t.researched)))
        if any(step in t.budget.late for step in ("draft", "tools")):
            t.lines.append(STILL_WORKING[t.language])
    elif t.intent == "greeting" or not t.reply:
        t.reply, t.writer = compose(language=t.language, intent=t.intent, message=t.message, recorded=t.recorded,
                                    result=None, news=[], question=None, case_summary=_case_summary(case, net),
                                    history=t.history, llm=t.llm, memory=None, facts=None, news_line=None)
    # What is new on the board about the people being discussed - told once.
    news_all, seen_now = briefing(case, case.seen)
    focus = [p for p in case.dialog.get("focus") or [] if p in net.people]
    focus_names = [net.name(p) for p in focus]
    if q is not None and q["type"] == "news":
        case.seen = seen_now
    else:
        told = set(case.dialog.get("told") or [])
        relevant = [line for line in news_all if any(n and n in line for n in focus_names)
                    and not set(re.findall(r"\[([DFL]\d+)\]", line)) & told][:2]
        case.dialog["told"] = sorted(told | {i for line in relevant for i in re.findall(r"\[([DFL]\d+)\]", line)})
        t.lines += relevant
        if len(news_all) > len(relevant) and len(news_all) != case.dialog.get("announced"):
            t.lines.append(NEWS_LINE[t.language].format(n=len(news_all)))
            case.dialog["announced"] = len(news_all)
    # Sources that disagree about what is being discussed: shown, never hidden.
    about = {net.name(p) for p in (q or {}).get("people") or focus}
    on_incident = (q or {}).get("shape") == "incident" or bool((q or {}).get("this_case")) or any(
        r.startswith("incident ") for r in t.recorded)
    shown = [c for c in conflicts(case, net) if (c["topic"].startswith("incident") and on_incident) or c.get("person") in about]
    # Each disagreement is told once; again only when the question is about that very thing (the incident, the person).
    told_conflicts = set(case.dialog.get("conflicts_told") or [])

    def conflict_key(c: dict[str, Any]) -> str:
        return c["topic"] + "|" + "|".join(str(v.get("value")) for v in c["values"][:2])

    asked_about = (q or {}).get("shape") == "incident" or any(c.get("person") in {net.name(p) for p in (q or {}).get("people") or []}
                                                             for c in shown)
    shown = [c for c in shown if asked_about or conflict_key(c) not in told_conflicts]
    case.dialog["conflicts_told"] = sorted(told_conflicts | {conflict_key(c) for c in shown})
    if shown:
        t.lines += conflict_lines(shown, t.language)
        t.agent("Answer checker", f"{len(shown)} conflict(s) between sources shown, not hidden")
    # One extra item: a critical finding first; else the question; else another finding.
    ask_gaps(case, t.graph, rules=False)
    desk = Gatekeeper(case, t.graph, rules=False)
    swept: list[dict[str, Any]] | None = []
    if t.sweep_future is not None:
        swept = t.budget.collect("sweep", t.sweep_future, 4.0, fallback=None)
        for hit in swept or []:
            desk._take(hit["q"], {"take": True, "answer": hit["answer"], "turn": hit["turn"]})
            case.log_desk({"key": hit["q"]["key"], "text": hit["q"]["text"][:200], "by": "Q1 Gatekeeper",
                           "decision": "withdrawn", "answer": hit["answer"], "turn": hit["turn"],
                           "reason": f"Already answered in turn {hit['turn']}: \"{hit['answer'][:160]}\""})
    pending = case.dialog.get("pending_correction")
    finding = insight_desk.pick(case, focus)
    # No answer from the Gatekeeper's reading of the chat: nothing new is asked this turn.
    to_ask = None if pending or swept is None else desk.pick(intent=t.intent)
    if finding is not None and (finding["severity"] == "critical" or to_ask is None):
        t.finding, to_ask = finding, None
        t.agent("Insight desk", f"{finding['ref']} ({finding['severity']}, {finding['kind']}): not told before - passed")
    if to_ask:
        t.to_ask = to_ask
        t.agent("Questioner", to_ask["text"])
        t.agent("Gatekeeper", f"approved {to_ask['key']} (checked again before asking)")
    return {}


def _check(state: TurnState) -> dict[str, Any]:
    """O2 Answer checker: code first (sources, quotes, numbers, the question after the
    answer, every section of the shape); the model only for contradictions; one revision."""
    from sherlocks.linkgraph.sherlock_team import (
        QUESTIONS_TEXT,
        drop_quotes,
        unverified_quotes,
        validate,
    )

    t = state["turn"]
    case, net, q = t.case, t.net, t.query
    t.status("check")
    final = {"type": "final", "hypotheses": [], "key_people": [], "next_steps": [], "suggestions": [], "findings": [],
             "confident": True, "model": getattr(t.llm, "model", None), "answer": t.reply, "language": t.language,
             "intent": t.intent, "query": q, "facts": t.facts, "investigation": None}
    if t.facts is not None:
        final["key_people"] = [{"id": i["pid"], "name": net.name(i["pid"])} for i in t.facts["items"] if i.get("pid")][:12]
    if not final["key_people"]:
        final["key_people"] = [{"id": p, "name": net.name(p)} for p in (q or {}).get("people") or []]
    if t.to_ask:
        final["asking"] = {**t.to_ask, "asked_text": QUESTIONS_TEXT(t.to_ask, t.language)}
    model = t.llm if t.intent == "question" and not t.not_found else None
    records = officer_agent.evidence_digest(t.given) if t.given else None
    asked = {**final, "facts": None} if (t.draft is not None and (t.facts or {}).get("empty")) else final
    check = t.budget.run("check", lambda: validate(case, net, asked, model, given=records, drafted_from=t.given), t.cfg.check_budget_s,
                         fallback=None)
    if check is None:                                  # the model check ran over: the rules alone decide
        check = {**validate(case, net, asked, None, drafted_from=t.given), "checked": "rules (model check over budget)"}
    if t.intent == "question" and t.draft is not None and not t.not_found:
        # Completeness: every section of the shape is filled or marked. One the draft dropped
        # was put back marked "not checked yet", and the check says so.
        check["rules"]["complete"] = not t.dropped
        if t.dropped:
            check["restored_sections"] = t.dropped
    if not check["ok"] and t.intent == "question" and not t.not_found and t.given and t.budget.left() > 5.0:
        # Back to O1 once, with the reasons: he drafts again from the same records, into the
        # same shape - the answer keeps its sections and its sources.
        again = {**t.given, "tools": {}, "fix_these_problems": check["issues"][:4],
                 "note": "Your draft was checked and sent back. Fix the problems from the RECORDS in GIVEN; change nothing else."}
        fixed = t.budget.run("revise", lambda: officer_agent.draft(t.llm, t.language, again),
                             min(t.cfg.draft_budget_s, t.budget.left() - 1.0), fallback=None)
        if fixed and (officer_agent.filled(fixed) or fixed.get("answered")):
            shape = (q or {}).get("shape") or "other"
            t.draft = shapes.fill_missing(officer_agent.normalise(fixed, case.known_ids(), officer_agent.systems_in(t.given)), shape)
            final["answer"] = shapes.render(t.draft, shape, t.language, depth=(q or {}).get("depth") or "detailed")
            t.agent("Validator", "sent the reply back once: " + "; ".join(check["issues"]))
            again_asked = {**final, "facts": None} if (t.facts or {}).get("empty") else final
            check = {**validate(case, net, again_asked, None, drafted_from=t.given), "revised": True,
                     "first_issues": check["issues"]}
    if t.finding is not None and not validate(case, net, {"answer": t.finding["text"]})["ok"]:
        t.agent("Validator", f"finding {t.finding['ref']} held back: it cites an entry that is not on the board or is out of date")
        t.finding = None
    bad = unverified_quotes(final["answer"], case)
    if bad:                                            # never shown as evidence, whatever else the check found
        final["answer"] = drop_quotes(final["answer"], bad)
    if not check["ok"] and t.intent == "question":
        t.lines.append(IN_DOUBT[t.language])
    t.agent("Validator", "checked: " + ("consistent with the records and your statements" if check["ok"]
                                        else "doubts remain - " + "; ".join(check["issues"])), check=check)
    t.final, t.check = final, check
    return {}


def _present(state: TurnState) -> dict[str, Any]:
    """O5 Presenter: the answer first, the shape under headings, evidence quoted, Sherlock's
    view apart, then one finding or one question - and the turn is closed on the board."""
    from sherlocks.evidence.question_desk import Gatekeeper
    from sherlocks.linkgraph.conversation import _REASK
    from sherlocks.linkgraph.presenter import layout
    from sherlocks.linkgraph.presenter import translate_line as _translate
    from sherlocks.linkgraph.sherlock_team import QUESTIONS_TEXT

    t = state["turn"]
    case, final, check, q = t.case, t.final, t.check, t.query
    t.status("present")
    ask_line = None
    if t.to_ask:
        ask_line = QUESTIONS_TEXT(t.to_ask, t.language)
        if t.to_ask.get("times", 0) >= 1:
            ask_line = _REASK[t.language] + ask_line
    finding_line = None
    if t.finding:
        text = t.finding["text"]
        if len(text) > 340:                          # one finding, briefly: the rest is on the case board
            text = text[:340].rsplit(" ", 1)[0] + f"… [{t.finding['ref']}]"
        if t.language != "en" and t.budget.left() > 3.0:
            # The team writes its findings in English: told in the officer's language (ids and names kept).
            said = t.budget.run("translate", lambda: _translate(t.llm, text, t.language), 5.0, fallback=None)
            text = said or text
        finding_line = f"{insight_desk.LABEL[t.language]}: {text}"
    final["answer"] = layout(final["answer"], language=t.language, case=case, llm=t.llm if t.intent == "question" else None,
                             lines=t.lines, view=(t.view or {}).get("view"), finding=finding_line, question=ask_line,
                             question_priority=(t.to_ask or {}).get("priority") or "orange",
                             evidence=t.intent == "question" and not t.not_found,
                             facts=t.facts if t.intent == "question" and not t.not_found else None)
    t.agent("Presenter", f"laid out in {LANG_LABEL[t.language]}"
            + (f", {len((t.draft or {}).get('sections') or [])} section(s)" if t.draft else ""))
    check["asked"] = t.to_ask["key"] if t.to_ask else None
    if t.to_ask:
        Gatekeeper(case, t.graph).asked(t.to_ask)
    if t.finding:
        insight_desk.told(case, t.finding, t.turn_no)
        check["told"] = t.finding["ref"]
    check["intent"] = t.intent
    final.update({
        "checks": check, "questions": case.open_questions(), "asking": t.to_ask, "finding": t.finding,
        "citations": case.citations_in(final["answer"]), "shape": (q or {}).get("shape"),
        "sections": [{"name": s["name"], "status": s.get("status", "filled")} for s in (t.draft or {}).get("sections") or []],
        "lookups": [{"tool": d["tool"], "args": d["args"]} for d in t.looked],
        "timing": {**t.budget.timing, "total": int((time.monotonic() - t.budget.started) * 1000)},
        "model_calls": t.llm.calls, "late": list(dict.fromkeys(t.budget.late)),
        "followup_pending": bool(t.budget.late) or t.deeper,
        "board_version": (t.pack or {}).get("board_version", case.version)})
    logger.info("Chat turn (graph): %s ms, %d model call(s), steps %s%s", final["timing"]["total"], final["model_calls"],
                t.budget.timing, f", late: {final['late']}" if final["late"] else "")
    case.add_turn(t.message, final["answer"], check)
    emit(final)
    if t.intent == "question" and t.llm is not None:
        # What the reply noticed, kept on the case board - only what rests on the case's records. In the
        # background: the officer has the reply already.
        import threading

        from sherlocks.team import hint_keeper

        def keep() -> None:
            try:
                if hint_keeper.keep(case, t.message, final["answer"], (t.view or {}).get("view") or "", t.raw_llm or t.llm,
                                    turn=t.turn_no) and t.on_late is not None:
                    t.on_late()
            except Exception:                  # a hint is never worth a failed turn
                logger.exception("Hint keeper failed")

        threading.Thread(target=keep, name="hint-keeper", daemon=True).start()
    return {}


# -- the graph -----------------------------------------------------------------------------


def _route(state: TurnState) -> str:
    return state["turn"].route


def _did(node: str, t: Turn) -> dict[str, Any]:
    """For the trace: what a node did, and what it hands to the next one (``passes``)."""
    from sherlocks.linkgraph.investigator import _args_text, _summary

    q = t.query or {}
    reading = (t.ask or {}).get("reading") or {}
    draft = t.draft or {}
    names = [t.net.name(p) for p in q.get("people") or []]
    if node == "read":
        if t.route == "rules":
            return {"decided": "no model reading of the message", "next": "rules",
                    "passes": "the message as it is: the rules answer it"}
        does = ("asks and tells" if t.asked and t.ask.get("tells") else "asks" if t.asked else "tells")
        return {"reading": {k: reading.get(k) for k in ("act", "asks_something", "states_something", "type", "depth",
                                                         "scope", "shape", "asks_for", "whole_picture", "wants_view",
                                                         "about_new_case", "language") if k in reading},
                "decided": f"the message {does}; language {t.language}", "next": t.route,
                "passes": ("what the officer states, to be recorded" if t.route == "record"
                           else "a question to answer")}
    if node == "record":
        return {"recorded": list(t.recorded), "next": t.route,
                "passes": (f"{len(t.recorded)} thing(s) recorded on the board" if t.recorded else "nothing to record")
                          + ("; now the question" if t.route == "brief" else "")}
    if node == "brief":
        return {"question_read_as": {"type": q.get("type"), "depth": q.get("depth"), "scope": q.get("scope"),
                                     "shape": q.get("shape"), "asks_for": q.get("asks_for"), "people": names},
                "dossiers": [c.get("name") for c in t.cards],
                "computed": ({"type": t.facts.get("type"), "count": t.facts.get("count")} if t.facts else None),
                "sources_fetched": {name: ("nothing" if _nothing(got) else "found") for name, got in t.sources.items()},
                "context_pack": ({"board_version": t.pack.get("board_version"),
                                  "matched_facts": len(t.pack.get("matched_facts") or [])} if t.pack else None),
                "sherlock_view_started": t.view_future is not None,
                "passes": f"context for a {q.get('depth')} {q.get('shape')} answer about {', '.join(names) or 'the case'}: "
                          f"{len(t.cards)} dossier(s), {len(t.sources)} source(s), {len(t.hits)} matching entr(ies)"}
    if node == "draft":
        if t.route == "rules":
            return {"round": t.rounds, "decided": "no draft within the time allowed: the rules answer now, the "
                                                  "model's answer follows when ready", "next": "rules",
                    "passes": "the question, to the rule answer"}
        wanted = [f"{c['tool']}({_args_text(t.net, c.get('args') or {})})" for c in draft.get("lookups") or []]
        return {"round": t.rounds, "given": {k: (len(v) if isinstance(v, list | dict | str) else v) for k, v in (t.given or {}).items()},
                "first_line": draft.get("first_line"), "answered": draft.get("answered"), "deeper": draft.get("deeper"),
                "sections": [{"name": s.get("name"), "status": s.get("status"), "chars": len(s.get("body") or "")}
                             for s in draft.get("sections") or []],
                "lookups_asked": [{"tool": c["tool"], "args": c.get("args"), "why": c.get("why")} for c in draft.get("lookups") or []],
                "next": t.route,
                "passes": ("asks for: " + ", ".join(wanted)) if t.route == "lookup" else
                          ("a draft that answers" if draft.get("answered") else "a draft that does not hold the answer")}
    if node == "lookup":
        done = [{"tool": d["tool"], "args": d.get("args"), "args_text": _args_text(t.net, d.get("args") or {}),
                 "found": not _nothing(d.get("result")),
                 "summary": (_summary(d["tool"], d["result"]) if isinstance(d.get("result"), dict) else "")[:300]}
                for d in t.looked]
        return {"lookups": done, "slow": "tools" in t.budget.late, "next": "draft",
                "passes": "; ".join(f"{d['tool']}: {'found' if d['found'] else 'nothing'}" for d in done) or "nothing came back in time"}
    if node == "desk":
        return {"not_found": t.not_found, "checked": list(t.checked)[:12], "sent_to_sherlock": t.deeper,
                "sherlock_view": (t.view or {}).get("view") if isinstance(t.view, dict) else None,
                "finding_to_tell": ({k: t.finding.get(k) for k in ("ref", "kind", "severity")} if t.finding else None),
                "question_to_ask": ({"key": t.to_ask.get("key"), "text": t.to_ask.get("text")} if t.to_ask else None),
                "reply_written_by": t.writer, "sections_put_back": list(t.dropped),
                "passes": "the reply" + (" (not found, with what was checked)" if t.not_found else "")
                          + (f", finding {t.finding['ref']}" if t.finding else "")
                          + (f", question {t.to_ask['key']}" if t.to_ask else "")}
    if node == "check":
        c = t.check or {}
        return {"ok": c.get("ok"), "issues": c.get("issues"), "sent_back_once": c.get("revised"),
                "first_issues": c.get("first_issues"), "rules": c.get("rules"),
                "passes": "checked: " + ("holds against the records" if c.get("ok") else "problems: "
                                         + "; ".join(str(i)[:120] for i in (c.get("issues") or [])[:3]))}
    if node == "present":
        return {"language": t.language, "answer_chars": len((t.final or {}).get("answer") or ""),
                "asked": (t.to_ask or {}).get("key"), "told": (t.finding or {}).get("ref"), "passes": "the reply"}
    if node == "rules":
        f = t.final or {}
        return {"intent": f.get("intent") or t.intent, "answer_chars": len(f.get("answer") or ""),
                "passes": "the rule-based reply"}
    return {}


def _traced(name: str, node: Any) -> Any:
    """The node, with its work written to the turn's trace. The trace never changes a turn."""
    def run(state: TurnState) -> dict[str, Any]:
        t = state["turn"]
        step = t.trace.begin(name) if t.trace is not None else None
        try:
            return node(state)
        finally:
            if step is not None:
                try:
                    t.trace.end(step, _did(name, t))
                except Exception:  # tracing is best-effort
                    logger.debug("Trace of %s failed", name, exc_info=True)
                    t.trace.end(step, None)
    return run


def build() -> Any:
    g = StateGraph(TurnState)
    for name, node in (("read", _read), ("rules", _rules), ("record", _record), ("brief", _brief), ("draft", _draft),
                       ("lookup", _lookup), ("desk", _desk), ("check", _check), ("present", _present)):
        g.add_node(name, _traced(name, node))
    g.add_edge(START, "read")
    g.add_conditional_edges("read", _route, {"rules": "rules", "record": "record", "brief": "brief"})
    g.add_conditional_edges("record", _route, {"rules": "rules", "desk": "desk", "brief": "brief"})
    g.add_edge("brief", "draft")
    g.add_conditional_edges("draft", _route, {"rules": "rules", "lookup": "lookup", "desk": "desk"})
    g.add_edge("lookup", "draft")
    g.add_edge("desk", "check")
    g.add_edge("check", "present")
    g.add_edge("present", END)
    g.add_edge("rules", END)
    return g.compile()


_GRAPH: Any = None


def officer_graph() -> Any:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build()
    return _GRAPH


def stream_turn(graph: dict[str, Any], question: str, *, llm: Any = None, history: list[dict] | None = None,
                case: CaseFile | None = None, agents: Any = None, backend: Any = None, live_calls: int = 0,
                officer: dict[str, Any] | None = None, on_late: Any = None, settings: Any = None) -> Iterator[dict[str, Any]]:
    """Run one turn through the graph, yielding its events as they happen."""
    from sherlocks.linkgraph.budget import Budget, CountingLlm
    from sherlocks.linkgraph.conversation import detect_language
    from sherlocks.settings import ChatSettings
    from sherlocks.team.trace import Trace

    case = case if case is not None else CaseFile()
    message = (question or "").strip()
    cfg = getattr(settings or getattr(agents, "settings", None), "chat", None) or ChatSettings()
    trace = Trace(len(case.conversation) + 1, message, detect_language(message))
    turn = Turn(graph=graph, message=message, case=case, net=PersonNetwork(graph),
                llm=CountingLlm(llm, on_call=trace.call) if llm is not None else None, raw_llm=llm,
                history=history or [{"q": x["q"], "a": x["a"]} for x in case.conversation[-4:]], agents=agents,
                backend=backend, live_calls=live_calls, officer=officer, on_late=on_late, settings=settings, cfg=cfg,
                budget=Budget(cfg.reply_budget_s), language=detect_language(message),
                turn_no=len(case.conversation) + 1, trace=trace)
    final: dict[str, Any] = {}
    try:
        for event in officer_graph().stream({"turn": turn}, stream_mode="custom"):
            if isinstance(event, dict) and event.get("type") == "final":
                final = event
            yield event
    finally:
        # Kept on the board whatever happened to the turn; work that finishes after the reply
        # (a late draft, Sherlock's view) is added to the same trace.
        try:
            trace.data["language"] = turn.language
            case.add_trace(trace.close(final))
        except Exception:  # tracing is best-effort
            logger.debug("Trace of the turn was not kept", exc_info=True)
