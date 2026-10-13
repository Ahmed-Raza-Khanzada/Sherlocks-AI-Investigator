"""The Report team (P1-P6), as a LangGraph graph.

Writing the report is a different job from solving the case, so it has its own team. It
reads the board and writes; it never judges and never looks anything up.

    freeze (P1 Report editor: one board version)
      -> evidence (P2) | statements (P3) | analysis (P4)        three writers side by side
      -> editor (P1: part F, the annex, the links between ids, the index by person)
      -> summary (P5: the seven-part executive summary, written LAST, from the finished parts)
      -> check (P6) --a failed summary goes back to P5 once--> summary -> check
      -> build (P1: the report kept with its board version; internal and file versions)

The parts: **A** overview and people · **B** evidence (B1 FIRs, B2 medical, B3 forensic and
lab, B4 recoveries and site plan, B5 police system records, B6 phones and presence, B7
evidence by topic) · **C** statements · **D** analysis · **E** one timeline · **F** open
points and next steps · the **annex**.

All parts come from the same frozen board version. The summary is written last, so it can
claim nothing the body does not show: a sentence that cites no real id, or an id the body
does not use, is dropped. Without the model every part is built from the board with
templates, and the cover says so. The team's writes never wake the Sherlock team.
"""

from __future__ import annotations

import logging
import operator
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import Field, field_validator

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.linkgraph.relevance import the_new_case
from sherlocks.team import runtime  # noqa: F401 - switches tracing off before a graph is built
from sherlocks.team.runtime import NEW_CASE, PLAIN, Typed, ask, dumps, real_refs

logger = logging.getLogger(__name__)
ID = re.compile(r"\b([GDFLHQWXK]\d{1,4})\b")
PARTS = ("A", "B", "C", "D", "E", "F")
SUMMARY = ("brief", "target", "connections", "past_cases", "established", "disputed", "leads", "stands", "unclear",
           "next_steps")
SUMMARY_TITLE = {"brief": "The case in brief", "target": "The target", "connections": "Connections",
                 "past_cases": "Earlier cases on record", "established": "What the evidence establishes",
                 "disputed": "What is disputed", "leads": "Leads to check", "stands": "Where the case stands",
                 "unclear": "What is not clear yet", "next_steps": "Next steps"}
# The most bullets a heading may hold: the summary is a brief, the parts below it hold the rest.
SUMMARY_MOST = {"brief": 4, "target": 6, "connections": 6, "past_cases": 5, "established": 6, "disputed": 4, "leads": 4,
                "stands": 4, "unclear": 5, "next_steps": 5}
# Said by code - never by the model - while the officer has not told us what the case is.
NOT_DECLARED = ("The investigating officer has not yet said what case is being investigated. This summary therefore "
                "says nothing about that case: it sets out what the records hold about the target, the people "
                "connected to them and their earlier cases.")
NOT_WEIGHED = "Not assessed: no case has been described yet, so no line of inquiry is weighed."
MEDICAL, LAB, SCENE = ("mlo", "postmortem"), ("dna", "chemical", "forensic"), ("recovery_memo", "site_plan")
# The report, in the order it is read - and the member of the Report team that writes each section.
# Everything else (the evidence register, the systems checked, Sherlock's working, the references) is annex.
STRUCTURE = (
    ("summary", "Case summary", "P5 Summary writer",
     ("The whole case in short points under headings: the case, the target, connections, earlier cases, what is "
     "established, what is disputed, leads, where it stands, what is unclear, next steps.")),
    ("persons", "Person by person", "P2 Person writer",
     ("The target, then each person related to them: who they are, how they are tied to the target, the FIRs that "
     "name them, the medical and other reports about them, what they said, what the police systems hold.")),
    ("statements", "Who said what", "P3 Statement writer",
     "Each person's recorded statements in order, what changed between them, and what does not fit the evidence."),
    ("relations", "People and their relations", "P4 Relations writer",
     ("Every tie between the people on the case, in plain words, with the record it comes from; the network; the "
     "patterns found across the records.")),
    ("timeline", "Timeline", "P3 Statement writer",
     ("The story of each case, the earliest first: the FIR, the people in it and their roles, what each of them "
      "said, the medical and laboratory reports and what they found, what the investigation did - then the dated "
      "records that belong to no case.")),
    ("contradictions", "Contradictions with the evidence", "P4 Analysis writer",
     "Where two sources disagree, or a statement does not fit a record: both sides, how serious, what would settle it."),
    ("leads", "Hints and leads", "P4 Analysis writer",
     "What looks worth checking and why, the next step for each, and where each stands. Not evidence."),
    ("next_steps", "Next steps", "P1 Report editor",
     "What to do now, in order, the questions still open and the reports awaited."),
)


@dataclass
class ReportRun:
    case: Any
    graph: dict[str, Any]
    net: Any
    llm: Any = None
    run: dict[str, Any] | None = None
    version: int = 0
    base: dict[str, Any] = field(default_factory=dict)        # every section, laid out from the board
    parts: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    summary_by: str = "templates"
    problems: list[str] = field(default_factory=list)
    send_back: list[str] = field(default_factory=list)
    retried: bool = False
    report: dict[str, Any] = field(default_factory=dict)


class ReportState(TypedDict, total=False):
    job: ReportRun
    log: Annotated[list[dict[str, Any]], operator.add]


def _member(name: str, work: Callable[[ReportRun], Any]) -> Callable[[ReportState], dict[str, Any]]:
    def node(state: ReportState) -> dict[str, Any]:
        started = time.monotonic()
        did = work(state["job"])
        return {"log": [{"agent": name, "did": did, "ms": int((time.monotonic() - started) * 1000)}]}

    return node


def _people(job: ReportRun) -> list[str]:
    """Everyone on the case: the targets, anyone given a role, anyone a claim or a statement is about."""
    case, net = job.case, job.net
    pids = [*net.seeds(), *case.roles, *[c["about_pid"] for c in case.claims.values() if c.get("about_pid")],
            *[w["pid"] for w in case.statements.values() if w.get("pid")]]
    return [p for p in dict.fromkeys(pids) if p in net.people][:40]


# -- P1: freeze ---------------------------------------------------------------------------


def _freeze(job: ReportRun) -> dict[str, Any]:
    from sherlocks.evidence.case_report import assemble

    job.version = job.case.version
    job.base = assemble(job.graph, job.case, llm=None, run=job.run)
    return {"board_version": job.version}


# -- P2 Evidence writer: parts A and B ----------------------------------------------------


