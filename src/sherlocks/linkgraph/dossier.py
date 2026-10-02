"""Per-person intelligence brief.

For one person in a finished (or in-progress) graph, this assembles everything known
about them **with the system each fact came from**, describes how they connect to the
target and to others, and writes a short brief. The point the analyst asked for: not
just "FIR 45/2023", but "FIR 45/2023 at PS Gulshan-e-Iqbal, role Accused — from PSRMS".

Provenance is deterministic: every fact carries its source system, built from the graph,
never from the model. The LLM only turns those sourced facts into fluent prose, and is
told to cite the bracketed source and invent nothing; if it is unavailable or strays,
the rule-based brief (also fully sourced) stands in.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from sherlocks.linkgraph.systems import system_label

logger = logging.getLogger(__name__)

# Flags that mark a criminal footprint on their own. "fir_record" is deliberately absent:
# complainants and witnesses are named in FIRs too - see is_criminal().
CRIMINAL_FLAGS = ("criminal_record", "watchlist", "arms_record", "stolen_vehicle")


def criminal_flags(data: dict[str, Any]) -> list[str]:
    """Why a person counts as having a criminal footprint, or [] if they do not.
    Being named in an FIR counts only as accused / suspect / nominated."""
    flags = [f for f in data.get("flags") or [] if f in CRIMINAL_FLAGS]
    accused = any(any(t in str(f.get("role") or "").lower() for t in ("accused", "suspect", "nominated"))
                  for f in data.get("firs") or [])
    if accused:
        flags.append("accused_in_fir")
    return flags


def _label(nodes: dict[str, dict], nid: str) -> str:
    n = nodes.get(nid)
    return n["label"] if n else nid


def _adjacency(edges: list[dict], include_weak: bool) -> dict[str, list[tuple[str, dict]]]:
    adj: dict[str, list[tuple[str, dict]]] = {}
    for e in edges:
        if e["kind"] == "weak" and not include_weak:
            continue
        adj.setdefault(e["source"], []).append((e["target"], e))
        adj.setdefault(e["target"], []).append((e["source"], e))
    return adj


def shortest_path(graph: dict[str, Any], a: str, b: str) -> list[str]:
    """Node path from ``a`` to ``b``. Stated (strong) links are tried first; only if
    there is no such path are inferred weak links allowed - so a chain of facts is
    never presented when one of its hops is a guess."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    if a not in nodes or b not in nodes or a == b:
        return []
    for include_weak in (False, True):
        adj = _adjacency(graph.get("edges", []), include_weak)
        seen, queue = {a: None}, [a]
        while queue:
            nxt: list[str] = []
            for cur in queue:
                for other, _edge in adj.get(cur, []):
                    if other in seen:
                        continue
                    seen[other] = cur
                    if other == b:
                        path, node = [b], b
                        while seen[node] is not None:
                            node = seen[node]
                            path.append(node)
                        return list(reversed(path))
                    nxt.append(other)
            queue = nxt
    return []


