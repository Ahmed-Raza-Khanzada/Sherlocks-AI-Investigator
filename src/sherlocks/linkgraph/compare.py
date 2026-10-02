"""What connects two people the analyst picked.

Everything the graph knows that ties person A to person B, grouped by how much weight it
carries:

1. **Direct links** - a system states it (B is A's landlord in Old Tenant; B is a
   co-accused of A in PSRMS), or a weak link was inferred straight between them.
2. **Named together** - one record (a FIR, a tenancy, a hotel booking) names both.
3. **Best route** - the shortest chain between them, stated links first, inferred
   links only if there is no stated route.
4. **Mutual contacts** - people each of them is linked to.
5. **Shared details** - the same number, address, father's name, FIR, hotel stay,
   employer, vehicle or police station, even where no record links them.

Every item carries the system it came from. The optional AI explanation is written from
these facts only; the facts themselves are never produced by the model.
"""

from __future__ import annotations

import itertools
import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from sherlocks.linkgraph.dossier import criminal_flags, describe_path, shortest_path
from sherlocks.linkgraph.normalize import name_key, parse_address, surname
from sherlocks.linkgraph.systems import system_label
from sherlocks.linkgraph.weak_links import _same_org, _stay_overlap, address_score

logger = logging.getLogger(__name__)


def _via(system: str | None) -> str:
    if not system or system in ("rules", "ai", "osint_rules"):
        return "inferred"
    if system == "identity":
        return "identity match"
    return system_label(system)


def _person_links(graph: dict[str, Any]) -> tuple[list[dict], dict[str, list[tuple[str, dict]]]]:
    """Every person-to-person link, with record hops collapsed.

    A strong edge ``record -> person`` means "this record, which belongs to its owner,
    names that person as <label>". Returns the links, and for each record the people it
    names (so two people named in the same record can be found).
    """
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    links: list[dict] = []
    named_in: dict[str, list[tuple[str, dict]]] = {}
    for e in graph.get("edges", []):
        src, dst = nodes.get(e["source"]), nodes.get(e["target"])
        if not src or not dst or e["kind"] == "found_in":
            continue
        reason = (e.get("reasons") or [None])[0]
        if src["kind"] == "system" and dst["kind"] == "person":
            owner = src["data"].get("owner")
            system = e.get("system") or src["data"].get("system")
            named_in.setdefault(src["id"], []).append((dst["id"], e))
            if owner and owner != dst["id"]:
                links.append({"a": owner, "b": dst["id"], "relation": e.get("label") or "named in record",
                              "via": _via(system), "system": system, "kind": "strong",
                              "score": None, "detail": reason, "record": src["id"],
                              "statement": e.get("relation")})
        elif src["kind"] == "person" and dst["kind"] == "person":
            links.append({"a": src["id"], "b": dst["id"], "relation": e.get("label") or "linked",
                          "via": _via(e.get("system")), "system": e.get("system"), "kind": e["kind"],
                          "score": e.get("score"), "detail": reason, "reasons": e.get("reasons") or [],
                          "record": None, "statement": e.get("relation")})
    return links, named_in


