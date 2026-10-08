"""The AI investigator: works a question against one graph, step by step, out loud.

An officer asks "how is Bilal tied to Kamran?" or "who is the facilitator here?". A
single prompt over a summary of the graph answers from whatever fitted in it. The
investigator instead *queries* the graph: it picks a tool (routes between two people,
the scenario findings, a person's facts, the network's brokers…), reads the result,
and picks the next, until it can answer. Each step is streamed, so the officer watches
the reasoning happen and can judge it.

What the model may and may not do:

* Every fact comes from a tool, and every tool is deterministic code over the graph
  (``network.py``, ``scenarios.py``, ``dossier.py``). The model chooses and explains; it
  never produces a link.
* The final answer's people must exist in the graph - names it invents are dropped.
* It may *suggest* searching someone further (that costs ~20 logged live queries), but
  never expands the graph: the officer decides.

With a case file (``sherlocks.evidence``) it also reads the documents gathered - FIR
files, lab reports, CRO dossiers - and cites them (``D#`` documents, ``F#`` quoted facts,
``L#`` people found in documents). With ``agents`` it may fetch a document itself (a
FIR file, the lab reports of a FIR, a CRO dossier) or ask one police system about one
person, to check a lead - at most ``live_calls`` such calls per question, each logged
upstream like any query. What it fetches joins the case file.

With no model configured, the same tools run in a fixed order and the findings are
reported as they are - still useful, just not conversational.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import BaseModel, Field

from sherlocks.linkgraph.compare import compare_people
from sherlocks.linkgraph.dossier import person_facts
from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.linkgraph.normalize import name_key, sound_key, sound_shape
from sherlocks.linkgraph.scenarios import SCENARIOS, TIERS, find_scenarios
from sherlocks.linkgraph.systems import system_label

logger = logging.getLogger(__name__)

MAX_STEPS = 6
MAX_STEPS_WITH_EVIDENCE = 9
_LIVE_TOOLS = {"fetch_fir", "fetch_lab_reports", "fetch_cro", "lookup", "cdr_lookup", "api_router"}
# Which agent of the team a tool belongs to (shown with each step).
AGENT_OF = {"fetch_fir": "API agent", "fetch_lab_reports": "API agent", "fetch_cro": "API agent",
            "lookup": "API agent", "cdr_lookup": "API agent", "evidence": "Document agent",
            "read_document": "Document agent", "search_evidence": "Document agent", "incident": "Case board",
            "uploads": "CDR agent", "board": "Board reader", "dossier": "A4 Dossier agent", "chat_memory": "Chat memory",
            "cdr_query": "CDR query", "api_router": "API router"}
_RESULT_CHARS = 3500


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


class _Tools:
    def __init__(self, graph: dict[str, Any], case: Any = None, agents: Any = None, backend: Any = None,
                 live_calls: int = 0, *, officer: dict[str, Any] | None = None, team: str = "Officer team",
                 reason: str = "") -> None:
        self.graph = graph
        self.net = PersonNetwork(graph)
        self.findings = find_scenarios(graph, net=self.net)
        self.case = case
        self.agents = agents if agents is not None and getattr(agents, "enabled", False) else None
        self.backend = backend
        self.live_left = live_calls if (self.agents is not None or backend is not None) else 0
        # Who the calls are made for, by which team and why - for the guard and the audit log.
        self.officer = officer
        self.team = team
        self.reason = reason
        self.settings = getattr(agents, "settings", None)
        import threading

        self._spend_lock = threading.Lock()      # calls may run in parallel

    def _who(self, args: dict[str, str], key: str = "who") -> str:
        pid = self.net.resolve(args.get(key))
        if pid is None:
            raise ValueError(f"No person matching {args.get(key)!r} in this graph")
        return pid

    def findings_tool(self, args: dict[str, str]) -> dict:
        rows = self.findings
        if args.get("scenario"):
            rows = [f for f in rows if f["scenario"] == args["scenario"]]
        if args.get("who"):
            pid = self._who(args)
            rows = [f for f in rows if pid in f["people"]]
        return {"count": len(rows), "findings": [
            {"title": f["title"], "tier": f["tier"], "score": f["score"], "people": f["names"],
             "summary": f["summary"], "evidence": [e["text"] for e in f["evidence"][:3]],
             "sensitive": f["sensitive"]} for f in rows[:12]]}

    def person(self, args: dict[str, str]) -> dict:
        facts = person_facts(self.graph, self._who(args)) or {}
        facts.pop("lookups", None)
        facts.pop("osint", None)
        return facts

    def neighbours(self, args: dict[str, str]) -> dict:
        pid = self._who(args)
        return {"person": self.net.name(pid), "linked_to": self.net.neighbours(pid)[:25]}

    def paths(self, args: dict[str, str]) -> dict:
        a, b = self._who(args, "a"), self._who(args, "b")
        routes = self.net.paths(a, b, k=3)
        return {"from": self.net.name(a), "to": self.net.name(b), "routes": [
            {"steps": [h["relation"] + f" [{h['via']}{', inferred' if h['kind'] == 'weak' else ''}]" for h in r["hops"]],
             "inferred": r["inferred"]} for r in routes] or "not connected in this graph"}

    def compare(self, args: dict[str, str]) -> dict:
        a, b = self._who(args, "a"), self._who(args, "b")
        c = compare_people(self.graph, a, b) or {}
        return {k: c.get(k) for k in ("verdict", "strength", "direct", "shared_records", "mutual", "shared_details")}

    def network(self, _: dict[str, str]) -> dict:
        return {"communities": [{"size": c["size"], "core": c["core"], "members": c["names"][:10],
                                 "criminals": c["criminals"]} for c in self.net.communities()],
                "brokers": self.net.brokers(top=5), "key_people": self.net.key_people(top=8),
                "hubs": self.net.hubs()[:5]}

    def hidden_associates(self, args: dict[str, str]) -> dict:
        focus = self._who(args) if args.get("who") else None
        return {"pairs": self.net.hidden_associates(top=8, focus=focus)}

    def criminal_proximity(self, args: dict[str, str]) -> dict:
        prox = self.net.criminal_proximity(self._who(args)) or {}
        route = prox.pop("route", None)
        if route:
            prox["how"] = [h["relation"] for h in route["hops"]]
        return prox

    def leads(self, _: dict[str, str]) -> dict:
        return {"not_yet_searched": self.net.unsearched_leads(top=8)}

    def timeline(self, args: dict[str, str]) -> dict:
        pid = self._who(args)
        d = self.net.data(pid)
        events = [{"when": f.get("label", "").split("/")[-1], "what": f"FIR {f.get('label')} at {f.get('ps')} as {f.get('role')}",
                   "source": system_label(f.get("system") or "")} for f in d.get("firs") or []]
        events += [{"when": s.get("check_in"), "what": f"Stayed at {s.get('hotel')} ({s.get('district') or '-'})",
                    "source": "Hotel Eye"} for s in d.get("stays") or []]
        return {"person": self.net.name(pid), "events": sorted(events, key=lambda e: str(e["when"] or ""))}

    # -- the case file ---------------------------------------------------------------

    def _digest(self, doc_id: str, text_chars: int = 1500) -> dict:
        doc = self.case.document(doc_id) if self.case is not None else None
        if doc is None:
            raise ValueError(f"No document {doc_id!r}; use 'evidence' to list them")
        return {"id": doc["id"], "title": doc["title"], "source": doc["source"],
                "summary": doc.get("ai_summary") or doc.get("summary"),
                "facts": [{"id": f["id"], "fact": f["statement"], "quote": f["quote"][:220]} for f in doc["fact_rows"][:20]],
                "people_found": [{"id": link["id"], "name": link["name"], "how": link["how"]} for link in doc["link_rows"]],
                "people_named": doc.get("people", [])[:20], "pictures": len(doc.get("images") or []),
                "unread_pages": doc.get("unread_pages"), "text": (doc.get("text") or "")[:text_chars]}

    def evidence(self, args: dict[str, str]) -> dict:
        if self.case is None:
            return {"documents": [], "note": "No case file for this graph"}
        docs = list(self.case.documents.values())
        if args.get("who"):
            pid = self._who(args)
            mine = self.case.for_person(pid)
            ids = {d["id"] for d in mine["documents"]}
            docs = [d for d in docs if d["id"] in ids]
        if args.get("kind"):
            docs = [d for d in docs if d["kind"] == args["kind"]]
        facts = [f for f in self.case.facts.values() if f["doc"] in {d["id"] for d in docs}]
        return {"documents": [{"id": d["id"], "kind": d["kind"], "title": d["title"],
                               "summary": (d.get("ai_summary") or d.get("summary") or "")[:300]} for d in docs[:30]],
                "facts": [{"id": f["id"], "doc": f["doc"], "fact": f["statement"]} for f in facts[:30]],
                "people_found_in_documents": [{"id": link["id"], "doc": link["doc"], "name": link["name"], "how": link["how"]}
                                              for link in self.case.links.values()][:30],
                "not_available": [f"{a['kind']} {a['key']}: {a['message']}" for a in self.case.attempts
                                  if a["status"] != "hit"][-10:]}

    def read_document(self, args: dict[str, str]) -> dict:
        return self._digest(str(args.get("id") or args.get("doc") or "").strip().upper(), text_chars=3500)

    def search_evidence(self, args: dict[str, str]) -> dict:
        if self.case is None:
            return {"matches": []}
        return {"matches": self.case.search(args.get("text") or args.get("query") or "")}

    # -- live tools ---------------------------------------------------------------------

    def _spend(self, system: str, identifier: str | None = None) -> None:
        """One live call: within this question's budget, the case's limits, the officer's
        systems, and only for an identifier on the case. Logged either way."""
        from sherlocks.evidence import guard

        with self._spend_lock:
            self._spend_locked(system, identifier, guard)

    def _spend_locked(self, system: str, identifier: str | None, guard: Any) -> None:
        if self.live_left <= 0:
            raise ValueError("No live calls left for this question - answer from what is known, or suggest the call")
        try:
            guard.check_limits(self.case, self.settings)
            guard.check_system(system, self.officer)
            guard.check_identifier(identifier, self.graph, self.case)
        except guard.GuardError:
            guard.log_call(self.case, system=system, identifier=identifier, agent=self.team, reason=self.reason,
                           officer=self.officer, status="refused")
            raise
        self.live_left -= 1
        guard.log_call(self.case, system=system, identifier=identifier, agent=self.team, reason=self.reason,
                       officer=self.officer, status="called")

    def _fir_ref(self, args: dict[str, str]) -> tuple[str, str, str]:
        """FIR number/year and police-station id, from the args or the graph."""
        text = str(args.get("fir") or "")
        no, year = str(args.get("fir_no") or ""), str(args.get("fir_year") or "")
        if "/" in text:
            no, year = (x.strip() for x in text.split("/", 1))
        ps_id = str(args.get("ps_id") or "")
        if not (no and year):
            raise ValueError("Give fir as 'number/year', e.g. 604/2025")
        if not ps_id:
            want = f"{no.lstrip('0')}/{year[-2:]}"
            for pid in self.net.people:
                for f in self.net.data(pid).get("firs") or []:
                    label = str(f.get("label") or "")
                    if "/" in label and f.get("ps_id"):
                        n, y = label.split("/", 1)
                        if f"{n.lstrip('0')}/{y[-2:]}" == want:
                            ps_id = str(f["ps_id"])
                            break
                if ps_id:
                    break
        if not ps_id:
            raise ValueError(f"The police-station id of FIR {no}/{year} is not known; give ps_id")
        return no, year, ps_id

    def fetch_fir(self, args: dict[str, str]) -> dict:
        if self.agents is None:
            raise ValueError("Documents cannot be fetched here")
        no, year, ps_id = self._fir_ref(args)
        if self.case.doc_for(f"fir:{no.lstrip('0')}/{year[-2:]}/{ps_id}") is None:
            self._spend("psrms_file")
        found = self.agents.fetch_fir(no, year, ps_id)
        if "error" in found:
            return found
        return self._digest(found["document"])

    def fetch_lab_reports(self, args: dict[str, str]) -> dict:
        if self.agents is None:
            raise ValueError("Documents cannot be fetched here")
        no, year, ps_id = self._fir_ref(args)
        self._spend("labs")
        found = self.agents.fetch_lab_reports(no, year, ps_id)
        return {"documents": [self._digest(d, text_chars=1200) for d in found.get("documents") or []],
                "message": found.get("message")}

    def fetch_cro(self, args: dict[str, str]) -> dict:
        if self.agents is None:
            raise ValueError("Documents cannot be fetched here")
        cro_no = str(args.get("cro_no") or "").strip()
        pid = None
        if not cro_no and args.get("who"):
            pid = self._who(args)
            cro_no = next((f["value"] for n in self.graph.get("nodes") or []
                           if n.get("kind") == "system" and (n.get("data") or {}).get("owner") == pid
                           for f in (n.get("data") or {}).get("fields") or [] if str(f.get("label", "")).startswith("CRO No")), "")
        if not cro_no:
            raise ValueError("No CRO number known; give cro_no")
        self._spend("safe")
        found = self.agents.fetch_cro(cro_no, pid)
        return found if "error" in found else self._digest(found["document"])

    def lookup(self, args: dict[str, str]) -> dict:
        from sherlocks.linkgraph.extractors import extract_record
        from sherlocks.linkgraph.images import MemoryImageStore
        from sherlocks.linkgraph.models import PersonRef
        from sherlocks.linkgraph.normalize import cnic13, mobile11

        if self.backend is None:
            raise ValueError("Lookups are not available here")
        system = str(args.get("system") or "").strip().lower()
        cnic, phone = cnic13(args.get("cnic")), mobile11(args.get("phone"))
        if args.get("who") and not (cnic or phone):
            d = self.net.data(self._who(args))
            cnic, phone = d.get("cnic"), (d.get("phones") or [None])[0]
        if not (cnic or phone):
            raise ValueError("Give who, cnic or phone")
        self._spend(system, cnic or phone)
        payload = self.backend.lookup(system, cnic, phone)
        rec = extract_record(system, payload, PersonRef(cnic=cnic, phones=[phone] if phone else []), MemoryImageStore())
        return {"system": system_label(system), "status": payload.get("status"), "summary": payload.get("summary"),
                "subject": rec.subject.name, "fields": [f"{f.label}: {f.value}" for f in rec.fields[:15]],
                "names": [f"{r.ref.label()} - {r.relation}" for r in rec.related[:15]],
                "firs": [f"{f.fir_no}/{f.fir_year} {f.police_station} {f.role or ''}" for f in rec.firs[:10]],
                "organisations": rec.organisations[:6], "vehicles": rec.vehicles[:8],
                "designation": rec.subject.extra.get("designation"),
                "stays": [f"{st.hotel} {st.check_in or ''}" for st in rec.stays[:6]]}

    def registry(self) -> dict[str, tuple[str, Callable[[dict[str, str]], dict]]]:
        scenarios = ", ".join(SCENARIOS)
        tools = self._graph_tools(scenarios)
        if self.case is not None:
            tools.update({
                "evidence": ("Case documents read so far (FIR files, lab reports, CRO dossiers), their quoted facts "
                             "and the people found in them. args: who?, kind? (fir|lab|cro)", self.evidence),
                "read_document": ("One document in full: summary, facts with quotes, people, text. args: id (e.g. D2)",
                                  self.read_document),
                "search_evidence": ("Lines of any document containing these words (a number, name, IMEI, plate). "
                                    "args: text", self.search_evidence),
            })
        if self.agents is not None and self.live_left > 0:
            tools.update({
                "fetch_fir": ("LIVE: fetch and read the FIR file (complainant, accused, witnesses, case diaries). "
                              "args: fir ('604/2025'), ps_id?", self.fetch_fir),
                "fetch_lab_reports": ("LIVE: DNA / chemical / FSL / medico-legal reports filed for a FIR, read. "
                                      "args: fir, ps_id?", self.fetch_lab_reports),
                "fetch_cro": ("LIVE: a criminal's CRO dossier (particulars, poses, fingerprints), read. "
                              "args: cro_no or who", self.fetch_cro),
            })
        if self.backend is not None and self.live_left > 0:
            tools["lookup"] = ("LIVE: ask ONE police system about one person, to verify something. args: system "
                               "(nadra, cro, psrms, hotel_eye, prvs, subscriber, sbvs, dls, tracs, arms, watchlist, "
                               "cfms, evs, hope, hrmis, old_tenant, trust, milap, excise, avlc, igp_cms, pfc), "
                               "who | cnic | phone", self.lookup)
        if self._cdr_server() is not None and self.live_left > 0:
            tools["cdr_lookup"] = ("LIVE: the CDR server's lookups: provider 'imei' (args: imei) -> the phone and owner "
                                   "behind a device; 'nearest_ps' (args: lat, lon) -> nearest police station; or any "
                                   "police provider by cnic/phone. args: provider, imei?, lat?, lon?, cnic?, phone?",
                                   self.cdr_lookup)
        if self.case is not None:
            tools.update({
                "board": ("Board reader: what the case board holds on a question - facts with tier and source, the "
                          "officer's statements, Sherlock's hypotheses. args: query, who?", self.board),
                "dossier": ("Everything the case holds on one person in one card: identity, role, FIRs, links, documents, "
                            "statements, CDRs, open hypotheses. args: who", self.dossier),
                "chat_memory": ("What the officer said in this chat about something, with the turn. args: query",
                                self.chat_memory),
                "cdr_query": ("The CDR team's facts for a person or a number: calls, towers, distances, IMEIs, with row "
                              "references. args: who | number", self.cdr_query),
            })
        if self.backend is not None and self.live_left > 0:
            tools["api_router"] = ("LIVE: ask the police systems that hold a topic about one person - the catalog picks "
                                   "the systems (identity, work, vehicle, phone, address, family, record, complaint, "
                                   "hotel). args: who, topic", self.api_router)
        if self.case is not None:
            tools["incident"] = ("The incident as the officer gave it (place pinned on the map, date, time, FIR), the "
                                 "nearest police station, and what the uploaded CDRs show near it. args: none",
                                 self.incident)
            tools["uploads"] = ("Files the officer uploaded (CDRs, tower dumps, documents, photos): what each shows, "
                                "numbers on the graph, records near the incident. args: none", self.uploads)
        return tools

    # -- the CDR agent and the case board ------------------------------------------------

    def _cdr_server(self) -> Any:
        if self.agents is None or getattr(self.agents.source, "name", "") == "demo":
            return None
        from sherlocks.evidence.cdr import CdrServer

        server = CdrServer(getattr(self.agents.source, "http", None))
        return server if server.ready else None

    def cdr_lookup(self, args: dict[str, str]) -> dict:
        from sherlocks.linkgraph.normalize import cnic13, mobile11

        server = self._cdr_server()
        if server is None:
            raise ValueError("The CDR server is not configured")
        provider = str(args.get("provider") or "").strip().lower()
        subject = {k: v for k, v in {
            "imei": str(args.get("imei") or "").strip() or None, "cnic": cnic13(args.get("cnic")),
            "mobile": mobile11(args.get("phone")), "latitude": float(args["lat"]) if args.get("lat") else None,
            "longitude": float(args["lon"]) if args.get("lon") else None}.items() if v}
        if not (provider and subject):
            raise ValueError("Give provider and an identifier (imei, cnic, phone or lat+lon)")
        self._spend(f"cdr:{provider}", subject.get("cnic") or subject.get("mobile"))
        out = server.provider(provider, subject)
        return {"provider": provider, "status": out.get("status"), "summary": out.get("summary"),
                "data": _trim(out.get("data"))}

    def incident(self, _: dict[str, str]) -> dict:
        inc = dict(self.case.incident or {}) if self.case is not None else {}
        if not inc:
            return {"incident": None, "note": "The officer has not pinned the incident yet - ask for it."}
        lines = [line for d in self.case.documents.values() if d["kind"] == "cdr"
                 for line in d["text"].splitlines() if line.startswith(("Near the incident", "Incident day", "Towers within"))]
        return {"incident": inc, "cdr_near_incident": lines[:30]}

    def uploads(self, _: dict[str, str]) -> dict:
        docs = [d for d in (self.case.documents.values() if self.case is not None else []) if d["kind"] in ("upload", "cdr")]
        return {"uploads": [{"id": d["id"], "title": d["title"], "summary": d.get("ai_summary") or d.get("summary"),
                             "owner": [self.net.name(o) for o in d.get("owners") or []],
                             "key_lines": [line for line in d["text"].splitlines()
                                           if line.startswith(("Subscriber", "On the graph", "Near the incident",
                                                               "Top contact 1", "Top contact 2", "Top contact 3"))][:12]}
                            for d in docs]}

    # -- the shared toolbox --------------------------------------------------------------

    def board(self, args: dict[str, str]) -> dict:
        from sherlocks.linkgraph.knowledge import knowledge

        query = str(args.get("query") or args.get("text") or "")
        people = [self._who(args)] if args.get("who") else None
        kb = knowledge(self.graph, self.case, self.net)
        hits = kb.search(query, people=people, k=20)
        return {"entries": [e.as_dict() for e in hits],
                "missing": [] if hits else [f"the board holds nothing on: {query}"]}

    def dossier(self, args: dict[str, str]) -> dict:
        from sherlocks.evidence.dossiers import dossier

        if self.case is None:
            raise ValueError("No case board here")
        return dossier(self.case, self.net, self._who(args))

    def chat_memory(self, args: dict[str, str]) -> dict:
        words = [w for w in re.findall(r"[\w؀-ۿ]+", str(args.get("query") or "").lower()) if len(w) > 2]
        said = []
        for i, t in enumerate(self.case.conversation if self.case is not None else [], 1):
            q = str(t.get("q") or "")
            if not words or any(w in q.lower() for w in words):
                said.append({"turn": t.get("turn") or i, "officer": q[:400]})
        answered = [{"question": q["text"], "answer": q.get("answer"), "turn": q.get("answer_turn"), "status": q["status"]}
                    for q in (self.case.questions if self.case is not None else []) if q["status"] != "open"]
        return {"said": said[-12:], "answered_questions": answered[-12:]}

    def cdr_query(self, args: dict[str, str]) -> dict:
        from sherlocks.linkgraph.normalize import mobile11

        numbers: set[str] = set()
        if args.get("who"):
            numbers |= set(self.net.data(self._who(args)).get("phones") or [])
        if mobile11(args.get("number")):
            numbers.add(mobile11(args.get("number")))
        facts = [f for f in (self.case.live_facts() if self.case is not None else [])
                 if f.get("by") == "cdr" and (not numbers or any(n in f["statement"] for n in numbers))]
        return {"numbers": sorted(numbers), "facts": [{"id": f["id"], "doc": f["doc"], "fact": f["statement"],
                                                       "tier": f.get("tier"), "rows": f.get("rows")} for f in facts[:30]],
                "note": None if facts else "No CDR on the board covers this - ask the officer for one."}

    def api_router(self, args: dict[str, str]) -> dict:
        from sherlocks.linkgraph import api_router as router

        pid = self._who(args)
        topic = str(args.get("topic") or "").strip().lower()
        planned = router.plan(self.net, self.case, pid, [topic], allowed=(self.officer or {}).get("systems"))
        if planned["unheld"]:
            return {"topic": topic, "result": f"No connected system holds {topic} information."}
        out = []
        for call in planned["calls"]:
            if self.live_left <= 0:
                break
            res = self.lookup({"system": call["system"], "who": self.net.name(pid)})
            found = bool(res.get("fields") or res.get("firs") or res.get("vehicles") or res.get("stays")
                         or res.get("organisations"))
            router.remember(self.case, pid, call["system"], "found" if found else "nothing found")
            out.append({**res, "checked": call["system"], "found": found})
        return {"topic": topic, "results": out, "already_searched": planned["skipped"],
                "result": None if any(r["found"] for r in out) else "checked, nothing found"}

    def _graph_tools(self, scenarios: str) -> dict[str, tuple[str, Callable[[dict[str, str]], dict]]]:
        return {
            "findings": (f"Scenario findings (patterns found by rule). args: scenario? (one of: {scenarios}), who?",
                         self.findings_tool),
            "person": ("Everything known about one person, each fact with its source. args: who", self.person),
            "neighbours": ("Who one person is directly linked to, and how. args: who", self.neighbours),
            "paths": ("Up to 3 cheapest routes between two people, stated hops first. args: a, b", self.paths),
            "compare": ("Everything connecting two people: direct links, shared records, mutual contacts. args: a, b", self.compare),
            "network": ("Clusters, brokers (connectors), key people and hubs of the whole graph. args: none", self.network),
            "hidden_associates": ("Pairs with no direct link but several shared contacts. args: who?", self.hidden_associates),
            "criminal_proximity": ("Nearest person with a criminal footprint, and the route. args: who", self.criminal_proximity),
            "leads": ("People drawn but not searched, ranked by what searching them could add. args: none", self.leads),
            "timeline": ("One person's dated events (FIRs, hotel stays). args: who", self.timeline),
        }


# --------------------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------------------


ToolName = Literal["findings", "person", "neighbours", "paths", "compare", "network", "hidden_associates",
                   "criminal_proximity", "leads", "timeline", "evidence", "read_document", "search_evidence",
                   "fetch_fir", "fetch_lab_reports", "fetch_cro", "lookup", "cdr_lookup", "incident", "uploads",
                   "board", "dossier", "chat_memory", "cdr_query", "api_router", "final"]


class _Step(BaseModel):
    thought: str = Field(description="One short sentence: what you need to learn next, and why.")
    action: ToolName = Field(description="The tool to call, or 'final' when you can answer.")
    args: dict[str, str] = Field(default_factory=dict, description="Tool arguments; people by name or id.")


class _Hypothesis(BaseModel):
    statement: str = Field(description="One sentence, naming people as they appear in the graph.")
    tier: Literal["stated", "corroborated", "inferred", "speculative"]
    people: list[str] = Field(default_factory=list, description="Names of the people involved, exactly as in the graph.")
    evidence: list[str] = Field(default_factory=list, description="Facts from the tool results, each with its source "
                                                                  "system or document/fact id ([CRO], [F3], [D1]).")


class _Final(BaseModel):
    answer: str = Field(description="Direct answer to the officer's question, 2-8 sentences, citing sources in [brackets]: "
                                    "a system ([PSRMS]) or a case-file id ([D2], [F7], [L1]).")
    confident: bool = Field(default=True, description="False if the graph does not settle the question.")
    hypotheses: list[_Hypothesis] = Field(default_factory=list, description="Up to 5, strongest first.")
    next_steps: list[str] = Field(default_factory=list, description="Up to 4 concrete actions for the officer.")


_SYSTEM = (
    "You are an investigator's assistant working on ONE link graph built from Sindh Police and "
    "government records. You cannot see the graph; you query it with tools and read the results. "
    "Rules: every fact you use must come from a tool result; never invent a person, FIR, number or "
    "relationship; stated links are facts from a system, inferred links are leads; say which is which. "
    "Prefer 'paths' or 'compare' for 'how is A linked to B', 'findings' and 'network' for 'what is going "
    "on here', 'person' for one individual. Do not repeat a call you already made. Stop with action "
    "'final' as soon as the results answer the question. Findings marked sensitive concern police "
    "officers: report them neutrally. You are Sherlock: think like a detective - look for who connects "
    "the targets, who keeps reappearing (as witness, guarantor, co-accused, hotel companion, SIM owner), "
    "and what the documents (FIR files, case diaries, lab reports, CRO dossiers) say about them. When a "
    "case file exists, read the relevant documents ('evidence', 'read_document', 'search_evidence') and "
    "cite their ids. LIVE tools query police systems (logged upstream): use them only when the answer "
    "needs a document or check that is not in the case file yet. Use 'incident' and 'uploads' for where and when "
    "it happened and what the officer's CDRs, tower dumps and files show; 'cdr_lookup' resolves an IMEI or the "
    "police station nearest a point. The officer's own statements are in the case file as 'Officer's statements' - "
    "treat them as the officer's account, to be checked against the records. " + DATA_NOT_INSTRUCTIONS
)

_FINAL_SYSTEM = (
    "Write the investigator's conclusion from the tool results only. Cite the source system of each "
    "fact in [brackets]. Tier each hypothesis honestly: 'stated' only when a system states it, "
    "'corroborated' when two systems agree, 'inferred' for patterns, 'speculative' for name or OSINT "
    "matches. Cite case documents and quoted facts by their ids ([D2], [F7]); quote a document only through "
    "its fact ids. If the results do not answer the question, say so and set confident to false. Next "
    "steps may suggest searching a person further (it costs live queries) - never claim results of a "
    "search that was not done."
)


def _trim(value: Any, limit: int = 2500) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= limit else text[:limit] + " …"


def _clip(value: Any, limit: int = _RESULT_CHARS) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + " …(truncated)"


def _args_text(net: PersonNetwork, args: dict[str, str]) -> str:
    """Tool arguments as the officer reads them: person ids become names."""
    return " · ".join(net.name(v) if v in net.people else str(v) for v in args.values())


def _summary(tool: str, result: dict) -> str:
    """One line for the portal's step list."""
    if "error" in result:
        return f"error: {result['error']}"
    if tool == "findings":
        return f"{result['count']} finding(s)" + (f": {result['findings'][0]['title']}…" if result["findings"] else "")
    if tool == "paths":
        routes = result["routes"]
        return routes if isinstance(routes, str) else f"{len(routes)} route(s); best: " + " → ".join(routes[0]["steps"])[:220]
    if tool == "neighbours":
        return f"{len(result['linked_to'])} direct link(s)"
    if tool == "network":
        return f"{len(result['communities'])} cluster(s), top broker: " + (result["brokers"][0]["name"] if result["brokers"] else "none")
    if tool == "criminal_proximity":
        return (f"nearest: {result.get('nearest_name')} in {result.get('hops')} hop(s)"
                if result.get("nearest") else "no criminal footprint reachable")
    if tool == "leads":
        return f"{len(result['not_yet_searched'])} person(s) worth searching"
    if tool == "compare":
        return str(result.get("verdict") or "")[:220]
    if tool == "person":
        return f"{result.get('name')}: {len(result.get('strong_links') or [])} stated, {len(result.get('weak_links') or [])} inferred link(s)"
    if tool == "hidden_associates":
        return f"{len(result['pairs'])} pair(s)"
    if tool == "timeline":
        return f"{len(result['events'])} dated event(s)"
    if tool == "evidence":
        return f"{len(result['documents'])} document(s), {len(result['facts'])} fact(s)"
    if tool in ("read_document", "fetch_fir", "fetch_cro") and result.get("id"):
        return f"[{result['id']}] {result['title']}: {len(result.get('facts') or [])} fact(s)"
    if tool == "fetch_lab_reports":
        return f"{len(result['documents'])} lab report(s)" + (f" - {result['message']}" if result.get("message") and not result["documents"] else "")
    if tool == "search_evidence":
        return f"{len(result['matches'])} matching line(s)"
    if tool == "lookup":
        return f"{result.get('system')}: {result.get('summary')}"
    if tool == "cdr_lookup":
        return f"{result.get('provider')}: {result.get('summary')}"
    if tool == "incident":
        inc = result.get("incident") or {}
        return (f"{inc.get('place') or 'pinned point'} {inc.get('date') or ''} · {len(result.get('cdr_near_incident') or [])} CDR line(s) near it"
                if inc else "not pinned yet")
    if tool == "uploads":
        return f"{len(result['uploads'])} uploaded file(s)"
    return ""


