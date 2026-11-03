"""The Report team (P1-P8), as a LangGraph graph.

Writing the report is a different job from solving the case, so it has its own team. It
reads the board and writes; it never judges and never looks anything up.

    freeze (P1 Report editor: one board version)
      -> scope (P1: what belongs to the case under investigation - case_scope)
      -> evidence (P2) | statements (P3) | analysis (P4)        three writers side by side
      -> editor (P1: part F, the annex, the links between ids)
      -> target (P2 Target writer: the target's profile, record and close circle)
      -> record (P8 Record writer: the target's earlier FIRs, relations, hotel stays, earlier reports)
      -> story (P7 Story writer: the case told in the order it happened, its people)
      -> summary (P5: the brief under fixed headings, written LAST, from the finished parts)
      -> check (P6) --a failed summary goes back to P5 once--> summary -> check
      -> build (P1: the report kept with its board version; internal and file versions)

The report is a brief for the investigating officer, about THE CASE and THE TARGET only
(``STRUCTURE``): the case at a glance, Sherlock's brief, the story of the case, the target,
the people in the case, statements and what does not fit, the reports, leads, next steps.
Other people's FIRs, the statements in them, tenancy witnesses, landlords, PRVS guarantors
and SIMs registered on another person's CNIC are not in it: a police officer who is a
witness in fifty FIRs does not bring those fifty cases into this one.

All parts come from the same frozen board version. The summary is written last, so it can
claim nothing the body does not show: a sentence that cites no real id, or an id the body
does not use, is dropped. Without the model every part is built from the board with
templates, and the cover says so. The team's writes never wake the Sherlock team.
"""

from __future__ import annotations

import json
import logging
import operator
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import Field, field_validator

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.linkgraph.relevance import the_new_case
from sherlocks.team import group_view, runtime, section_writer  # noqa: F401 - runtime switches tracing off first
from sherlocks.team.runtime import NEW_CASE, PLAIN, Typed, ask, dumps, real_refs

logger = logging.getLogger(__name__)
ID = re.compile(r"\b([GDFLHQWXK]\d{1,4})\b")
PARTS = ("A", "B", "C", "D", "E", "F")
SUMMARY = ("brief", "established", "disputed", "unclear", "stands", "next_steps")
SUMMARY_TITLE = {"brief": "The case in short", "established": "What the evidence shows",
                 "disputed": "What does not fit", "unclear": "What is still missing", "stands": "Sherlock's view",
                 "next_steps": "Next steps"}
# The most bullets a heading may hold: the brief is short, the sections below it hold the rest.
SUMMARY_MOST = {"brief": 5, "established": 6, "disputed": 4, "unclear": 5, "stands": 4, "next_steps": 5}
# Said by code - never by the model - while the officer has not told us what the case is.
NOT_DECLARED = ("The investigating officer has not yet said what case is being investigated. This summary therefore "
                "says nothing about that case: it sets out what the records hold about the target, the people "
                "connected to them and their earlier cases.")
# ... and when the officer said there is no case: they are only checking the target's record.
NO_CASE = ("The investigating officer said there is no case: they are checking the target's record. This summary "
           "sets out what the records hold about the target, the people connected to them and their earlier cases.")
# What wrote a stored report: a report kept from before a change to the report team is written again, not
# re-drawn - its parts lack what the new layout reads (a case shown with no summary, under the wrong part).
# Raise it whenever a part's content changes.
REPORT_BUILD = "2026-11-02.3"
MOST_LEADS = 12
NOT_WEIGHED = "Not assessed: no case has been described yet, so no line of inquiry is weighed."
MEDICAL, LAB, SCENE = ("mlo", "postmortem"), ("dna", "chemical", "forensic"), ("recovery_memo", "site_plan")
# The report, in the order it is read - and the member of the Report team that writes each section.
# The annex holds only the police systems checked for the target and the references.
STRUCTURE = (
    ("glance", "The case at a glance", "P1 Report editor",
     ("The FIR, police station, offence, when it happened and was registered, the place, the complainant, the "
      "accused and their status, the investigating officer, where the case stands, the reports awaited.")),
    ("summary", "Sherlock's brief", "P5 Summary writer",
     ("The case in short, what the evidence shows, what does not fit, what is still missing, Sherlock's view and "
      "the next steps - short points under fixed headings.")),
    ("story", "The story of the case", "P7 Story writer",
     ("The case in the order it happened: the incident, the FIR, arrests, disclosures and recoveries, statements, "
      "reports sent and received and what they found, where it stands - each step with its source.")),
    ("target", "The target", "P2 Target writer",
     ("Who the target is, their part and status in this case, where the records put them at the time, their own "
      "earlier cases, and their close circle (family, co-accused, the same vehicle, travel, phone contact).")),
    ("group", "How the targets are connected", "P12 Group writer",
     ("When several targets are searched together (a gang, a group of accused): each target's identity, phones and "
      "addresses side by side; the ties between the targets (accused together, family, the same or consecutive "
      "phone numbers, the same address or village); names on a record that may be a target; the people tied to two "
      "or more targets; the groups the ties make; and each target's contacts to investigate.")),
    ("network", "The target's network: step 1 and step 2", "P9 Network writer",
     ("A picture of everyone the records tie to the target directly, and the people one step further who matter "
      "(involved in this case, with a criminal record, or tied to two or more of the direct ones) - with tables "
      "of who, how and through whom.")),
    ("record", "The target's record", "P8 Record writer",
     ("Before the case is described, the body of the report: each earlier FIR (when, where, the offence, their "
      "part, where it stands - convicted, acquitted, under trial - what the complainant said, what was found "
      "against them), everyone the records tie to them and how, their hotel stays, and the earlier reports.")),
    ("people", "People in this case", "P7 Story writer",
     "Each person this case's records name: their part, their status, what they did or said."),
    ("statements", "Statements and what does not fit", "P3 Statement writer",
     ("What each person of the case said, what changed between their statements, and every contradiction with the "
      "evidence - both sides and what would settle it.")),
    ("reports", "Medical, forensic and laboratory reports", "P2 Evidence writer",
     "Each report of this case: what was sent, when it came back, what it found; gaps in custody; reports awaited."),
    ("leads", "Leads worth checking", "P4 Analysis writer",
     ("Only leads about the case and its people - never a SIM in another person's name, a tenancy witness or a "
      "guarantor. Not evidence.")),
    ("next_steps", "Next steps for the investigating officer", "P1 Report editor",
     "What to do now, in order, and the questions waiting for the officer."),
    ("added", "Sections written for this case", "P13 Section writer",
     ("Headings and tables the fixed sections do not give, chosen for THIS case from its records - a gang's "
      "crimes across FIRs, the vehicles or weapons on record, money moved, arrests by date - each placed where "
      "it reads best. Every row is checked against the records; what they do not hold is dropped.")),
)
# Headings the PDF always prints (besides STRUCTURE's): the Section writer never repeats one.
FIXED_HEADINGS = ("The targets", "Forensic and medical findings", "Documents uploaded", "Weighing the evidence",
                  "Statements", "Persons of interest", "Police and officials on the case", "Earlier cases of the target",
                  "Hotel stays", "Tenancy records", "Reports on the target", "Statements in the earlier FIRs",
                  "Case papers: on file and still needed", "Questions still open", "Action plan for the investigating officer",
                  "Police systems checked for the target", "Records this report rests on", "Contacts of each target")


