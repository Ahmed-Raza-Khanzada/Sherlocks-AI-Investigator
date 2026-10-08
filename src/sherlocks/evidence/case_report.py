"""The case report: what the investigation established about the targets, and on what.

Written when the graph finishes (or is stopped), from two kinds of evidence, each with
an id the report cites:

* ``G#`` - graph evidence: a route between two targets, a target's FIR involvement or
  hotel stay, a linkage pattern the rules found - each naming the police systems that
  state it;
* ``D#`` / ``F#`` / ``L#`` - the case file: documents read, the quoted facts taken from
  them, and the people found inside them.

**S3 Report writer: the report is Sherlock's work, in full.** It writes no new
judgments: it lays out what the Sherlock team already concluded on the case board -
the summary and timeline (S2), the hypotheses with their evidence for and against,
contradictions, suspicions and next steps (S1), the ranked gaps (S4) - with the CDR
analysis, the officer's statements and every question asked, Sherlock's quick views
from the chat and whether they held, what was checked and found nothing, and the audit
of live calls. Every line cites ids (G, D, F, L, H, Q, rows, call ids); a line citing
nothing real is dropped; replaced and stale entries stay out of the body and appear
only under "How the assessment changed". The report is tied to a board version and
says whether the assessment is current or "as of" an earlier version. The model is
used only to join the summary into readable paragraphs (numbers and ids guarded);
without it every section is built from the board.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.linkgraph.scenarios import find_scenarios
from sherlocks.linkgraph.systems import system_label

logger = logging.getLogger(__name__)

_ID = re.compile(r"\b([GDFLHQ]\d{1,4})\b")
_ROLE_ORDER = ("accused", "suspect", "complainant", "witness", "victim")


def _now() -> str:
    return datetime.now(UTC).strftime("%d %b %Y, %H:%M UTC")


def _clip(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + " …"


class _Joined(BaseModel):
    text: str = Field(description="The same content as 2 short paragraphs for senior officers; every id in [brackets] "
                                  "and every number kept exactly.")


_JOIN_SYSTEM = ("You join an investigation summary into two readable paragraphs for senior officers. Keep every "
                "fact, number, name and id in [brackets] exactly; add nothing, drop nothing that matters, no opinion.")


# --------------------------------------------------------------------------------------
# Graph evidence
# --------------------------------------------------------------------------------------


def _target(net: PersonNetwork, pid: str, case: CaseFile) -> dict[str, Any]:
    d = net.data(pid)
    prox = net.criminal_proximity(pid) or {}
    route = prox.get("route") or {}
    return {
        "id": pid, "name": net.name(pid), "father": d.get("father_name"), "cnic": d.get("cnic"),
        "phones": (d.get("phones") or [])[:4], "addresses": (d.get("addresses") or [])[:2],
        "images": (d.get("images") or [])[:3], "flags": d.get("flags") or [],
        "firs": d.get("firs") or [], "stays": d.get("stays") or [], "vehicles": d.get("vehicles") or [],
        "organisations": d.get("organisations") or [], "records": len(d.get("records") or []),
        "links": net.G.degree(pid) if pid in net.G else 0,
        "criminal": net.criminal(pid),
        "nearest_criminal": None if prox.get("self_criminal") or not prox.get("nearest") else {
            "name": prox.get("nearest_name"), "hops": prox.get("hops"),
            "route": [h["relation"] for h in route.get("hops") or []]},
        "evidence": case.for_person(pid),
        "role": case.roles.get(pid),
    }


def graph_evidence(graph: dict[str, Any], net: PersonNetwork, findings: list[dict]) -> list[dict[str, Any]]:
    """Citable statements the graph makes, ``G1``…"""
    items: list[dict[str, Any]] = []

    def add(kind: str, text: str, sources: list[str], people: list[str], **extra: Any) -> str:
        gid = f"G{len(items) + 1}"
        items.append({"id": gid, "kind": kind, "text": text, "sources": sorted({s for s in sources if s}),
                      "people": people, **extra})
        return gid

    seeds = net.seeds()
    for a, b in itertools.combinations(seeds, 2):
        routes = net.paths(a, b, k=2)
        if not routes:
            add("no_route", f"No connection between {net.name(a)} and {net.name(b)} was found in the records searched.",
                [], [a, b])
        for route in routes:
            hops = "; ".join(h["relation"] + (" (inferred)" if h["kind"] == "weak" else "") for h in route["hops"])
            add("route", f"{net.name(a)} → {net.name(b)} in {len(route['hops'])} step(s): {hops}",
                [h["via"] for h in route["hops"]], route["nodes"], inferred=route["inferred"],
                hops=route["hops"])
    for pid in seeds:
        d = net.data(pid)
        for fir in d.get("firs") or []:
            add("fir", f"{net.name(pid)} is recorded as {fir.get('role') or 'named'} in FIR {fir.get('label')} at "
                       f"{fir.get('ps') or '-'}" + (f" ({fir['offence']})" if fir.get("offence") else ""),
                [system_label(fir.get("system") or "")], [pid])
        for stay in d.get("stays") or []:
            add("stay", f"{net.name(pid)} stayed at {stay.get('hotel')} ({stay.get('district') or '-'})"
                        + (f", checked in {stay['check_in']}" if stay.get("check_in") else ""), [system_label("hotel_eye")], [pid])
        for other in net.neighbours(pid)[:8] if pid in net.G else []:
            add("link", f"{net.name(pid)} – {other['relation']}" + ("" if other["stated"] else " (inferred)"),
                [other["via"]], [pid, other["id"]], stated=other["stated"])
    for f in findings[:12]:
        add("finding", f"{f['title']} ({f['tier']}): {f['summary']}",
            [e.get("system") or "" for e in f.get("evidence") or []], f["people"], tier=f["tier"],
            title=f["title"], summary=f["summary"],
            evidence=[e["text"] for e in f.get("evidence") or []][:4])
    return items


def _timeline(net: PersonNetwork, case: CaseFile) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for doc in case.documents.values():
        data = doc.get("data") or {}
        if doc["kind"] == "fir":
            if data.get("occurred"):
                rows.append({"when": data["occurred"], "what": f"Occurrence: {doc['title']} ({data.get('sections') or '-'})",
                             "ref": doc["id"]})
            if data.get("reported"):
                rows.append({"when": data["reported"], "what": f"FIR registered: {doc['title']}", "ref": doc["id"]})
            for diary in data.get("case_diaries") or []:
                rows.append({"when": diary.get("date") or "", "what": f"Case diary {diary.get('no')} - {doc['title']}: "
                             + (diary.get("remarks") or "")[:160], "ref": doc["id"]})
        elif doc["kind"] == "lab":
            received = re.search(r"case received ([\d\-: ]+)", doc.get("summary") or "")
            rows.append({"when": received.group(1).strip() if received else "", "what": doc["title"], "ref": doc["id"]})
    for pid in net.seeds():
        for stay in net.data(pid).get("stays") or []:
            rows.append({"when": stay.get("check_in") or "", "what": f"{net.name(pid)} at {stay.get('hotel')}",
                         "ref": "Hotel Eye"})
    return sorted([r for r in rows if r["when"]], key=lambda r: _date_key(r["when"]))


def _date_key(text: str) -> str:
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})", text)
    if m:
        d, mth, y = m.groups()
        y = y if len(y) == 4 else f"20{y}"
        return f"{y}-{int(mth):02d}-{int(d):02d}{text}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    return f"{m.group(0)}{text}" if m else f"9999{text}"


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------


def _valid_ids(text: str, known: set[str]) -> list[str]:
    return [i for i in dict.fromkeys(_ID.findall(text or "")) if i in known]


def _join(llm: Any, text: str) -> str:
    """The model joins the summary into paragraphs - only if no number or id changes."""
    from sherlocks.linkgraph.presenter import format_guard

    if llm is None or not text.strip():
        return text
    try:
        out, _ = llm.generate_structured(prompt=text, schema=_Joined, system=_JOIN_SYSTEM, cache_kind="report_join",
                                         prompt_version="v1", max_tokens=900)
        joined = out.text.strip()
        return joined if joined and not format_guard(text, joined) else text
    except Exception as exc:  # noqa: BLE001 - the board's own sentences stand
        logger.info("Report join failed: %s", exc)
        return text


def board_sections(case: CaseFile, net: PersonNetwork, graph: dict[str, Any], llm: Any = None) -> dict[str, Any]:
    """Every section of the report, laid out from the case board."""
    from sherlocks.evidence.guard import audit_summary
    from sherlocks.evidence.trust import conflicts
    from sherlocks.linkgraph.casework import not_covered

    known = case.known_ids()
    assessment = case.assessment or {}
    live = {f["id"]: f for f in case.live_facts()}

    def cited(ids: list[str]) -> list[str]:
        return [i for i in dict.fromkeys(ids) if i in known and (i not in case.facts or i in live)]

    hyps = [h for h in case.hypotheses.values() if not h.get("stale")]
    order = {"supported": 0, "open": 1, "weakened": 2, "ruled out": 3}
    hyps.sort(key=lambda h: (order.get(h["status"], 4), {"high": 0, "medium": 1, "low": 2}.get(h["confidence"], 3)))
    hypotheses = [{"id": h["id"], "statement": h["statement"], "status": h["status"], "confidence": h["confidence"],
                   "tier": h["tier"], "people": [net.name(p) for p in h.get("people") or [] if p in net.people],
                   "for": cited(h.get("for") or []), "against": cited(h.get("against") or [])}
                  for h in hyps if h["tier"] != "speculation"]
    suspicions = [{"id": h["id"], "statement": h["statement"], "for": cited(h.get("for") or [])}
                  for h in hyps if h["tier"] == "speculation"]
    suspicions += [{"id": None, "statement": s, "for": []} for s in assessment.get("speculation") or []]
    found_conflicts = conflicts(case, net)
    contradictions = [{"topic": c["topic"], "values": [f"{v['value']} ({v['source']}{', ' + v['ref'] if v.get('ref') else ''})"
                                                       for v in c["values"]], "leads": c["values"][0]["value"],
                       "settle": c["settle"]} for c in found_conflicts]
    contradictions += [{"topic": "", "values": [], "leads": "", "settle": t} for t in assessment.get("contradictions") or []
                       if not any(t == c["settle"] for c in contradictions)]
    history = [{**h, "statement": (case.hypotheses.get(h["id"]) or {}).get("statement", "")} for h in case.history]
    replaced = [{"id": f["id"], "statement": f["statement"], "replaced_by": f["replaced_by"]}
                for f in case.facts.values() if f.get("replaced_by")]
    stale = [{"id": f["id"], "statement": f["statement"], "why": f.get("stale_reason")}
             for f in case.facts.values() if f.get("stale")]
    # CDR analysis, per file.
    cdrs = []
    for doc in case.documents.values():
        if doc["kind"] != "cdr":
            continue
        analysis = (doc.get("data") or {}).get("analysis") or {}
        rows = [live[f] for f in doc.get("facts") or [] if f in live]
        cdrs.append({"id": doc["id"], "title": doc["title"], "subject": analysis.get("subject"),
                     "owner": ", ".join(net.name(o) for o in doc.get("owners") or [] if o in net.people) or None,
                     "summary": doc.get("summary") or "",
                     "findings": [{"id": f["id"], "text": f["statement"], "tier": f.get("tier", "fact"), "rows": f.get("rows")}
                                  for f in rows][:60]})
    statements = [{"id": f["id"], "statement": f["statement"].replace("Stated by the officer: ", "")}
                  for f in live.values() if f.get("by") == "officer"]
    qa = [{"id": q["id"], "question": q["text"], "status": q["status"], "answer": q.get("answer"),
           "asked": q.get("times", 0), "turns": q.get("asked_turns") or [], "answer_turn": q.get("answer_turn"),
           "earlier": q.get("answers") or []} for q in case.questions]
    views = [{"id": v["id"], "turn": v.get("turn"), "question": v["question"], "view": v["view"],
              "status": v.get("status", "given"), "note": v.get("checked", "")} for v in case.views]
    nothing = [f"{a['kind'].upper()} {a['key']}: {a['message'] or a['status']}" for a in case.attempts
               if a["status"] in ("none", "error", "skipped")]
    for key, status in (case.dialog.get("searched") or {}).items():
        pid, _, system = key.partition("|")
        if status == "nothing found":
            nothing.append(f"{system.upper()} for {net.name(pid) if pid in net.people else pid}: checked, nothing found")
    nothing += [f"{d['title']}: page(s) {', '.join(map(str, d['unread_pages']))} could not be read"
                for d in case.documents.values() if d.get("unread_pages")]
    next_steps = list(assessment.get("next_steps") or [])
    next_steps += [g["text"] for g in case.gaps if g.get("decision") in (None, "approved")][:6]
    summary = case.summary or {}
    conclusion = assessment.get("answer") or ""
    spec_ids = {f["id"] for f in live.values() if f.get("tier") == "speculation"} | {s["id"] for s in suspicions if s["id"]}
    sentences = [x for x in re.split(r"(?<=[.!?])\s+", f"{summary.get('text') or ''} {conclusion}".strip()) if x]
    # Facts only in the executive summary: a sentence resting on a suspicion goes to its own section.
    exec_text = " ".join(x for x in sentences if not set(_ID.findall(x)) & spec_ids and "[H" not in x)
    covered = not_covered(case)
    return {
        "board_version": case.version,
        "assessment_version": assessment.get("board_version"),
        "status": "current" if case.assessment is not None and not covered else "as_of",
        "as_of": assessment.get("at"),
        "not_covered": [f"{c['entry']} {c.get('id') or ''} ({c.get('agent')})".strip() for c in covered][:40],
        "executive_summary": _join(llm, exec_text) if exec_text else "",
        "conclusion": conclusion,
        "hypotheses": hypotheses, "suspicions": suspicions, "contradictions": contradictions,
        "concerns": assessment.get("concerns") or [], "history": history, "replaced": replaced, "stale": stale,
        "cdrs": cdrs, "statements": statements, "qa": qa, "views": views, "nothing_found": nothing[:60],
        "next_steps": list(dict.fromkeys(next_steps))[:10], "audit": audit_summary(case),
        "timeline_board": summary.get("timeline") or [],
        "assessment_model": assessment.get("model"),
    }


def _rule_written(targets: list[dict], gitems: list[dict], case: CaseFile, findings: list[dict],
                  net: PersonNetwork) -> dict[str, Any]:
    """The report's opening sections when Sherlock has not assessed the case yet."""
    names = ", ".join(t["name"] for t in targets) or "the subjects"
    routes = [g for g in gitems if g["kind"] == "route"]
    stated = [g for g in routes if not g.get("inferred")]
    counts = case.counts()
    para1 = (f"This report covers {names}. The search reached {len(net.people)} people across the police and "
             f"government records queried and read {counts['documents']} case document(s) "
             f"({counts['facts']} quoted fact(s)).")
    if len(targets) > 1:
        para1 += (f" {len(stated)} stated and {len(routes) - len(stated)} inferred route(s) join the targets"
                  + (f" [{stated[0]['id']}]." if stated else "."))
    crim = [t for t in targets if t["criminal"]]
    para2 = (f"{len(crim)} target(s) carry a criminal footprint: " + ", ".join(
        f"{t['name']} ({len(t['firs'])} FIR record(s))" for t in crim) + "." if crim else
        "No target carries a criminal record in the systems searched.")
    assessments = []
    for g in [*stated[:3], *[g for g in gitems if g["kind"] == "finding" and g.get("tier") in ("stated", "corroborated")][:4]]:
        assessments.append({"statement": g["text"], "confidence": "high" if g["kind"] == "route" or g.get("tier") == "stated"
                            else "medium", "basis": [g["id"]]})
    leads = net.unsearched_leads(top=4)
    recs = [f"Search {lead['name']} ({lead['why']})." for lead in leads]
    no_labs = [a for a in case.attempts if a["kind"] == "lab" and a["status"] == "none"]
    open_q = [f"No forensic or medical report is filed for {a['key'].split(':', 1)[1]}." for a in no_labs[:3]]
    return {"executive_summary": f"{para1}\n\n{para2}", "assessments": assessments[:8],
            "open_questions": open_q, "recommendations": recs[:6], "model": None}