def investigate(graph: dict[str, Any], question: str, llm: Any = None, *,
                history: list[dict] | None = None, max_steps: int | None = None, case: Any = None,
                agents: Any = None, backend: Any = None, live_calls: int = 0,
                status: bool = False, officer: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
    """Work ``question`` against ``graph``. Yields events:

    ``{"type": "start", ...}``, then ``{"type": "step", tool, args, thought, summary, result}``
    per tool call, then ``{"type": "final", answer, hypotheses, key_people, next_steps,
    suggestions, findings, model}``. With ``status``, a ``{"type": "status", key | tool, on}``
    comes before each planning call and each tool call, for the chat's waiting line.
    """
    question = (question or "").strip() or "What are the most important links and patterns in this graph?"
    tools = _Tools(graph, case=case, agents=agents, backend=backend, live_calls=live_calls, officer=officer,
                   team="S1 Sherlock (in chat)", reason=f"officer asked: {question[:120]}")
    registry = tools.registry()
    net = tools.net
    seeds = [net.name(s) for s in net.seeds()]
    if max_steps is None:
        max_steps = MAX_STEPS_WITH_EVIDENCE if case is not None else MAX_STEPS
    yield {"type": "start", "question": question, "people": len(net.people), "targets": seeds,
           "findings": len(tools.findings), "model": getattr(llm, "model", None) if llm else None,
           "documents": len(case.documents) if case is not None else 0, "live_calls": tools.live_left}

    def run(tool: str, args: dict[str, str]) -> dict:
        if tool not in registry:
            return {"error": f"Unknown or unavailable tool {tool!r}"}
        try:
            return registry[tool][1](args)
        except Exception as exc:  # noqa: BLE001 - a bad argument is the model's to correct
            return {"error": str(exc)}

    transcript: list[dict[str, Any]] = []
    if llm is None:
        named = _mentioned(net, question)
        if len(named) >= 2:
            a, b = named[:2]
            plan = [("paths", {"a": a, "b": b}), ("compare", {"a": a, "b": b}), ("findings", {"who": a})]
        elif named:
            plan = [("person", {"who": named[0]}), ("timeline", {"who": named[0]}),
                    ("criminal_proximity", {"who": named[0]}), ("findings", {"who": named[0]})]
        else:
            plan = [("findings", {}), ("network", {}), ("leads", {})]
        if case is not None and case.documents:
            plan.append(("evidence", {"who": named[0]} if named else {}))
        for tool, args in plan:
            if status:
                yield {"type": "status", "tool": tool, "on": _args_text(net, args)}
            result = run(tool, args)
            transcript.append({"tool": tool, "args": args, "result": result})
            yield {"type": "step", "tool": tool, "agent": AGENT_OF.get(tool, "Sherlock"), "args": args,
                   "args_text": _args_text(net, args),
                   "thought": "Rule-based review (no AI model configured).",
                   "summary": _summary(tool, result), "result": result}
        yield _cite(tools, _rule_final(tools, question, transcript))
        return

    convo = "".join(f"\nOfficer: {t.get('q', '')}\nYou: {t.get('a', '')}" for t in (history or [])[-3:])
    overview = {
        "people": len(net.people), "targets": seeds,
        "top_findings": [f"{f['title']} ({f['tier']}): {f['summary']}" for f in tools.findings[:8]],
        "tools": {name: desc for name, (desc, _) in registry.items()},
    }
    if case is not None:
        overview["case_file"] = [f"{d['id']} {d['title']}" for d in list(case.documents.values())[:25]]
        overview["live_calls_left"] = tools.live_left
    seen: set[str] = set()
    for index in range(max_steps):
        done = "\n".join(f"[{i + 1}] {t['tool']}({json.dumps(t['args'], ensure_ascii=False)}) -> {_clip(t['result'])}"
                         for i, t in enumerate(transcript))
        prompt = (f"Graph overview: {_clip(overview, 6000)}"
                  + (f"\n\nEarlier conversation:{convo}" if convo else "")
                  + f"\n\nOfficer's question: {question}"
                  + (f"\n\nTool calls so far:\n{done}" if done else "")
                  + "\n\nChoose the next step as JSON.")
        if status:
            yield {"type": "status", "key": "think"}
        try:
            step, _ = llm.generate_structured(prompt=prompt, schema=_Step, system=_SYSTEM,
                                              cache_kind="investigate_step", prompt_version="v1")
        except Exception as exc:  # noqa: BLE001
            logger.info("Investigator step failed: %s", exc)
            yield {"type": "step", "tool": "error", "args": {}, "thought": "The model could not plan a step.",
                   "summary": f"{type(exc).__name__}: {exc}", "result": {}}
            break
        if step.action == "final":
            break
        key = f"{step.action}:{json.dumps(step.args, sort_keys=True)}"
        if key in seen:
            break  # going round in circles: answer from what it has
        seen.add(key)
        if status:
            yield {"type": "status", "tool": step.action, "on": _args_text(net, step.args)}
        result = run(step.action, step.args)
        transcript.append({"tool": step.action, "args": step.args, "result": result})
        yield {"type": "step", "index": index + 1, "tool": step.action, "agent": AGENT_OF.get(step.action, "Sherlock"),
               "args": step.args,
               "args_text": _args_text(net, step.args), "thought": step.thought,
               "summary": _summary(step.action, result), "result": result,
               "live": step.action in _LIVE_TOOLS}

    yield _cite(tools, _llm_final(tools, question, transcript, llm))


def _cite(tools: _Tools, final: dict[str, Any]) -> dict[str, Any]:
    """Resolve the case-file ids an answer cites, so the portal can open them."""
    if tools.case is None:
        final["citations"] = []
        return final
    text = " ".join([final.get("answer") or ""] + [e for h in final.get("hypotheses") or [] for e in h.get("evidence") or []])
    final["citations"] = tools.case.citations_in(text)
    return final


def _suggestions(tools: _Tools, people: list[str]) -> list[dict]:
    """Unsearched people worth the next queries: the ones the answer names first."""
    leads = tools.net.unsearched_leads(top=10)
    named = [lead for lead in leads if lead["id"] in people]
    rest = [lead for lead in leads if lead["id"] not in people]
    return (named + rest)[:5]


# Ranks and titles are not names: "sub inspector fahim" is Fahim, not every sub inspector.
_TITLES = {"sub", "inspector", "si", "asi", "ssi", "pc", "hc", "constable", "head", "sho", "dsp", "sp", "ssp", "io",
           "lady", "officer", "sip", "انسپکٹر", "سب", "اے", "ایس", "آئی", "کانسٹیبل", "ہیڈ", "پی", "سی"}
# Words a question is made of, whatever the spelling ("ktne", "kitni", "batou"): their sound
# never names anyone ("ktne" sounds like "khatoon").
_FUNCTION_WORDS = ("kaun", "kon", "kahan", "kab", "kyun", "kaise", "kitna", "kitne", "kitni", "ktne", "ktni", "fir",
                   "firs", "batao", "batou", "btao", "bataen", "abhi", "jari", "upar", "uper", "wala", "wali", "wale",
                   "hai", "hain", "tha", "thi", "kar", "karo", "raha", "rahi", "rahe", "konsa", "konsi", "kis", "kin",
                   "knc", "kns", "knsi", "kaunsi", "kaunsa", "lagta", "lagti", "lagte", "lagt", "sangeen", "khatarnak")


def _function_keys() -> dict[str, list[str]]:
    from sherlocks.linkgraph.knowledge import _CONCEPT_OF, _STOP

    out: dict[str, list[str]] = {}
    for w in (*_FUNCTION_WORDS, *_STOP, *_CONCEPT_OF, *_TITLES):
        if len(k := sound_key(w)) >= 2:
            out.setdefault(k, []).append(w)
    return out


def _is_function_word(word: str, keys: dict[str, list[str]]) -> bool:
    """"ktne" is "kitne" misspelt; "ahmed" only sounds like "muddai" - spelling decides."""
    w = word.lower()
    return any(SequenceMatcher(None, w, f).ratio() >= 0.7 for f in keys.get(sound_key(w), []))


_NOT_A_NAME = re.compile(r"cnic|phone|number|unknown|نامعلوم", re.IGNORECASE)


def _name_words(label: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z]+|[\u0600-\u06FF]+", _NOT_A_NAME.sub(" ", label or ""))
            if w.lower() not in _TITLES and w.lower() not in ("s", "o", "d", "w")]