@dataclass
class ReportRun:
    case: Any
    graph: dict[str, Any]
    net: Any
    llm: Any = None
    run: dict[str, Any] | None = None
    version: int = 0
    scope: dict[str, Any] = field(default_factory=dict)   # the case's own material (case_scope.build)
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
    return {"statements": len(job.parts["C"]["recorded"]), "events": len(timeline)}


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


# -- P1: what belongs to this case -------------------------------------------------------


def _scope(job: ReportRun) -> dict[str, Any]:
    from sherlocks.team import case_scope

    job.scope = case_scope.build(job.case, job.net)
    return {"this_fir": job.scope["this_fir"], "linked_firs": len(job.scope["linked"]),
            "documents": len(job.scope["documents"]), "people": len(job.scope["people"])}


# -- P2 Target writer ---------------------------------------------------------------------


def _sim_owners(net: Any, pid: str) -> dict[str, str]:
    """Each of the target's numbers that is registered on another person's CNIC, and whose:
    said once next to the number - it is common (a relative's or a friend's SIM), not a lead."""
    owners: dict[str, str] = {}
    if pid not in net.G:
        return owners
    for other in net.G.neighbors(pid):
        if other not in net.people:
            continue
        for link in net.G[pid][other]["links"]:
            m = re.search(r"registered owner of sim (\S+)", f"{link.label or ''} {link.sentence or ''}", re.IGNORECASE)
            if m and (link.sentence or "").startswith(net.name(other)):
                owners[re.sub(r"\D", "", m.group(1))[-10:]] = net.name(other)
    return owners


def _target(job: ReportRun) -> dict[str, Any]:
    """The target: identity, their part and status in this case, where the records put them
    in the incident window, their OWN earlier cases (not other people's), the police systems
    that hold them, and their close circle."""
    from sherlocks.evidence.dossiers import build_card
    from sherlocks.linkgraph.systems import system_label
    from sherlocks.team import network_view
    from sherlocks.team.case_scope import MOST_TARGETS, close_circle

    case, net, scope = job.case, job.net, job.scope
    presence = {r["pid"]: r for r in (case.presence or {}).get("rows") or []}
    found = {s["pid"]: s["lines"] for s in job.parts["B"]["B5"]["systems"]}
    targets = []
    for pid in scope["targets"][:MOST_TARGETS]:
        card = build_card(case, net, pid)
        owners = _sim_owners(net, pid)
        firs: dict[str, dict[str, Any]] = {}
        for f in (f for f in scope["own_firs"] if f["pid"] == pid):
            row = firs.setdefault("/".join(f["number"]), {
                "fir": f.get("label"), "police_station": f.get("ps") or f.get("police_station") or "-",
                "offence": f.get("offence") or "-", "roles": [], "status": f.get("status") or "-", "sources": []})
            role = str(f.get("role") or "named").split("(")[0].strip()
            if role not in row["roles"]:
                row["roles"].append(role)
            label = system_label(f.get("system") or "")
            if label and label not in row["sources"]:
                row["sources"].append(label)
        lines = found.get(pid) or []
        targets.append({
            "pid": pid, "name": net.name(pid), "cnic": card.get("cnic"), "father": card.get("father"),
            "also_recorded_as": card.get("also_recorded_as") or [],
            "phones": [{"number": p, "registered_to": owners.get(re.sub(r"\D", "", str(p))[-10:])}
                       for p in card.get("phones") or []],
            "addresses": card.get("addresses") or [], "vehicles": card.get("vehicles") or [],
            "work": card.get("organisations") or [], "police_officer": card.get("police_officer"),
            "role_in_case": case.roles.get(pid) or "",
            "status_in_case": (case.incident or {}).get("target_status") or "" if declared(case) else "",
            "presence": [f"{c['text']} [{c['source']}] ({c['kind']})" for c in (presence.get(pid) or {}).get("cells") or []],
            "window": ((case.presence or {}).get("window") or None),
            "hotel_stays": card.get("hotel_stays") or [],
            "firs": [{**r, "role": ", ".join(r["roles"]), "source": ", ".join(r["sources"]) or "-"} for r in firs.values()][:15],
            "systems": list({(system_label(x["system"]), x["found"]): {"system": system_label(x["system"]), "found": x["found"]}
                             for x in lines if not x.get("nothing")}.values())[:12],
            "nothing_in": [system_label(x["system"]) for x in lines if x.get("nothing")][:14]})
    job.parts["target"] = {"targets": targets, "circle": close_circle(net, scope)}
    # P9: the target's network up to two steps - direct connections, and the people who matter one step on.
    job.parts["network"] = network_view.build(job.case, net, scope)
    return {"targets": len(targets), "own_firs": sum(len(t["firs"]) for t in targets),
            "circle": len(job.parts["target"]["circle"]), "network_first": len(job.parts["network"]["first"]),
            "network_second": len(job.parts["network"]["second"])}


# -- P13 Section writer -------------------------------------------------------------------


def _section_material(job: ReportRun) -> dict[str, Any]:
    """Everything the finished parts hold, short: what the Section writer may build its tables from."""
    target, record = job.parts["target"], job.parts["record"]
    group = job.parts.get("group") or {}
    analysis = job.parts.get("case_analysis") or {}
    return {
        "finished_parts": _digest(job),
        "targets": [{k: t.get(k) for k in ("name", "cnic", "father", "phones", "addresses", "vehicles", "work", "firs",
                                           "hotel_stays", "role_in_case")} for t in target["targets"]],
        "earlier_cases": [{"target": t["name"], **{k: c.get(k) for k in (
            "fir", "police_station", "offence", "role", "occurred", "registered", "place", "complainant", "outcome_detail",
            "found_against", "summary", "doc")}} for t in record["targets"] for c in t["past_cases"]],
        "hotel_stays": [{"target": t["name"], **h} for t in record["targets"] for h in t["hotels"]][:30],
        "contacts": [{"target": c["target"], "contacts": [{k: r[k] for k in ("name", "cnic", "phones", "address", "how", "record")}
                                                          for r in c["rows"]]} for c in group.get("contacts") or []],
        "common_connections": [{k: c[k] for k in ("name", "cnic", "phones", "address", "tied_to", "record")}
                               for c in group.get("common") or []],
        "network_step_2": [{k: r.get(k) for k in ("name", "through", "how", "record")}
                           for r in (job.parts.get("network") or {}).get("second") or []],
        "reports_read": [{k: r.get(k) for k in ("doc", "title", "findings", "indicates")} for r in analysis.get("reports") or []],
        "documents_uploaded": analysis.get("documents") or [],
        "statements": [{"fir": x.get("fir"), "people": [{k: p.get(k) for k in ("person", "part", "date", "explained")}
                                                        for p in x.get("people") or []]} for x in analysis.get("statements") or []],
    }


