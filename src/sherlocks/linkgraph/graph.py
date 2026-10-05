"""The graph: identity resolution plus node and edge bookkeeping.

Identity rules, strictest first - a wrong merge invents a relationship between two
real citizens, so these err towards two nodes rather than one:

1. Same CNIC -> same person. Always.
2. Different CNICs -> different people. Always, whatever else matches. If they share a
   phone, that becomes a strong "shared phone" edge, not a merge.
3. A person with no CNIC yet matches on a mobile number held by exactly one node.
4. A person with neither CNIC nor phone matches only on name *and* father's name, and
   only against another identifier-less node (the same unnamed-CNIC FIR witness
   appearing in two FIR reports).

When a phone-only node later resolves to a CNIC that another node already holds, the
two are merged and the merge is recorded, so queued work still finds the survivor.
"""

from __future__ import annotations

import hashlib
import itertools
import re
import threading
from typing import Any

from sherlocks.linkgraph.models import GraphEdge, GraphNode, PersonRef, RelatedPerson, SystemRecord
from sherlocks.linkgraph.normalize import name_key, normalize_address
from sherlocks.linkgraph.relations import describe, weak_relation
from sherlocks.linkgraph.systems import SYSTEMS, system_label


def _display_name(names: list[str]) -> str | None:
    """The first name recorded for this identity, de-shouted ("KAMRAN AHMED" -> "Kamran Ahmed").

    First-seen, not "best-looking": identity systems answer first, so the first name is
    the one tied to the CNIC. Later spellings are kept as "also recorded as".
    """
    if not names:
        return None
    return names[0].title() if names[0].isupper() else names[0]


def _edge_id(kind: str, source: str, target: str, label: str) -> str:
    digest = hashlib.sha1(label.encode()).hexdigest()[:8]
    return f"e:{kind}:{source}>{target}:{digest}"


def witness_role(role: str | None) -> bool:
    """A role that is about someone else's case: witness, verification witness, guarantor."""
    text = (role or "").lower()
    return "witness" in text or "guarantor" in text