def describe_path(graph: dict[str, Any], path: list[str]) -> list[dict[str, Any]]:
    """Turn a node path into readable hops, collapsing person -> record -> person into
    a single hop that names the relation and the system that states it."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    pairs: dict[tuple[str, str], dict] = {}
    for e in graph.get("edges", []):
        pairs[(e["source"], e["target"])] = e
        pairs.setdefault((e["target"], e["source"]), e)

    hops: list[dict[str, Any]] = []
    i = 0
    while i < len(path) - 1:
        cur, nxt = path[i], path[i + 1]
        if nodes.get(nxt, {}).get("kind") == "system" and i + 2 < len(path):
            record, person = nodes[nxt], path[i + 2]
            edge = pairs.get((nxt, person), {})
            hops.append({
                "from": _label(nodes, cur), "to": _label(nodes, person),
                "relation": edge.get("label") or "named in the same record",
                "via": system_label(record["data"].get("system", "")),
                "kind": "strong", "detail": (edge.get("reasons") or [None])[0],
                "sentence": (edge.get("relation") or {}).get("sentence"),
            })
            i += 2
            continue
        edge = pairs.get((cur, nxt), {})
        hops.append({
            "from": _label(nodes, cur), "to": _label(nodes, nxt),
            "relation": edge.get("label") or "linked",
            "via": system_label(edge.get("system") or "") if edge.get("system") else "",
            "kind": edge.get("kind", "strong"), "score": edge.get("score"),
            "detail": (edge.get("reasons") or [None])[0],
            "sentence": (edge.get("relation") or {}).get("sentence"),
        })
        i += 1
    return hops


def path_between(graph: dict[str, Any], a: str, b: str) -> dict[str, Any]:
    """How two people are connected: the hops, and a one-line summary."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    hops = describe_path(graph, shortest_path(graph, a, b))
    if not hops:
        return {"from": _label(nodes, a), "to": _label(nodes, b), "hops": [],
                "summary": f"No link found between {_label(nodes, a)} and {_label(nodes, b)} in this graph.",
                "inferred": False}
    inferred = any(h["kind"] == "weak" for h in hops)
    # node-centric so a shared person is written once: A —[x]→ B —[y]→ C
    chain = hops[0]["from"] + "".join(
        f" —[{h['relation']}{(' · ' + h['via']) if h['via'] else ''}]→ {h['to']}" for h in hops)
    return {"from": _label(nodes, a), "to": _label(nodes, b), "hops": hops, "summary": chain,
            "inferred": inferred, "degrees": len(hops)}


