"""The graph as people connected to people, and the questions only structure answers.

The link graph has record nodes between people ("Kamran's Old Tenant record names
Tariq as landlord"). For network questions - who bridges two groups, who is two steps
from a criminal, which two people share suspiciously many contacts - records are in
the way. This module projects the graph onto people:

* a strong edge from X's record to Y          ->  X - Y  (stated by a system)
* two people on the same record (an FIR)      ->  X - Y  (shared record)
* a weak edge                                 ->  X - Y  (inferred, scored)

Each person-person edge keeps every link behind it, with its source system, so every
answer can still be traced to a record.

**Cost, not distance.** A route is only as good as its weakest hop, and a route through
a crowd means little: everyone in a district shares its police station, every tenant a
big landlord. So each hop costs more when it is inferred rather than stated, and more
again when it passes through a hub - a person or record joined to many others. Routes,
brokers and hidden associates are all computed on that cost.

Nothing here decides that anyone is connected: it ranks and explains what the records
already say.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

import networkx as nx
from rapidfuzz import fuzz, process

from sherlocks.linkgraph.dossier import criminal_flags
from sherlocks.linkgraph.graph import witness_role
from sherlocks.linkgraph.normalize import name_key
from sherlocks.linkgraph.systems import system_label

# A record naming more people than this (a big FIR roster) is a crowd: its people are
# not paired with each other, only with the record's owner.
HUB_RECORD = 8
# A person linked to more people than this is a hub; routes through them cost more and
# they never count as a "shared contact".
HUB_PERSON = 12

_COST = {"strong": 1.0, "shared": 1.3}


def _weak_cost(score: float | None) -> float:
    return 1.0 + 3.0 * (1.0 - float(score or 0.0))


@dataclass
class Link:
    """One reason two people are joined."""

    kind: str                 # strong | shared | weak
    label: str
    system: str | None
    edge: str | None          # graph edge id, for highlighting
    record: str | None = None
    sentence: str | None = None
    score: float | None = None

    @property
    def cost(self) -> float:
        return _weak_cost(self.score) if self.kind == "weak" else _COST.get(self.kind, 1.0)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "system": self.system,
                "via": system_label(self.system) if self.system else None, "edge": self.edge,
                "record": self.record, "sentence": self.sentence, "score": self.score}


@dataclass
class PersonNetwork:
    graph: dict[str, Any]
    G: nx.Graph = field(init=False)
    people: dict[str, dict] = field(init=False)
    hub_records: list[dict] = field(init=False)

    def __post_init__(self) -> None:
        nodes = {n["id"]: n for n in self.graph.get("nodes", [])}
        self.people = {i: n for i, n in nodes.items() if n["kind"] == "person"}
        self.G = nx.Graph()
        for pid, n in self.people.items():
            self.G.add_node(pid, name=n["label"], data=n["data"])
        members: dict[str, list[str]] = {}
        roles: dict[tuple[str, str], str] = {}   # (record, person) -> their role on it
        for e in self.graph.get("edges", []):
            src, dst = e["source"], e["target"]
            if e["kind"] == "found_in":
                members.setdefault(dst, []).append(src)
                roles[(dst, src)] = e.get("label") or ""
                continue
            if e["kind"] == "strong" and str(src).startswith("s:"):
                roles.setdefault((src, dst), e.get("label") or "")
            if e["kind"] == "strong" and str(src).startswith("s:"):
                owner = (nodes.get(src) or {}).get("data", {}).get("owner")
                if owner:
                    self._add(owner, dst, Link("strong", e.get("label") or "", e.get("system"), e["id"], src,
                                               (e.get("relation") or {}).get("sentence")))
            else:
                self._add(src, dst, Link(e["kind"], e.get("label") or "", e.get("system"), e["id"], None,
                                         (e.get("relation") or {}).get("sentence"), e.get("score")))
        self.hub_records = []
        for rid, pids in members.items():
            pids = list(dict.fromkeys(pids))
            if len(pids) < 2:
                continue
            record = nodes.get(rid) or {}
            if len(pids) > HUB_RECORD:
                self.hub_records.append({"record": rid, "label": record.get("label"), "people": len(pids)})
                continue
            for a, b in itertools.combinations(pids, 2):
                if witness_role(roles.get((rid, a))) and witness_role(roles.get((rid, b))):
                    # Witnesses of the same case are not witnesses of each other.
                    self._add(a, b, Link("weak", f"Both witnesses on {record.get('label') or rid}",
                                         record.get("data", {}).get("system"), None, rid, score=0.5))
                    continue
                self._add(a, b, Link("shared", f"Both on {record.get('label') or rid}",
                                     record.get("data", {}).get("system"), None, rid))
        # Hop cost: the best link's cost, plus a toll for each end that is a hub.
        for u, v, d in self.G.edges(data=True):
            toll = 0.25 * (math.log2(1 + self.G.degree(u)) + math.log2(1 + self.G.degree(v)))
            d["cost"] = min(link.cost for link in d["links"]) + toll
            d["stated"] = any(link.kind != "weak" for link in d["links"])

    def _add(self, a: str, b: str, link: Link) -> None:
        if a == b or a not in self.people or b not in self.people:
            return
        if not self.G.has_edge(a, b):
            self.G.add_edge(a, b, links=[])
        self.G[a][b]["links"].append(link)

    # -- lookup ---------------------------------------------------------------------

    def name(self, pid: str) -> str:
        return self.people[pid]["label"] if pid in self.people else pid

    def data(self, pid: str) -> dict:
        return self.people[pid]["data"] if pid in self.people else {}

    def resolve(self, ref: str | None) -> str | None:
        """A person id, CNIC, phone or (fuzzy) name -> person id."""
        ref = str(ref or "").strip()
        if not ref:
            return None
        if ref in self.people:
            return ref
        digits = "".join(c for c in ref if c.isdigit())
        for pid, n in self.people.items():
            d = n["data"]
            if digits and (digits == (d.get("cnic") or "") or any(digits[-10:] == p[-10:] for p in d.get("phones") or [])):
                return pid
        names = {pid: name_key(n["label"]) for pid, n in self.people.items()}
        hit = process.extractOne(name_key(ref), names, scorer=fuzz.WRatio, score_cutoff=80)
        return hit[2] if hit else None

    def criminal(self, pid: str) -> bool:
        return bool(criminal_flags(self.data(pid)))

    def seeds(self) -> list[str]:
        return [pid for pid, n in self.people.items() if n["data"].get("seed")]

    def is_hub(self, pid: str) -> bool:
        return self.G.degree(pid) > HUB_PERSON

    def link_between(self, a: str, b: str) -> list[dict]:
        if not self.G.has_edge(a, b):
            return []
        return [link.to_dict() for link in self.G[a][b]["links"]]

    def best_link(self, a: str, b: str) -> Link:
        return min(self.G[a][b]["links"], key=lambda link: link.cost)

    # -- questions ------------------------------------------------------------------

    def neighbours(self, pid: str) -> list[dict]:
        out = []
        for other in self.G.neighbors(pid):
            best = self.best_link(pid, other)
            out.append({"id": other, "name": self.name(other), "stated": self.G[pid][other]["stated"],
                        "relation": best.sentence or best.label, "via": system_label(best.system or ""),
                        "links": len(self.G[pid][other]["links"]), "criminal": self.criminal(other)})
        return sorted(out, key=lambda x: (not x["stated"], -x["links"]))

    def describe(self, path: list[str]) -> dict[str, Any]:
        hops = []
        for a, b in itertools.pairwise(path):
            best = self.best_link(a, b)
            hops.append({"from": a, "to": b, "from_name": self.name(a), "to_name": self.name(b),
                         "relation": best.sentence or f"{self.name(a)} – {best.label} – {self.name(b)}",
                         "kind": best.kind, "via": system_label(best.system or ""), "edge": best.edge,
                         "record": best.record, "score": best.score,
                         "through_hub": self.is_hub(b) and b != path[-1]})
        return {"nodes": path, "names": [self.name(p) for p in path], "hops": hops,
                "inferred": any(h["kind"] == "weak" for h in hops),
                "cost": round(sum(self.G[a][b]["cost"] for a, b in itertools.pairwise(path)), 2)}

    def paths(self, a: str, b: str, k: int = 3, max_hops: int = 6) -> list[dict]:
        """The ``k`` cheapest distinct routes from ``a`` to ``b``: stated hops before
        inferred ones, and around hubs rather than through them."""
        if a not in self.G or b not in self.G or a == b or not nx.has_path(self.G, a, b):
            return []
        out = []
        for path in nx.shortest_simple_paths(self.G, a, b, weight="cost"):
            if len(path) - 1 > max_hops:
                break
            out.append(self.describe(path))
            if len(out) >= k:
                break
        return out

    def communities(self, min_size: int = 3) -> list[dict]:
        """Clusters of people more tied to each other than to the rest (Louvain)."""
        if self.G.number_of_edges() == 0:
            return []
        H = self.G.copy()
        for _, _, d in H.edges(data=True):
            d["strength"] = 1.0 / d["cost"]
        groups = nx.community.louvain_communities(H, weight="strength", seed=7)
        out = []
        for index, members in enumerate(sorted(groups, key=len, reverse=True)):
            if len(members) < min_size:
                continue
            members = sorted(members, key=lambda p: -self.G.degree(p))
            out.append({"id": f"c{index + 1}", "size": len(members), "members": members,
                        "names": [self.name(p) for p in members],
                        "criminals": [self.name(p) for p in members if self.criminal(p)],
                        "seeds": [self.name(p) for p in members if self.data(p).get("seed")],
                        "core": self.name(members[0])})
        return out

    def brokers(self, top: int = 8) -> list[dict]:
        """People on many of the cheapest routes between others - the connectors. A
        broker between two clusters is worth more than a busy person inside one."""
        if self.G.number_of_nodes() < 3:
            return []
        centrality = nx.betweenness_centrality(self.G, weight="cost", normalized=True)
        membership = {p: c["id"] for c in self.communities(min_size=2) for p in c["members"]}
        out = []
        for pid, value in sorted(centrality.items(), key=lambda x: -x[1]):
            if value <= 0 or len(out) >= top:
                continue
            touches = {membership.get(n) for n in self.G.neighbors(pid)} - {None}
            out.append({"id": pid, "name": self.name(pid), "betweenness": round(value, 3),
                        "degree": self.G.degree(pid), "bridges_clusters": len(touches),
                        "criminal": self.criminal(pid), "hub": self.is_hub(pid)})
        return out

    def hidden_associates(self, top: int = 10, focus: str | None = None) -> list[dict]:
        """Pairs with no direct link who share two or more contacts. Shared *rare*
        contacts count most (Adamic-Adar): two people who both know the same
        small-time fixer say more than two who both appear under the same big landlord."""
        pool = [focus] if focus else list(self.G.nodes)
        seen: set[tuple[str, str]] = set()
        out = []
        for u in pool:
            if u not in self.G:
                continue
            around = {w for n in self.G.neighbors(u) if not self.is_hub(n) for w in self.G.neighbors(n)}
            for v in around - {u} - set(self.G.neighbors(u)):
                key = (min(u, v), max(u, v))
                if key in seen:
                    continue
                seen.add(key)
                common = [c for c in nx.common_neighbors(self.G, u, v) if not self.is_hub(c)]
                if len(common) < 2:
                    continue
                score = sum(1 / math.log(max(2, self.G.degree(c))) for c in common)
                out.append({"a": u, "b": v, "a_name": self.name(u), "b_name": self.name(v),
                            "score": round(score, 2), "shared": [self.name(c) for c in common],
                            "shared_ids": common,
                            "criminal": self.criminal(u) or self.criminal(v)})
        return sorted(out, key=lambda x: -x["score"])[:top]

    def criminal_proximity(self, pid: str, max_cost: float = 8.0) -> dict | None:
        """The nearest person with a criminal footprint, and the route to them."""
        if pid not in self.G:
            return None
        lengths, routes = nx.single_source_dijkstra(self.G, pid, weight="cost", cutoff=max_cost)
        best = min(((c, p) for p, c in lengths.items() if p != pid and self.criminal(p)), default=None)
        if best is None:
            return {"person": pid, "name": self.name(pid), "self_criminal": self.criminal(pid), "nearest": None}
        route = self.describe(routes[best[1]])
        return {"person": pid, "name": self.name(pid), "self_criminal": self.criminal(pid),
                "nearest": best[1], "nearest_name": self.name(best[1]),
                "flags": criminal_flags(self.data(best[1])), "hops": len(route["hops"]), "route": route}

    def hubs(self) -> list[dict]:
        people = [{"id": p, "name": self.name(p), "degree": self.G.degree(p)}
                  for p in self.G.nodes if self.is_hub(p)]
        return sorted(people, key=lambda x: -x["degree"]) + self.hub_records

    def key_people(self, top: int = 10) -> list[dict]:
        """Who matters most to this network: connections, brokerage and criminal
        exposure together. A ranking to direct attention, not a verdict."""
        if not self.G.number_of_nodes():
            return []
        between = nx.betweenness_centrality(self.G, weight="cost", normalized=True) if self.G.number_of_nodes() > 2 else {}
        rows = []
        for pid in self.G.nodes:
            d = self.data(pid)
            score = (self.G.degree(pid) * 0.15 + between.get(pid, 0) * 4 + (1.5 if self.criminal(pid) else 0)
                     + (0.5 if "police_officer" in (d.get("flags") or []) else 0))
            rows.append({"id": pid, "name": self.name(pid), "score": round(score, 2), "degree": self.G.degree(pid),
                         "criminal": self.criminal(pid), "seed": bool(d.get("seed")),
                         "searched": d.get("search_status") == "searched"})
        return sorted(rows, key=lambda x: -x["score"])[:top]

    def unsearched_leads(self, top: int = 8) -> list[dict]:
        """People drawn but not searched (depth limit, budget) ranked by how much
        searching them could add - what an analyst should spend the next queries on."""
        rows = []
        for pid in self.G.nodes:
            d = self.data(pid)
            if d.get("search_status") not in ("depth_limit", "budget") or not d.get("searchable"):
                continue
            stated = sum(1 for n in self.G.neighbors(pid) if self.G[pid][n]["stated"])
            near_criminal = any(self.criminal(n) for n in self.G.neighbors(pid))
            flags = d.get("flags") or []
            score = stated + (2 if near_criminal else 0) + (2 if flags else 0) + len(d.get("discovered_via") or []) * 0.5
            rows.append({"id": pid, "name": self.name(pid), "cnic": d.get("cnic"),
                         "phone": (d.get("phones") or [None])[0], "score": round(score, 1),
                         "why": ", ".join(x for x in (
                             f"{stated} stated link(s)" if stated else "",
                             "next to a person with a criminal record" if near_criminal else "",
                             f"flags: {', '.join(flags)}" if flags else "",
                             f"not searched ({d.get('search_status').replace('_', ' ')})") if x)})
        return sorted(rows, key=lambda x: -x["score"])[:top]

    def overview(self) -> dict[str, Any]:
        return {"people": self.G.number_of_nodes(), "links": self.G.number_of_edges(),
                "components": nx.number_connected_components(self.G) if self.G.number_of_nodes() else 0,
                "communities": self.communities(), "brokers": self.brokers(), "key_people": self.key_people(),
                "hidden_associates": self.hidden_associates(), "hubs": self.hubs(),
                "unsearched_leads": self.unsearched_leads(),
                "criminal_proximity": [self.criminal_proximity(s) for s in self.seeds()]}