class GraphBuilder:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.nodes: dict[str, GraphNode] = {}
        self.edges: dict[str, GraphEdge] = {}
        self._by_cnic: dict[str, str] = {}
        self._phone_owners: dict[str, set[str]] = {}
        self._by_name_father: dict[tuple[str, str], str] = {}
        self._merged: dict[str, str] = {}
        self._ids = itertools.count(1)
        self.version = 0
        # A phone held by more than this many different people is a shared/office line;
        # its "shared phone" links are dropped so it cannot fabricate a hairball.
        self.max_shared_owners = 6
        self._shared_phones: set[str] = set()

    # -- lookup ---------------------------------------------------------------------

    def canonical(self, pid: str) -> str:
        while pid in self._merged:
            pid = self._merged[pid]
        return pid

    def person(self, pid: str) -> dict[str, Any]:
        return self.nodes[self.canonical(pid)].data

    def person_ref(self, pid: str) -> PersonRef:
        data = self.person(pid)
        return PersonRef(name=data.get("name"), father_name=data.get("father_name"), cnic=data.get("cnic"),
                         phones=list(data.get("phones") or []))

    def persons(self) -> list[GraphNode]:
        return [n for n in self.nodes.values() if n.kind == "person"]

    def find_person(self, ref: PersonRef) -> str | None:
        if ref.cnic:
            if ref.cnic in self._by_cnic:
                return self._by_cnic[ref.cnic]
            for phone in ref.phones:
                owners = [o for o in self._phone_owners.get(phone, ()) if not self.nodes[o].data.get("cnic")]
                if len(owners) == 1:
                    return owners[0]
            return None
        for phone in ref.phones:
            owners = self._phone_owners.get(phone, set())
            if len(owners) == 1:
                return next(iter(owners))
            if owners:
                return None  # held by several different CNICs - ambiguous, do not guess
        if ref.name and ref.father_name and not ref.phones:
            return self._by_name_father.get((name_key(ref.name), name_key(ref.father_name)))
        return None

    # -- persons --------------------------------------------------------------------

    def upsert_person(self, ref: PersonRef, *, depth: int, seed: bool = False,
                      via: dict[str, str] | None = None) -> tuple[str, bool]:
        with self._lock:
            pid = self.find_person(ref)
            created = pid is None
            if created:
                pid = f"p{next(self._ids)}"
                self.nodes[pid] = GraphNode(id=pid, kind="person", label="", data={
                    "name": None, "names": [], "father_name": None, "cnic": None, "phones": [],
                    "addresses": [], "images": [], "image_sources": {}, "extra": {}, "flags": [], "depth": depth,
                    "seed": seed, "search_status": "pending", "searchable": False, "records": [],
                    "lookups": {}, "discovered_via": [], "firs": [], "stays": [], "organisations": [],
                    "vehicles": [], "police_stations": [], "osint": [],
                })
            data = self.nodes[pid].data
            data["depth"] = min(data["depth"], depth)
            data["seed"] = data["seed"] or seed
            if via and via not in data["discovered_via"] and len(data["discovered_via"]) < 25:
                data["discovered_via"].append(via)
            self._merge_ref(pid, ref)
            self.version += 1
            return pid, created

    def apply_subject(self, pid: str, ref: PersonRef) -> str:
        """Fold what a record says about its own subject into that person's node."""
        with self._lock:
            pid = self.canonical(pid)
            data = self.nodes[pid].data
            if ref.cnic and not data.get("cnic") and ref.cnic in self._by_cnic and self._by_cnic[ref.cnic] != pid:
                keep = self._by_cnic[ref.cnic]
                self._merge_nodes(keep, pid)
                pid = keep
            elif ref.cnic and data.get("cnic") and ref.cnic != data["cnic"]:
                # Should not happen - extractors match subjects by CNIC - but if it does,
                # nothing of that other person may be grafted onto this one.
                return pid
            self._merge_ref(pid, ref)
            self.version += 1
            return pid

    def _merge_ref(self, pid: str, ref: PersonRef) -> None:
        node = self.nodes[pid]
        data = node.data
        if ref.name and all(name_key(ref.name) != name_key(n) for n in data["names"]):
            data["names"].append(ref.name)
        for alias in (ref.extra.get("aliases") or "").split(";"):
            alias = alias.strip()
            if alias and alias not in data["extra"].get("aliases", []):
                data["extra"].setdefault("aliases", []).append(alias)
        data["name"] = _display_name(data["names"])
        if ref.father_name and not data["father_name"]:
            data["father_name"] = ref.father_name.title() if ref.father_name.isupper() else ref.father_name
        if ref.cnic and not data["cnic"]:
            data["cnic"] = ref.cnic
            self._by_cnic[ref.cnic] = pid
        for phone in ref.phones:
            if phone not in data["phones"]:
                data["phones"].append(phone)
            owners = self._phone_owners.setdefault(phone, set())
            owners.add(pid)
            if len(owners) > 1:
                self._shared_phone_edges(phone)
        known = {normalize_address(a) for a in data["addresses"]}
        for address in ref.addresses:
            if normalize_address(address) not in known:
                data["addresses"].append(address)
                known.add(normalize_address(address))
        for image in ref.images:
            if image not in data["images"]:
                data["images"].append(image)
        for key, value in ref.extra.items():
            if key != "aliases":
                data["extra"].setdefault(key, value)
        if data["name"] and data["father_name"] and not data["cnic"] and not data["phones"]:
            self._by_name_father.setdefault((name_key(data["name"]), name_key(data["father_name"])), pid)
        data["searchable"] = bool(data["cnic"] or data["phones"])
        node.label = data["name"] or (f"CNIC {data['cnic']}" if data["cnic"] else None) or (
            data["phones"][0] if data["phones"] else None) or next(
            (e.get("email") for e in data.get("emails") or [] if e.get("email")), "Unknown")

    def _shared_phone_edges(self, phone: str) -> None:
        owners = sorted(self._phone_owners.get(phone, ()))
        # Shared/office line: too many people hold it. Drop any edges already drawn for
        # it and never draw more - it links no one.
        if len(owners) > self.max_shared_owners:
            if phone not in self._shared_phones:
                self._shared_phones.add(phone)
                for edge_id in [e.id for e in self.edges.values()
                                if e.system == "identity" and e.label == f"Shared phone {phone}"]:
                    self.edges.pop(edge_id, None)
            return
        if phone in self._shared_phones:
            return
        for a, b in itertools.combinations(owners, 2):
            ca, cb = self.nodes[a].data.get("cnic"), self.nodes[b].data.get("cnic")
            if ca and cb and ca != cb:
                self._add_edge(GraphEdge(id=_edge_id("strong", a, b, f"phone {phone}"), source=a, target=b,
                                         kind="strong", label=f"Shared phone {phone}", system="identity",
                                         reasons=[f"{phone} appears in records of both people"]))

    def _merge_nodes(self, keep: str, drop: str) -> None:
        kept, dropped = self.nodes[keep].data, self.nodes.pop(drop).data
        self._merged[drop] = keep
        for phone in dropped["phones"]:
            owners = self._phone_owners.get(phone, set())
            owners.discard(drop)
            owners.add(keep)
        ref = PersonRef(name=None, father_name=dropped["father_name"], phones=dropped["phones"],
                        addresses=dropped["addresses"], images=dropped["images"])
        for name in dropped["names"]:
            if all(name_key(name) != name_key(n) for n in kept["names"]):
                kept["names"].append(name)
        self._merge_ref(keep, ref)
        kept["depth"] = min(kept["depth"], dropped["depth"])
        kept["seed"] = kept["seed"] or dropped["seed"]
        for key in ("flags", "records", "discovered_via", "firs", "stays", "organisations", "vehicles",
                    "police_stations", "osint"):
            for item in dropped[key]:
                if item not in kept[key]:
                    kept[key].append(item)
        kept["lookups"] = {**dropped["lookups"], **kept["lookups"]}
        kept["image_sources"] = {**dropped.get("image_sources", {}), **kept.get("image_sources", {})}
        if dropped["search_status"] == "searched":
            kept["search_status"] = "searched"
        for edge_id, edge in list(self.edges.items()):
            if drop not in (edge.source, edge.target):
                continue
            del self.edges[edge_id]
            source = keep if edge.source == drop else edge.source
            target = keep if edge.target == drop else edge.target
            if source != target:
                self._add_edge(edge.model_copy(update={"id": _edge_id(edge.kind, source, target, edge.label),
                                                       "source": source, "target": target}))
        for node in self.nodes.values():
            if node.kind == "system" and node.data.get("owner") == drop:
                node.data["owner"] = keep

    def set_search_status(self, pid: str, status: str) -> None:
        with self._lock:
            self.person(pid)["search_status"] = status
            self.version += 1

    def note_lookup(self, pid: str, system: str, status: str, summary: str) -> None:
        with self._lock:
            self.person(pid)["lookups"][system] = {"status": status, "summary": summary[:200]}

    # -- system records -------------------------------------------------------------

    def add_record(self, pid: str, rec: SystemRecord, *, record_key: str | None = None,
                   edge_label: str | None = None) -> tuple[str, str]:
        """Attach ``rec`` to ``pid``. Returns ``(system_node_id, canonical_pid)``."""
        with self._lock:
            pid = self.apply_subject(pid, rec.subject)
            # Every photo this record carried came from this system - remember which,
            # so the analyst sees where a face was taken from (DLS licence, SBVS, NADRA…).
            for image in rec.subject.images:
                self.nodes[pid].data["image_sources"].setdefault(image, system_label(rec.system))
            sid = f"s:{rec.system}:{record_key or pid}"
            info = SYSTEMS.get(rec.system)
            if sid not in self.nodes:
                label = system_label(rec.system)
                if rec.system == "fir_roster" and isinstance(rec.raw, dict) and rec.raw.get("fir_label"):
                    label = str(rec.raw["fir_label"])
                self.nodes[sid] = GraphNode(id=sid, kind="system", label=label, data={
                    "system": rec.system, "system_label": system_label(rec.system),
                    "category": info.category if info else "identity",
                    "description": info.description if info else "",
                    "summary": rec.summary, "status": rec.status, "cached": rec.cached,
                    "fields": [f.model_dump() for f in rec.fields], "flags": rec.flags,
                    "owner": pid, "record_key": record_key, "raw": rec.raw,
                    "firs": [f.model_dump() for f in rec.firs], "stays": [s.model_dump() for s in rec.stays],
                    "organisations": rec.organisations, "vehicles": rec.vehicles,
                    "related_count": len(rec.related), "images": rec.subject.images,
                })
            self._add_edge(GraphEdge(id=_edge_id("found_in", pid, sid, ""), source=pid, target=sid,
                                     kind="found_in", label=edge_label or "", system=rec.system))
            data = self.nodes[pid].data
            for flag in rec.flags:
                if flag not in data["flags"]:
                    data["flags"].append(flag)
            entry = {"system": rec.system, "node": sid, "summary": rec.summary}
            if entry not in data["records"]:
                data["records"].append(entry)
            for fir in rec.firs:
                item = {"key": fir.key, "label": f"{fir.fir_no}/{fir.fir_year}", "ps": fir.police_station,
                        "role": fir.role, "system": rec.system, "offence": fir.offence}
                if fir.ps_id:
                    item["ps_id"] = fir.ps_id
                if item not in data["firs"]:
                    data["firs"].append(item)
            for stay in rec.stays:
                if stay.model_dump() not in data["stays"]:
                    data["stays"].append(stay.model_dump())
            for org in rec.organisations:
                if org not in data["organisations"]:
                    data["organisations"].append(org)
            for plate in rec.vehicles:
                if plate not in data["vehicles"]:
                    data["vehicles"].append(plate)
            if rec.police_station and rec.police_station not in data["police_stations"]:
                data["police_stations"].append(rec.police_station)
            self.version += 1
            return sid, pid

    def attach(self, pid: str, sid: str, label: str = "") -> None:
        """A second person's link to an existing shared record (an FIR both are in)."""
        with self._lock:
            pid = self.canonical(pid)
            self._add_edge(GraphEdge(id=_edge_id("found_in", pid, sid, ""), source=pid, target=sid,
                                     kind="found_in", label=label, system=self.nodes[sid].data.get("system")))
            self.version += 1

    def link_related(self, sid: str, rel: RelatedPerson, *, depth: int, from_pid: str) -> tuple[str, bool]:
        with self._lock:
            system = self.nodes[sid].data.get("system", "")
            pid, created = self.upsert_person(rel.ref, depth=depth, via={
                "system": system, "relation": rel.relation, "from": self.canonical(from_pid)})
            if pid == self.canonical(from_pid):
                return pid, False
            if rel.relation.lower().startswith(("co-witness", "co-witness on a tenancy")):
                # Both are witnesses of someone else's case: they share the case, they do not
                # vouch for each other. An inferred link between them, not a stated one.
                a, b = sorted((self.canonical(from_pid), pid))
                fir = rel.relation.split(" in ", 1)[-1] if " in " in rel.relation else "the same tenancy"
                self._add_edge(GraphEdge(id=_edge_id("weak", a, b, "co_witness"), source=a, target=b, kind="weak",
                                         label=f"Both witnesses in {fir}", score=0.5, system=system,
                                         reasons=[f"Both are witnesses in {fir} ({system_label(system)})"]))
                self.version += 1
                return pid, created
            already_in = any(e.kind == "found_in" and e.source == pid and e.target == sid for e in self.edges.values())
            if not already_in:
                self._add_edge(GraphEdge(id=_edge_id("strong", sid, pid, rel.relation), source=sid, target=pid,
                                         kind="strong", label=rel.relation, system=system,
                                         reasons=[rel.detail] if rel.detail else []))
            return pid, created

    # -- edges ----------------------------------------------------------------------

    def _add_edge(self, edge: GraphEdge) -> None:
        if edge.source == edge.target:
            return
        self.edges.setdefault(edge.id, edge)

    def add_weak(self, a: str, b: str, *, score: float, label: str, reasons: list[str], source: str) -> None:
        with self._lock:
            a, b = sorted((self.canonical(a), self.canonical(b)))
            self._add_edge(GraphEdge(id=_edge_id("weak", a, b, source), source=a, target=b, kind="weak",
                                     label=label, score=round(score, 2), reasons=reasons, system=source))
            self.version += 1

    def add_direct_strong(self, a: str, b: str, *, label: str, reasons: list[str], system: str) -> None:
        with self._lock:
            a, b = sorted((self.canonical(a), self.canonical(b)))
            self._add_edge(GraphEdge(id=_edge_id("strong", a, b, label), source=a, target=b, kind="strong",
                                     label=label, reasons=reasons, system=system))
            self.version += 1

    def link_mention(self, sid: str, pid: str, *, label: str, reason: str) -> bool:
        """A case document (a FIR file the record node stands for) names a person of the
        graph by identifier: a stated link from that record to them, unless they are
        already joined to it."""
        with self._lock:
            pid = self.canonical(pid)
            if sid not in self.nodes or pid not in self.nodes:
                return False
            if any({e.source, e.target} == {sid, pid} for e in self.edges.values()):
                return False
            self._add_edge(GraphEdge(id=_edge_id("strong", sid, pid, label), source=sid, target=pid, kind="strong",
                                     label=label, reasons=[reason], system=self.nodes[sid].data.get("system")))
            self.version += 1
            return True

    def add_images(self, pid: str, image_ids: list[str], source: str) -> None:
        """Pictures of a person found in a document (CRO dossier poses)."""
        with self._lock:
            pid = self.canonical(pid)
            if pid not in self.nodes:
                return
            data = self.nodes[pid].data
            for image_id in image_ids:
                if image_id not in data["images"]:
                    data["images"].append(image_id)
                    data.setdefault("image_sources", {})[image_id] = source
            self.version += 1

    def connected_directly(self, a: str, b: str) -> bool:
        """Are ``a`` and ``b`` already joined by a strong path of length <= 2 (via one record)?"""
        a, b = self.canonical(a), self.canonical(b)
        neighbours: dict[str, set[str]] = {}
        for edge in self.edges.values():
            if edge.kind == "weak":
                continue
            neighbours.setdefault(edge.source, set()).add(edge.target)
            neighbours.setdefault(edge.target, set()).add(edge.source)
        near_a = neighbours.get(a, set())
        if b in near_a:
            return True
        return any(b in neighbours.get(n, set()) for n in near_a if n.startswith("s:"))

    def derive_shared_records(self) -> int:
        """Strong edges the systems imply but no single record states: two people whose
        own records list the same FIR, or the same vehicle."""
        added = 0
        with self._lock:
            people = self.persons()
            for x, y in itertools.combinations(people, 2):
                if self.connected_directly(x.id, y.id):
                    continue
                firs_x = {f["key"]: f for f in x.data["firs"]}
                shared = [firs_x[f["key"]] for f in y.data["firs"] if f["key"] in firs_x]
                roles_y = {f["key"]: f.get("role") for f in y.data["firs"]}
                for fir in shared[:3]:
                    if witness_role(fir.get("role")) and witness_role(roles_y.get(fir["key"])):
                        # Two witnesses of the same case are witnesses of a third person, not
                        # of each other: they only share the case - an inferred link.
                        self.add_weak(x.id, y.id, score=0.5, label=f"Both witnesses in FIR {fir['label']}",
                                      reasons=[f"Both are witnesses in FIR {fir['label']} at {fir['ps']}; "
                                               "neither is a witness about the other"], source="shared_fir")
                    else:
                        self.add_direct_strong(x.id, y.id, label=f"Same FIR {fir['label']}", system=fir["system"],
                                               reasons=[f"FIR {fir['label']} at {fir['ps']} appears in both people's {system_label(fir['system'])} records"])
                    added += 1
                for plate in sorted(set(x.data["vehicles"]) & set(y.data["vehicles"])):
                    self.add_direct_strong(x.id, y.id, label=f"Same vehicle {plate}", system="tracs",
                                           reasons=[f"Vehicle {plate} appears on challans of both"])
                    added += 1
        return added

    # -- output ---------------------------------------------------------------------

    def stats(self) -> dict[str, int]:
        with self._lock:
            people = self.persons()
            return {
                "persons": len(people),
                "searched": sum(1 for p in people if p.data["search_status"] == "searched"),
                "records": sum(1 for n in self.nodes.values() if n.kind == "system"),
                "strong_links": sum(1 for e in self.edges.values() if e.kind == "strong"),
                "weak_links": sum(1 for e in self.edges.values() if e.kind == "weak"),
                "flagged": sum(1 for p in people if p.data["flags"]),
            }

    def _charges(self) -> dict[str, str]:
        """FIR number -> the charges any record gave for it ("302 PPC; 34 PPC")."""
        out: dict[str, list[str]] = {}
        for node in self.nodes.values():
            if node.kind == "person":
                for fir in node.data.get("firs") or []:
                    if fir.get("offence"):
                        out.setdefault(str(fir["label"]).replace(" ", ""), []).append(str(fir["offence"]))
            elif node.data.get("system") == "fir_roster":
                fields = {f.get("label"): f.get("value") for f in node.data.get("fields") or []}
                label, sections = str(fields.get("FIR") or ""), fields.get("Sections")
                num = re.search(r"\d+\s*/\s*\d{2,4}", label)
                if num and sections:
                    out.setdefault(num.group(0).replace(" ", ""), []).append(str(sections))
        return {k: "; ".join(dict.fromkeys(v)) for k, v in out.items()}

    def _relation(self, edge: GraphEdge, names: dict[str, str], charges: dict[str, str]) -> dict | None:
        """The edge as a directed, readable statement between two people."""
        if edge.kind == "found_in":
            return None
        src = self.nodes.get(edge.source)
        if edge.kind == "weak":
            return weak_relation(edge.label, edge.source, edge.target, edge.score).to_dict(names)

        def roles(pid: str) -> dict[str, str]:
            person = self.nodes.get(pid)
            firs = person.data.get("firs") or [] if person is not None else []
            return {str(f.get("label", "")).replace(" ", ""): str(f.get("role") or "") for f in firs}

        if src is not None and src.kind == "system":
            owner = src.data.get("owner")
            if not owner or owner == edge.target:
                return None
            return describe(edge.label, named=edge.target, owner=owner, charges_for=charges,
                            owner_roles=roles(owner)).to_dict(names)
        return describe(edge.label, named=edge.target, owner=edge.source, charges_for=charges,
                        owner_roles=roles(edge.source)).to_dict(names)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            names = {n.id: n.label for n in self.nodes.values() if n.kind == "person"}
            charges = self._charges()
            edges = []
            for e in self.edges.values():
                item = e.model_dump(mode="json")
                relation = self._relation(e, names, charges)
                if relation:
                    item["relation"] = relation
                edges.append(item)
            return {
                "version": self.version,
                "nodes": [n.model_dump(mode="json") for n in self.nodes.values()],
                "edges": edges,
            }
