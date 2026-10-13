"""A1 Graph analyst: every graph change, read as facts and inferences on the case board.

While the graph builds (and when it is done), the rules in ``scenarios.py`` find the
patterns that matter - repeat co-offenders, shared phones and vehicles, landlords with
many tenants, overlapping hotel stays, corroborated links, brokers, a target close to a
criminal. The Graph analyst writes each one onto the board as an entry of the "Graph
analysis" document, so the chat, Sherlock and the report can cite it like any other:

* tier: ``stated`` / ``corroborated`` -> fact (a system states it), ``inferred`` ->
  inference, ``speculative`` -> speculation (name or OSINT matches);
* trust: a stated pattern is a system record (1); the rest is derived by us (4);
* the people it names, so dossiers refresh and the report files it under them.

Code only; one batch per run of the analyst, so the board wakes the Sherlock team once.
"""

from __future__ import annotations

import logging
from typing import Any

from sherlocks.evidence.case_file import TRUST_DERIVED, TRUST_SYSTEM

logger = logging.getLogger(__name__)

KEY = "graph:analysis"
_TIER = {"stated": "fact", "corroborated": "fact", "inferred": "inference", "speculative": "speculation"}


def _line(f: dict[str, Any]) -> str:
    names = ", ".join(f.get("names") or [])
    return f"{f['title']} ({f['tier']}): {f['summary']}" + (f" - {names}" if names and names not in f["summary"] else "")


def analyse(case: Any, graph: dict[str, Any], *, limit: int = 40) -> list[str]:
    """Write the graph's findings onto the board; returns the new fact ids."""
    from sherlocks.linkgraph.network import PersonNetwork
    from sherlocks.linkgraph.scenarios import find_scenarios

    if not graph.get("nodes"):
        return []
    net = PersonNetwork(graph)
    try:
        findings = find_scenarios(graph, net=net)[:limit]
    except Exception:  # noqa: BLE001 - a broken detector must not stop the run
        logger.exception("Graph analyst: scenario rules failed")
        return []
    if not findings:
        return []
    lines = [_line(f) for f in findings]
    new: list[str] = []
    with case.batch("A1 Graph analyst", "graph", graph_version=graph.get("version")):
        doc = case.doc_for(KEY)
        if doc is None:
            doc_id = case.add_document(kind="graph", key=KEY, title="Graph analysis", source="Link graph (rules)",
                                       text="\n".join(lines), summary="Patterns the rules found in the link graph")
        else:
            doc_id = doc["id"]
            missing = [ln for ln in lines if ln not in doc["text"]]
            if missing:
                case.append_text(doc_id, "\n".join(missing))
        before = set(case.facts)
        for f, line in zip(findings, lines, strict=True):
            fid = case.add_fact(doc_id, line, line, kind=f"graph_{f['scenario']}", by="graph",
                                tier=_TIER.get(f["tier"], "inference"),
                                trust=TRUST_SYSTEM if f["tier"] == "stated" else TRUST_DERIVED,
                                pids=list(f.get("people") or []), people=list(f.get("names") or []),
                                rests_on=[e["record"] for e in f.get("evidence") or [] if e.get("record")][:6])
            if fid:
                case.facts[fid]["pattern"] = f["subject"]
            if fid and fid not in before:
                new.append(fid)
        restated(case)
    return new


def pattern_of(fact: dict[str, Any]) -> str | None:
    """The pattern a graph fact states (also for facts written before patterns were kept)."""
    from sherlocks.linkgraph.scenarios import subject_of

    kind = str(fact.get("kind") or "")
    if not kind.startswith("graph_"):
        return None
    return fact.get("pattern") or subject_of(kind.removeprefix("graph_"), list(fact.get("pids") or []))


def restated(case: Any) -> int:
    """As the graph grows the same pattern is stated again ("links 2 groups", then 3, then 4).
    Only the latest statement stays in use: the earlier ones are marked replaced by it, so no
    answer, lead or question is built twice on one pattern."""
    latest: dict[str, str] = {}
    done = 0
    with case._lock:
        for fid, fact in case.facts.items():                 # in the order they were written
            pattern = pattern_of(fact)
            if pattern is None or fact.get("replaced_by") or fact.get("stale"):
                continue
            old = latest.get(pattern)
            if old is not None:
                case.facts[old]["replaced_by"] = fid
                case._bump("fact", old)
                done += 1
            latest[pattern] = fid
    return done


def link_co_accused(builder: Any, case: Any = None) -> list[tuple[str, str, str]]:
    """Two people on the graph accused in the same FIR are linked "Co-accused in FIR X" -
    from each one's own records (CRO, PSRMS, ARMS...) and from the FIR file's nominated
    accused - even when no FIR roster names them together. A pair already linked for that
    FIR is left as it is. Returns the links added: ``(a, b, FIR)``."""
    import itertools
    import re

    from sherlocks.linkgraph.case_queries import fir_key, role_kind
    from sherlocks.linkgraph.normalize import cnic13, mobile11
    from sherlocks.linkgraph.systems import system_label

    graph = builder.snapshot()
    people = {n["id"]: n for n in graph.get("nodes") or [] if n.get("kind") == "person"}
    accused: dict[str, dict[str, Any]] = {}          # FIR key -> {label, people: {pid: sources}}
    for pid, node in people.items():
        for f in (node.get("data") or {}).get("firs") or []:
            label = str(f.get("label") or "").strip()
            if label and role_kind([str(f.get("role") or "")]) == "accused":
                entry = accused.setdefault(fir_key(label), {"label": label, "people": {}})
                entry["people"].setdefault(pid, set()).add(system_label(f.get("system") or "") or "records")
    if case is not None:
        ids = {}
        for pid, node in people.items():
            d = node.get("data") or {}
            if cnic13(d.get("cnic")):
                ids[cnic13(d.get("cnic"))] = pid
            for p in d.get("phones") or []:
                if mobile11(p):
                    ids[mobile11(p)] = pid
        for doc in case.documents.values():
            ref = doc.get("ref") or {}
            if doc.get("kind") != "fir" or not ref.get("fir_no"):
                continue
            label = f"{ref['fir_no']}/{ref.get('fir_year')}"
            rows = (doc.get("data") or {}).get("nominated_suspects") or []
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                pid = ids.get(cnic13(row.get("cnic"))) or ids.get(mobile11(row.get("phone")))
                if pid:
                    entry = accused.setdefault(fir_key(label), {"label": label, "people": {}})
                    entry["people"].setdefault(pid, set()).add(f"FIR file {doc['id']}")
    edges = graph.get("edges") or []
    added = []
    for entry in accused.values():
        if len(entry["people"]) < 2:
            continue
        number = re.escape(entry["label"].split("/")[0].lstrip("0"))
        for a, b in itertools.combinations(sorted(entry["people"]), 2):
            linked = any({e.get("source"), e.get("target")} == {a, b}
                         and re.search(rf"\bco-accused\b.*\b0*{number}\s*/", str(e.get("label") or ""), re.IGNORECASE)
                         for e in edges)
            if linked:
                continue
            sources = sorted(entry["people"][a] | entry["people"][b])
            builder.add_direct_strong(a, b, label=f"Co-accused in FIR {entry['label']}",
                                      reasons=[f"Both accused in FIR {entry['label']} ({', '.join(sources)})"],
                                      system="fir_roster")
            added.append((a, b, entry["label"]))
    return added
