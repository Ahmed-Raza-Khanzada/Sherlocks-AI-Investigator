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
from collections.abc import Callable, Iterator
from typing import Any, Literal

from pydantic import BaseModel, Field

from sherlocks.linkgraph.compare import compare_people
from sherlocks.linkgraph.dossier import person_facts
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.linkgraph.normalize import name_key
from sherlocks.linkgraph.scenarios import SCENARIOS, TIERS, find_scenarios
from sherlocks.linkgraph.systems import system_label

logger = logging.getLogger(__name__)

MAX_STEPS = 6
MAX_STEPS_WITH_EVIDENCE = 9
_LIVE_TOOLS = {"fetch_fir", "fetch_lab_reports", "fetch_cro", "lookup"}
_RESULT_CHARS = 3500


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


class _Tools:
    def __init__(self, graph: dict[str, Any], case: Any = None, agents: Any = None, backend: Any = None,
                 live_calls: int = 0) -> None:
        self.graph = graph
        self.net = PersonNetwork(graph)
        self.findings = find_scenarios(graph, net=self.net)
        self.case = case
        self.agents = agents if agents is not None and getattr(agents, "enabled", False) else None
        self.backend = backend
        self.live_left = live_calls if (self.agents is not None or backend is not None) else 0

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

    def _spend(self) -> None:
        if self.live_left <= 0:
            raise ValueError("No live calls left for this question - answer from what is known, or suggest the call")
        self.live_left -= 1

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
            self._spend()
        found = self.agents.fetch_fir(no, year, ps_id)
        if "error" in found:
            return found
        return self._digest(found["document"])

    def fetch_lab_reports(self, args: dict[str, str]) -> dict:
        if self.agents is None:
            raise ValueError("Documents cannot be fetched here")
        no, year, ps_id = self._fir_ref(args)
        self._spend()
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
        self._spend()
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
        self._spend()
        payload = self.backend.lookup(system, cnic, phone)
        rec = extract_record(system, payload, PersonRef(cnic=cnic, phones=[phone] if phone else []), MemoryImageStore())
        return {"system": system_label(system), "status": payload.get("status"), "summary": payload.get("summary"),
                "subject": rec.subject.name, "fields": [f"{f.label}: {f.value}" for f in rec.fields[:15]],
                "names": [f"{r.ref.label()} - {r.relation}" for r in rec.related[:15]],
                "firs": [f"{f.fir_no}/{f.fir_year} {f.police_station} {f.role or ''}" for f in rec.firs[:10]]}

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
                               "(nadra, cro, psrms, hotel_eye, prvs, subscriber, dls, ...), who | cnic | phone", self.lookup)
        return tools

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
                   "fetch_fir", "fetch_lab_reports", "fetch_cro", "lookup", "final"]


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
    "needs a document or check that is not in the case file yet."
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
    return ""


def investigate(graph: dict[str, Any], question: str, llm: Any = None, *,
                history: list[dict] | None = None, max_steps: int | None = None, case: Any = None,
                agents: Any = None, backend: Any = None, live_calls: int = 0) -> Iterator[dict[str, Any]]:
    """Work ``question`` against ``graph``. Yields events:

    ``{"type": "start", ...}``, then ``{"type": "step", tool, args, thought, summary, result}``
    per tool call, then ``{"type": "final", answer, hypotheses, key_people, next_steps,
    suggestions, findings, model}``.
    """
    question = (question or "").strip() or "What are the most important links and patterns in this graph?"
    tools = _Tools(graph, case=case, agents=agents, backend=backend, live_calls=live_calls)
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
            plan = [("person", {"who": named[0]}), ("criminal_proximity", {"who": named[0]}),
                    ("findings", {"who": named[0]})]
        else:
            plan = [("findings", {}), ("network", {}), ("leads", {})]
        if case is not None and case.documents:
            plan.append(("evidence", {"who": named[0]} if named else {}))
        for tool, args in plan:
            result = run(tool, args)
            transcript.append({"tool": tool, "args": args, "result": result})
            yield {"type": "step", "tool": tool, "args": args, "args_text": _args_text(net, args),
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
        result = run(step.action, step.args)
        transcript.append({"tool": step.action, "args": step.args, "result": result})
        yield {"type": "step", "index": index + 1, "tool": step.action, "args": step.args,
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


def _mentioned(net: PersonNetwork, question: str) -> list[str]:
    """People the question names - full name, or a first name only one person has -
    in the order they appear."""
    q = f" {name_key(question)} "
    hits: list[tuple[int, str]] = []
    firsts: dict[str, list[str]] = {}
    for pid, node in net.people.items():
        key = name_key(node["label"])
        if key and f" {key} " in q:
            hits.append((q.index(f" {key} "), pid))
        elif key:
            firsts.setdefault(key.split()[0], []).append(pid)
    for first, pids in firsts.items():
        if len(pids) == 1 and f" {first} " in q and pids[0] not in {p for _, p in hits}:
            hits.append((q.index(f" {first} "), pids[0]))
    return [pid for _, pid in sorted(hits)]


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