def person_facts(graph: dict[str, Any], pid: str) -> dict[str, Any] | None:
    """Everything known about ``pid``, each fact tagged with its source system."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    node = nodes.get(pid)
    if not node or node["kind"] != "person":
        return None
    d = node["data"]
    edges = graph.get("edges", [])

    # system records this person was found in
    found_in = [{"system": r["system"], "label": system_label(r["system"]), "summary": r["summary"]}
                for r in d.get("records", [])]

    # owner of each system node, to describe cross-person links by their source
    owner_of = {n["id"]: n["data"].get("owner") for n in nodes.values() if n["kind"] == "system"}

    strong, weak = [], []
    own_records = {r["node"] for r in d.get("records", [])}
    for e in edges:
        if e["kind"] == "strong":
            if e["target"] == pid and str(e["source"]).startswith("s:"):
                strong.append({"other": _label(nodes, owner_of.get(e["source"], e["source"])),
                               "relation": e["label"], "via": system_label(e.get("system") or ""),
                               "statement": (e.get("relation") or {}).get("sentence"),
                               "reason": (e.get("reasons") or [None])[0]})
            elif e["source"] in own_records and e["target"] != pid:
                strong.append({"other": _label(nodes, e["target"]), "relation": e["label"],
                               "via": system_label(nodes[e["source"]]["data"].get("system", "")),
                               "statement": (e.get("relation") or {}).get("sentence"),
                               "reason": (e.get("reasons") or [None])[0]})
            elif not str(e["source"]).startswith("s:") and pid in (e["source"], e["target"]):
                other = e["target"] if e["source"] == pid else e["source"]
                strong.append({"other": _label(nodes, other), "relation": e["label"],
                               "via": system_label(e.get("system") or ""),
                               "statement": (e.get("relation") or {}).get("sentence"),
                               "reason": (e.get("reasons") or [None])[0]})
        elif e["kind"] == "weak" and pid in (e["source"], e["target"]):
            other = e["target"] if e["source"] == pid else e["source"]
            weak.append({"other": _label(nodes, other), "label": e["label"],
                         "score": e.get("score"), "reasons": e.get("reasons", [])})

    # How this person chains back to the subject of the graph.
    target = next((n["id"] for n in nodes.values() if n["kind"] == "person" and n["data"].get("seed")), None)
    connection = path_between(graph, target, pid) if (target and target != pid) else {}

    # People with a criminal footprint this person is connected to, and how.
    criminal_links = []
    for other in nodes.values():
        if other["kind"] != "person" or other["id"] == pid:
            continue
        od = other["data"]
        flags = criminal_flags(od)
        if not flags:
            continue
        route = path_between(graph, pid, other["id"])
        if not route.get("hops"):
            continue
        criminal_links.append({
            "name": other["label"], "id": other["id"], "flags": flags, "degrees": route.get("degrees"),
            "inferred": route.get("inferred"),
            "firs": [f"FIR {f['label']} ({f.get('role') or 'named'}"
                     + (f", charges {f['offence']}" if f.get("offence") else "") + f", {system_label(f.get('system') or '')})"
                     for f in od.get("firs", [])][:4],
            "how": [h.get("sentence") or f"{h['from']} → {h['to']}: {h['relation']}" for h in route["hops"]],
        })
    criminal_links.sort(key=lambda c: (c["inferred"], c["degrees"] or 9))

    return {
        "name": d.get("name"),
        "is_target": bool(d.get("seed")),
        "connection_to_target": connection,
        "cnic": d.get("cnic"),
        "father_name": d.get("father_name"),
        "phones": d.get("phones", []),
        "emails": [f"{e['email']} (from {e['source']})" for e in d.get("emails") or []],
        "addresses": d.get("addresses", []),
        "flags": d.get("flags", []),
        "found_in": found_in,
        "firs": [{"fir": f["label"], "police_station": f.get("ps"), "role": f.get("role"),
                  "charges": f.get("offence"),
                  "source": system_label(f.get("system") or "")} for f in d.get("firs", [])],
        "criminal_links": criminal_links[:12],
        "vehicles": d.get("vehicles", []),
        "organisations": d.get("organisations", []),
        "police_stations": d.get("police_stations", []),
        "strong_links": strong,
        "weak_links": weak,
        "discovered_via": [{"relation": v.get("relation"), "from": _label(nodes, v.get("from")),
                            "source": v.get("system")} for v in d.get("discovered_via", [])],
        "osint": d.get("osint", []),
        "lookups": d.get("lookups", {}),
    }


def rule_brief(f: dict[str, Any]) -> str:
    """A fully-sourced brief with no LLM. Always available."""
    who = f["name"] or (f["cnic"] and f"CNIC {f['cnic']}") or (f["phones"] or ["Unknown"])[0]
    lines: list[str] = []
    head = f"{who} is the subject of this graph." if f["is_target"] else f"{who} is linked to the subject."
    ids = []
    if f["cnic"]:
        ids.append(f"CNIC {f['cnic']}")
    if f["phones"]:
        ids.append(f"{len(f['phones'])} number(s): {', '.join(f['phones'])}")
    lines.append(head + (f" Identifiers: {'; '.join(ids)}." if ids else ""))

    conn = f.get("connection_to_target") or {}
    if conn.get("hops"):
        lines.append(f"Connection to the subject ({conn['degrees']} step(s)"
                     + (", includes an inferred link" if conn.get("inferred") else ", all stated by systems")
                     + f"): {conn['summary']}.")
        for hop in conn["hops"]:
            said = hop.get("sentence") or f"{hop['from']} → {hop['to']}: {hop['relation']}"
            lines.append(f"  · {said}"
                         + (f" (per {hop['via']})" if hop["via"] else "")
                         + (f" [inferred, {hop.get('score')}]" if hop["kind"] == "weak" else "")
                         + (f" — {hop['detail']}" if hop.get("detail") else ""))
    elif conn:
        lines.append(conn.get("summary", ""))

    if f["found_in"]:
        lines.append("Found in: " + "; ".join(f"{r['label']} ({r['summary']})" for r in f["found_in"]) + ".")
    if f["flags"]:
        lines.append("Flags: " + ", ".join(f["flags"]) + ".")
    for fir in f["firs"]:
        lines.append(f"FIR {fir['fir']} at {fir['police_station'] or 'unknown PS'}"
                     + (f", role {fir['role']}" if fir['role'] else "")
                     + (f", charges {fir['charges']}" if fir.get('charges') else "") + f" — from {fir['source']}.")
    if f.get("criminal_links"):
        lines.append(f"Connected to {len(f['criminal_links'])} person(s) with a criminal footprint:")
        for c in f["criminal_links"][:6]:
            lines.append(f"  · {c['name']} [{', '.join(c['flags'])}]"
                         + (f" — {'; '.join(c['firs'][:2])}" if c["firs"] else "")
                         + f". How: {' → '.join(c['how'])}" + (" (includes an inferred link)" if c["inferred"] else ""))
    if f["vehicles"]:
        lines.append("Vehicles: " + ", ".join(f["vehicles"]) + ".")
    if f["organisations"]:
        lines.append("Organisations: " + ", ".join(f["organisations"]) + ".")
    if f["discovered_via"]:
        v = f["discovered_via"][0]
        lines.append(f"Discovered as “{v['relation']}” via {v['from']}'s {system_label(v['source'] or '')} record.")
    for s in f["strong_links"]:
        said = s.get("statement") or f"{s['relation']}: {s['other']}"
        lines.append(f"Strong link — {said} (stated by {s['via']}"
                     + (f"; {s['reason']}" if s.get('reason') else "") + ").")
    for w in f["weak_links"]:
        lines.append(f"Weak link ({w['score']}) — {w['label']}: {w['other']}"
                     + (f" [{w['reasons'][0]}]" if w.get('reasons') else "") + ". Lead only.")
    return "\n".join(lines)


class _Brief(BaseModel):
    summary: str = Field(description="2-5 sentence intelligence brief. Cite the source system in (brackets) for every claim.")
    key_points: list[str] = Field(default_factory=list, description="Short bullet facts, each ending with (source).")


_SYSTEM = (
    "You are a police intelligence analyst assistant. You are given, as JSON, everything "
    "the case systems returned about ONE person and how they link to others - each fact "
    "already tagged with the system it came from. Write a brief for the investigating "
    "officer. Rules: use ONLY the facts given; never invent names, FIRs, numbers or "
    "relationships; cite the source system in (brackets) after each claim, e.g. "
    "'FIR 45/2023 at PS Gulshan (PSRMS)'; state links to the subject and others plainly; "
    "call weak links 'unconfirmed leads'. Be concise and factual. "
    "If 'connection_to_target' is present, OPEN the brief by explaining, hop by hop, how "
    "this person is connected to the subject of the graph - naming each intermediate "
    "person, the relationship, and the system that states it - and say plainly whether "
    "any hop is inferred rather than stated. Word every relationship as a directed "
    "sentence between two named people, using the 'statement'/'sentence' fields where "
    "given (e.g. 'Waqas Javed filed FIR 45/2023 against Kamran Ahmed (charges: 395/34 PPC)'). "
    "If 'criminal_links' is present, give it its own paragraph: who the criminals are, "
    "their FIRs and charges, and exactly how this person is connected to each."
)


def llm_brief(facts: dict[str, Any], llm: Any) -> tuple[str, list[str]] | None:
    try:
        brief, _ = llm.generate_structured(
            prompt="Person and links (JSON):\n" + json.dumps(facts, ensure_ascii=False, default=str)[:8000]
            + "\n\nWrite the brief as JSON.",
            schema=_Brief, system=_SYSTEM, cache_kind="person_brief", prompt_version="v1",
        )
    except Exception as exc:  # noqa: BLE001 - LLM optional; rule brief covers it
        logger.info("Person brief LLM unavailable (%s); rule brief used", exc)
        return None
    return brief.summary.strip(), [k.strip() for k in brief.key_points if k.strip()]


def graph_digest(graph: dict[str, Any], max_people: int = 60) -> dict[str, Any]:
    """A compact, fully-sourced view of the whole graph for question answering."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    people = [n for n in nodes.values() if n["kind"] == "person"][:max_people]
    owner_of = {n["id"]: n["data"].get("owner") for n in nodes.values() if n["kind"] == "system"}

    links: list[dict[str, Any]] = []
    for e in graph.get("edges", []):
        if e["kind"] == "found_in":
            continue
        source = e["source"]
        if str(source).startswith("s:"):
            source = owner_of.get(source, source)
        if source == e["target"]:
            continue
        links.append({
            "a": _label(nodes, source), "b": _label(nodes, e["target"]),
            "relation": e["label"], "type": "stated" if e["kind"] == "strong" else "inferred",
            "statement": (e.get("relation") or {}).get("sentence"),
            "source_system": system_label(e.get("system") or "") if e.get("system") else "",
            "score": e.get("score"), "why": (e.get("reasons") or [None])[0],
        })

    return {
        "people": [{
            "name": n["data"].get("name"), "is_target": bool(n["data"].get("seed")),
            "cnic": n["data"].get("cnic"), "phones": n["data"].get("phones", [])[:4],
            "father_name": n["data"].get("father_name"),
            "addresses": n["data"].get("addresses", [])[:2],
            "flags": n["data"].get("flags", []),
            "found_in": [r["system"] for r in n["data"].get("records", [])],
            "firs": [f"{f['label']} at {f.get('ps')} ({f.get('role')}, {f.get('system')})"
                     + (f" charges: {f['offence']}" if f.get("offence") else "")
                     for f in n["data"].get("firs", [])],
            "vehicles": n["data"].get("vehicles", []),
            "organisations": n["data"].get("organisations", []),
            "searched": n["data"].get("search_status"),
        } for n in people],
        "links": links[:200],
    }


