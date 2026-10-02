"""Linkage scenarios: patterns an investigator looks for, found by rule.

Each detector reads one finished (or growing) graph and reports **findings**: a named
pattern, the people in it, and the evidence - every item traceable to a record and its
source system. Findings are leads with a tier, never verdicts:

* ``stated``        - one system states it outright (two FIRs name both as accused)
* ``corroborated``  - two or more independent systems agree
* ``inferred``      - a rule combines stated facts into a pattern (same method, safe house)
* ``speculative``   - rests on names or OSINT alone

``sensitive`` marks findings about police officers. They are for supervisors: shown,
but flagged, because the cost of being wrong about an officer is high.

A note on FIR identity: CRO names a police station ("PS Gulshan-e-Iqbal") where PSRMS
gives its number ("PS#501"), so FIRs are matched on number/year. Two different stations
can register the same number in a year - every FIR finding lists the stations seen, and
the analyst should confirm them.
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.linkgraph.normalize import name_key
from sherlocks.linkgraph.rarity import rarity_factor
from sherlocks.linkgraph.systems import system_label
from sherlocks.linkgraph.weak_links import _stay_overlap

TIERS = ("stated", "corroborated", "inferred", "speculative")

# What each scenario means, for the portal's legend and the investigator's tool list.
SCENARIOS: dict[str, tuple[str, str]] = {
    "repeat_co_offenders": ("Repeat co-offenders", "Accused together in two or more FIRs - the core of a gang."),
    "counter_fir": ("Counter-FIRs (feud)", "Each has filed an FIR in which the other is accused."),
    "recurring_witness": ("Recurring witness", "The same person witnesses FIRs of different accused - a stock witness or facilitator."),
    "recurring_officer": ("Recurring investigating officer", "One officer investigates several of this network's FIRs."),
    "same_method": ("Same method, no shared FIR", "Same charges at the same police station, different accused - a possible cell."),
    "vehicle_owner_driver": ("Owner is not the driver", "A vehicle registered to one person is driven or challaned under another."),
    "shared_vehicle": ("Shared vehicle", "The same plate appears in the records of several people."),
    "safe_house": ("Landlord with many tenants", "One landlord houses several people in this network - a possible safe house when any is flagged."),
    "tenancy_witness": ("Recurring tenancy witness", "The same person signs as witness on several tenancies."),
    "recurring_guarantor": ("Recurring guarantor", "The same person vouches as PRVS verification witness for several people."),
    "sim_front": ("SIMs in someone else's name", "SIMs used by others are registered on one CNIC - a front or SIM supplier."),
    "insider": ("Possible insider", "An employee/servant of a complainant is tied to the accused side."),
    "police_link": ("Officer linked to a criminal", "A police officer is linked to a person with a criminal footprint (not as investigator)."),
    "travel_together": ("Travelled together", "Overlapping hotel stays, more than once."),
    "dual_identity": ("Possible second identity", "Same name and father's name under different CNICs."),
    "alias": ("Aliases", "One person recorded under clearly different names."),
    "multi_source_link": ("Corroborated link", "Two people linked by two or more independent systems."),
    "hidden_associate": ("Hidden associate", "No direct link, but several shared contacts."),
    "broker": ("Broker", "Sits between clusters of this network - its connector."),
    "criminal_proximity": ("Close to a criminal", "A target is one or two stated hops from a person with a criminal footprint."),
}

_FIR_NO = re.compile(r"(\d+)\s*/\s*(\d{2,4})")
_SECTION = re.compile(r"\b(\d{2,3}(?:-?[A-Z])?)\b")
# Sections that ride along with almost any charge (common intention, abetment, rioting).
_GENERIC_SECTIONS = {"34", "109", "114", "147", "148", "149", "PPC"}


def fir_id(text: object) -> str | None:
    m = _FIR_NO.search(str(text or ""))
    if not m:
        return None
    year = m.group(2)
    year = f"20{year}" if len(year) == 2 else year
    return f"{int(m.group(1))}/{year}"


def role_of(text: object) -> str:
    t = str(text or "").lower()
    if any(w in t for w in ("accused", "suspect", "nominated")):
        return "accused"
    if "complainant" in t:
        return "complainant"
    if "investigating" in t or "officer" in t:
        return "io"
    if "witness" in t:
        return "witness"
    if "victim" in t:
        return "victim"
    return "named"


def sections(text: object) -> set[str]:
    return {s.replace("-", "") for s in _SECTION.findall(str(text or "").upper())} - _GENERIC_SECTIONS


@dataclass
class Finding:
    scenario: str
    tier: str
    score: float
    people: list[str]
    summary: str
    evidence: list[dict] = field(default_factory=list)
    sensitive: bool = False

    def to_dict(self, net: PersonNetwork) -> dict[str, Any]:
        title, meaning = SCENARIOS[self.scenario]
        return {"id": f"{self.scenario}:{','.join(sorted(self.people))}", "scenario": self.scenario,
                "title": title, "meaning": meaning, "tier": self.tier, "score": round(self.score, 2),
                "people": self.people, "names": [net.name(p) for p in self.people],
                "summary": self.summary, "evidence": self.evidence[:8], "sensitive": self.sensitive}


def _ev(text: str, system: str | None = None, edge: str | None = None, record: str | None = None) -> dict:
    return {"text": text, "system": system_label(system) if system else None, "edge": edge, "record": record}


# --------------------------------------------------------------------------------------
# Indexes the detectors share
# --------------------------------------------------------------------------------------


@dataclass
class _Fir:
    roles: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    stations: set[str] = field(default_factory=set)
    sections: set[str] = field(default_factory=set)
    systems: set[str] = field(default_factory=set)

    def with_role(self, role: str) -> set[str]:
        return {p for p, roles in self.roles.items() if role in roles}


class _Context:
    def __init__(self, graph: dict[str, Any], net: PersonNetwork) -> None:
        self.graph, self.net = graph, net
        self.nodes = {n["id"]: n for n in graph.get("nodes", [])}
        # Strong edges as (owner of the record, named person, label, system, edge id).
        self.stated: list[tuple[str, str, str, str | None, str]] = []
        for e in graph.get("edges", []):
            if e["kind"] != "strong":
                continue
            src = e["source"]
            owner = self.nodes.get(src, {}).get("data", {}).get("owner") if str(src).startswith("s:") else src
            if owner and owner in net.people and e["target"] in net.people:
                self.stated.append((owner, e["target"], e.get("label") or "", e.get("system"), e["id"]))
        self.firs: dict[str, _Fir] = defaultdict(_Fir)
        for pid, node in net.people.items():
            for f in node["data"].get("firs") or []:
                fid = fir_id(f.get("label"))
                if fid:
                    fir = self.firs[fid]
                    fir.roles[pid].add(role_of(f.get("role")))
                    fir.stations.add(str(f.get("ps") or ""))
                    fir.sections |= sections(f.get("offence"))
                    fir.systems.add(f.get("system") or "")
        for owner, named, label, system, _ in self.stated:
            fid = fir_id(label) if "fir" in label.lower() else None
            if not fid:
                continue
            fir = self.firs[fid]
            fir.systems.add(system or "")
            low = label.lower()
            if low.startswith("same identifier"):
                inner = re.search(r"\(([^)]*)\)", label)
                fir.roles[named].add(role_of(inner.group(1) if inner else ""))
            elif low.startswith("complainant against the subject"):
                fir.roles[named].add("complainant")
                fir.roles[owner].add("accused")
            elif low.startswith("accused by the subject"):
                fir.roles[named].add("accused")
                fir.roles[owner].add("complainant")
            elif low.startswith("co-accused in fir"):
                fir.roles[named].add("accused")
                fir.roles[owner].add("accused")
            else:
                fir.roles[named].add(role_of(label))
        for node in self.nodes.values():
            if node["kind"] == "system" and node["data"].get("system") == "fir_roster":
                fid = fir_id(node["label"])
                fields = {f.get("label"): f.get("value") for f in node["data"].get("fields") or []}
                if fid and fields.get("Sections"):
                    self.firs[fid].sections |= sections(fields["Sections"])

    def fir_text(self, fid: str) -> str:
        stations = sorted(s for s in self.firs[fid].stations if s)
        charges = ", ".join(sorted(self.firs[fid].sections))
        return f"FIR {fid}" + (f" ({' / '.join(stations)})" if stations else "") + (f" · {charges}" if charges else "")

    def labelled(self, *prefixes: str) -> list[tuple[str, str, str, str | None, str]]:
        return [s for s in self.stated if any(s[2].lower().startswith(p) for p in prefixes)]


# --------------------------------------------------------------------------------------
# Detectors
# --------------------------------------------------------------------------------------


def _repeat_co_offenders(ctx: _Context) -> list[Finding]:
    together: dict[tuple[str, str], list[str]] = defaultdict(list)
    for fid, fir in ctx.firs.items():
        for a, b in itertools.combinations(sorted(fir.with_role("accused")), 2):
            together[(a, b)].append(fid)
    return [Finding("repeat_co_offenders", "stated", min(1.0, 0.6 + 0.15 * len(firs)), [a, b],
                    f"{ctx.net.name(a)} and {ctx.net.name(b)} are accused together in {len(firs)} FIRs.",
                    [_ev(ctx.fir_text(f), ",".join(sorted(ctx.firs[f].systems - {''}))) for f in firs])
            for (a, b), firs in together.items() if len(firs) >= 2]


def _counter_fir(ctx: _Context) -> list[Finding]:
    filed: dict[tuple[str, str], list[str]] = defaultdict(list)   # (complainant, accused) -> FIRs
    for fid, fir in ctx.firs.items():
        for c, a in itertools.product(fir.with_role("complainant"), fir.with_role("accused")):
            if c != a:
                filed[(c, a)].append(fid)
    out = []
    for (c, a), firs in filed.items():
        if c < a and (a, c) in filed:
            back = filed[(a, c)]
            out.append(Finding("counter_fir", "stated", 0.8, [c, a],
                               f"{ctx.net.name(c)} filed against {ctx.net.name(a)}, and {ctx.net.name(a)} filed "
                               f"against {ctx.net.name(c)} - a feud or a counter-case.",
                               [_ev(f"{ctx.net.name(c)} complainant, {ctx.net.name(a)} accused: {ctx.fir_text(f)}") for f in firs]
                               + [_ev(f"{ctx.net.name(a)} complainant, {ctx.net.name(c)} accused: {ctx.fir_text(f)}") for f in back]))
    return out


def _recurring(ctx: _Context, role: str, scenario: str, noun: str) -> list[Finding]:
    by_person: dict[str, list[str]] = defaultdict(list)
    for fid, fir in ctx.firs.items():
        for pid in fir.with_role(role):
            by_person[pid].append(fid)
    out = []
    for pid, firs in by_person.items():
        # At least two of their FIRs must have no accused in common: the same witness or
        # officer on two cases against the same man is ordinary, across unrelated cases it
        # is a pattern.
        sets = [ctx.firs[f].with_role("accused") for f in firs]
        if not any(x and y and not x & y for x, y in itertools.combinations(sets, 2)):
            continue
        people = sorted({a for f in firs for a in ctx.firs[f].with_role("accused")} - {pid})
        out.append(Finding(scenario, "inferred", min(0.85, 0.45 + 0.1 * len(firs)), [pid, *people],
                           f"{ctx.net.name(pid)} is {noun} in {len(firs)} FIRs with different accused.",
                           [_ev(ctx.fir_text(f) + " - accused: " + ", ".join(ctx.net.name(a) for a in ctx.firs[f].with_role("accused")))
                            for f in firs], sensitive=scenario == "recurring_officer"))
    return out


def _same_method(ctx: _Context) -> list[Finding]:
    out = []
    items = [(fid, fir) for fid, fir in ctx.firs.items() if fir.sections and fir.with_role("accused")]
    for (f1, x), (f2, y) in itertools.combinations(items, 2):
        shared = x.sections & y.sections
        same_station = bool({name_key(s) for s in x.stations if s} & {name_key(s) for s in y.stations if s})
        ax, ay = x.with_role("accused"), y.with_role("accused")
        if not shared or not same_station or ax & ay:
            continue
        people = sorted(ax | ay)
        out.append(Finding("same_method", "inferred", 0.45 + 0.05 * min(3, len(shared)), people,
                           f"{ctx.fir_text(f1)} and {ctx.fir_text(f2)} share charges {', '.join(sorted(shared))} at the "
                           f"same police station but no accused - {', '.join(ctx.net.name(p) for p in people)}.",
                           [_ev(ctx.fir_text(f1)), _ev(ctx.fir_text(f2))]))
    return out


def _vehicles(ctx: _Context) -> list[Finding]:
    out = []
    for owner_rec, named, label, system, edge in ctx.labelled("vehicle owner", "driver of vehicle"):
        driver, owner = (owner_rec, named) if label.lower().startswith("vehicle owner") else (named, owner_rec)
        out.append(Finding("vehicle_owner_driver", "stated", 0.75, [owner, driver],
                           f"A vehicle registered to {ctx.net.name(owner)} is driven by {ctx.net.name(driver)}.",
                           [_ev(label, system, edge)]))
    covered = {frozenset(f.people) for f in out}
    plates: dict[str, set[str]] = defaultdict(set)
    for pid, node in ctx.net.people.items():
        for plate in node["data"].get("vehicles") or []:
            plates[re.sub(r"[^A-Z0-9]", "", str(plate).upper())].add(pid)
    for plate, pids in plates.items():
        if len(pids) >= 2 and frozenset(pids) not in covered:
            out.append(Finding("shared_vehicle", "stated", 0.6, sorted(pids),
                               f"Vehicle {plate} appears in the records of {', '.join(ctx.net.name(p) for p in pids)}.",
                               [_ev(f"{plate} on {ctx.net.name(p)}'s records") for p in pids]))
    return out


def _tenancy(ctx: _Context) -> list[Finding]:
    tenants: dict[str, dict[str, tuple]] = defaultdict(dict)
    for owner, named, label, system, edge in ctx.labelled("landlord"):
        tenants[named][owner] = (label, system, edge)
    for owner, named, label, system, edge in ctx.labelled("tenant", "co-tenant"):
        if not label.lower().startswith("tenancy witness"):
            tenants[owner][named] = (label, system, edge)
    out = []
    for landlord, rows in tenants.items():
        if len(rows) < 3:
            continue
        flagged = [t for t in rows if ctx.net.criminal(t)]
        out.append(Finding("safe_house", "inferred", 0.65 if flagged else 0.4, [landlord, *rows],
                           f"{ctx.net.name(landlord)} is landlord to {len(rows)} people in this network"
                           + (f", including {', '.join(ctx.net.name(t) for t in flagged)} with a criminal record"
                              " - check for a safe house." if flagged else "."),
                           [_ev(f"{ctx.net.name(t)}: {lab}", sys_, e) for t, (lab, sys_, e) in rows.items()]))
    vouched: dict[str, dict[str, tuple]] = defaultdict(dict)
    for owner, named, label, system, edge in ctx.labelled("verification witness"):
        vouched[named][owner] = (label, system, edge)
    for witness, rows in vouched.items():
        if len(rows) >= 2:
            flagged = [p for p in rows if ctx.net.criminal(p)]
            out.append(Finding("recurring_guarantor", "stated", 0.6 if flagged else 0.5, [witness, *rows],
                               f"{ctx.net.name(witness)} vouched in PRVS for {', '.join(ctx.net.name(p) for p in rows)}"
                               + (f" - including {', '.join(ctx.net.name(p) for p in flagged)} with a criminal record." if flagged else "."),
                               [_ev(f"{ctx.net.name(p)}: {lab}", sys_, e) for p, (lab, sys_, e) in rows.items()]))
    witnessed: dict[str, dict[str, tuple]] = defaultdict(dict)
    for owner, named, label, system, edge in ctx.labelled("tenancy witness"):
        witnessed[named][owner] = (label, system, edge)
    for witness, rows in witnessed.items():
        if len(rows) >= 2:
            out.append(Finding("tenancy_witness", "inferred", 0.5, [witness, *rows],
                               f"{ctx.net.name(witness)} witnessed the tenancies of {', '.join(ctx.net.name(t) for t in rows)}.",
                               [_ev(f"{ctx.net.name(t)}: {lab}", sys_, e) for t, (lab, sys_, e) in rows.items()]))
    return out


def _sim_front(ctx: _Context) -> list[Finding]:
    users: dict[str, dict[str, tuple]] = defaultdict(dict)
    for user, owner, label, system, edge in ctx.labelled("registered owner of sim", "sim "):
        users[owner][user] = (label, system, edge)
    out = []
    for owner, rows in users.items():
        flagged = [u for u in rows if ctx.net.criminal(u)]
        if len(rows) < 2 and not flagged:
            continue
        out.append(Finding("sim_front", "stated" if len(rows) >= 2 else "inferred", 0.6 + 0.1 * min(3, len(rows) - 1),
                           [owner, *rows],
                           f"SIMs used by {', '.join(ctx.net.name(u) for u in rows)} are registered on "
                           f"{ctx.net.name(owner)}'s CNIC" + (" - including a person with a criminal record." if flagged else "."),
                           [_ev(f"{ctx.net.name(u)}: {lab}", sys_, e) for u, (lab, sys_, e) in rows.items()]))
    return out


def _insider(ctx: _Context) -> list[Finding]:
    out = []
    victims = {p: f for f, fir in ctx.firs.items() for p in fir.with_role("complainant") | fir.with_role("victim")}
    accused_anywhere = {p for fir in ctx.firs.values() for p in fir.with_role("accused")}
    for a, b, label, system, edge in ctx.stated:
        if system not in ("sbvs", "evs", "hope"):
            continue
        for boss, worker in ((a, b), (b, a)):
            if boss not in victims:
                continue
            fid = victims[boss]
            accused = ctx.firs[fid].with_role("accused")
            near = [x for x in accused if ctx.net.G.has_edge(worker, x) and ctx.net.G[worker][x]["stated"]]
            if worker in accused_anywhere or near:
                out.append(Finding("insider", "inferred", 0.6, [boss, worker, *near],
                                   f"{ctx.net.name(worker)} is tied to {ctx.net.name(boss)} by an employment/verification "
                                   f"record, and {ctx.net.name(boss)} is complainant in {ctx.fir_text(fid)}; "
                                   + (f"{ctx.net.name(worker)} has an accused record." if worker in accused_anywhere
                                      else f"{ctx.net.name(worker)} is linked to the accused {', '.join(ctx.net.name(x) for x in near)}."),
                                   [_ev(label, system, edge), _ev(ctx.fir_text(fid))]))
    return out


def _police_link(ctx: _Context) -> list[Finding]:
    out = []
    officers = [p for p, n in ctx.net.people.items() if "police_officer" in (n["data"].get("flags") or [])]
    for officer in officers:
        for other in ctx.net.G.neighbors(officer):
            if not ctx.net.criminal(other):
                continue
            links = [link for link in ctx.net.G[officer][other]["links"] if "investigating" not in link.label.lower()]
            if not links:
                continue
            best = min(links, key=lambda link: link.cost)
            out.append(Finding("police_link", "inferred" if best.kind != "weak" else "speculative",
                               0.55 if best.kind != "weak" else 0.35, [officer, other],
                               f"Police officer {ctx.net.name(officer)} is linked to {ctx.net.name(other)} "
                               f"(criminal footprint) other than as investigator: {best.sentence or best.label}.",
                               [_ev(link.sentence or link.label, link.system, link.edge) for link in links],
                               sensitive=True))
    return out


def _travel(ctx: _Context) -> list[Finding]:
    out = []
    people = [(p, n["data"].get("stays") or []) for p, n in ctx.net.people.items() if n["data"].get("stays")]
    for (a, sa), (b, sb) in itertools.combinations(people, 2):
        hits = [hit[1] for x, y in itertools.product(sa, sb) if (hit := _stay_overlap(x, y)) and hit[0] >= 0.7]
        if len(hits) >= 2 or (hits and (ctx.net.criminal(a) or ctx.net.criminal(b))):
            out.append(Finding("travel_together", "inferred", min(0.8, 0.45 + 0.15 * len(hits)), [a, b],
                               f"{ctx.net.name(a)} and {ctx.net.name(b)} stayed at the same hotel at the same time "
                               f"{len(hits)} time(s).", [_ev(h, "hotel_eye") for h in hits]))
    return out


def _identity(ctx: _Context) -> list[Finding]:
    out = []
    people = list(ctx.net.people.items())
    for (a, na), (b, nb) in itertools.combinations(people, 2):
        da, db = na["data"], nb["data"]
        if not (da.get("cnic") and db.get("cnic")) or da["cnic"] == db["cnic"]:
            continue
        if not (da.get("father_name") and db.get("father_name")):
            continue
        if fuzz.token_sort_ratio(name_key(da.get("name")), name_key(db.get("name"))) < 95:
            continue
        if fuzz.token_sort_ratio(name_key(da["father_name"]), name_key(db["father_name"])) < 92:
            continue
        score = round(0.6 * rarity_factor(f"{da.get('name')} {da['father_name']}"), 2)
        if score >= 0.3:
            out.append(Finding("dual_identity", "inferred", score, [a, b],
                               f"{na['label']} s/o {da['father_name']} appears under CNIC {da['cnic']} and {db['cnic']}.",
                               [_ev(f"CNIC {da['cnic']}"), _ev(f"CNIC {db['cnic']}")]))
    for pid, node in people:
        names = list(dict.fromkeys(node["data"].get("names") or []))
        distinct = [n for n in names if all(fuzz.token_set_ratio(name_key(n), name_key(m)) < 75 for m in names if m != n)]
        if len(names) >= 2 and distinct:
            out.append(Finding("alias", "speculative", 0.3, [pid],
                               f"{node['label']} is recorded as: {'; '.join(names[:5])}.",
                               [_ev(n) for n in names[:5]]))
    return out


# Links that exist only because two records carry the same phone/CNIC. However many
# systems repeat it, it is one fact - the shared identifier - not independent evidence.
_IDENTIFIER_LINK = ("same identifier", "used same phone", "registered with the same number", "same number on",
                    "with the same contact", "shared phone", "registered owner of", "sim ", "verification record on same",
                    "police officer record with the same")


def _source(link: Any) -> str | None:
    if link.kind == "weak" or not link.system:
        return None
    label = link.label.lower()
    return "shared identifier" if any(marker in label for marker in _IDENTIFIER_LINK) else link.system


def _corroborated(ctx: _Context) -> list[Finding]:
    out = []
    for a, b, d in ctx.net.G.edges(data=True):
        systems = {_source(link) for link in d["links"]} - {None}
        if len(systems) >= 2:
            out.append(Finding("multi_source_link", "corroborated", min(0.95, 0.7 + 0.08 * len(systems)), [a, b],
                               f"{ctx.net.name(a)} and {ctx.net.name(b)} are linked by {len(systems)} independent systems: "
                               f"{', '.join(sorted(s if s == 'shared identifier' else system_label(s) for s in systems))}.",
                               [_ev(link.sentence or link.label, link.system, link.edge, link.record)
                                for link in d["links"] if link.kind != "weak"]))
    return out


def _structure(ctx: _Context) -> list[Finding]:
    net, out = ctx.net, []
    for h in net.hidden_associates(top=5):
        out.append(Finding("hidden_associate", "inferred", min(0.7, 0.3 + 0.1 * h["score"]), [h["a"], h["b"], *h["shared_ids"]],
                           f"{h['a_name']} and {h['b_name']} have no direct link but share {len(h['shared'])} contacts: "
                           f"{', '.join(h['shared'])}.", [_ev(f"shared contact: {s}") for s in h["shared"]]))
    for b in net.brokers(top=3):
        if b["bridges_clusters"] >= 2 and not b["hub"]:
            out.append(Finding("broker", "inferred", min(0.8, 0.4 + b["betweenness"]), [b["id"]],
                               f"{b['name']} connects {b['bridges_clusters']} clusters of this network "
                               f"(on {b['betweenness']:.0%} of the shortest routes).", []))
    for seed in net.seeds():
        prox = net.criminal_proximity(seed)
        if prox and prox.get("nearest") and prox["hops"] <= 2 and not prox["route"]["inferred"]:
            route = prox["route"]
            out.append(Finding("criminal_proximity", "stated", 0.7 if prox["hops"] == 1 else 0.55,
                               route["nodes"], f"{net.name(seed)} is {prox['hops']} stated hop(s) from "
                               f"{prox['nearest_name']} ({', '.join(prox['flags'])}).",
                               [_ev(h["relation"], None, h["edge"]) for h in route["hops"]]))
    return out


DETECTORS: list[Callable[[_Context], list[Finding]]] = [
    _repeat_co_offenders, _counter_fir,
    lambda c: _recurring(c, "witness", "recurring_witness", "a witness"),
    lambda c: _recurring(c, "io", "recurring_officer", "investigating officer"),
    _same_method, _vehicles, _tenancy, _sim_front, _insider, _police_link, _travel, _identity,
    _corroborated, _structure,
]


def find_scenarios(graph: dict[str, Any], *, only: set[str] | None = None,
                   net: PersonNetwork | None = None) -> list[dict[str, Any]]:
    """Every finding in the graph, strongest tier first."""
    net = net or PersonNetwork(graph)
    ctx = _Context(graph, net)
    findings: list[Finding] = []
    for detector in DETECTORS:
        findings.extend(detector(ctx))
    rows = [f.to_dict(net) for f in findings if not only or f.scenario in only]
    unique = {r["id"]: r for r in rows}
    return sorted(unique.values(), key=lambda r: (TIERS.index(r["tier"]), -r["score"]))