def _full_names(net: PersonNetwork, question: str, hits: list[tuple[int, str]]) -> set[str]:
    """Add people whose whole name (or its first 2+ words) is said, by sound; returns the
    question's words used (name-key form)."""
    fk = _function_keys()
    words = re.findall(r"[A-Za-z]+|[\u0600-\u06FF]+", question)
    qk = [None if (w.lower() in _FUNCTION_WORDS or w.lower() in _TITLES or _is_function_word(w, fk))
          else sound_key(w) or None for w in words]
    taken = {p for _, p in hits}
    found: dict[tuple[int, int], list[tuple[bool, str]]] = {}
    for pid, node in net.people.items():
        if pid in taken:
            continue
        nk = [k for k in (sound_key(w) for w in _name_words(node["label"])) if k]
        if not nk or (len(nk) < 2 and len(nk[0]) < 5):
            continue
        prefixes = {"".join(nk[:m]): m == len(nk) for m in range(min(2, len(nk)), len(nk) + 1)}
        for i in range(len(qk)):
            for n in (4, 3, 2):
                window = qk[i:i + n]
                if len(window) < n or None in window:
                    continue
                joined = "".join(window)       # type: ignore[arg-type]
                if len(joined) >= 5 and joined in prefixes:
                    found.setdefault((i, n), []).append((prefixes[joined], pid))
    covered: set[str] = set()
    used: set[int] = set()
    for (i, n), cands in sorted(found.items(), key=lambda x: (-x[0][1], x[0][0])):
        if used & set(range(i, i + n)):
            continue
        others = {p for _, p in hits}

        def rank(c: tuple[bool, str], others: set[str] = others) -> tuple:
            whole, p = c
            near = p in net.G and any(o in net.G and net.G.has_edge(p, o) for o in others)
            return (whole, near, len(net.data(p).get("records") or []), len(net.data(p).get("firs") or []))

        best = max(cands, key=rank)[1]
        hits.append((5_000 + i, best))
        used |= set(range(i, i + n))
        covered |= {name_key(w) for w in words[i:i + n]}
    return covered