def _sections(job: ReportRun) -> dict[str, Any]:
    """The headings and tables this case needs beyond the fixed sections (``team/section_writer``)."""
    weighing = (job.parts.get("case_analysis") or {}).get("weighing") or {}
    holds = {title: what for _, title, _, what in STRUCTURE if title != "Sections written for this case"}
    holds.update({title: "" for title in FIXED_HEADINGS})
    if job.parts.get("group"):
        holds["How the targets are connected"] = next(what for key, _, _, what in STRUCTURE if key == "group")
    # The analysis's own extra sections: never one that repeats a fixed section.
    before = len(weighing.get("sections") or [])
    if weighing.get("sections"):
        weighing["sections"] = [x for x in weighing["sections"]
                                if not any(section_writer.same_heading(x["heading"], f) for f in holds)]
    fixed = [*holds, *(x["heading"] for x in weighing.get("sections") or [])]
    out = section_writer.write(_section_material(job), fixed, job.case.known_ids(), job.llm, holds=holds,
                               shown=_shown(job))
    job.parts["added"] = out
    if weighing.get("sections") and out["sections"]:
        # ... nor one whose points the added tables already hold (the same vehicles, numbers, FIRs).
        tables = " ".join(" ".join(" ".join(r) for r in s["rows"]) + " ".join(s["points"]) for s in out["sections"])
        weighing["sections"] = [x for x in weighing["sections"] if not _covered(x["points"], tables)]
    return {"sections": [s["heading"] for s in out["sections"]], "rows_dropped": out["dropped"],
            "analysis_sections_dropped": before - len(weighing.get("sections") or [])}


def _shown(job: ReportRun) -> dict[str, list[str]]:
    """What the fixed sections print, item by item - each target, contact, tie, step of the story, exhibit and
    finding of a report, earlier FIR, hotel stay, point of the analysis. Not the long narratives (an FIR's first
    information, a statement in full): a table that gathers facts out of those is not a repeat."""
    parts, analysis = job.parts, job.parts.get("case_analysis") or {}
    group, weighing = parts.get("group") or {}, (analysis.get("weighing") or {})

    def text(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    record = parts["record"]["targets"]
    reports = []
    for r in analysis.get("reports") or []:
        head = text({k: r.get(k) for k in ("title", "lab", "report_no", "received", "issued", "submitted_by")})
        each = [*(r.get("exhibits") or []), *(r.get("findings") or []), *(r.get("indicates") or [])]
        # Each exhibit and finding, and the report as a whole (a point that joins two of its findings repeats it).
        reports += [head + " " + text(x) for x in each] + [head + " " + text(each)]
    return {
        "The targets": [text({k: t.get(k) for k in ("name", "cnic", "father", "phones", "addresses", "vehicles", "firs")})
                        for t in parts["target"]["targets"]] + [text(t) for t in group.get("targets") or []],
        "How the targets are connected": [text(x) for x in [*(group.get("ties") or []), *(group.get("common") or []),
                                                            *(group.get("same_person") or [])]]
        + [text({"target": c["target"], **r}) for c in group.get("contacts") or [] for r in c["rows"]],
        "The story of the case": [text([e.get("when"), e.get("what")]) for e in parts["case_story"]["events"]],
        "People in this case": [text(p) for p in parts["case_story"]["people"]],
        "Forensic and medical findings": reports,
        "Weighing the evidence": [text(x) for e in weighing.get("explanations") or [] for x in [*e["for"], *e["against"]]]
        + [weighing.get("view") or ""],
        "Earlier cases": [text({k: c.get(k) for k in ("fir", "police_station", "offence", "occurred", "registered",
                                                      "outcome_detail", "complainant")}) for t in record for c in t["past_cases"]],
        "Hotel stays": [text(h) for t in record for h in t["hotels"]],
    }


def _covered(points: list[str], tables: str) -> bool:
    """Do the added tables already hold most of these points (their numbers: plates, FIRs, phones, dates)?"""
    numbered = [p for p in points if re.findall(r"\d{2,}", p)]
    held = [p for p in numbered if all(n in tables for n in re.findall(r"[A-Z]*-?\d[\d/.-]*\d", p))]
    return bool(numbered) and len(held) >= max(1, len(numbered) * 0.6)


# -- P12 Group writer ---------------------------------------------------------------------


def _graph_doc(case: Any) -> str | None:
    """The board's link-analysis document - what the ties between people rest on."""
    return next((d for d, doc in case.documents.items() if doc.get("kind") == "graph"), None)


def _group(job: ReportRun) -> dict[str, Any]:
    """Several targets searched together (a gang, a group of accused): how the records tie them to each other, the
    people tied to two or more of them, and each one's contacts to investigate (``team/group_view``)."""
    group = job.parts["group"] = group_view.build(job.case, job.net, job.scope)
    if not group:
        return {"targets": len(job.scope.get("targets") or []), "group": "one target - nothing to compare"}
    return {"targets": len(group["targets"]), "ties": len(group["ties"]), "groups": len(group["groups"]),
            "alone": len(group["alone"]), "same_person": len(group["same_person"]), "common": len(group["common"])}


# -- P8 Record writer ---------------------------------------------------------------------


def _record(job: ReportRun) -> dict[str, Any]:
    """The target's record. Before the officer describes the case it is the body of the report,
    and each earlier FIR file is read by the model; once the case is described it only dates
    the earlier cases and gives where each stands, from the records."""
    from sherlocks.team import record_writer

    known = declared(job.case)
    job.parts["record"] = record_writer.build(job.case, job.net, job.graph, job.scope, None if known else job.llm)
    targets = job.parts["record"]["targets"]
    return {"past_cases": sum(len(t["past_cases"]) for t in targets), "relations": sum(len(t["relations"]) for t in targets),
            "hotel_stays": sum(len(t["hotels"]) for t in targets), "reports": sum(len(t["reports"]) for t in targets),
            "read_by_model": sum(1 for t in targets for c in t["past_cases"] if c["by"] == "model")}


# -- P7 Story writer, and the case's own parts --------------------------------------------


def _in_case(job: ReportRun) -> dict[str, Any]:
    """The statements, contradictions, reports and leads of THIS case - from the parts the
    writers laid out for the whole board."""
    from sherlocks.team.case_scope import NOT_EVIDENCE, in_scope

    case, scope, base = job.case, job.scope, job.base
    said_ids = set(scope["statements"])
    said = []
    for r in base.get("recorded") or []:
        mine = [w for w in r["statements"] if w["id"] in said_ids]
        if not mine:
            continue
        ids = {w["id"] for w in mine}
        said.append({**r, "statements": mine,
                     "changes": [c for c in r.get("changes") or [] if c.get("earlier") in ids and c.get("later") in ids],
                     "clashes": [c for c in r.get("clashes") or [] if c.get("statement") in ids]})
    contradictions = [x for x in base.get("contradictions_found") or []
                      if any(in_scope(case, scope, ref) for ref in (case.contradictions.get(x["id"]) or {}).get("rests_on") or [])]
    docs = set(scope["documents"])
    cards = case.register.get("cards") or {}
    reports = []
    for d in scope["documents"]:
        card, doc = cards.get(d) or {}, case.documents.get(d) or {}
        if doc.get("reading"):
            continue        # read in full by the Document reader: its own section, not a laboratory's report
        if card.get("record") in (*MEDICAL, *LAB, *SCENE) or (doc.get("kind") == "lab" and card.get("record") != "fir"):
            reports.append({"id": d, "kind": "Medical report" if card.get("record") in MEDICAL else "Scene record"
                            if card.get("record") in SCENE else "Laboratory report",
                            "title": card.get("title") or doc.get("title"),
                            "found": [f"{str(f.get('name') or '').replace('_', ' ').capitalize()}: {f.get('value')}"
                                      + (f" ({f['when']})" if f.get("when") else "")
                                      for f in card.get("fields") or [] if f.get("value")][:8]})
    custody = [c for c in base.get("custody") or [] if any(r in docs for g in c.get("gaps") or [] for r in g.get("refs") or [])]
    pending = [p for p in base.get("pending_reports") or [] if p.get("doc") in docs]
    pids = set(scope["pids"])
    leads = []
    for k in base.get("leads") or []:
        entry = case.leads.get(k["id"]) or {}
        source = (case.facts.get(entry.get("source") or "") or {}).get("kind")
        doc = (case.documents.get((case.facts.get(entry.get("source") or "") or {}).get("doc") or "") or {})
        if k["status"] in ("dead end", "answered") or source in NOT_EVIDENCE or (entry.get("people") and not pids & set(entry["people"])) \
                or doc.get("kind") == "graph" or not in_scope(case, scope, entry.get("source") or ""):
            continue            # a pattern of the link graph (a go-between, contacts in common) is not this case's lead
        leads.append(k)
    return {"said": said, "contradictions": contradictions, "reports": reports, "custody": custody,
            "pending": pending, "leads": leads[:MOST_LEADS]}


def _when(text: Any) -> datetime | None:
    """A record's date and time ("30-09-2026 بوقت 0730", "30-09-2026 18:45"), when it gives both."""
    m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", str(text or ""))
    t = re.search(r"(?<![\d/.-])([01]?\d|2[0-3]):?([0-5]\d)(?![\d/.-])", str(text or "")[m.end():] if m else "")
    if not (m and t):
        return None
    hour = int(t.group(1)) + (12 if re.search(r"\bpm\b", str(text), re.IGNORECASE) and int(t.group(1)) < 12 else 0)
    try:
        # Local time, as the record gives it: only the difference between two of them is used.
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), hour, int(t.group(2)))  # noqa: DTZ001
    except ValueError:
        return None


