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
  never searches: the officer decides.

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
_RESULT_CHARS = 3500


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


class _Tools:
    def __init__(self, graph: dict[str, Any]) -> None:
        self.graph = graph
        self.net = PersonNetwork(graph)
        self.findings = find_scenarios(graph, net=self.net)

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

    def registry(self) -> dict[str, tuple[str, Callable[[dict[str, str]], dict]]]:
        scenarios = ", ".join(SCENARIOS)
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
                   "criminal_proximity", "leads", "timeline", "final"]


class _Step(BaseModel):
    thought: str = Field(description="One short sentence: what you need to learn next, and why.")
    action: ToolName = Field(description="The tool to call, or 'final' when you can answer.")
    args: dict[str, str] = Field(default_factory=dict, description="Tool arguments; people by name or id.")


class _Hypothesis(BaseModel):
    statement: str = Field(description="One sentence, naming people as they appear in the graph.")
    tier: Literal["stated", "corroborated", "inferred", "speculative"]
    people: list[str] = Field(default_factory=list, description="Names of the people involved, exactly as in the graph.")
    evidence: list[str] = Field(default_factory=list, description="Facts from the tool results, each with its source system.")


class _Final(BaseModel):
    answer: str = Field(description="Direct answer to the officer's question, 2-6 sentences, citing sources in [brackets].")
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
    "officers: report them neutrally."
)

_FINAL_SYSTEM = (
    "Write the investigator's conclusion from the tool results only. Cite the source system of each "
    "fact in [brackets]. Tier each hypothesis honestly: 'stated' only when a system states it, "
    "'corroborated' when two systems agree, 'inferred' for patterns, 'speculative' for name or OSINT "
    "matches. If the results do not answer the question, say so and set confident to false. Next "
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
    return ""


def investigate(graph: dict[str, Any], question: str, llm: Any = None, *,
                history: list[dict] | None = None, max_steps: int = MAX_STEPS) -> Iterator[dict[str, Any]]:
    """Work ``question`` against ``graph``. Yields events:

    ``{"type": "start", ...}``, then ``{"type": "step", tool, args, thought, summary, result}``
    per tool call, then ``{"type": "final", answer, hypotheses, key_people, next_steps,
    suggestions, findings, model}``.
    """
    question = (question or "").strip() or "What are the most important links and patterns in this graph?"
    tools = _Tools(graph)
    registry = tools.registry()
    net = tools.net
    seeds = [net.name(s) for s in net.seeds()]
    yield {"type": "start", "question": question, "people": len(net.people), "targets": seeds,
           "findings": len(tools.findings), "model": getattr(llm, "model", None) if llm else None}

    def run(tool: str, args: dict[str, str]) -> dict:
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
        for tool, args in plan:
            result = run(tool, args)
            transcript.append({"tool": tool, "args": args, "result": result})
            yield {"type": "step", "tool": tool, "args": args, "args_text": _args_text(net, args),
                   "thought": "Rule-based review (no AI model configured).",
                   "summary": _summary(tool, result), "result": result}
        yield _rule_final(tools, question, transcript)
        return

    convo = "".join(f"\nOfficer: {t.get('q', '')}\nYou: {t.get('a', '')}" for t in (history or [])[-3:])
    overview = {
        "people": len(net.people), "targets": seeds,
        "top_findings": [f"{f['title']} ({f['tier']}): {f['summary']}" for f in tools.findings[:8]],
        "tools": {name: desc for name, (desc, _) in registry.items()},
    }
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
               "summary": _summary(step.action, result), "result": result}

    yield _llm_final(tools, question, transcript, llm)


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