def _shared_details(pa: dict, pb: dict) -> list[dict]:
    """Identifiers and circumstances the two have in common, whether or not any record
    links them. Each says what matched and, where it applies, which system said so."""
    out: list[dict] = []

    for phone in sorted(set(pa.get("phones") or []) & set(pb.get("phones") or [])):
        out.append({"kind": "Same mobile number", "detail": phone, "weight": "high"})

    best = (0.0, "", "", "")
    for x, y in itertools.product((pa.get("addresses") or [])[:8], (pb.get("addresses") or [])[:8]):
        score, reason = address_score(x, y, parse_address(x), parse_address(y))
        if score > best[0]:
            best = (score, reason, x, y)
    if best[0] >= 0.3:
        kind = "Neighbours" if "Neighbour" in best[1] else ("Same address" if best[0] >= 0.6 else "Nearby address")
        out.append({"kind": kind, "detail": f"{best[1]}: “{best[2]}” ~ “{best[3]}”",
                    "weight": "high" if best[0] >= 0.6 else "medium"})

    fa, fb = name_key(pa.get("father_name")), name_key(pb.get("father_name"))
    if fa and fb and fa == fb:
        out.append({"kind": "Same father's name", "detail": pa.get("father_name"), "weight": "medium"})
    sa, sb = surname(pa.get("name")), surname(pb.get("name"))
    if sa and sa == sb and not (fa and fb and fa == fb):
        out.append({"kind": "Same surname", "detail": sa.title(), "weight": "low"})

    firs_b = {f.get("key"): f for f in pb.get("firs") or []}
    for f in pa.get("firs") or []:
        other = firs_b.get(f.get("key"))
        if other:
            out.append({"kind": "Same FIR", "weight": "high",
                        "detail": f"FIR {f.get('label')} {f.get('ps') or ''} — "
                                  f"{pa.get('name') or 'A'}: {f.get('role') or 'named'} ({system_label(f.get('system') or '')}); "
                                  f"{pb.get('name') or 'B'}: {other.get('role') or 'named'} ({system_label(other.get('system') or '')})"})

    for x, y in itertools.product(pa.get("stays") or [], pb.get("stays") or []):
        hit = _stay_overlap(x, y)
        if hit:
            out.append({"kind": "Hotel stay overlap", "detail": hit[1], "weight": "high" if hit[0] >= 0.6 else "medium"})
            break

    for x, y in itertools.product(pa.get("organisations") or [], pb.get("organisations") or []):
        if _same_org(x, y):
            out.append({"kind": "Same organisation", "detail": x, "weight": "medium"})
            break

    for plate in sorted(set(pa.get("vehicles") or []) & set(pb.get("vehicles") or [])):
        out.append({"kind": "Same vehicle", "detail": plate, "weight": "high"})

    stations = {name_key(s): s for s in pa.get("police_stations") or []}
    for s in pb.get("police_stations") or []:
        if name_key(s) in stations:
            out.append({"kind": "Same police station", "detail": s, "weight": "low"})
            break
    return out


def _profile(node: dict) -> dict[str, Any]:
    d = node["data"]
    flags = d.get("flags") or []
    return {
        "name": node["label"],
        "criminal": bool(criminal_flags(d)),
        "criminal_reasons": criminal_flags(d),
        "flags": flags,
        "firs": [{"fir": f.get("label"), "role": f.get("role"), "ps": f.get("ps"), "charges": f.get("offence"),
                  "source": system_label(f.get("system") or "")} for f in d.get("firs") or []],
        "found_in": sorted({system_label(r["system"]) for r in d.get("records") or []}),
        "father_name": d.get("father_name"),
        "addresses": (d.get("addresses") or [])[:3],
        "organisations": d.get("organisations") or [],
        "vehicles": d.get("vehicles") or [],
    }