def _mentioned(net: PersonNetwork, question: str, *, sound: bool = True) -> list[str]:
    """People the question names - full name, or a first name only one person has -
    in the order they appear. ``sound=False``: only names as written, not sound-alikes."""
    q = " " + " ".join(t for t in name_key(question).split() if t not in _TITLES) + " "
    hits: list[tuple[int, str]] = []
    firsts: dict[str, list[str]] = {}
    for pid, node in net.people.items():
        key = " ".join(t for t in name_key(node["label"]).split() if t not in _TITLES)
        if key and f" {key} " in q:
            hits.append((q.index(f" {key} "), pid))
        elif key:
            firsts.setdefault(key.split()[0], []).append(pid)
    # A whole name by sound, across scripts and spacing: "gul zaman khan" = گلزمان خان,
    # "imran ali" = عمران علی. Its words then name nobody else ("zaman" is not Qaiser Zaman).
    covered = _full_names(net, question, hits)
    if covered:
        q = " " + " ".join(t for t in q.split() if t not in covered) + " "
    for first, pids in firsts.items():
        if len(pids) == 1 and f" {first} " in q and pids[0] not in {p for _, p in hits}:
            hits.append((q.index(f" {first} "), pids[0]))
    # A distinctive word of a name ("kamran" for Kamran Ahmed): common name words
    # (muhammad, ali, khan...) never decide; among several holders, the richest record wins.
    from sherlocks.linkgraph.rarity import COMMON_NAME_TOKENS

    taken = {p for _, p in hits}
    holders: dict[str, list[str]] = {}
    for pid, node in net.people.items():
        for tok in set(name_key(node["label"]).split()):
            if len(tok) >= 3 and tok not in COMMON_NAME_TOKENS and tok not in _TITLES:
                holders.setdefault(tok, []).append(pid)
    for tok, pids in holders.items():
        if f" {tok} " in q and not set(pids) & taken:
            best = max(pids, key=lambda p: (bool(net.data(p).get("seed")), len(net.data(p).get("records") or []),
                                            len(net.data(p).get("firs") or [])))
            hits.append((q.index(f" {tok} "), best))
            taken.add(best)
    if not sound:
        return [pid for _, pid in sorted(hits)]
    # Words of the names already found are not searched again by sound.
    named = {t for _, p in hits for t in name_key(net.name(p)).split()}
    # By sound, across scripts: "afzal" in a Roman Urdu question finds افضل on the graph.
    function_keys = _function_keys()
    words = [w for w in re.findall(r"[A-Za-z]+|[\u0600-\u06FF]+", question)
             if w.lower() not in _FUNCTION_WORDS and w.lower() not in _TITLES and not _is_function_word(w, function_keys)
             and name_key(w) not in named and name_key(w) not in covered]
    qkeys = [sound_key(w) for w in words]
    taken = {p for _, p in hits}
    by_word: dict[int, list[str]] = {}
    full: set[str] = set()
    for pid, node in net.people.items():
        if pid in taken:
            continue
        keys = [k for k in (sound_key(w) for w in _name_words(node["label"])) if k]
        if not keys:
            continue
        # The whole name in a row, however short its words ("imran ali" = عمران علی).
        if 2 <= len(keys) <= len(qkeys) and sum(map(len, keys)) >= 3:
            at = next((i for i in range(len(qkeys) - len(keys) + 1) if qkeys[i:i + len(keys)] == keys), None)
            if at is not None:
                by_word.setdefault(at, []).append(pid)
                full.add(pid)
                continue
        for i, k in enumerate(qkeys):
            # A distinctive word of the name (3+ consonants: Altaf, Kamran), or two words in a row.
            if (len(k) >= 3 and k in keys) or any(
                    len(k) >= 2 and k == keys[j] and i + 1 < len(qkeys) and qkeys[i + 1] == keys[j + 1]
                    and max(len(k), len(keys[j + 1])) >= 3
                    for j in range(len(keys) - 1)):
                by_word.setdefault(i, []).append(pid)
                break
    # One person per word: "afzal" sounds like افضل and فضل - the same spelling first, else the
    # richest record; the others are alternatives, not more people asked about.
    for i, pids in by_word.items():
        word = words[i].lower()
        shape = sound_shape(word)

        def closeness(p: str, shape: str = shape, key: str = qkeys[i]) -> float:
            return max((SequenceMatcher(None, shape, sound_shape(t)).ratio()
                        for t in re.findall(r"[A-Za-z]+|[\u0600-\u06FF]+", net.name(p)) if sound_key(t) == key),
                       default=0.0)

        # A person linked to someone else in the question ("imran ali kamran ka kya lagta hai")
        # is the one meant among namesakes.
        others = {p for _, p in hits}

        def near(p: str, others: set[str] = others) -> bool:
            return p in net.G and any(o in net.G and net.G.has_edge(p, o) for o in others)

        best = max(pids, key=lambda p: (p in full, near(p), word in name_key(net.name(p)).split(), closeness(p),
                                        bool(net.data(p).get("seed")), len(net.data(p).get("records") or [])))
        hits.append((10_000 + i, best))
    return [pid for _, pid in sorted(hits)]