def _evidence(job: ReportRun) -> dict[str, Any]:
    from sherlocks.linkgraph.api_router import searched
    from sherlocks.linkgraph.case_queries import hotels
    from sherlocks.team import register

    case, net, base = job.case, job.net, job.base
    rows = register.rows(case)
    cards = case.register.get("cards") or {}
    people = []
    for pid in _people(job):
        d = net.data(pid)
        people.append({"pid": pid, "name": net.name(pid), "role": case.roles.get(pid) or "-", "father": d.get("father_name") or "-",
                       "cnic": d.get("cnic") or "-", "phones": (d.get("phones") or [])[:3],
                       "vehicles": [str(v) for v in (d.get("vehicles") or [])[:3]],
                       "addresses": [str(a) for a in (d.get("addresses") or [])[:2]], "firs": len(d.get("firs") or []),
                       "links": net.G.degree(pid) if pid in net.G else 0})
    job.parts["A"] = {"incident": base.get("incident"), "roles": base.get("roles"), "people": people}

    def of(kinds: tuple[str, ...]) -> list[dict[str, Any]]:
        return [{"doc": d, "record": c["record"], "title": c["title"], "background": bool(c.get("background")),
                 "fields": [{k: f.get(k) for k in ("name", "value", "person", "when", "where", "exhibit", "page", "quote")}
                            for f in c.get("fields") or []]} for d, c in cards.items() if c["record"] in kinds]

    other_firs = []
    for p in people:
        for f in (net.data(p["pid"]).get("firs") or [])[:20]:
            other_firs.append({"person": p["name"], "fir": f.get("label"), "police_station": f.get("police_station") or f.get("ps") or "-",
                               "offence": f.get("offence") or "-", "role": f.get("role") or "-", "status": f.get("status") or "-",
                               "source": f.get("system") or "-"})
    systems = []
    for p in people:
        pid = p["pid"]
        stays = hotels(net, pid, case)
        mine = sorted((c for c in cards.values() if c["record"] == "system" and c.get("person") == pid),
                      key=lambda c: c["system"])
        lines = [{"system": c["system"], "found": c.get("summary") or f"{len(c['fields'])} field(s)", "nothing": bool(c.get("nothing")),
                  "source": ", ".join(x for x in (c.get("call"), c.get("checked")) if x) or "graph run",
                  "background": bool(c.get("background"))} for c in mine]
        if not mine:                                # before S10's first run: what the graph and the lookups say
            found = {str(r.get("system") or ""): r.get("summary") or "on record" for r in net.data(pid).get("records") or []}
            lines = [{"system": s, "found": found.get(s) or status, "nothing": status == "nothing found", "source": "graph run",
                      "background": False} for s, status in sorted(searched(net, case, pid).items())]
        for m in stays.get("at_times") or []:
            lines.append({"system": "hotel_eye", "nothing": False, "source": "against each date", "background": False,
                          "found": f"{m.get('what')}: " + ("; ".join(m.get("stays") or []) if m.get("stays")
                                                           else ("date not on record" if not m.get("day") else "no stay covers it"))})
        systems.append({"person": p["name"], "pid": pid, "lines": lines})
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_topic.setdefault(r["topic"], []).append({k: r.get(k) for k in ("record", "name", "value", "person", "when", "ref", "background")})
    job.parts["B"] = {
        "B1": {"cards": of(("fir",)), "other_firs": other_firs},
        "B2": {"cards": of(MEDICAL)},
        "B3": {"cards": of(LAB), "custody": base.get("custody") or [], "pending": base.get("pending_reports") or [],
               "linked": register.links(case)},
        "B4": {"cards": of(SCENE)},
        "B5": {"systems": systems},
        "B6": {"cdrs": base.get("cdrs") or [], "presence": base.get("presence")},
        "B7": {"by_topic": by_topic},
    }
    return {"people": len(people), "records": len(cards), "systems": sum(len(s["lines"]) for s in systems)}


# -- P3 Statements and timeline writer: parts C and E -------------------------------------


def _statements(job: ReportRun) -> dict[str, Any]:
    from sherlocks.evidence.case_report import _date_key
    from sherlocks.team import register

    case, base = job.case, job.base
    job.parts["C"] = {"recorded": base.get("recorded") or [], "officer": base.get("statements") or [], "qa": base.get("qa") or []}
    events = [{"when": t["when"], "what": t["what"], "ref": t["ref"]} for t in base.get("timeline") or []]
    for r in register.rows(case):
        if r.get("when") and not r.get("background"):
            events.append({"when": str(r["when"]).replace("T", " "), "what": f"{r['card']}: {r['name']} - {r['value']}"[:200],
                           "ref": r["ref"]})
    for w in case.statements.values():
        if w.get("date"):
            events.append({"when": w["date"], "what": f"Statement of {w['speaker']} ({w['kind']}) recorded", "ref": w["id"]})
    seen, timeline = set(), []
    for e in sorted(events, key=lambda e: _date_key(e["when"])):
        key = (e["when"], e["what"])
        if key not in seen:
            seen.add(key)
            timeline.append(e)
    # Sources that give different times for the same event sit side by side.
    conflicts = [{"id": x["id"], "first": x["a"].get("when") or x["a"].get("value"), "second": x["b"].get("when") or x["b"].get("value"),
                  "sources": [x["a"].get("source"), x["b"].get("source")], "text": x["text"]}
                 for x in case.contradictions.values() if x["topic"] == "time"]
    job.parts["E"] = {"timeline": timeline[:300], "conflicting_times": conflicts}
    job.parts["story"] = _story(job, timeline)
    return {"statements": len(job.parts["C"]["recorded"]), "events": len(timeline),
            "cases_in_the_story": len(job.parts["story"]["cases"])}