def assemble(graph: dict[str, Any], case: CaseFile, *, llm: Any = None, run: dict[str, Any] | None = None) -> dict[str, Any]:
    """The report as data (rendered to PDF by ``report_pdf``): the case board laid out -
    Sherlock's assessment and everything it rests on. No new judgment is written here."""
    net = PersonNetwork(graph)
    findings = find_scenarios(graph, net=net)
    seeds = net.seeds() or [k["id"] for k in net.key_people(top=1)]
    targets = [_target(net, pid, case) for pid in seeds]
    gitems = graph_evidence(graph, net, findings)
    board = board_sections(case, net, graph, llm)
    rule = _rule_written(targets, gitems, case, findings, net)
    # Sherlock's hypotheses are the assessments; before his first run, the rules' strongest links.
    assessments = [{"statement": h["statement"], "confidence": h["confidence"], "basis": h["for"] or h["against"],
                    "status": h["status"], "id": h["id"]} for h in board["hypotheses"] if h["for"] or h["against"]]
    written = {
        "executive_summary": board["executive_summary"] or rule["executive_summary"],
        "assessments": assessments or rule["assessments"],
        "open_questions": [q["question"] for q in board["qa"] if q["status"] == "open"] or rule["open_questions"],
        "recommendations": board["next_steps"] or rule["recommendations"],
        "model": board["assessment_model"],
    }
    known = {g["id"] for g in gitems} | case.known_ids()
    cited = set()
    texts = [written["executive_summary"], *[" ".join(a["basis"]) for a in written["assessments"]],
             *[" ".join(h["for"] + h["against"]) for h in board["hypotheses"]],
             *[f["id"] for c in board["cdrs"] for f in c["findings"]], *[s["id"] for s in board["statements"]],
             *[q["id"] for q in board["qa"]]]
    for text in texts:
        cited |= set(_valid_ids(text, known))
    run = run or {}
    stats = {"people": len(net.people), "links": net.G.number_of_edges(), **case.counts(),
             "findings": len(findings), "hypotheses": len(board["hypotheses"])}
    timeline = board["timeline_board"] or _timeline(net, case)
    return {
        "title": "Case Intelligence Report",
        "subject": ", ".join(t["name"] for t in targets) or "Link analysis",
        "generated_at": _now(), "run": {k: run.get(k) for k in ("id", "seed_label", "backend", "status", "created_at")},
        "stats": stats, "targets": targets, "graph_evidence": gitems, "findings": findings[:12],
        "key_people": net.key_people(top=8), "timeline": timeline, **written, **{
            k: v for k, v in board.items() if k not in ("executive_summary", "timeline_board")},
        "cited": sorted(cited, key=lambda x: (x[0], int(re.sub(r"\D", "", x) or 0))),
        "incident": case.incident,
        "roles": {net.name(p): r for p, r in case.roles.items() if p in net.people},
        "uploads": [{"id": d["id"], "title": d["title"], "summary": d.get("summary")}
                    for d in case.documents.values() if d["kind"] in ("upload", "cdr")],
    }