def _person_answer(tools: _Tools, pid: str) -> str:
    """Everything the records say about one person, cases first - the rule answer to
    "which cases is X in", "who is X", "tell me about X"."""
    net = tools.net
    d = net.data(pid)
    ids = ", ".join(x for x in (f"CNIC {d['cnic']}" if d.get("cnic") else "",
                                f"phone {', '.join(d['phones'][:2])}" if d.get("phones") else "") if x)
    lines = [f"{net.name(pid)}" + (f" ({ids})" if ids else "") + ":"]
    firs = d.get("firs") or []
    if firs:
        seen: set[str] = set()
        lines.append(f"Cases (FIRs) - {len({f.get('label') for f in firs})}:")
        for f in firs:
            key = f"{f.get('label')}|{f.get('role')}"
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"• FIR {f.get('label')} at {f.get('ps') or '-'} - {f.get('role') or 'named'}"
                         + (f" - {f['offence']}" if f.get("offence") else "") + f" [{system_label(f.get('system') or '')}]")
    else:
        lines.append("No FIR is recorded against or naming this person in the systems searched.")
    flags = [x for x in d.get("flags") or [] if x]
    if flags:
        lines.append("Flags: " + ", ".join(x.replace("_", " ") for x in flags) + ".")
    for s in (d.get("stays") or [])[:4]:
        lines.append(f"• Hotel stay: {s.get('hotel')} ({s.get('district') or '-'}) {s.get('check_in') or ''} [Hotel Eye]")
    links = net.neighbours(pid)[:6] if pid in net.G else []
    if links:
        lines.append("Linked to:")
        lines += [f"• {x['relation']}{'' if x['stated'] else ' (inferred)'}" + (f" [{x['via']}]" if x["via"] else "")
                  for x in links]
    if tools.case is not None:
        docs = tools.case.for_person(pid)["documents"]
        if docs:
            lines.append("Case documents: " + "; ".join(f"[{x['id']}] {x['title']}" for x in docs[:6]))
    found_in = sorted({system_label(r.get("system") or "") for r in d.get("records") or []})
    if found_in:
        lines.append("Found in: " + ", ".join(found_in) + ".")
    return "\n".join(lines)