def _diary_steps(diaries: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Case diaries as steps of the investigation. Diaries open with the same heading every day
    (the accused, the sections): each line gives only what is NEW in that diary, and diaries
    that add nothing are told together in one line instead of the same text again and again."""
    from sherlocks.evidence.case_report import _date_key

    def words(d: dict[str, Any]) -> str:
        return " ".join(str(d.get("remarks") or "").split())

    out: list[dict[str, str]] = []
    seen: list[str] = []
    same: list[dict[str, Any]] = []

    def flush() -> None:
        if same:
            nos = ", ".join(str(d.get("no") or "?") for d in same)
            out.append({"when": f"{same[0].get('date') or '-'}" + (f" to {same[-1].get('date')}" if len(same) > 1 else ""),
                        "what": f"Case diar{'ies' if len(same) > 1 else 'y'} {nos}: nothing new recorded - the same entry "
                                "as the diary before."})
            same.clear()

    for d in sorted(diaries, key=lambda d: _date_key(str(d.get("date") or ""))):
        text = words(d)
        shared = 0
        for earlier in seen:                              # the longest opening it shares with an earlier diary
            n = 0
            for x, y in zip(earlier, text, strict=False):
                if x != y:
                    break
                n += 1
            shared = max(shared, n)
        if shared < len(text):
            shared = text.rfind(" ", 0, shared) + 1       # back to a whole word
        new = text[shared:].strip(" .،۔:-") if shared >= 60 else text
        seen.append(text)
        if text and len(new) < 25:                        # nothing of substance beyond what was already written
            same.append(d)
            continue
        flush()
        out.append({"when": str(d.get("date") or "-"),
                    "what": f"Case diary {d.get('no') or ''}: " + ("... " if shared >= 60 else "") + (new or "no entry")[:240]})
    flush()
    return out


def _fir_of(text: str) -> tuple[str, str] | None:
    m = re.search(r"(\d{1,5})\s*/\s*(\d{2,4})", text or "")
    return (m.group(1).lstrip("0"), m.group(2)[-2:]) if m else None


def _story(job: ReportRun, timeline: list[dict[str, Any]]) -> dict[str, Any]:
    """The timeline as the story of each case, the earliest first: the FIR, the people in it
    and their roles, what each of them said, the medical and laboratory reports and what they
    found, then what the investigation did - and after the cases, the dated records that
    belong to none of them (hotel stays, uploads)."""
    from sherlocks.evidence.case_report import _date_key

    case = job.case
    cards = case.register.get("cards") or {}
    firs = {}
    for doc in case.documents.values():
        ref = doc.get("ref") or {}
        if doc.get("kind") == "fir" and ref.get("fir_no"):
            firs[(str(ref["fir_no"]).lstrip("0"), str(ref.get("fir_year") or "")[-2:])] = doc
    used: set[str] = set()

    def is_report(doc: dict[str, Any]) -> bool:
        return doc.get("kind") == "lab" or (cards.get(doc["id"]) or {}).get("record") in (*MEDICAL, *LAB, *SCENE)

    def findings(doc: dict[str, Any]) -> list[str]:
        card = cards.get(doc["id"]) or {}
        out = [f"{str(f.get('name') or '').replace('_', ' ').capitalize()}: {f.get('value')}"
               for f in (card.get("fields") or []) if f.get("value")][:8]
        return out or [case.facts[f]["statement"][:200] for f in doc.get("facts") or []
                       if f in case.facts and case.usable(f)][:6]

    def as_report(doc: dict[str, Any]) -> dict[str, Any]:
        card = cards.get(doc["id"]) or {}
        record = card.get("record")
        kind = "Medical report" if record in MEDICAL else "Scene record" if record in SCENE else "Laboratory report"
        received = re.search(r"case received ([\d\-: ]+)", doc.get("summary") or "")
        return {"id": doc["id"], "kind": kind, "title": card.get("title") or doc.get("title"),
                "when": (received.group(1).strip() if received else "") or "-", "found": findings(doc)}

    def reports_of(number: tuple[str, str], only: bool) -> list[dict[str, Any]]:
        found = []
        for doc in case.documents.values():
            if doc["id"] in used or not is_report(doc):
                continue
            named = _fir_of(str((doc.get("ref") or {}).get("fir") or "")) or _fir_of(doc.get("title") or "") \
                or _fir_of((doc.get("text") or "")[:1500])
            if named == number or (named is None and only):     # one case on the board: its reports
                used.add(doc["id"])
                found.append(as_report(doc))
        return sorted(found, key=lambda r: _date_key(r["when"]))

    stations = {_fir_of(str(f.get("label") or "")): f.get("ps") for pid in job.net.people
                for f in job.net.data(pid).get("firs") or [] if f.get("ps")}

    def unknown(value: Any) -> str:
        """The FIR's "unknown accused" as words: a count or a description, never raw data."""
        if isinstance(value, list):
            words = [max((str(v) for v in item.values()), key=len) if isinstance(item, dict) else str(item) for item in value]
            return "; ".join(w for w in words if w.strip("0 ") )[:160]
        return str(value or "").strip()[:160]

    cases = []
    for number, doc in firs.items():
        data = doc.get("data") or {}
        people: list[dict[str, str]] = []

        def add(who: Any, role: str, people: list[dict[str, str]] = people) -> None:
            name = str((who.get("name") if isinstance(who, dict) else who) or "").strip()
            if name and not any(p["name"] == name for p in people):
                people.append({"name": name, "role": role})

        add(data.get("complainant"), "complainant")
        for m in data.get("nominated_suspects") or []:
            add(m, "accused (named in the FIR)")
        roster = data.get("roster") if isinstance(data.get("roster"), dict) else {}
        for role, members in roster.items():
            for m in members if isinstance(members, list) else []:
                add(m, str(role).removesuffix("es").removesuffix("s").replace("_", " ") if str(role) != "witnesses" else "witness")
        for m in data.get("witnesses") or []:
            add(m, "witness")
        for m in data.get("investigating_officers") or []:
            add(m, "investigating officer")
        said = sorted(({"id": w["id"], "speaker": w["speaker"], "role": w.get("role") or "", "kind": w["kind"],
                        "date": w.get("date") or "-", "recorded_by": w.get("recorded_by") or "-", "text": w["text"][:420]}
                       for w in case.statements.values() if w["doc"] == doc["id"]), key=lambda w: _date_key(w["date"]))
        steps = _diary_steps([d for d in data.get("case_diaries") or [] if isinstance(d, dict)])
        positions: dict[str, str] = {}
        for p in data.get("case_positions") or []:
            if isinstance(p, dict):                      # one line per change of position, at its first date
                positions.setdefault(str(p.get("position") or p.get("status") or "").strip()[:160], str(p.get("date") or "-"))
        steps += [{"when": when, "what": f"Position: {text}"} for text, when in positions.items() if text]
        used.add(doc["id"])
        cases.append({
            "fir": f"{number[0]}/{number[1]}", "id": doc["id"], "title": doc["title"],
            "this_case": _is_this_case(job, doc),
            "police_station": str(data.get("police_station") or stations.get(number) or "-").removeprefix("PS ").strip(),
            "offence": data.get("sections") or "-", "occurred": data.get("occurred") or "-",
            "registered": data.get("reported") or "-", "place": str(data.get("place") or "-")[:200],
            "unknown_accused": unknown(data.get("unknown_suspects")),
            "people": people[:24], "statements": said[:20],
            "reports": reports_of(number, only=len(firs) == 1),
            "steps": sorted(steps, key=lambda s: _date_key(s["when"]))[:14],
            "result": str(data.get("investigation_result") or "")[:300]})
    cases.sort(key=lambda c: _date_key(c["occurred"] if c["occurred"] != "-" else c["registered"]))
    loose = []
    for doc in case.documents.values():                       # reports that no FIR on the board claims
        if doc["id"] not in used and is_report(doc):
            used.add(doc["id"])
            loose.append(as_report(doc))
    of_cases = used | {w["id"] for w in case.statements.values() if w["doc"] in used}
    other = [e for e in timeline if e["ref"] not in of_cases][:60]
    return {"cases": cases, "reports_without_a_case": loose, "other": other}


# -- P4 Analysis writer: part D -----------------------------------------------------------


def _analysis(job: ReportRun) -> dict[str, Any]:
    case, net, base = job.case, job.net, job.base
    presence = {r["pid"]: r for r in (case.presence or {}).get("rows") or []}
    by_person = []
    for pid in _people(job):
        by_person.append({
            "name": net.name(pid), "role": case.roles.get(pid) or "-",
            "presence": [f"{c['text']} [{c['source']}] ({c['kind']})" for c in (presence.get(pid) or {}).get("cells") or []],
            "statements": [w["id"] for w in case.statements.values() if w.get("pid") == pid],
            "contradictions": [x["id"] for x in case.contradictions.values() if x.get("person") == pid],
            "leads": [k["id"] for k in case.leads.values() if pid in (k.get("people") or [])],
            "hypotheses": [h["id"] for h in case.hypotheses.values() if pid in (h.get("people") or []) and not h.get("stale")]})
    job.parts["D"] = {
        "by_person": by_person, "contradictions": base.get("contradictions_found") or [],
        "source_conflicts": base.get("contradictions") or [], "leads": base.get("leads") or [], "matrix": base.get("matrix"),
        "challenges": base.get("challenges") or [], "suspicions": base.get("suspicions") or [],
        "history": base.get("history") or [],
        # What the file version may not hold: opinion. Contradictions stay, as conflicts between sources.
        "internal_only": ["by_person", "leads", "matrix", "challenges", "suspicions", "history"]}
    return {"contradictions": len(job.parts["D"]["contradictions"]), "leads": len(job.parts["D"]["leads"])}


# -- P1 Report editor: part F, the annex, the links ---------------------------------------


def _editor(job: ReportRun) -> dict[str, Any]:
    from sherlocks.team.entities import open_maybes
    from sherlocks.team.questions import open_tasks

    case, net, base = job.case, job.net, job.base
    asked = {q.get("source"): q for q in case.questions if q.get("source")}
    points = []
    for x in case.contradictions.values():
        if x["status"] == "open" and x["severity"] != "minor":
            q = asked.get(x["id"])
            points.append({"ref": x["id"], "unclear": x["text"], "why": f"{x['severity']} contradiction",
                           "question": (q or {}).get("text") or x.get("question") or x.get("settle") or "-",
                           "who": "the officer" if q else "to be decided"})
    for k in case.leads.values():
        if k["status"] in ("spotted", "open", "pursuing"):
            q = asked.get(k["id"])
            points.append({"ref": k["id"], "unclear": k["hint"], "why": k.get("why") or "a lead to check",
                           "question": (q or {}).get("text") or k.get("question") or k.get("next_step") or "-",
                           "who": "the officer" if q else "to be decided"})
    for e in open_maybes(case):
        points.append({"ref": e["id"], "unclear": f"Is '{e['name']}' in {', '.join(e['sources'][:3])} the same person as "
                                                  f"{e.get('name_on_graph')}?", "why": "a name-only match is never merged",
                       "question": (asked.get(e["id"]) or {}).get("text") or "-", "who": "the officer"})
    for p in case.register.get("pending") or []:
        points.append({"ref": p["doc"], "unclear": f"{p['what']} ({p['record']}) is awaited", "why": "a pending report",
                       "question": (asked.get(f"pending:{p['record']}") or {}).get("text") or "-", "who": "the laboratory, through the officer"})
    job.parts["F"] = {"open_points": points, "next_steps": (base.get("recommendations") or [])[:10],
                      "tasks": [{"id": q["id"], "text": q["text"], "asked": q.get("times", 0)} for q in open_tasks(case)],
                      "nothing_found": base.get("nothing_found") or []}
    # Linked ids: each contradiction and lead points at the entries behind it; each entry lists what it feeds.
    feeds: dict[str, list[str]] = {}
    for store in (case.contradictions, case.leads):
        for entry in store.values():
            for src in entry.get("rests_on") or []:
                feeds.setdefault(src, []).append(entry["id"])
    for h in case.hypotheses.values():
        for src in (h.get("for") or []) + (h.get("against") or []):
            feeds.setdefault(src, []).append(h["id"])
    index = []
    for pid in _people(job):
        mine = case.for_person(pid)
        index.append({"name": net.name(pid), "role": case.roles.get(pid) or "-",
                      "documents": [d["id"] for d in mine["documents"]][:30],
                      "facts": [f["id"] for f in mine["facts"] if case.usable(f["id"])][:40],
                      "statements": [w["id"] for w in case.statements.values() if w.get("pid") == pid],
                      "contradictions": [x["id"] for x in case.contradictions.values() if x.get("person") == pid],
                      "leads": [k["id"] for k in case.leads.values() if pid in (k.get("people") or [])],
                      "hypotheses": [h["id"] for h in case.hypotheses.values() if pid in (h.get("people") or [])]})
    job.parts["annex"] = {"feeds": {k: sorted(set(v)) for k, v in feeds.items()}, "index_by_person": index,
                          "audit": base.get("audit")}
    return {"open_points": len(points), "indexed": len(index)}


# -- P2 Person writer: the target and each related person ---------------------------------


def _plain_tie(text: str) -> str:
    """A tie as a reader needs it: the number behind "inferred" is the system's own working."""
    return re.sub(r"\s*[—-]\s*inferred(?: \d\.\d+)?", " (inferred)", text or "").replace("(inferred) (inferred)", "(inferred)")


def _tie(net: Any, seeds: list[str], pid: str) -> dict[str, Any] | None:
    """How a person is tied to the target: the record that links them directly, or the
    shortest route through other people."""
    from sherlocks.linkgraph.systems import system_label

    for seed in seeds:
        if pid in net.G and seed in net.G and net.G.has_edge(seed, pid):
            best = net.best_link(seed, pid)
            return {"to": net.name(seed),
                    "how": _plain_tie(best.sentence or f"{net.name(seed)} - {best.label} - {net.name(pid)}"),
                    "source": system_label(best.system or "") or "record", "stated": bool(net.G[seed][pid]["stated"]),
                    "steps": 1}
    for seed in seeds:
        routes = net.paths(seed, pid, k=1)
        if routes:
            hops = routes[0]["hops"]
            return {"to": net.name(seed), "how": "; then ".join(_plain_tie(h["relation"]) for h in hops),
                    "source": ", ".join(dict.fromkeys(h["via"] for h in hops if h.get("via"))) or "records",
                    "stated": not routes[0]["inferred"], "steps": len(hops)}
    return None


def _persons(job: ReportRun) -> dict[str, Any]:
    """One card per person, the target first: identity, the tie to the target, FIRs, the
    medical and other reports about them, what they said, what the police systems hold."""
    from sherlocks.linkgraph.systems import system_label

    case, net = job.case, job.net
    seeds = net.seeds()
    cards = case.register.get("cards") or {}
    near = [n["id"] for s in seeds if s in net.G for n in net.neighbours(s)[:10]]
    order = [p for p in dict.fromkeys([*seeds, *_people(job), *near]) if p in net.people][:16]
    found = {s["pid"]: s["lines"] for s in job.parts["B"]["B5"]["systems"]}
    people = []
    for pid in order:
        d = net.data(pid)
        mine = case.for_person(pid)
        docs = {doc["id"]: case.documents[doc["id"]] for doc in mine["documents"] if doc["id"] in case.documents}
        reports, files = [], {}
        for doc_id, doc in docs.items():
            card = cards.get(doc_id) or {}
            if doc.get("kind") == "fir":
                ref = doc.get("ref") or {}
                files[f"{str(ref.get('fir_no') or '').lstrip('0')}/{str(ref.get('fir_year') or '')[-2:]}"] = doc
            elif card.get("record") in (*MEDICAL, *LAB, *SCENE):
                kind = "Medical report" if card["record"] in MEDICAL else "Laboratory report" if card["record"] in LAB \
                    else "Scene record"
                reports.append({"id": doc_id, "kind": kind, "title": card.get("title") or doc.get("title"),
                                "points": [f"{str(f.get('name') or '').replace('_', ' ').capitalize()}: {f.get('value')}"
                                           + (f" ({f['when']})" if f.get("when") else "")
                                           for f in (card.get("fields") or [])[:7] if f.get("value")]})
        by_fir: dict[str, dict[str, Any]] = {}
        for f in (d.get("firs") or [])[:20]:          # one row per FIR: every role and register it is found in
            number = "/".join(x.lstrip("0") if i == 0 else x[-2:] for i, x in enumerate(str(f.get("label") or "").split("/")[:2]))
            doc = files.get(number)
            data = (doc or {}).get("data") or {}
            row = by_fir.setdefault(number, {
                "fir": f.get("label"), "police_station": f.get("ps") or data.get("police_station") or "-",
                "offence": f.get("offence") or data.get("sections") or "-", "roles": [], "status": f.get("status") or "-",
                "occurred": data.get("occurred") or "-", "sources": [], "file": (doc or {}).get("id")})
            role = str(f.get("role") or "named").split("(")[0].strip()
            if role and role not in row["roles"]:
                row["roles"].append(role)
            if f.get("system") and system_label(f["system"]) not in row["sources"]:
                row["sources"].append(system_label(f["system"]))
        for number, doc in files.items():             # an FIR file that names them, not listed in their own record
            if number not in by_fir:
                data = doc.get("data") or {}
                by_fir[number] = {"fir": f"{number}", "police_station": data.get("police_station") or "-",
                                  "offence": data.get("sections") or "-", "roles": ["named in the FIR file"], "status": "-",
                                  "occurred": data.get("occurred") or "-", "sources": ["FIR file"], "file": doc["id"]}
        firs = [{**row, "role": ", ".join(row["roles"]) or "named", "source": ", ".join(row["sources"]) or "-"}
                for row in by_fir.values()][:12]
        said = [{"id": w["id"], "kind": w["kind"], "date": w.get("date") or "-", "recorded_by": w.get("recorded_by") or "-",
                 "text": w["text"][:260]} for w in case.statements.values() if w.get("pid") == pid]
        lines = found.get(pid)
        if lines is None:
            lines = [{"system": str(r.get("system") or ""), "found": r.get("summary") or "on record", "nothing": False}
                     for r in d.get("records") or []]
        people.append({
            "pid": pid, "name": net.name(pid), "target": pid in seeds, "role": case.roles.get(pid) or "",
            "tie": None if pid in seeds else _tie(net, seeds, pid),
            "identity": {"cnic": d.get("cnic"), "father": d.get("father_name"),
                         "also_recorded_as": [n for n in (d.get("names") or []) if n != net.name(pid)][:4],
                         "phones": (d.get("phones") or [])[:4], "addresses": [str(a) for a in (d.get("addresses") or [])[:2]],
                         "work": [str(o) for o in (d.get("organisations") or [])[:3]],
                         "police_officer": "police_officer" in (d.get("flags") or []),
                         "criminal_record": net.criminal(pid),
                         "vehicles": [str(v) for v in (d.get("vehicles") or [])[:3]]},
            "firs": firs, "reports": reports, "said": said,
            "stays": [{k: s.get(k) for k in ("hotel", "district", "check_in", "check_out")} for s in (d.get("stays") or [])[:6]],
            "systems": list({(system_label(x["system"]), x["found"]): {"system": system_label(x["system"]), "found": x["found"]}
                             for x in lines if not x.get("nothing")}.values())[:10],
            "nothing_in": [system_label(x["system"]) for x in lines if x.get("nothing")][:12]})
    job.parts["persons"] = {"people": people}
    return {"people": len(people), "with_reports": sum(1 for p in people if p["reports"]),
            "with_statements": sum(1 for p in people if p["said"])}


# -- P4 Relations writer ------------------------------------------------------------------


def _relations(job: ReportRun) -> dict[str, Any]:
    """Every tie between the people of the report, each as one plain line with its record;
    and the patterns the link analysis found (a SIM in another person's name is not one)."""
    from sherlocks.linkgraph.systems import system_label

    net = job.net
    pids = [p["pid"] for p in job.parts["persons"]["people"]]
    seeds = set(net.seeds())
    rows, seen = [], set()
    for a in pids:
        if a not in net.G:
            continue
        for b in net.G.neighbors(a):
            if b not in net.people or frozenset((a, b)) in seen or not ({a, b} & seeds or (a in pids and b in pids)):
                continue
            seen.add(frozenset((a, b)))
            best = net.best_link(a, b)
            others = [link.label for link in net.G[a][b]["links"] if link is not best and link.label][:3]
            rows.append({"a": net.name(a), "b": net.name(b),
                         "how": _plain_tie(best.sentence or f"{net.name(a)} - {best.label} - {net.name(b)}"),
                         "also": others, "source": system_label(best.system or "") or "record",
                         "stated": bool(net.G[a][b]["stated"]), "with_target": bool({a, b} & seeds)})
    rows.sort(key=lambda r: (not r["with_target"], not r["stated"]))
    patterns = [{"id": g["id"], "title": g.get("title"), "tier": g.get("tier"), "text": g.get("summary") or g["text"],
                 "rests_on": (g.get("evidence") or [])[:3]}
                for g in job.base.get("graph_evidence") or []
                if g["kind"] == "finding" and not str(g.get("title") or "").startswith("SIMs in someone else")]
    job.parts["relations"] = {"rows": rows[:60], "patterns": patterns[:12], "pairs": _relations_by_pair(job, pids)}
    return {"relations": len(rows), "patterns": len(patterns), "pairs": len(job.parts["relations"]["pairs"])}


def _relations_by_pair(job: ReportRun, pids: list[str]) -> list[dict[str, Any]]:
    """Two people, and EVERY tie between them on one row: "Co-accused in FIR 121/25; was a
    witness in the same tenancy; PRVS witness". First what the FIR files say (each person's
    role in the FIR, with its number - also for people an FIR names who are not on the
    graph), then every other tie of the link graph with the register it comes from. The same
    tie found in two registers is told once, with both."""
    from sherlocks.linkgraph.systems import system_label

    net = job.net
    seeds = [net.name(p) for p in net.seeds()]
    rank: dict[str, int] = {name: i for i, name in enumerate(seeds)}     # the target first, then as met
    pairs: dict[frozenset[str], dict[str, Any]] = {}
    plain = {"IDENTITY": "the same identifier in two records", "RULES": "address comparison"}

    def add(a: str, b: str, relation: str, source: str, stated: bool = True, minor: bool = False) -> None:
        if not (a and b and a != b):
            return
        for name in (a, b):
            rank.setdefault(name, len(rank))
        pair = pairs.setdefault(frozenset((a, b)), {"ties": {}})
        tie = pair["ties"].setdefault(relation, {"sources": [], "stated": stated, "minor": minor})
        tie["stated"] = tie["stated"] or stated
        if source and source not in tie["sources"]:
            tie["sources"].append(source)

    in_fir: set[frozenset[str]] = set()                  # pairs an FIR file already ties, by their roles in it
    for c in (job.parts.get("story") or {}).get("cases") or []:
        fir = f"FIR {c['fir']}"
        role_of = {p["name"]: p["role"].split(" (")[0] for p in c["people"]}
        names = list(role_of)
        for x, a in enumerate(names):
            for b in names[x + 1:]:
                in_fir.add(frozenset((a, b)))
                ra, rb = role_of[a], role_of[b]
                if ra == rb:
                    add(a, b, f"Co-{ra if ra != 'witness' else 'witnesses'} in {fir}".replace("Co-investigating officer",
                                                                                               "Both investigating officers"),
                        c["id"])
                else:
                    # An officer who investigated the FIR, or a witness against an accused, is tied to
                    # everyone in it: said only where the two have another tie as well (the FIR's own
                    # people are listed in the timeline).
                    minor = "investigating officer" in (ra, rb) or {ra, rb} == {"accused", "witness"}
                    add(a, b, f"In {fir}: {a} is {ra}, {b} is {rb}", c["id"], minor=minor)
    order = [p for p in dict.fromkeys([*net.seeds(), *pids]) if p in net.G]
    seen = set()
    for a in order:
        for b in net.G.neighbors(a):
            if b not in net.people or frozenset((a, b)) in seen:
                continue
            seen.add(frozenset((a, b)))
            names = (net.name(a), net.name(b))
            for link in sorted(net.G[a][b]["links"], key=lambda link: link.cost)[:4]:
                kind = (link.label or "linked").split("+")[0].strip()
                sentence = re.sub(r"\s*\(charges:.*$", "", _plain_tie(link.sentence or "")).strip()
                about_fir = "fir" in kind.lower() or " FIR " in f" {sentence} "
                if about_fir and (frozenset(names) in in_fir or kind.lower().startswith("both ")):
                    continue                      # the FIR's own roles above already say it
                label = system_label(link.system or "") or "record"
                # A tie with a direction reads as its sentence ("A was witness to the tenancy of B");
                # one without ("Same locality") reads as its name.
                directed = bool(sentence) and sentence.startswith(names) and not kind.lower().startswith(
                    ("co-", "both ", "same ", "shared "))
                add(names[0], names[1], sentence if directed else kind, plain.get(label.upper(), label),
                    bool(net.G[a][b]["stated"]) and link.kind != "weak")
    out = []
    for key, pair in pairs.items():
        if all(t["minor"] for t in pair["ties"].values()):
            continue
        a, b = sorted(key, key=lambda n: rank[n])
        ties = [{"relation": relation, "source": ", ".join(t["sources"][:3]), "stated": t["stated"]}
                for relation, t in pair["ties"].items()]
        out.append({"a": a, "b": b, "ties": sorted(ties, key=lambda t: not t["stated"])[:8],
                    "with_target": a in seeds or b in seeds})
    return sorted(out, key=lambda r: (rank[r["a"]], rank[r["b"]]))[:120]


# -- P5 Summary writer --------------------------------------------------------------------


class _Summary(Typed):
    brief: list[str] = Field(default_factory=list, description="The case under investigation, ONLY as the officer "
                             "described it (the_new_case): the offence, the date, the place, its FIR, the target's "
                             "status. At most four points. Empty when the officer has not described it.")
    target: list[str] = Field(default_factory=list, description="Who the target is, from the police systems: identity "
                              "and other names on record, work, phones, addresses, vehicles, hotel stays. One fact a "
                              "point, each ending with the system it comes from, e.g. [HRMIS].")
    connections: list[str] = Field(default_factory=list, description="The people tied to the target and HOW each tie "
                                   "is recorded (tenancy witness, co-accused, registered owner of his SIM, same hotel "
                                   "stay...), strongest first, each ending with its system or id.")
    past_cases: list[str] = Field(default_factory=list, description="Each earlier FIR on record, as a past case: its "
                                  "number, police station, offence, date, WHO it names and in what role (say plainly "
                                  "when the target is not named in it), its status. With its id. Never as part of "
                                  "the case under investigation.")
    established: list[str] = Field(default_factory=list, description="What the evidence establishes about the case under "
                                   "investigation: key facts from records and reports, one a point, with ids. Empty "
                                   "when the officer has not described the case.")
    disputed: list[str] = Field(default_factory=list, description="Each critical and major contradiction, both sides in "
                                "one line, starting with its id (X#).")
    leads: list[str] = Field(default_factory=list, description="Each open or confirmed lead with its status and next "
                             "step, starting with its id (K#).")
    stands: list[str] = Field(default_factory=list, description="Where the case stands: each hypothesis with its status, "
                              "by id (H#), one a point. Sherlock's view, if given, labelled as not confirmed.")
    unclear: list[str] = Field(default_factory=list, description="Each open point with the question that would clear "
                               "it, and the reports still awaited. With ids.")
    next_steps: list[str] = Field(default_factory=list, description="The top five actions, each with what it would settle.")

    @field_validator("*", mode="before")
    @classmethod
    def _points(cls, value: Any) -> Any:
        """A heading written as one piece of text is one point."""
        return [value] if isinstance(value, str) and value.strip() else ([] if isinstance(value, str) else value)


_SUMMARY_SYSTEM = ("You write the executive summary of a police case report for the investigating officer, in plain "
                   "English, from the FINISHED PARTS of the report given - nothing else. It is a BRIEF under fixed "
                   "headings: every heading is a short list of points, one fact a point, one line each (25 words at "
                   "most), no paragraphs. The headings: the case in brief; the target; connections; earlier cases on "
                   "record; what the evidence establishes; what is disputed; leads; where the case stands; what is "
                   "not clear yet; the five most useful next steps. A heading with nothing to say stays empty. "
                   "CASE_DECLARED tells you whether the officer has said what case is being investigated. When it "
                   "is false, ASSUME NOTHING about that case: leave `brief`, `established` and `stands` empty; "
                   "nowhere write what 'this case' or 'the current case' is, its offence, date, place, complainant, "
                   "motive, or who did it; do not call the target an accused or a suspect of anything current; "
                   "describe only who the target is, how people are connected to them, and their earlier cases as "
                   "earlier cases. When it is true, `brief` holds only what the officer said (THE_NEW_CASE), and what "
                   "they have not said yet is not filled in from any record. "
                   "A sentence that rests on a record ends with that record's id in [brackets] - "
                   "only ids that appear in the parts, such as [D1], [F7], [W2], [X1], [K4], [H2], or a police "
                   "system's name such as [PSRMS]. A sentence that rests on no such id is written without brackets. "
                   "NEVER put the name of a part or of a field in brackets. Say only what the parts show: no new "
                   "judgment, no guess, nothing about anyone's guilt. Facts and disputes are records and findings; "
                   "Sherlock's own view belongs only in 'where the case stands', labelled as his view and not "
                   "confirmed. " + NEW_CASE + PLAIN + DATA_NOT_INSTRUCTIONS)


def _digest(job: ReportRun) -> dict[str, Any]:
    """The finished parts, short enough for one call."""
    a, b, d, f = job.parts["A"], job.parts["B"], job.parts["D"], job.parts["F"]
    cards = [c for key in ("B1", "B2", "B3", "B4") for c in b[key]["cards"]]
    known = declared(job.case)
    about = _about_the_target(job)
    out = {
        "case_declared": known, "the_new_case": the_new_case(job.case) if known else "not described by the officer yet",
        "the_target": about["target"], "connections_of_the_target": about["connections"],
        "earlier_cases_on_record": about["past_cases"],
        "people_on_the_case": [{k: p[k] for k in ("name", "role", "cnic", "firs")} for p in a["people"]],
        "records": [{"id": c["doc"], "record": c["record"], "title": c["title"],
                       "fields": [f"{x['name']}: {x['value']}" for x in c["fields"]][:14]} for c in cards][:12],
        "custody_and_pending_reports": {"gaps": [f"{c['exhibit']}: {g['to']} {g['days']} days after {g['from']} [{', '.join(g['refs'])}]"
                                           for c in b["B3"]["custody"] for g in c.get("gaps") or []],
                                  "pending": [f"{p['what']} [{p['doc']}]" for p in b["B3"]["pending"]]},
        "police_systems": [{"person": s["person"],
                            "found": [f"{x['found']} [{x['system'].upper()}]" for x in s["lines"] if not x["nothing"]][:10]}
                      for s in b["B5"]["systems"]],
        "call_records": [{"id": c["id"], "findings": [f"{x['text']} [{x['id']}]" for x in c["findings"]][:8]} for c in b["B6"]["cdrs"]][:4],
        "statements": [{"name": r["name"], "summary": r["summary"], "statements": [w["id"] for w in r["statements"]],
                          "changes": [c["what"] for c in r["changes"]]} for r in job.parts["C"]["recorded"]][:10],
        "contradictions": [{"id": x["id"], "severity": x["severity"], "status": x["status"], "text": x["text"]}
                             for x in d["contradictions"]],
        "leads": [{"id": k["id"], "status": k["status"], "hint": k["hint"], "next_step": k["next_step"]} for k in d["leads"]
                  if not (k["status"] == "dead end" and "not a lead" in (k.get("reason") or ""))],
        "hypotheses": [{"id": h["id"], "statement": h["statement"], "status": h["status"],
                          "against": ((d["matrix"] or {}).get("score") or {}).get(h["id"], {}).get("against")}
                         for h in ((d["matrix"] or {}).get("hypotheses") or job.base.get("hypotheses") or [])],
        "sherlocks_view_not_confirmed": job.base.get("conclusion"),
        "open_points": [{"ref": p["ref"], "unclear": p["unclear"], "question": p["question"]} for p in f["open_points"]][:12],
        "next_steps": f["next_steps"][:8],
    }
    if not known:
        # Nothing that theorises about a case nobody has described is put before the writer.
        for key in ("hypotheses", "sherlocks_view_not_confirmed"):
            out.pop(key, None)
    elif job.base.get("conclusion_about_past"):
        out.pop("sherlocks_view_not_confirmed", None)
    return out


def declared(case: Any) -> bool:
    """Has the officer said what case is being investigated - described it, or said which it is?"""
    return bool(the_new_case(case)["described_by_the_officer"] or case.dialog.get("case_basis"))


def _about_the_target(job: ReportRun) -> dict[str, list[Any]]:
    """What the records hold about the targets themselves: who they are, who is tied to them
    and how, and their earlier cases - the part of a summary that needs no case described."""
    from sherlocks.evidence.dossiers import build_card
    from sherlocks.linkgraph.systems import system_label

    case, net = job.case, job.net
    target, connections, past = [], [], []
    for pid in net.seeds()[:3]:
        card = build_card(case, net, pid)
        target.append({k: card[k] for k in ("name", "cnic", "father", "also_recorded_as", "phones", "addresses", "vehicles",
                                            "organisations", "police_officer", "hotel_stays", "records",
                                            "checked_nothing_found") if card.get(k)})
        connections += [{"of": card["name"], **link} for link in card["links"][:12]]
        past += [{"of": card["name"], **{k: v for k, v in f.items() if v},
                  "system": system_label(f["system"]) if f.get("system") else None} for f in card["firs"][:8]]
    own = {str(f.get("label") or "").split("/")[0].lstrip("0") for pid in net.seeds() for f in net.data(pid).get("firs") or []}
    for doc in case.documents.values():                       # the FIR files on the board, whoever they name
        if doc.get("kind") == "fir":
            data = doc.get("data") or {}
            named = str((doc.get("ref") or {}).get("fir_no") or "").lstrip("0") in own or any(
                link["doc"] == doc["id"] and link["pid"] in net.seeds() for link in case.links.values())
            past.append({"id": doc["id"], "fir": doc["title"], "names_the_target": named, "sections": data.get("sections"),
                         "occurred": data.get("occurred"), "police_station": data.get("police_station"),
                         "complainant": (data.get("complainant") or {}).get("name") if isinstance(data.get("complainant"), dict)
                         else data.get("complainant"), "result": data.get("investigation_result")})
    return {"target": target, "connections": connections[:16], "past_cases": past[:10]}


def _target_lines(job: ReportRun) -> dict[str, list[str]]:
    """The same three headings without the model: one line per fact, from the records."""
    about = _about_the_target(job)
    target: list[str] = []
    for t in about["target"]:
        target.append(f"{t['name']}" + (f", CNIC {t['cnic']}" if t.get("cnic") else "")
                      + (f", s/o {t['father']}" if t.get("father") else "")
                      + (f"; also recorded as {', '.join(t['also_recorded_as'][:3])}" if t.get("also_recorded_as") else "") + ".")
        target += [line for line in (
            ("Phones on record: " + ", ".join(t["phones"][:4]) + "." if t.get("phones") else ""),
            ("Addresses on record: " + "; ".join(t["addresses"][:2]) + "." if t.get("addresses") else ""),
            ("Vehicles: " + "; ".join(t["vehicles"][:3]) + "." if t.get("vehicles") else ""),
            ("Works with / linked organisations: " + "; ".join(t["organisations"][:3]) + "." if t.get("organisations") else ""),
            ("Recorded as a police officer [HRMIS]." if t.get("police_officer") else ""),
            ("Hotel stays: " + "; ".join(f"{s.get('hotel')} ({s.get('check_in') or '?'} to {s.get('check_out') or '?'})"
                                         for s in t["hotel_stays"][:3]) + " [Hotel Eye]." if t.get("hotel_stays") else "")) if line]
    connections = [f"{c['to']}: {c['relation']}" + (f" [{c['via']}]" if c.get("via") else "")
                   + ("" if c.get("stated") else " (inferred, not stated by a record)") for c in about["connections"]]
    past = []
    for f in about["past_cases"]:
        if f.get("fir"):
            past.append(f"{f['fir']}" + ("" if f.get("names_the_target") else " (does not name the target)")
                        + (f" - {f['sections']}" if f.get("sections") else "")
                        + (f", occurred {f['occurred']}" if f.get("occurred") else "")
                        + (f", complainant {f['complainant']}" if f.get("complainant") else "") + f" [{f['id']}]")
        else:
            past.append(f"FIR {f.get('label')}" + (f" - {f['offence']}" if f.get("offence") else "")
                        + (f", {f['of']} as {f['role']}" if f.get("role") else "")
                        + (f", status {f['status']}" if f.get("status") else "") + (f" [{f['system']}]" if f.get("system") else ""))
    return {"target": target, "connections": connections, "past_cases": past}


def _from_parts(job: ReportRun) -> dict[str, Any]:
    """The summary without the model: every line taken from a part, with its ids."""
    a, b, d, f = job.parts["A"], job.parts["B"], job.parts["D"], job.parts["F"]
    inc = a["incident"] or {}
    roles = "; ".join(f"{n}: {r}" for n, r in (a["roles"] or {}).items())
    fir = next(iter(b["B1"]["cards"]), None)
    cite = f" [{fir['doc']}]" if fir and inc.get("fir") else ""
    brief = [x for x in (
        (f"FIR {inc['fir']}.{cite}" if inc.get("fir") else ""), (f"Offence: {inc['offence']}." if inc.get("offence") else ""),
        (f"On {inc['date']}" + (f" at about {inc['time']}" if inc.get("time") else "")
         + (f", at {inc['place']}" if inc.get("place") else "") + "." if inc.get("date") else
         (f"Place: {inc['place']}." if inc.get("place") else "")),
        (f"Target: {inc['target_status']}." if inc.get("target_status") else ""),
        (f"Roles given by the officer: {roles}." if roles else "")) if x]
    established = [f"{c['title']}: {x['name']} - {x['value']} [{c['doc']}]"
                   for key in ("B1", "B2", "B3", "B4") for c in b[key]["cards"] if not c["background"] for x in c["fields"][:3]][:10]
    hyps = (d["matrix"] or {}).get("hypotheses") or job.base.get("hypotheses") or []
    leader = (d["matrix"] or {}).get("leader")
    stands = [f"{h['id']} ({h['statement']}): {h['status']}" + (" - fewest against." if h["id"] == leader else ".")
              for h in hyps[:5]]
    return {
        "brief": brief, **_target_lines(job), "established": established,
        "disputed": [f"{x['id']} {x['severity']}: {x['text']}" for x in d["contradictions"]
                     if x["severity"] != "minor" and x["status"] == "open"][:8],
        "leads": [f"{k['id']} {k['status']}: {k['hint']}" + (f" Next: {k['next_step']}" if k.get("next_step") else "")
                  for k in d["leads"] if k["status"] in ("open", "pursuing", "confirmed")][:8],
        "stands": stands,
        "unclear": [f"{p['unclear']} Ask: {p['question']} [{p['ref']}]" for p in f["open_points"]][:8],
        "next_steps": f["next_steps"][:5],
    }


def _is_this_case(job: ReportRun, doc: dict[str, Any]) -> bool:
    from sherlocks.team.recorder import _fir_number

    inc = job.case.incident or {}
    ref = doc.get("ref") or {}
    return bool(job.case.dialog.get("case_basis") == "fir_on_record" and inc.get("fir") and _fir_number(str(inc["fir"])) == (
        str(ref.get("fir_no") or "").lstrip("0"), str(ref.get("fir_year") or "")[-2:]))


def past_only(job: ReportRun, ref: str, _seen: frozenset[str] = frozenset()) -> bool:
    """Does this entry rest only on an earlier case - an FIR file on the board that the
    officer has not said is the case under investigation? Decided from where the entry
    comes from, not from its words."""
    case = job.case
    if ref in _seen:
        return False
    if ref in case.statements:
        ref = case.statements[ref]["doc"]
    elif ref in case.facts:
        ref = case.facts[ref]["doc"]
    if ref in case.documents:
        doc = case.documents[ref]
        return doc.get("kind") == "fir" and not _is_this_case(job, doc)
    if ref in case.hypotheses:
        h = case.hypotheses[ref]
        rests = [*(h.get("for") or []), *(h.get("against") or [])]
        return names_past_fir(job, h.get("statement") or "") or (
            bool(rests) and all(past_only(job, r, _seen | {ref}) for r in rests))
    return False


def names_past_fir(job: ReportRun, text: str) -> bool:
    """Does the text name an FIR that is on file as an earlier case (by its number)?"""
    from sherlocks.team.recorder import _fir_number

    past = set()
    for doc in job.case.documents.values():
        ref = doc.get("ref") or {}
        if doc.get("kind") == "fir" and ref.get("fir_no") and not _is_this_case(job, doc):
            past.add((str(ref["fir_no"]).lstrip("0"), str(ref.get("fir_year") or "")[-2:]))
    for pid in job.net.seeds():
        for f in job.net.data(pid).get("firs") or []:
            number = _fir_number(str(f.get("label") or ""))
            mine = job.case.dialog.get("case_basis") == "fir_on_record" and number == _fir_number(
                str((job.case.incident or {}).get("fir") or ""))
            if number[0] and not mine:
                past.add(number)
    if any((a.lstrip("0"), b[-2:]) in past for a, b in re.findall(r"(\d{1,5})\s*/\s*(\d{2,4})", text or "")):
        return True
    # ... or by the day it happened ("get the CDR for 23-10-2022" is a step in the earlier case).
    days = set()
    for doc in job.case.documents.values():
        if doc.get("kind") == "fir" and not _is_this_case(job, doc):
            m = re.match(r"\s*(\d{1,2})-(\d{1,2})-(\d{4})", str((doc.get("data") or {}).get("occurred") or ""))
            if m:
                days.add((int(m.group(3)), int(m.group(2)), int(m.group(1))))
    said = {(int(y), int(mo), int(d)) for d, mo, y in re.findall(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})\b", text or "")}
    said |= {(int(y), int(mo), int(d)) for y, mo, d in re.findall(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text or "")}
    return bool(days & said)


def _keep_to_this_case(job: ReportRun) -> None:
    """Sherlock's assessment is about the case under investigation. A line of inquiry that
    rests only on earlier cases is not shown as part of it - and when that is all there is,
    the report says so instead of presenting a view on another case."""
    if job.base.get("kept_to_this_case"):
        return
    job.base["kept_to_this_case"] = True
    hyps = job.base.get("hypotheses") or []
    drop = {h["id"] for h in hyps if past_only(job, h["id"])}
    known = declared(job.case)
    if not drop and known:
        return
    job.base["hypotheses"] = [h for h in hyps if h["id"] not in drop]
    job.base["assessments"] = [a for a in job.base.get("assessments") or [] if a.get("id") not in drop]
    matrix = (job.parts.get("D") or {}).get("matrix")
    if matrix and matrix.get("hypotheses"):
        matrix["hypotheses"] = [h for h in matrix["hypotheses"] if h["id"] not in drop]
    notes = []
    if known and job.base.get("conclusion") and names_past_fir(job, job.base["conclusion"]):
        # Written about the earlier cases: shown as that, never as a view on the case under investigation.
        job.base["conclusion_about_past"] = True
    if not known:
        # No view on a case nobody has described: Sherlock's overall conclusion is held back, and
        # what remains is read as what the records show about the people - not as a view on the case.
        job.base["conclusion"] = ""
        notes.append("The officer has not yet said what case is being investigated, so no conclusion about it is "
                     "given. The points below are what the records show about the people and their connections.")
    if drop:
        notes.append(f"Not shown: {', '.join(sorted(drop))} - about earlier cases on record, not about the case under "
                     "investigation.")
        if known and not job.base["hypotheses"]:
            job.base["conclusion"] = ""
            notes.append("Nothing on the board yet is evidence about this case beyond what the officer has said; the "
                         "earlier cases are set out as past records.")
    job.base["assessment_note"] = " ".join(notes)


def _summary(job: ReportRun) -> dict[str, Any]:
    _keep_to_this_case(job)
    out = ask(job.llm, _Summary, dumps({"finished_parts": _digest(job),
                                        "fix_these_problems": job.send_back or None}),
              system=_SUMMARY_SYSTEM, kind="report_summary", version="v2", max_tokens=2200)
    if out is None:
        job.summary, job.summary_by = _from_parts(job), "templates"
    else:
        job.summary, job.summary_by = out.model_dump(), "model"
    known = job.case.known_ids() | {g["id"] for g in job.base.get("graph_evidence") or []}
    for name, value in list(job.summary.items()):
        # Only a real id (or a police system's name) may stand in brackets: anything else is taken out.
        job.summary[name] = real_refs(value, known) if isinstance(value, str) else [real_refs(x, known) for x in value]
    lines = _target_lines(job)
    for name in SUMMARY:
        value = job.summary.get(name) or []
        value = [value] if isinstance(value, str) else [str(x).strip().lstrip("•- ").strip() for x in value if str(x).strip()]
        # The target, their connections and their earlier cases are on record whatever the writer left out.
        job.summary[name] = (value or lines.get(name) or [])[:SUMMARY_MOST[name]]
    if job.case.dialog.get("case_basis") != "fir_on_record":
        job.summary["next_steps"] = [line for line in job.summary["next_steps"] if not names_past_fir(job, line)]
    for name in ("established", "stands"):
        # A point that rests only on an earlier case, or is about one, does not "establish" this one.
        job.summary[name] = [line for line in job.summary[name]
                             if not (ID.findall(line) and all(past_only(job, i) for i in ID.findall(line)))
                             and not names_past_fir(job, line)]
    if declared(job.case) and not job.summary["stands"]:
        job.summary["stands"] = [job.base.get("assessment_note") or "No line of inquiry about this case is on the board yet."]
    if not declared(job.case):
        # The officer has not said what the case is: nothing is assumed about it. This is said in
        # fixed words, and what would weigh or establish "the case" is left out - by code.
        which = next((q["text"] for q in job.case.open_questions() if q["key"] == "case:which"), "")
        job.summary.update({"brief": [NOT_DECLARED], "established": [], "stands": [NOT_WEIGHED]})
        steps = ["Tell Sherlock what is being investigated - which case, what happened, when and where. "
                 + (which or "Until then nothing in this report is about that case.")]
        job.summary["next_steps"] = (steps + [s for s in job.summary["next_steps"] if s not in steps])[:SUMMARY_MOST["next_steps"]]
    return {"written_by": job.summary_by, "again": bool(job.send_back), "case_declared": declared(job.case)}


# -- P6 Report checker --------------------------------------------------------------------


def _body_ids(job: ReportRun) -> set[str]:
    return set(ID.findall(dumps({k: job.parts[k] for k in (*PARTS, "annex", "persons", "relations", "story")
                                 if k in job.parts}))) | set(
        ID.findall(dumps([job.base.get(k) for k in ("hypotheses", "graph_evidence", "cited", "statements", "qa")])))


def _check(job: ReportRun) -> dict[str, Any]:
    """Every id real and current, every part present, every summary sentence backed by the body."""
    from sherlocks.evidence.case_report import check_report

    case = job.case
    known = case.known_ids() | {g["id"] for g in job.base.get("graph_evidence") or []}
    body = _body_ids(job)
    problems = list(check_report(job.base, case))
    problems += [f"Part {p} is missing." for p in PARTS if p not in job.parts]
    bad: list[str] = []
    kept: dict[str, Any] = {}
    for name in SUMMARY:
        value = job.summary.get(name) or []
        lines = [value] if isinstance(value, str) else list(value)
        good = []
        for line in lines:
            ids = set(ID.findall(line))
            wrong = [i for i in ids if i not in known or not case.usable(i)]
            # Who the target is, their connections and their earlier cases come straight from the
            # records (the dossier): a real id there needs no second mention in the body.
            unbacked = [i for i in ids if i in known and i not in body and name not in ("target", "connections", "past_cases")]
            needs_id = name in ("established", "disputed", "leads") and not ids and "[" not in line
            if wrong or unbacked or needs_id:
                bad.append(f"{SUMMARY_TITLE[name]}: \"{line[:90]}\" - " + (
                    f"cites {', '.join(wrong)}, not on the board or out of date" if wrong else
                    f"cites {', '.join(unbacked)}, which the body does not show" if unbacked else "cites no id"))
            else:
                good.append(line)
        kept[name] = " ".join(good) if isinstance(value, str) else good
    job.send_back = bad if (bad and not job.retried and job.summary_by == "model") else []
    if bad and not job.send_back:
        # Failed again (or written from templates): what fails is left out, and the report says so.
        job.summary = kept
        problems += [f"Left out of the summary - {b}" for b in bad]
    job.problems = problems
    return {"problems": len(problems), "sent_back": len(job.send_back)}


def _again(state: ReportState) -> str:
    job = state["job"]
    if job.send_back:
        job.retried = True
        return "summary"
    return "build"


# -- P1: build ----------------------------------------------------------------------------


def summary_text(summary: dict[str, Any], *, file: bool = False) -> str:
    """The seven parts as text (the file version: no leads, and lines of inquiry in place of Sherlock's ranking)."""
    blocks = []
    for name in SUMMARY:
        if file and name == "leads":
            continue
        value = summary.get("stands_file" if file and name == "stands" else name)
        text = value if isinstance(value, str) else "\n".join(f"• {x}" for x in value or [])
        if text.strip():
            blocks.append(f"{SUMMARY_TITLE[name]}\n{text.strip()}")
    return "\n\n".join(blocks)


def _build(job: ReportRun) -> dict[str, Any]:
    case = job.case
    d = job.parts["D"]
    # The file version's "where the case stands": the lines of inquiry, no ranking, no view on anyone.
    hyps = (d["matrix"] or {}).get("hypotheses") or job.base.get("hypotheses") or []
    job.base.pop("kept_to_this_case", None)
    job.summary["stands_file"] = (NOT_WEIGHED if not declared(case) else
                                  "Lines of inquiry: " + "; ".join(h["statement"] for h in hyps[:6]) + "."
                                  if hyps else "No line of inquiry is recorded yet.")
    report = {**job.base, "parts": job.parts, "summary_parts": job.summary, "summary_titles": SUMMARY_TITLE,
              "summary_order": list(SUMMARY), "case_declared": declared(case),
              "structure": [{"key": k, "title": t, "written_by": who, "holds": what} for k, t, who, what in STRUCTURE],
              "executive_summary": summary_text(job.summary), "summary_by": job.summary_by,
              "model": getattr(job.llm, "model", None) if job.summary_by == "model" else None,
              "built_without_model": job.summary_by != "model", "checked": job.problems,
              "report_board_version": job.version, "team": "P1-P6"}
    with case.acting("P1 Report editor"):
        case.set_report(report, "ready")
    report["saved_at_version"] = case.version
    job.report = report
    return {"board_version": job.version, "warnings": len(job.problems)}


def build() -> Any:
    g = StateGraph(ReportState)
    g.add_node("freeze", _member("P1 Report editor", _freeze))
    g.add_node("evidence", _member("P2 Evidence writer", _evidence))
    g.add_node("statements", _member("P3 Statements and timeline writer", _statements))
    g.add_node("analysis", _member("P4 Analysis writer", _analysis))
    g.add_node("editor", _member("P1 Report editor", _editor))
    g.add_node("persons", _member("P2 Person writer", _persons))
    g.add_node("relations", _member("P4 Relations writer", _relations))
    g.add_node("summary", _member("P5 Summary writer", _summary))
    g.add_node("check", _member("P6 Report checker", _check))
    g.add_node("build", _member("P1 Report editor", _build))
    g.add_edge(START, "freeze")
    for writer in ("evidence", "statements", "analysis"):       # the three writers work side by side
        g.add_edge("freeze", writer)
    g.add_edge(["evidence", "statements", "analysis"], "editor")
    g.add_edge("editor", "persons")
    g.add_edge("persons", "relations")
    g.add_edge("relations", "summary")
    g.add_edge("summary", "check")
    g.add_conditional_edges("check", _again, {"summary": "summary", "build": "build"})
    g.add_edge("build", END)
    return g.compile()


_GRAPH: Any = None


def report_graph() -> Any:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build()
    return _GRAPH


def build_report(case: Any, graph: dict[str, Any], *, llm: Any = None, run: dict[str, Any] | None = None) -> dict[str, Any]:
    """The Report team writes the report of ``case`` as it is now, and keeps it on the
    board with its version. Returns the report (data; ``report_pdf.render`` makes the PDF)."""
    from sherlocks.linkgraph.network import PersonNetwork

    job = ReportRun(case=case, graph=graph, net=PersonNetwork(graph), llm=llm, run=run)
    out = report_graph().invoke({"job": job, "log": []})
    job.report["team_log"] = out.get("log") or []
    return job.report