def _flat(text: Any) -> str:
    """A status put in brackets has none of its own: "Dead (body found)" -> "dead, body found"."""
    text = re.sub(r"\s*\(([^()]*)\)", r", \1", str(text or "")).strip(" ,")
    return text[:1].lower() + text[1:] if text[:2] != text[:2].upper() else text


def _glance(job: ReportRun) -> dict[str, Any] | None:
    """The case at a glance: its FIR and the people in it, from the FIR file and the story."""
    from sherlocks.team.case_scope import _person_names

    case, scope = job.case, job.scope
    if not declared(case):
        return None
    inc = case.incident or {}
    main = case.documents.get(scope.get("main") or "") or {}
    data = main.get("data") or {}
    named = _person_names(data)
    people = job.parts["case_story"]["people"]

    def status(name: str) -> str:
        p = next((p for p in people if p["name"] == name or name in p["name"] or p["name"] in name), None)
        return f" ({_flat(p['status'])})" if p and p["status"] and p["status"] != "not on record" else ""

    occurred, registered = data.get("occurred") or inc.get("date"), data.get("reported")
    delay = ""
    if _when(occurred) and _when(registered) and _when(registered) >= _when(occurred):
        hours = (_when(registered) - _when(occurred)).total_seconds() / 3600
        delay = f" - {hours:.0f} hour(s) after the time given for the incident" if hours >= 1 else ""
    from sherlocks.evidence.case_report import _date_key

    positions = sorted((p for p in data.get("case_positions") or [] if isinstance(p, dict) and (p.get("position") or p.get("status"))),
                       key=lambda p: _date_key(str(p.get("date") or "")))
    linked = []
    for d in scope["linked"]:
        doc = case.documents[d]
        linked.append(f"FIR {doc['ref']['fir_no']}/{str(doc['ref'].get('fir_year') or '')[-2:]}"
                      + (f" ({(doc.get('data') or {}).get('sections')})" if (doc.get("data") or {}).get("sections") else "")
                      + f" [{d}]")
    from sherlocks.linkgraph.offences import classify, labels

    sections = str(data.get("sections") or "").strip()
    said = str(inc.get("offence") or "").replace("_", " ").strip()         # "vehicle_theft" is the board's key
    # The offence in words, with its sections; what the officer called it only when it is a name for a crime
    # (a few words) - a whole message once landed here.
    offence = "; ".join(x for x in (
        f"{labels(classify(sections)) or 'offence'} ({sections})" if sections else "",
        f"as the officer describes it: {said}" if said and len(said.split()) <= 6 and said.lower() not in sections.lower() else "")
        if x)
    rows = [
        ("FIR", (f"{scope['this_fir'] or inc.get('fir')}" + (f", PS {data.get('police_station') or inc.get('police_station')}"
                                                            if data.get("police_station") or inc.get("police_station") else ""))
         if (scope["this_fir"] or inc.get("fir")) else "No FIR given - a new matter as the officer described it"),
        ("Offence", offence),
        ("Occurred", str(occurred or "")),
        ("FIR registered", f"{registered}{delay}" if registered else ""),
        ("Place", str(data.get("place") or (inc.get("place") if len(str(inc.get("place") or "").split()) <= 15 else "")
                      or "")[:220]),
        ("Complainant", "; ".join(n + status(n) for n, r in named if r == "complainant")),
        # The victim is the one whose part or status IS victim or deceased - not the father "of the deceased".
        ("Victim", "; ".join(f"{p['name']}" + (f" ({_flat(p['status'])})" if p["status"] != "not on record" else "")
                             for p in people if re.match(r"\s*(victim|deceased|the deceased)\b", p["part"], re.IGNORECASE)
                             or re.match(r"\s*(victim|deceased)\b", p["status"], re.IGNORECASE))
         or "not named in the records yet"),
        ("Accused", "; ".join(n + status(n) for n, r in named if r.startswith("accused"))
         or "; ".join(f"{job.net.name(p)}: {r}" for p, r in case.roles.items() if p in job.net.people)),
        ("Investigating officer", "; ".join(n for n, r in named if r == "investigating officer")),
        ("Case position", " ".join(str(x) for x in (positions[-1].get("date") or "", "-", positions[-1].get("position")
                                                   or positions[-1].get("status"))) if positions else ""),
        ("Linked FIRs", "; ".join(linked)),
        ("Reports awaited", "; ".join(f"{p['what']} [{p['doc']}]" for p in job.parts["in_case"]["pending"])),
    ]
    return {"rows": [{"label": k, "value": v} for k, v in rows if str(v or "").strip()]}