def _rule_final(tools: _Tools, question: str, transcript: list[dict] | None = None,
                focus: list[str] | None = None) -> dict[str, Any]:
    routes = next((t["result"].get("routes") for t in transcript or [] if t["tool"] == "paths"), None)
    if isinstance(routes, list) and routes:
        best = routes[0]
        answer = (f"{'Inferred' if best['inferred'] else 'Stated'} route, {len(best['steps'])} step(s):\n"
                  + "\n".join(f"{i + 1}. {step}" for i, step in enumerate(best["steps"])))
        if len(routes) > 1:
            answer += f"\n({len(routes) - 1} more route(s) found.)"
        people = [pid for pid in (tools.net.resolve(n) for t in transcript if t["tool"] == "paths"
                                  for n in (t["args"]["a"], t["args"]["b"])) if pid]
        out = _rule_final(tools, question, focus=people)
        out["answer"] = answer + "\n\n" + out["answer"]
        out["key_people"] = [{"id": p, "name": tools.net.name(p)} for p in people] + out["key_people"]
        return out
    asked = [t["args"].get("who") for t in transcript or [] if t["tool"] in ("person", "timeline")]
    if asked and not focus:
        pid = tools.net.resolve(asked[0])
        if pid:
            out = _rule_final(tools, question, focus=[pid])
            out["answer"] = _person_answer(tools, pid) + (
                "\n\nPatterns involving them:\n" + out["answer"] if any(pid in f["people"] for f in tools.findings) else "")
            out["key_people"] = [{"id": pid, "name": tools.net.name(pid)}] + [k for k in out["key_people"] if k["id"] != pid]
            out["confident"] = True
            return out
    findings = [f for f in tools.findings if not focus or set(focus) & set(f["people"])]
    by_tier = {tier: [f for f in findings if f["tier"] == tier] for tier in TIERS}
    lines = [f"{len(findings)} pattern(s) found by rule: "
             + ", ".join(f"{len(v)} {k}" for k, v in by_tier.items() if v) + "." if findings
             else "No linkage patterns were found in this graph."]
    lines += [f"• {f['title']}: {f['summary']}" for f in findings[:6]]
    people = list(dict.fromkeys(p for f in findings[:6] for p in f["people"]))
    return {"type": "final", "answer": "\n".join(lines), "confident": False,
            "hypotheses": [{"statement": f["summary"], "tier": f["tier"], "people": f["names"],
                            "evidence": [e["text"] for e in f["evidence"][:3]]} for f in findings[:5]],
            "key_people": [{"id": p, "name": tools.net.name(p)} for p in people[:8]],
            "next_steps": ["Configure the AI model for a conversational investigation of your question."],
            "suggestions": _suggestions(tools, people), "findings": findings[:12], "model": None}


