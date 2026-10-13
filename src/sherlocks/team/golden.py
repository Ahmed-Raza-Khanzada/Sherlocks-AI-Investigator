"""Golden cases: is Sherlock getting better or worse at solving a case?

The unit tests check that the parts work. A golden case checks the whole: a closed case
with a known outcome (anonymised), or a made-up one with planted contradictions, leads and
a known answer - its records, recorded statements, a scripted officer chat, and an answer
key. ``run`` works the case with the team and ``score`` measures it against the key:

    planted contradictions found          bar: 90% or more
    false contradictions                  bar: at most 1 per case
    the lead that solved it, in the top 3 bar: 80% or more (over the cases)
    the right hypothesis ranked first     tracked
    wrong merges of different people      bar: 0
    speculation shown as fact             bar: 0
    time for the team's run               tracked

Run them on every change to prompts, models, rules or tolerances
(``scripts/golden_cases.py``); a change fails if a measure with a bar gets worse.
A case file is JSON: ``{name, graph, incident, documents: [{kind, key, title, source,
text}], facts: [...], chat: ["..."], key: {contradictions: [{between: [key, key]}],
lead: {kind}, culprit: pid, same: {name: pid}, different: [name]}}``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

BARS = {"contradictions_found": 0.9, "false_contradictions": 1, "wrong_merges": 0, "speculation_as_fact": 0}
LEAD_TOP = 3


def load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def build(spec: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """The case board and graph of a golden case, before anyone has worked it."""
    from sherlocks.evidence.case_file import CaseFile

    case = CaseFile()
    for d in spec.get("documents") or []:
        case.add_document(kind=d["kind"], key=d["key"], title=d["title"], source=d.get("source") or "golden case",
                          text=d["text"])
    for f in spec.get("facts") or []:
        doc = case.doc_for(f["document"])
        if doc is not None:
            case.add_fact(doc["id"], f["statement"], f.get("quote") or f["statement"], kind=f.get("kind", "fact"),
                          by=f.get("by", "rule"), pids=f.get("pids") or [], people=f.get("people") or [],
                          check_quote=bool(f.get("quote")))
    if spec.get("incident"):
        case.set_incident(spec["incident"])
    for pid, role in (spec.get("roles") or {}).items():
        case.set_role(pid, role)
    return case, spec["graph"]


def run(spec: dict[str, Any], llm: Any, *, runs: int = 2) -> dict[str, Any]:
    """Work the case: the scripted officer chat, then the team (twice by default - leads
    found before Sherlock had a hypothesis are tied to one on the second run)."""
    from sherlocks.linkgraph.sherlock_team import run_turn
    from sherlocks.team.sherlock_graph import run_team

    case, graph = build(spec)
    started = time.monotonic()
    replies = []
    for message in spec.get("chat") or []:
        replies.append(list(run_turn(graph, message, llm=llm, case=case))[-1])
    for i in range(max(1, runs)):
        run_team(case, graph, llm=llm, reason=f"golden case {spec.get('name')} ({i + 1})")
    return {"case": case, "graph": graph, "replies": replies, "seconds": round(time.monotonic() - started, 1),
            "scores": score(case, spec.get("key") or {})}


def _doc_key(case: Any, source: str) -> str | None:
    if source in case.statements:
        source = case.statements[source]["doc"]
    elif source in case.facts:
        source = case.facts[source]["doc"]
    return (case.documents.get(source) or {}).get("key")


def score(case: Any, key: dict[str, Any]) -> dict[str, Any]:
    """The measures of one worked case against its answer key."""
    planted = [frozenset(c["between"]) for c in key.get("contradictions") or []]
    found = [x for x in case.contradictions.values() if x["severity"] != "minor"]
    pairs = [frozenset(k for k in (_doc_key(case, x["a"]["source"]), _doc_key(case, x["b"]["source"])) if k) for x in found]
    hit = [p for p in planted if p in pairs]
    leads = sorted(case.leads.values(), key=lambda k: (0 if k.get("hypothesis") else 1, int(k["id"][1:])))
    want = (key.get("lead") or {}).get("kind")
    rank = next((i + 1 for i, k in enumerate(leads) if (case.facts.get(k["source"]) or {}).get("kind") == want), None)
    leader = case.hypotheses.get((case.matrix or {}).get("leader") or "") or {}
    wrong = [e["name"] for e in case.entities.values() if e["state"] == "same" and (
        e["name"] in (key.get("different") or []) or (key.get("same") or {}).get(e["name"], e.get("pid")) != e.get("pid"))]
    return {
        "contradictions_planted": len(planted),
        "contradictions_found": (len(hit) / len(planted)) if planted else 1.0,
        "false_contradictions": sum(1 for p in pairs if p not in planted),
        "lead_rank": rank, "lead_in_top": rank is not None and rank <= LEAD_TOP if want else None,
        "right_hypothesis_first": (key["culprit"] in (leader.get("people") or [])) if key.get("culprit") else None,
        "wrong_merges": len(wrong), "wrong_merge_names": wrong,
        "speculation_as_fact": sum(1 for h in case.hypotheses.values()
                                   if h.get("tier") == "speculation" and h["status"] == "supported"),
        "statements": len(case.statements), "claims": len(case.claims), "leads": len(case.leads),
    }


def failures(scores: dict[str, Any], before: dict[str, Any] | None = None) -> list[str]:
    """The bars a worked case misses - and, against an earlier run, what got worse."""
    out = []
    if scores["contradictions_found"] < BARS["contradictions_found"]:
        out.append(f"planted contradictions found: {scores['contradictions_found']:.0%} (bar 90%)")
    if scores["false_contradictions"] > BARS["false_contradictions"]:
        out.append(f"false contradictions: {scores['false_contradictions']} (bar: at most 1)")
    for name in ("wrong_merges", "speculation_as_fact"):
        if scores[name] > BARS[name]:
            out.append(f"{name.replace('_', ' ')}: {scores[name]} (bar 0)")
    if before:
        if scores["contradictions_found"] < before["contradictions_found"]:
            out.append("fewer planted contradictions found than before")
        if scores["false_contradictions"] > before["false_contradictions"]:
            out.append("more false contradictions than before")
        if before.get("lead_in_top") and not scores.get("lead_in_top"):
            out.append("the lead that solved it fell out of the top 3")
    return out