_FAMILY = re.compile(r"\b(mother|father|brother|sister|wife|husband|son|daughter|uncle|aunt|cousin|nephew|niece|"
                     r"grand\w*|\w+-in-law|parents?|spouse|fianc\w*)\b", re.IGNORECASE)


def _named(job: ReportRun) -> dict[str, str]:
    """Everyone the case's records name, with their part: the story's people and the uploaded documents' people."""
    named = {p["name"]: p["part"] for p in job.parts["case_story"]["people"] if p.get("name")}
    for d in job.scope["documents"]:
        for p in ((job.case.documents.get(d) or {}).get("reading") or {}).get("people") or []:
            named.setdefault(p["name"], p.get("part") or "named in an uploaded document")
    return named


def _family_circle(job: ReportRun) -> None:
    """Family the case's own records name (the mother who last spoke to him) belong in the close circle, even when
    no police system ties them to the target."""
    from sherlocks.linkgraph.normalize import sounds_like
    from sherlocks.team.network_view import named_in

    net, circle = job.net, job.parts["target"]["circle"]
    targets = [net.name(t) for t in job.scope["targets"]]
    have = [r["name"] for r in circle]
    for p in job.parts["case_story"]["people"]:
        name, part = p["name"], p.get("part") or ""
        if not _FAMILY.search(part) or any(sounds_like(name, t) and sounds_like(t, name) for t in targets):
            continue
        if any(sounds_like(name, h) and sounds_like(h, name) for h in have):
            continue
        pid = next((o for t in job.scope["targets"] if t in net.G for o in net.G.neighbors(t)
                    if o in net.people and named_in(net, o, {name: part})), "")
        circle.append({"pid": pid, "name": net.name(pid) if pid else name, "kind": "family", "how": part,
                       "source": ", ".join(_source_names(job, p.get("sources") or [])) or "case records", "stated": True,
                       "criminal": bool(pid and net.criminal(pid)), "in_case": True})
        have.append(name)


def _source_names(job: ReportRun, ids: list[str]) -> list[str]:
    from sherlocks.team.case_scope import source_doc

    docs = dict.fromkeys(source_doc(job.case, i) for i in ids)
    return [str((job.case.documents.get(d) or {}).get("title") or d).replace("Upload: ", "") for d in docs if d][:2]


def _case_story(job: ReportRun) -> dict[str, Any]:
    from sherlocks.team import network_view, story_writer

    job.parts["in_case"] = _in_case(job)
    if declared(job.case):
        job.parts["case_story"] = story_writer.write(job.case, job.scope, job.llm, net=job.net)
    else:
        job.parts["case_story"] = {"in_short": [], "events": [], "people": [], "by": "the case is not described yet"}
    if job.parts["case_story"]["people"] or any((job.case.documents.get(d) or {}).get("reading") for d in job.scope["documents"]):
        # Now that the case's people are known: the network again, with them, and the family they name.
        job.parts["network"] = network_view.build(job.case, job.net, job.scope, named=_named(job))
        _family_circle(job)
    job.parts["glance"] = _glance(job)
    story = job.parts["case_story"]
    return {"written_by": story["by"], "events": len(story["events"]), "people": len(story["people"]),
            "leads_kept": len(job.parts["in_case"]["leads"])}


# -- P10 Case analyst ---------------------------------------------------------------------


def _analyst(job: ReportRun) -> dict[str, Any]:
    """The reports read in full, the statements explained, and - once the case is described - the evidence
    weighed (``team/case_analyst``)."""
    from sherlocks.linkgraph.relevance import the_new_case as described
    from sherlocks.team import case_analyst, case_papers, case_reader, document_reader

    case, scope, mine, record = job.case, job.scope, job.parts["in_case"], job.parts["record"]
    read_docs = [case.documents[d] for d in scope["documents"] if (case.documents.get(d) or {}).get("reading")]
    documents = [_document_part(case, d) for d in read_docs]
    case_reports = [r["id"] for r in mine["reports"]]
    reads = case_analyst.forensics(case, [*case_reports, *(r["doc"] for t in record["targets"] for r in t.get("reports_on") or [])],
                                   job.llm)
    firs = [d for d in [scope.get("main"), *scope["linked"], *(c["doc"] for t in record["targets"] for c in t["past_cases"])] if d]
    said = case_analyst.statements(case, firs, job.llm)
    own = {scope.get("main"), *scope["linked"]}
    for s in said:
        s["this_case"] = s["doc"] in own              # this case's own FIRs, or the target's earlier ones
    weighing = None
    if declared(case):
        story = job.parts["case_story"]
        ours = set(case_reports)
        weighing = case_analyst.weigh(case, {
            "the_officer_said": [f["statement"].replace("Stated by the officer: ", "") for f in case.live_facts()
                                 if f.get("by") == "officer"][-12:],
            "the_case": described(case), "how_the_targets_connect": group_view.lines(job.parts.get("group"), _graph_doc(job.case)),
            "everything_on_the_target": case_reader.read(case, job.net),
            "reports_of_this_case": [{"id": r["doc"], "report": r["title"], "findings": r["findings"], "indicates": r["indicates"],
                                      "not_stated": r["not_stated"]} for r in reads if r["doc"] in ours],
            "statements": [{"fir": s["fir"], "person": p["person"], "part": p["part"], "date": p["date"],
                            "says": p["explained"] or p["said"][:1500]} for s in said for p in s["people"]],
            "other_documents": [document_reader.outline(d) for d in read_docs],
            "story_of_the_case": story["events"][:60], "people_of_the_case": story["people"][:24]}, job.llm)
    job.parts["case_analysis"] = {"reports": reads, "statements": said, "weighing": weighing, "documents": documents,
                                  "papers": case_papers.check(case, job.net, scope, job.parts) if declared(case) else None}
    return {"reports_read": len(reads), "by_model": sum(r["by"] == "model" for r in reads),
            "statement_firs": len(said), "weighed": weighing is not None, "documents_read": len(documents),
            "weighing_lines_taken_out": len((weighing or {}).get("checked") or [])}