def compare_people(graph: dict[str, Any], a: str, b: str) -> dict[str, Any] | None:
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    na, nb = nodes.get(a), nodes.get(b)
    if not na or not nb or na["kind"] != "person" or nb["kind"] != "person" or a == b:
        return None
    name = {nid: n["label"] for nid, n in nodes.items()}

    def who(nid: str) -> dict:
        d = nodes[nid]["data"]
        return {"id": nid, "name": name[nid], "cnic": d.get("cnic"), "phones": d.get("phones") or [],
                "is_target": bool(d.get("seed"))}

    links, named_in = _person_links(graph)

    # 1. Direct links, in either direction.
    direct = []
    seen: set[tuple] = set()
    for link in links:
        if {link["a"], link["b"]} == {a, b}:
            # The same statement reached through two records (by CNIC and by number) is
            # one fact, not two.
            key = ((link.get("statement") or {}).get("sentence") or (link["a"], link["b"], link["relation"]),
                   link["via"], link["detail"])
            if key in seen:
                continue
            seen.add(key)
            st = link.get("statement") or {}
            direct.append({"from": name.get(st.get("from"), name[link["a"]]), "to": name.get(st.get("to"), name[link["b"]]),
                           "relation": link["relation"], "sentence": st.get("sentence"),
                           "verb": st.get("verb"), "charges": st.get("charges"), "symmetric": st.get("symmetric"),
                           "via": link["via"], "kind": link["kind"], "score": link["score"],
                           "detail": link["detail"], "reasons": link.get("reasons") or []})
    direct.sort(key=lambda d: (d["kind"] != "strong", -(d["score"] or 0)))

    # 2. One record naming both (and belonging to neither - otherwise it is a direct link).
    together = []
    for record, named in named_in.items():
        people = {pid: edge for pid, edge in named}
        owner = nodes[record]["data"].get("owner")
        if a in people and b in people and owner not in (a, b):
            system = nodes[record]["data"].get("system")
            together.append({"record": record, "via": system_label(system or ""),
                             "owner": name.get(owner, "?"),
                             "a_role": people[a].get("label"), "b_role": people[b].get("label"),
                             "summary": nodes[record]["data"].get("summary")})

    # 3. Best route, stated links first.
    path_ids = shortest_path(graph, a, b)
    hops = describe_path(graph, path_ids)
    route = {"hops": hops, "degrees": len(hops), "inferred": any(h["kind"] == "weak" for h in hops),
             "nodes": path_ids}

    # 4. Mutual contacts.
    neighbours: dict[str, dict[str, list[dict]]] = {}
    for link in links:
        for me, other in ((link["a"], link["b"]), (link["b"], link["a"])):
            neighbours.setdefault(me, {}).setdefault(other, []).append(link)
    mutual = []
    for m in sorted(set(neighbours.get(a, {})) & set(neighbours.get(b, {})) - {a, b}):
        if nodes.get(m, {}).get("kind") != "person":
            continue

        def rel(side: str, m: str = m) -> dict:
            best = min(neighbours[side][m], key=lambda x: (x["kind"] != "strong", -(x["score"] or 0)))
            return {"relation": best["relation"], "via": best["via"], "kind": best["kind"],
                    "from": name[best["a"]], "to": name[best["b"]],
                    "sentence": (best.get("statement") or {}).get("sentence")}

        mutual.append({"id": m, "name": name[m], "to_a": rel(a), "to_b": rel(b)})
    mutual.sort(key=lambda x: (x["to_a"]["kind"] != "strong") + (x["to_b"]["kind"] != "strong"))

    # 5. Shared details.
    shared = _shared_details(na["data"], nb["data"])

    # 6. What each of them has on record, criminal first.
    profiles = {"a": _profile(na), "b": _profile(nb)}

    stated = [d for d in direct if d["kind"] == "strong"]
    if stated:
        verdict, strength = (f"Directly connected — stated by {', '.join(sorted({d['via'] for d in stated}))}", "strong")
    elif together:
        verdict, strength = (f"Named together in {len(together)} record(s)", "strong")
    elif hops and not route["inferred"]:
        verdict, strength = (f"Connected through {len(hops)} step(s), every step stated by a system", "strong")
    elif direct or hops:
        verdict, strength = ("Possibly connected — the link is inferred, not stated. Verify before relying on it", "weak")
    elif shared:
        verdict, strength = ("No record links them, but they share details worth checking", "hint")
    else:
        verdict, strength = ("No connection found between them in this graph", "none")

    return {"a": who(a), "b": who(b), "verdict": verdict, "strength": strength,
            "direct": direct, "together": together, "route": route, "mutual": mutual,
            "shared": shared, "profiles": profiles}


def rule_explanation(c: dict[str, Any]) -> str:
    a, b = c["a"]["name"], c["b"]["name"]
    lines = [f"{a} and {b}: {c['verdict']}."]
    for side in ("a", "b"):
        pr = c.get("profiles", {}).get(side)
        if pr and pr["criminal"]:
            firs = "; ".join(f"FIR {f['fir']} as {f['role'] or 'named'}" + (f", charges {f['charges']}" if f["charges"] else "")
                             + f" ({f['source']})" for f in pr["firs"][:4])
            lines.append(f"⚠ {pr['name']} has a criminal footprint [{', '.join(pr['criminal_reasons'])}]" + (f": {firs}" if firs else "") + ".")
    for d in c["direct"]:
        tag = f"per {d['via']}" if d["kind"] == "strong" else f"inferred, score {d['score']}"
        said = d.get("sentence") or f"{d['from']} → {d['to']}: {d['relation']}"
        lines.append(f"• {said} ({tag})" + (f" — {d['detail']}" if d.get("detail") else ""))
    for t in c["together"]:
        lines.append(f"• Both named in a {t['via']} record of {t['owner']}: {a} as {t['a_role']}, {b} as {t['b_role']}.")
    if c["route"]["hops"] and not c["direct"]:
        lines.append("Route:")
        for h in c["route"]["hops"]:
            said = h.get("sentence") or f"{h['from']} → {h['to']}: {h['relation']}"
            lines.append(f"  {said}" + (f" (per {h['via']})" if h.get("via") else "")
                         + (" [inferred]" if h["kind"] == "weak" else ""))
    if c["mutual"]:
        lines.append("Mutual contacts: " + "; ".join(
            f"{m['name']} ({m['to_a']['relation']} / {m['to_b']['relation']})" for m in c["mutual"][:6]))
    if c["shared"]:
        lines.append("Shared details: " + "; ".join(f"{s['kind']}: {s['detail']}" for s in c["shared"][:8]))
    return "\n".join(lines)