def _llm_final(tools: _Tools, question: str, transcript: list[dict], llm: Any) -> dict[str, Any]:
    results = "\n".join(f"[{i + 1}] {t['tool']}({json.dumps(t['args'], ensure_ascii=False)}) -> {_clip(t['result'], 5000)}"
                        for i, t in enumerate(transcript)) or "(no tool calls)"
    top = [f"{f['title']} ({f['tier']}): {f['summary']}" for f in tools.findings[:8]]
    try:
        final, _ = llm.generate_structured(
            prompt=(f"Officer's question: {question}\n\nRule findings: {_clip(top, 4000)}\n\n"
                    f"Tool results:\n{results}\n\nWrite the conclusion as JSON."),
            schema=_Final, system=_FINAL_SYSTEM, cache_kind="investigate_final", prompt_version="v1")
    except Exception as exc:  # noqa: BLE001 - fall back to the rules rather than fail
        logger.info("Investigator conclusion failed: %s", exc)
        out = _rule_final(tools, question)
        out["answer"] = f"The model could not write a conclusion ({type(exc).__name__}); rule findings:\n" + out["answer"]
        return out
    # Keep only people who exist: a hypothesis about someone not in the graph is dropped.
    hypotheses, people = [], []
    for h in final.hypotheses[:5]:
        ids = [pid for pid in (tools.net.resolve(n) for n in h.people) if pid]
        if h.people and not ids:
            continue
        people += ids
        hypotheses.append({"statement": h.statement, "tier": h.tier, "people": [tools.net.name(p) for p in ids],
                           "people_ids": ids, "evidence": h.evidence[:4]})
    people = list(dict.fromkeys(people))
    return {"type": "final", "answer": final.answer.strip(), "confident": final.confident,
            "hypotheses": hypotheses, "key_people": [{"id": p, "name": tools.net.name(p)} for p in people[:8]],
            "next_steps": final.next_steps[:4], "suggestions": _suggestions(tools, people),
            "findings": tools.findings[:12], "model": getattr(llm, "model", None) or "llm"}