def _document_part(case: Any, doc: dict[str, Any]) -> dict[str, Any]:
    """An uploaded document as read in full (R7), for its own section of the report."""
    r = doc.get("reading") or {}

    def live(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [x for x in rows if not x.get("fact") or case.usable(x["fact"])]

    return {"doc": doc["id"], "title": str(doc.get("title") or doc["id"]).replace("Upload: ", ""),
            "kind": r.get("kind") or "", "issued_by": r.get("issued_by") or "", "date": r.get("date") or "",
            "about": r.get("about") or "", "summary": r.get("summary") or "", "top": r.get("top") or [],
            "findings": live(r.get("findings") or []), "evidence": live(r.get("evidence") or []),
            "leads": live(r.get("leads") or []), "people": r.get("people") or [], "said": live(r.get("said") or []),
            "events": len(r.get("events") or []), "pages": r.get("pages"), "parts": r.get("parts"),
            "parts_read": r.get("parts_read")}


# -- P5 Summary writer --------------------------------------------------------------------


class _Summary(Typed):
    established: list[str] = Field(default_factory=list, description="What the evidence of THIS case shows, the most "
                                   "telling first: what ties the target to the crime (or does not) - a recovery, a "
                                   "disclosure, a report's finding, a record that places them. One fact a point, with "
                                   "ids. Empty when the officer has not described the case.")
    disputed: list[str] = Field(default_factory=list, description="Each critical and major contradiction, both sides in "
                                "one line, starting with its id (X#).")
    unclear: list[str] = Field(default_factory=list, description="What is still missing for this case (the victim's "
                               "identity, a motive, a report awaited, a delay not explained), each with the question or "
                               "the record that would settle it. With ids.")
    stands: list[str] = Field(default_factory=list, description="Where the case stands: each line of inquiry with its "
                              "status, by id (H#), one a point. Sherlock's view, if given, labelled as not confirmed.")
    next_steps: list[str] = Field(default_factory=list, description="The five most useful actions for the officer, the "
                                  "most useful first, each with what it would settle.")

    @field_validator("*", mode="before")
    @classmethod
    def _points(cls, value: Any) -> Any:
        """A heading written as one piece of text is one point."""
        return [value] if isinstance(value, str) and value.strip() else ([] if isinstance(value, str) else value)


_SUMMARY_SYSTEM = ("You are a senior investigating officer writing the brief at the top of a case report for the "
                   "officer who works the case, in plain English, from the FINISHED PARTS of the report given - nothing "
                   "else. The report is about THE CASE and THE TARGET only. Write short points under fixed headings, "
                   "one fact a point, one line each (25 words at most): what the evidence shows (what ties the target "
                   "to the crime first), what does not fit, what is still missing, where the case stands, and the "
                   "five most useful next steps. A heading with nothing to say stays empty. Leave out anything that "
                   "is not about this case or its people: other people's cases, tenancy witnesses, landlords, "
                   "guarantors. A SIM registered in another person's name is common in Pakistan (a relative's or a "
                   "friend's) - never a point, a lead or a step by itself. "
                   "When several targets were searched together, HOW_THE_TARGETS_CONNECT says how the records of the "
                   "search tie them (never cite the officer for these): "
                   "give the strongest ties (who is accused together, family, a shared or consecutive number, the same "
                   "address, the people tied to two or more targets) as points under what the evidence shows, the "
                   "targets nothing ties to the others under what is missing, and the checks that would confirm a tie "
                   "as next steps. "
                   "CASE_DECLARED tells you whether the officer has said what case is being investigated. When it is "
                   "false, ASSUME NOTHING about that case: leave `established` and `stands` empty and do not call the "
                   "target an accused or a suspect of anything current. "
                   "A sentence that rests on a record ends with that record's id in [brackets] - only ids that appear "
                   "in the parts, such as [D1], [F7], [W2], [X1], [K4], [H2], or a police system's name such as "
                   "[PSRMS]. A sentence that rests on no such id is written without brackets. NEVER put the name of a "
                   "part or of a field in brackets. Say only what the parts show: no new judgment, no guess, nothing "
                   "about anyone's guilt. Sherlock's own view belongs only in `stands`, labelled as his view and not "
                   "confirmed. " + NEW_CASE + PLAIN + DATA_NOT_INSTRUCTIONS)


def _digest(job: ReportRun) -> dict[str, Any]:
    """The finished parts of THIS case, short enough for one call."""
    d, f = job.parts["D"], job.parts["F"]
    mine, story, target = job.parts["in_case"], job.parts["case_story"], job.parts["target"]
    cards = job.case.register.get("cards") or {}
    known = declared(job.case)
    out = {
        "case_declared": known, "the_new_case": the_new_case(job.case) if known else "not described by the officer yet",
        "story_of_the_case": {"in_short": story["in_short"], "events": story["events"][:24]},
        "people_in_this_case": story["people"] or job.scope["people"][:16],
        "how_the_targets_connect": group_view.lines(job.parts.get("group"), _graph_doc(job.case)),
        "the_target": [{"name": t["name"], "cnic": t["cnic"], "role_in_case": t["role_in_case"],
                        "status_in_case": t["status_in_case"], "where_at_the_time": t["presence"][:4],
                        "own_earlier_cases": [f"FIR {r['fir']} {r['offence']} ({r['role']}, {r['status']})" for r in t["firs"][:6]]}
                       for t in target["targets"]],
        "records": [{"id": doc, "record": cards[doc]["record"], "title": cards[doc]["title"],
                     "fields": [f"{x.get('name')}: {x.get('value')}" for x in cards[doc].get("fields") or []][:14]}
                    for doc in job.scope["documents"] if doc in cards][:12],
        "reports": mine["reports"][:10],
        "custody_and_pending_reports": {"gaps": [f"{c['exhibit']}: {g['to']} {g['days']} days after {g['from']} [{', '.join(g['refs'])}]"
                                                 for c in mine["custody"] for g in c.get("gaps") or []],
                                        "pending": [f"{p['what']} [{p['doc']}]" for p in mine["pending"]]},
        "statements": [{"name": r["name"], "summary": r["summary"], "statements": [w["id"] for w in r["statements"]],
                        "changes": [c["what"] for c in r["changes"]]} for r in mine["said"]][:10],
        "contradictions": [{"id": x["id"], "severity": x["severity"], "status": x["status"], "text": x["text"]}
                           for x in mine["contradictions"]],
        "leads": [{"id": k["id"], "status": k["status"], "hint": k["hint"], "next_step": k["next_step"]} for k in mine["leads"]],
        "hypotheses": [{"id": h["id"], "statement": h["statement"], "status": h["status"],
                        "against": ((d["matrix"] or {}).get("score") or {}).get(h["id"], {}).get("against")}
                       for h in ((d["matrix"] or {}).get("hypotheses") or job.base.get("hypotheses") or [])],
        "sherlocks_view_not_confirmed": job.base.get("conclusion"),
        "open_points": [{"ref": p["ref"], "unclear": p["unclear"], "question": p["question"]} for p in _ours(job, f["open_points"])][:12],
        "what_the_reports_found": [{"report": r["title"], "findings": r["findings"][:10], "indicates": r["indicates"][:6]}
                                   for r in (job.parts.get("case_analysis") or {}).get("reports") or []
                                   if r["doc"] in {x["id"] for x in mine["reports"]}],
        "the_analysis": {k: v for k, v in ((job.parts.get("case_analysis") or {}).get("weighing") or {}).items()
                         if k in ("question", "view", "gaps", "actions")},
        "documents_uploaded": [{"id": d["doc"], "what_it_is": d["kind"], "summary": d["summary"], "most_important": d["top"],
                                "findings": [f"{x['fact']}: {x['text']}" for x in d["findings"]][:15]}
                               for d in (job.parts.get("case_analysis") or {}).get("documents") or []],
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
    from sherlocks.evidence.questioner import RECORD_CHECK

    basis = case.dialog.get("case_basis")
    return bool(the_new_case(case)["described_by_the_officer"] or (basis and basis != RECORD_CHECK))


def _brief_lines(job: ReportRun) -> list[str]:
    """The case in short without the model: what the officer said and the FIR records."""
    from sherlocks.team.case_scope import _person_names

    inc = job.case.incident or {}
    main = job.scope.get("main")
    cite = f" [{main}]" if main else ""
    roles = "; ".join(f"{n}: {r}" for n, r in (job.parts["A"]["roles"] or {}).items())
    if main:
        # The case's own FIR tells it: what, where, when, and whom it names.
        data = job.case.documents[main].get("data") or {}
        accused = ", ".join(n for n, r in _person_names(data) if r.startswith("accused"))
        linked = [n for n in job.scope["fir_numbers"] if n != job.scope["this_fir"]]
        return [x for x in (
            " ".join(x for x in (str(data.get("sections") or inc.get("offence") or "An offence"),
                                 f"at {data['place']}" if data.get("place") else "",
                                 f"on {data['occurred']}" if data.get("occurred") else "") if x)
            + f" - FIR {job.scope['this_fir']}" + (f", PS {data['police_station']}" if data.get("police_station") else "")
            + (f", registered {data['reported']}" if data.get("reported") else "") + f".{cite}",
            (f"Accused named in the FIR: {accused}.{cite}" if accused else ""),
            (f"The officer's part for the target: {inc['target_status']}." if inc.get("target_status") else ""),
            (f"Linked FIR(s): {', '.join(linked)} - they name this case's FIR." if linked else "")) if x]
    return [x for x in (
        (f"FIR {inc['fir']}" + (f", PS {inc['police_station']}" if inc.get("police_station") else "") + f".{cite}"
         if inc.get("fir") else ""),
        (f"Offence: {inc['offence']}.{cite if inc.get('fir') else ''}" if inc.get("offence") else ""),
        (f"On {inc['date']}" + (f" at about {inc['time']}" if inc.get("time") else "")
         + (f", at {inc['place']}" if inc.get("place") else "") + "." if inc.get("date") else
         (f"Place: {inc['place']}." if inc.get("place") else "")),
        (f"Target: {inc['target_status']}." if inc.get("target_status") else ""),
        (f"Roles given by the officer: {roles}." if roles else "")) if x]


def _from_parts(job: ReportRun) -> dict[str, Any]:
    """The summary without the model: every line taken from a part of this case, with its ids."""
    d, f, mine = job.parts["D"], job.parts["F"], job.parts["in_case"]
    cards = job.case.register.get("cards") or {}
    established = [f"{cards[doc]['title']}: {x.get('name')} - {x.get('value')} [{doc}]"
                   for doc in job.scope["documents"] if doc in cards and not cards[doc].get("background")
                   for x in (cards[doc].get("fields") or [])[:3]][:10]
    hyps = (d["matrix"] or {}).get("hypotheses") or job.base.get("hypotheses") or []
    leader = (d["matrix"] or {}).get("leader")
    return {
        "brief": job.parts["case_story"]["in_short"] or _brief_lines(job), "established": established,
        "disputed": [f"{x['id']} {x['severity']}: {x['text']}" for x in mine["contradictions"]
                     if x["severity"] != "minor" and x["status"] == "open"][:8],
        "stands": [f"{h['id']} ({h['statement']}): {h['status']}" + (" - fewest against." if h["id"] == leader else ".")
                   for h in hyps[:5]],
        "unclear": [f"{p['unclear']} Ask: {p['question']} [{p['ref']}]" for p in _ours(job, f["open_points"])][:8],
        "next_steps": [],
    }


def _ours(job: ReportRun, points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The open points of THIS case: a point about another FIR on the board (its ballistics, its accused's
    record) is that case's, not this one's."""
    from sherlocks.team.case_scope import in_scope

    mine = job.parts["in_case"]
    keep = {k["id"] for k in mine["leads"]} | {x["id"] for x in mine["contradictions"]}
    return [p for p in points if str(p.get("ref") or "") in keep or in_scope(job.case, job.scope, str(p.get("ref") or ""))]


def _is_this_case(job: ReportRun, doc: dict[str, Any]) -> bool:
    from sherlocks.team.recorder import _fir_number

    if doc.get("id") in (job.scope.get("linked") or []):
        return True                                  # an FIR that names this case's FIR is part of it
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
            if number[0] and not mine and "/".join(number) not in (job.scope.get("fir_numbers") or []):
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
    from sherlocks.team.case_scope import weak

    for h in job.base.get("hypotheses") or []:
        # A SIM in another person's name, a tenancy witness, a guarantor: common, never evidence for a line of inquiry.
        h["for"] = [i for i in h.get("for") or [] if not weak(job.case, i)]
        h["against"] = [i for i in h.get("against") or [] if not weak(job.case, i)]
    job.base["hypotheses"] = [h for h in job.base.get("hypotheses") or [] if h["for"] or h["against"]]
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
              system=_SUMMARY_SYSTEM, kind="report_summary", version="v3", max_tokens=2200)
    if out is None:
        job.summary, job.summary_by = _from_parts(job), "templates"
    else:
        job.summary, job.summary_by = out.model_dump(), "model"
    known = job.case.known_ids() | {g["id"] for g in job.base.get("graph_evidence") or []}
    for name, value in list(job.summary.items()):
        # Only a real id (or a police system's name) may stand in brackets: anything else is taken out.
        job.summary[name] = real_refs(value, known) if isinstance(value, str) else [real_refs(x, known) for x in value]
    if not job.summary.get("brief"):
        # The case in short is the story writer's; without it, what the officer said and the FIR record.
        job.summary["brief"] = job.parts["case_story"]["in_short"] or _brief_lines(job)
    for name in SUMMARY:
        value = job.summary.get(name) or []
        value = [value] if isinstance(value, str) else [str(x).strip().lstrip("•- ").strip() for x in value if str(x).strip()]
        seen: set[str] = set()
        value = [x for x in value if x.lower() not in seen and not seen.add(x.lower())]     # the same point once
        job.summary[name] = value[:SUMMARY_MOST[name]]
    if job.case.dialog.get("case_basis") != "fir_on_record":
        job.summary["next_steps"] = [line for line in job.summary["next_steps"] if not names_past_fir(job, line)]
    for name in ("established", "stands"):
        # A point that rests only on an earlier case, or is about one, does not "establish" this one.
        job.summary[name] = [line for line in job.summary[name]
                             if not (ID.findall(line) and all(past_only(job, i) for i in ID.findall(line)))
                             and not names_past_fir(job, line)]
    weighing = (job.parts.get("case_analysis") or {}).get("weighing") or {}
    if declared(job.case) and weighing:
        # What is still missing, Sherlock's view and the next steps are the analysis's: this case's, and reasoned.
        if weighing.get("gaps"):
            # The three that matter most; all of them are under "Questions still open".
            job.summary["unclear"] = weighing["gaps"][:3]
        if weighing.get("view"):
            job.summary["stands"] = [f"Sherlock's view (not confirmed): {weighing['view']}"]
        if weighing.get("actions"):
            job.summary["next_steps"] = [a["action"] + (f" (settles: {a['settles'][:1].lower() + a['settles'][1:]})"
                                                       if a["settles"] else "")
                                         for a in weighing["actions"]][:SUMMARY_MOST["next_steps"]]
    if declared(job.case) and not job.summary["stands"]:
        job.summary["stands"] = [job.base.get("assessment_note") or "No line of inquiry about this case is on the board yet."]
    if not declared(job.case):
        # The officer has not said what the case is: nothing is assumed about it. This is said in
        # fixed words, and what would weigh or establish "the case" is left out - by code.
        from sherlocks.evidence.questioner import only_the_record

        which = next((q["text"] for q in job.case.open_questions() if q["key"] == "case:which"), "")
        record = [line for t in job.parts["record"]["targets"] for line in t["overview"]]
        no_case = only_the_record(job.case)
        job.summary.update({"brief": [NO_CASE if no_case else NOT_DECLARED, *record], "established": [],
                            "stands": [NOT_WEIGHED]})
        steps = [] if no_case else ["Tell Sherlock what is being investigated - which case, what happened, when and "
                                    "where. " + (which or "Until then nothing in this report is about that case.")]
        job.summary["next_steps"] = (steps + [s for s in job.summary["next_steps"] if s not in steps])[:SUMMARY_MOST["next_steps"]]
    return {"written_by": job.summary_by, "again": bool(job.send_back), "case_declared": declared(job.case)}


# -- P6 Report checker --------------------------------------------------------------------


def _body_ids(job: ReportRun) -> set[str]:
    return set(ID.findall(dumps({k: job.parts[k] for k in (*PARTS, "annex", "target", "in_case", "case_story", "case_analysis")
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
            # The case in short is the story writer's, already held to the case's own ids.
            unbacked = [i for i in ids if i in known and i not in body and name != "brief"]
            needs_id = name in ("established", "disputed") and not ids and "[" not in line
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
    """The brief as text (the file version: lines of inquiry in place of Sherlock's view)."""
    blocks = []
    for name in SUMMARY:
        value = summary.get("stands_file" if file and name == "stands" else name)
        text = value if isinstance(value, str) else "\n".join(f"• {x}" for x in value or [])
        if text.strip():
            blocks.append(f"{'Lines of inquiry' if file and name == 'stands' else SUMMARY_TITLE[name]}\n{text.strip()}")
    return "\n\n".join(blocks)


def _build(job: ReportRun) -> dict[str, Any]:
    case = job.case
    d = job.parts["D"]
    # The file version's "where the case stands": the lines of inquiry, no ranking, no view on anyone.
    hyps = (d["matrix"] or {}).get("hypotheses") or job.base.get("hypotheses") or []
    job.base.pop("kept_to_this_case", None)
    job.summary["stands_file"] = (NOT_WEIGHED if not declared(case) else
                                  [h["statement"] for h in hyps[:6]] if hyps else "No line of inquiry is recorded yet.")
    report = {**job.base, "parts": job.parts, "summary_parts": job.summary, "summary_titles": SUMMARY_TITLE,
              "summary_order": list(SUMMARY), "case_declared": declared(case),
              "structure": [{"key": k, "title": t, "written_by": who, "holds": what} for k, t, who, what in STRUCTURE],
              "executive_summary": summary_text(job.summary), "summary_by": job.summary_by,
              "model": getattr(job.llm, "model", None) if job.summary_by == "model" else None,
              "built_without_model": job.summary_by != "model", "checked": job.problems,
              "report_board_version": job.version, "team": "P1-P13", "build": REPORT_BUILD}
    with case.acting("P1 Report editor"):
        case.set_report(report, "ready")
    report["saved_at_version"] = case.version
    job.report = report
    return {"board_version": job.version, "warnings": len(job.problems)}


def build() -> Any:
    g = StateGraph(ReportState)
    g.add_node("freeze", _member("P1 Report editor", _freeze))
    g.add_node("scope", _member("P1 Report editor", _scope))
    g.add_node("evidence", _member("P2 Evidence writer", _evidence))
    g.add_node("statements", _member("P3 Statements and timeline writer", _statements))
    g.add_node("analysis", _member("P4 Analysis writer", _analysis))
    g.add_node("editor", _member("P1 Report editor", _editor))
    g.add_node("target", _member("P2 Target writer", _target))
    g.add_node("group", _member("P12 Group writer", _group))
    g.add_node("record", _member("P8 Record writer", _record))
    g.add_node("story", _member("P7 Story writer", _case_story))
    g.add_node("analyst", _member("P10 Case analyst", _analyst))
    g.add_node("sections", _member("P13 Section writer", _sections))
    g.add_node("summary", _member("P5 Summary writer", _summary))
    g.add_node("check", _member("P6 Report checker", _check))
    g.add_node("build", _member("P1 Report editor", _build))
    g.add_edge(START, "freeze")
    g.add_edge("freeze", "scope")
    for writer in ("evidence", "statements", "analysis"):       # the three writers work side by side
        g.add_edge("scope", writer)
    g.add_edge(["evidence", "statements", "analysis"], "editor")
    g.add_edge("editor", "target")
    g.add_edge("target", "group")
    g.add_edge("group", "record")
    g.add_edge("record", "story")
    g.add_edge("story", "analyst")
    g.add_edge("analyst", "sections")
    g.add_edge("sections", "summary")
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