class _Explanation(BaseModel):
    summary: str = Field(description="2-4 sentences: how the two are connected and how strongly.")
    key_points: list[str] = Field(default_factory=list, description="Up to 5 short bullets, each citing [System].")
    next_steps: list[str] = Field(default_factory=list, description="Up to 3 concrete checks an investigator could do.")


_COMPARE_SYSTEM = (
    "You are a police intelligence analyst. You are given the complete, sourced facts on how "
    "two people are connected in a link graph. Explain the connection to an investigator. "
    "Rules: use ONLY the facts given; cite the source system in square brackets for every "
    "claim, e.g. [PSRMS]; a link marked inferred or kind 'weak' is a lead, say so, never "
    "present it as established; do not invent names, numbers, FIRs or relationships; if the "
    "facts show no connection, say that plainly. Word each relationship as a directed "
    "sentence between the two named people using the 'sentence' fields (e.g. 'Waqas Javed "
    "filed FIR 45/2023 against Kamran Ahmed (charges: 395/34 PPC)'). If either person has "
    "a criminal footprint (profiles.*.criminal), say so first, with FIRs, roles and charges. "
    "Suggest next steps that follow from the facts."
)


def explain_with_llm(c: dict[str, Any], llm: Any) -> dict[str, Any] | None:
    facts = {k: v for k, v in c.items() if k != "route"}
    facts["route"] = {"hops": c["route"]["hops"], "inferred": c["route"]["inferred"]}
    try:
        out, _meta = llm.generate_structured(
            prompt="Connection facts (JSON):\n" + json.dumps(facts, ensure_ascii=False, default=str)[:14000]
                   + "\n\nExplain the connection as JSON.",
            schema=_Explanation, system=_COMPARE_SYSTEM, cache_kind="compare_people", prompt_version="v1",
        )
    except Exception as exc:  # noqa: BLE001 - no explanation is not a failed comparison
        logger.info("Connection explanation unavailable: %s", exc)
        return None
    return {"summary": out.summary.strip(), "key_points": [p.strip() for p in out.key_points][:5],
            "next_steps": [p.strip() for p in out.next_steps][:3], "model": getattr(llm, "model", None) or "llm"}


class _PairAnswer(BaseModel):
    answer: str = Field(description="Direct answer, citing [System] for every claim.")
    confident: bool = Field(description="False if the facts do not contain the answer.")


_PAIR_ASK_SYSTEM = (
    "You answer an investigating officer's questions about TWO specific people and how they "
    "are connected. You are given, as JSON: everything connecting them (direct links, shared "
    "records, route, mutual contacts, shared details) and each person's own facts (FIRs with "
    "roles and charges, flags, records, addresses, employer, links). Rules: use ONLY these "
    "facts; cite the source system in [brackets]; word relationships as directed sentences "
    "between named people (who filed an FIR against whom, who is whose landlord); give FIR "
    "charges where known; call inferred links unconfirmed leads; never invent a person, FIR, "
    "number or relationship; if the facts do not answer the question, say so and set "
    "confident to false. Be concise."
)


def ask_about_pair(graph: dict[str, Any], a: str, b: str, question: str, llm: Any = None,
                   history: list[dict] | None = None) -> dict[str, Any] | None:
    """Answer a question about two selected people, grounded in their facts only."""
    from sherlocks.linkgraph.dossier import person_facts

    c = compare_people(graph, a, b)
    if c is None:
        return None
    question = (question or "").strip()
    if not question:
        return {"answer": "Ask something about these two people.", "confident": False, "model": None}
    if llm is None:
        return {"answer": "No LLM is configured, so questions cannot be answered in prose. "
                          "The connection summary above is complete and sourced.\n\n" + rule_explanation(c),
                "confident": False, "model": None}
    facts = {
        "connection": {k: v for k, v in c.items() if k != "route"} | {"route": {"hops": c["route"]["hops"],
                                                                        "inferred": c["route"]["inferred"]}},
        "person_a": person_facts(graph, a),
        "person_b": person_facts(graph, b),
    }
    for side in ("person_a", "person_b"):   # the lookup log is noise for this question
        if facts[side]:
            facts[side].pop("lookups", None)
    convo = "".join(f"\nOfficer: {t.get('q', '')}\nYou: {t.get('a', '')}" for t in (history or [])[-4:])
    try:
        out, _meta = llm.generate_structured(
            prompt=("Facts (JSON):\n" + json.dumps(facts, ensure_ascii=False, default=str)[:18000]
                    + (f"\n\nEarlier in this conversation:{convo}" if convo else "")
                    + f"\n\nQuestion: {question}\n\nAnswer as JSON."),
            schema=_PairAnswer, system=_PAIR_ASK_SYSTEM, cache_kind="pair_qa", prompt_version="v1",
        )
    except Exception as exc:  # noqa: BLE001 - a failed answer is not a failed comparison
        logger.info("Pair Q&A unavailable: %s", exc)
        return {"answer": f"The model could not answer right now ({type(exc).__name__}).", "confident": False, "model": None}
    return {"answer": out.answer.strip(), "confident": out.confident, "model": getattr(llm, "model", None) or "llm"}


