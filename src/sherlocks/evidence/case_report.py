"""The case report: what the investigation established about the targets, and on what.

Written when the graph finishes (or is stopped), from two kinds of evidence, each with
an id the report cites:

* ``G#`` - graph evidence: a route between two targets, a target's FIR involvement or
  hotel stay, a linkage pattern the rules found - each naming the police systems that
  state it;
* ``D#`` / ``F#`` / ``L#`` - the case file: documents read, the quoted facts taken from
  them, and the people found inside them.

The model ("Sherlock") writes the executive summary, the assessments, the open
questions and the recommendations. Every assessment must cite at least one of those
ids; an assessment citing nothing real is dropped, so the report can never assert
something it cannot point at. With no model, the same sections are written by rule.
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

_ID = re.compile(r"\b([GDFL]\d{1,4})\b")
_ROLE_ORDER = ("accused", "suspect", "complainant", "witness", "victim")


def _now() -> str:
    return datetime.now(UTC).strftime("%d %b %Y, %H:%M UTC")


def _clip(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + " …"


class _Assessment(BaseModel):
    statement: str = Field(description="One or two sentences: a conclusion about the targets and their links.")
    confidence: Literal["high", "medium", "low"] = Field(
        description="high = stated by systems/documents and consistent; medium = corroborated pattern; low = lead.")
    basis: list[str] = Field(default_factory=list, description="Evidence ids it rests on: G#, D#, F#, L#.")


class _Written(BaseModel):
    executive_summary: str = Field(description="2 short paragraphs for senior officers, citing ids in [brackets].")
    assessments: list[_Assessment] = Field(default_factory=list, description="Up to 8, strongest first.")
    open_questions: list[str] = Field(default_factory=list, description="Up to 5 things the evidence leaves open.")
    recommendations: list[str] = Field(default_factory=list, description="Up to 6 concrete next actions.")


_SYSTEM = (
    "You are Sherlock, the senior analyst writing the case report on a police link-analysis investigation for "
    "upper management. You receive the targets, graph evidence (G#), documents (D#), quoted facts (F#) and "
    "evidence links (L#). Rules: use ONLY this evidence; cite the ids each sentence rests on in [brackets], e.g. "
    "[G2][F7]; never invent a person, number, date or event; distinguish what records STATE from what patterns "
    "SUGGEST; inferred or name-only links are leads, say so; be concise, formal and specific (names, FIR "
    "numbers, dates). Every assessment needs a basis of ids."
)


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


def _rule_written(targets: list[dict], gitems: list[dict], case: CaseFile, findings: list[dict],
                  net: PersonNetwork) -> dict[str, Any]:
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
    for fact in list(case.facts.values())[:3]:
        if fact["kind"] in ("forensic", "statement", "event") or fact["by"] == "ai":
            assessments.append({"statement": fact["statement"], "confidence": "medium", "basis": [fact["id"], fact["doc"]]})
    leads = net.unsearched_leads(top=4)
    recs = [f"Search {lead['name']} ({lead['why']})." for lead in leads]
    unread = [d for d in case.documents.values() if d.get("unread_pages")]
    recs += [f"Read {d['title']} manually: page(s) {', '.join(map(str, d['unread_pages']))} could not be read." for d in unread[:2]]
    no_labs = [a for a in case.attempts if a["kind"] == "lab" and a["status"] == "none"]
    open_q = [f"No forensic or medical report is filed for {a['key'].split(':', 1)[1]}." for a in no_labs[:3]]
    return {"executive_summary": f"{para1}\n\n{para2}", "assessments": assessments[:8],
            "open_questions": open_q, "recommendations": recs[:6], "model": None}


def _llm_written(llm: Any, targets: list[dict], gitems: list[dict], case: CaseFile, known: set[str]) -> dict[str, Any]:
    brief_targets = [{k: t[k] for k in ("name", "cnic", "phones", "flags", "criminal", "nearest_criminal")}
                     | {"firs": [f"{f.get('label')} {f.get('role')} {f.get('offence') or ''}" for f in t["firs"]][:8]}
                     for t in targets]
    docs = [{"id": d["id"], "title": d["title"], "source": d["source"],
             "summary": d.get("ai_summary") or d.get("summary")} for d in case.documents.values()]
    facts = [{"id": f["id"], "doc": f["doc"], "fact": f["statement"], "quote": f["quote"][:160]} for f in case.facts.values()]
    links = [{"id": link["id"], "doc": link["doc"], "person": link["name"], "how": link["how"]} for link in case.links.values()]
    prompt = (f"Targets: {_clip(brief_targets, 3000)}\n\nGraph evidence: {_clip([{k: g[k] for k in ('id', 'text', 'sources')} for g in gitems], 7000)}"
              f"\n\nDocuments: {_clip(docs, 3000)}\n\nFacts: {_clip(facts, 7000)}\n\nEvidence links: {_clip(links, 2000)}"
              "\n\nWrite the case report sections as JSON.")
    written, _ = llm.generate_structured(prompt=prompt, schema=_Written, system=_SYSTEM, cache_kind="case_report",
                                         prompt_version="v1", max_tokens=3000)
    assessments = []
    for a in written.assessments[:8]:
        basis = [i for i in dict.fromkeys([*a.basis, *_ID.findall(a.statement)]) if i in known]
        if not basis:
            continue  # an assessment that cites nothing real is not reported
        assessments.append({"statement": re.sub(r"\s*\[[^\]]*\]", "", a.statement).strip(), "confidence": a.confidence,
                            "basis": basis})
    return {"executive_summary": written.executive_summary.strip(), "assessments": assessments,
            "open_questions": written.open_questions[:5], "recommendations": written.recommendations[:6],
            "model": getattr(llm, "model", None) or "llm"}


def assemble(graph: dict[str, Any], case: CaseFile, *, llm: Any = None, run: dict[str, Any] | None = None) -> dict[str, Any]:
    """The report as data (rendered to PDF by ``report_pdf``)."""
    net = PersonNetwork(graph)
    findings = find_scenarios(graph, net=net)
    seeds = net.seeds() or [k["id"] for k in net.key_people(top=1)]
    targets = [_target(net, pid, case) for pid in seeds]
    gitems = graph_evidence(graph, net, findings)
    known = {g["id"] for g in gitems} | set(case.documents) | set(case.facts) | set(case.links)

    written = None
    if llm is not None:
        try:
            written = _llm_written(llm, targets, gitems, case, known)
        except Exception as exc:  # noqa: BLE001 - the rule report is always available
            logger.info("Case report model failed: %s", exc)
    written = written or _rule_written(targets, gitems, case, findings, net)

    cited = set()
    for text in [written["executive_summary"], *[" ".join(a["basis"]) for a in written["assessments"]]]:
        cited |= set(_valid_ids(text, known))
    run = run or {}
    stats = {"people": len(net.people), "links": net.G.number_of_edges(), **case.counts(),
             "findings": len(findings)}
    return {
        "title": "Case Intelligence Report",
        "subject": ", ".join(t["name"] for t in targets) or "Link analysis",
        "generated_at": _now(), "run": {k: run.get(k) for k in ("id", "seed_label", "backend", "status", "created_at")},
        "stats": stats, "targets": targets, "graph_evidence": gitems, "findings": findings[:12],
        "key_people": net.key_people(top=8), "timeline": _timeline(net, case), **written,
        "cited": sorted(cited, key=lambda x: (x[0], int(x[1:]))),
        "incident": case.incident,
        "roles": {net.name(p): r for p, r in case.roles.items() if p in net.people},
        "uploads": [{"id": d["id"], "title": d["title"], "summary": d.get("summary")}
                    for d in case.documents.values() if d["kind"] in ("upload", "cdr")],
    }