class _Answer(BaseModel):
    answer: str = Field(description="Answer the question using ONLY the graph data. Cite the source system in (brackets).")
    confident: bool = Field(default=True, description="False if the graph does not contain the answer.")


_ASK_SYSTEM = (
    "You answer an investigating officer's questions about ONE link graph built from "
    "police/government records. You are given the graph as JSON: people (each with the "
    "systems they were found in, FIRs, flags, addresses) and links (each marked 'stated' "
    "by a system or 'inferred'). Rules: use ONLY this data; never invent a person, FIR, "
    "number or relationship; cite the source system in (brackets); when explaining how "
    "two people connect, walk the chain hop by hop; call inferred links unconfirmed "
    "leads; if the graph does not contain the answer, say so plainly and set confident "
    "to false. Word relationships as directed sentences between named people using each "
    "link's 'statement' (who filed an FIR against whom, who is whose landlord), and give "
    "FIR charges where known. Pay particular attention to criminal records, FIRs, "
    "watchlist and arms flags. Be brief and concrete."
)


def answer_question(graph: dict[str, Any], question: str, llm: Any = None,
                    history: list[dict] | None = None) -> dict[str, Any]:
    """Answer a free-text question about the graph, grounded in its data. ``history`` is
    the conversation so far (``[{q, a}]``), so a follow-up like "and his brother?" reads
    in context."""
    question = (question or "").strip()
    if not question:
        return {"answer": "Ask a question about this graph.", "confident": False, "model": None}
    digest = graph_digest(graph)
    if llm is None:
        people = ", ".join(p["name"] or "?" for p in digest["people"][:12])
        return {
            "answer": ("No LLM is configured, so questions cannot be answered in prose. "
                       f"This graph holds {len(digest['people'])} people ({people}) and "
                       f"{len(digest['links'])} links - open a person for their sourced brief."),
            "confident": False, "model": None,
        }
    convo = "".join(f"\nOfficer: {t.get('q', '')}\nYou: {t.get('a', '')}" for t in (history or [])[-4:])
    try:
        result, _meta = llm.generate_structured(
            prompt=("Graph (JSON):\n" + json.dumps(digest, ensure_ascii=False, default=str)[:16000]
                    + (f"\n\nEarlier in this conversation:{convo}" if convo else "")
                    + f"\n\nQuestion: {question}\n\nAnswer as JSON."),
            schema=_Answer, system=_ASK_SYSTEM, cache_kind="graph_qa", prompt_version="v1",
        )
    except Exception as exc:  # noqa: BLE001 - a failed answer is not a failed run
        logger.info("Graph Q&A unavailable: %s", exc)
        return {"answer": f"The model could not answer right now ({type(exc).__name__}).",
                "confident": False, "model": None}
    return {"answer": result.answer.strip(), "confident": result.confident,
            "model": getattr(llm, "model", None) or "llm"}


def build_person_brief(graph: dict[str, Any], pid: str, llm: Any = None) -> dict[str, Any] | None:
    facts = person_facts(graph, pid)
    if facts is None:
        return None
    rule = rule_brief(facts)
    narrative, points, model = rule, [], None
    if llm is not None:
        out = llm_brief(facts, llm)
        if out:
            narrative, points = out
            model = getattr(llm, "model", None) or "llm"
    return {"facts": facts, "narrative": narrative, "key_points": points,
            "rule_brief": rule, "model": model}