# --------------------------------------------------------------------------------------
# Multiple people: relations among a chosen set
# --------------------------------------------------------------------------------------


def _seed_people(graph: dict[str, Any]) -> list[str]:
    return [n["id"] for n in graph.get("nodes", []) if n["kind"] == "person" and n["data"].get("seed")]


def group_relations(graph: dict[str, Any], ids: list[str] | None = None) -> dict[str, Any]:
    """How every one of the chosen people relates to every other. ``ids`` defaults to the
    seed people (the ones the analyst entered). Returns the pairwise connections and, per
    person, their relation to each of the others - the deterministic, sourced view."""
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    people = [pid for pid in (ids or _seed_people(graph)) if pid in nodes and nodes[pid]["kind"] == "person"]
    name = {pid: nodes[pid]["label"] for pid in people}

    pairs: list[dict[str, Any]] = []
    for i, a in enumerate(people):
        for b in people[i + 1:]:
            c = compare_people(graph, a, b)
            if c is None:
                continue
            pairs.append({
                "a": a, "b": b, "a_name": name[a], "b_name": name[b],
                "strength": c["strength"], "verdict": c["verdict"],
                "degrees": c["route"]["degrees"], "inferred": c["route"]["inferred"],
                "direct": len(c["direct"]), "shared": len(c["shared"]), "mutual": len(c["mutual"]),
                "summary": rule_explanation(c),
                "sentences": [d.get("sentence") for d in c["direct"] if d.get("sentence")],
                "route": [h.get("sentence") or f"{h['from']} → {h['to']}: {h['relation']}" for h in c["route"]["hops"]],
            })

    strong = sum(1 for p in pairs if p["strength"] == "strong")
    linked = sum(1 for p in pairs if p["strength"] in ("strong", "weak"))
    return {
        "people": [{"id": pid, "name": name[pid], "cnic": nodes[pid]["data"].get("cnic"),
                    "phones": nodes[pid]["data"].get("phones", [])[:2],
                    "criminal": bool(criminal_flags(nodes[pid]["data"]))} for pid in people],
        "pairs": pairs,
        "summary": (f"{len(people)} people, {len(pairs)} pairs: {strong} connected by a stated link, "
                    f"{linked - strong} by an inferred lead, {len(pairs) - linked} with no link found."),
    }


class _GroupRead(BaseModel):
    overview: str = Field(description="2-4 sentences: how this group of people hangs together, if at all.")
    findings: list[str] = Field(default_factory=list, description="Up to 6 bullets, each citing [System], naming who relates to whom and how.")
    next_steps: list[str] = Field(default_factory=list, description="Up to 3 concrete investigative checks.")


_GROUP_SYSTEM = (
    "You are a police intelligence analyst. You are given several people and, for each "
    "pair, the sourced facts of how they connect (stated links, shared records, routes, "
    "shared details). Explain how this GROUP relates. Rules: use ONLY the facts; cite the "
    "source system in [brackets]; word each relationship as a directed sentence between "
    "named people (who filed an FIR against whom, who is whose landlord) with FIR charges "
    "where known; call inferred links unconfirmed leads; if a criminal footprint is "
    "present, foreground it; if some people have no link, say so plainly; never invent a "
    "person, FIR, number or relationship. Point out any person who ties several others "
    "together."
)


def explain_group(group: dict[str, Any], llm: Any) -> dict[str, Any] | None:
    if llm is None:
        return None
    try:
        out, _meta = llm.generate_structured(
            prompt="Group facts (JSON):\n" + json.dumps(group, ensure_ascii=False, default=str)[:16000]
                   + "\n\nExplain how the group relates, as JSON.",
            schema=_GroupRead, system=_GROUP_SYSTEM, cache_kind="group_relate", prompt_version="v1",
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("Group explanation unavailable: %s", exc)
        return None
    return {"overview": out.overview.strip(), "findings": [x.strip() for x in out.findings][:6],
            "next_steps": [x.strip() for x in out.next_steps][:3], "model": getattr(llm, "model", None) or "llm"}