def check_report(report: dict[str, Any], case: CaseFile) -> list[str]:
    """The Answer checker on the report: every id real, no out-of-date entry in the body,
    no suspicion in the executive summary. Returns the problems found (and fixes them in
    place by dropping what fails)."""
    problems = []
    known = {g["id"] for g in report.get("graph_evidence") or []} | case.known_ids()
    for a in report.get("assessments") or []:
        bad = [b for b in a.get("basis") or [] if b not in known or not case.usable(b)]
        if bad:
            problems.append(f"Assessment cites {', '.join(bad)} - not on the board or out of date: dropped from its basis.")
            a["basis"] = [b for b in a["basis"] if b not in bad]
    report["assessments"] = [a for a in report.get("assessments") or [] if a.get("basis")]
    spec = {s["id"] for s in report.get("suspicions") or [] if s.get("id")}
    summary = report.get("executive_summary") or ""
    if spec & set(_ID.findall(summary)) or "[H" in summary:
        problems.append("A suspicion was in the executive summary: removed.")
        report["executive_summary"] = " ".join(x for x in re.split(r"(?<=[.!?])\s+", summary)
                                               if not (spec & set(_ID.findall(x))) and "[H" not in x)
    stale = [f["id"] for c in report.get("cdrs") or [] for f in c["findings"] if not case.usable(f["id"])]
    if stale:
        problems.append(f"Out-of-date CDR findings left out: {', '.join(stale)}.")
        for c in report.get("cdrs") or []:
            c["findings"] = [f for f in c["findings"] if case.usable(f["id"])]
    return problems
