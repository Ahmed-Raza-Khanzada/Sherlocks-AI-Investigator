"""The Sherlock team's background run on one case board, as a LangGraph graph.

    ingest (A2 reads each record again for its typed card and its recorded statements W#)
      -> entities (A5) -> register (S10) -> claims (S5) -> presence
      -> contradictions (S6) | statements (S7) | leads (S8)        three agents side by side
      -> summary (S2) -> assess (S1: hypotheses, then the matrix)
      -> skeptic (S9 challenges, S1 answers - once)
      -> questions (S4, through Q1) -> insights (Q3) -> END

The order is fixed in code: no model decides who runs next, a run never loops, and every
agent writes its result to the case board with its own name, so nothing here wakes the
team again (``casework.TEAM``). One failing agent is logged and the run goes on.

The scheduler that decides *when* to run (debounce, one run at a time, the run budget)
is ``linkgraph.casework.Casework``; this graph is *what* one run does.
"""

from __future__ import annotations

import itertools
import logging
import operator
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from sherlocks.team import runtime  # noqa: F401 - switches tracing off before a graph is built

logger = logging.getLogger(__name__)
DOCS_PER_RUN = 8
PROMPTS = "v5.1"


@dataclass
class TeamRun:
    """What the agents of one run share. The case board is the state; this only carries it."""

    case: Any
    graph: dict[str, Any]
    net: Any
    llm: Any = None
    settings: Any = None
    reason: str = ""
    started: float = field(default_factory=time.monotonic)


class TeamState(TypedDict, total=False):
    run: TeamRun
    log: Annotated[list[dict[str, Any]], operator.add]


def _agent(name: str, work: Callable[[TeamRun], Any]) -> Callable[[TeamState], dict[str, Any]]:
    """A graph node: run one agent, time it, and never let its failure stop the run."""

    def node(state: TeamState) -> dict[str, Any]:
        run = state["run"]
        started = time.monotonic()
        try:
            did = work(run)
            entry = {"agent": name, "did": did, "ms": int((time.monotonic() - started) * 1000)}
        except Exception as exc:
            logger.exception("%s failed", name)
            entry = {"agent": name, "error": f"{type(exc).__name__}: {exc}", "ms": int((time.monotonic() - started) * 1000)}
        return {"log": [entry]}

    return node


# -- the agents ---------------------------------------------------------------------------


def _ingest(run: TeamRun) -> dict[str, int]:
    from sherlocks.team import register, statements

    cards = recorded = 0
    todo = [d for d in run.case.documents.values() if d["kind"] in register.READS
            and (d["id"] not in (run.case.register.get("cards") or {}) or "W" not in d.get("read_by", []))]
    for doc in todo[:DOCS_PER_RUN]:
        if register.read_card(run.case, doc["id"], run.llm):
            cards += 1
        recorded += len(statements.extract(run.case, run.net, doc["id"], run.llm))
    return {"cards": cards, "statements": recorded, "waiting": max(0, len(todo) - DOCS_PER_RUN)}


def _entities(run: TeamRun) -> dict[str, int]:
    from sherlocks.team import entities

    return entities.run(run.case, run.net)


def _register(run: TeamRun) -> dict[str, int]:
    from sherlocks.team import register

    return register.collect(run.case, run.net, run.graph)


def _claims(run: TeamRun) -> dict[str, int]:
    from sherlocks.team import claims, entities

    done = claims.run(run.case, run.net, run.llm)
    entities.run(run.case, run.net)             # the people the new claims name
    return done


def _presence(run: TeamRun) -> dict[str, Any]:
    from sherlocks.team import presence

    matrix = presence.run(run.case, run.net)
    return {"people": len((matrix or {}).get("rows") or []), "window": (matrix or {}).get("window")}


def _contradictions(run: TeamRun) -> dict[str, int]:
    from sherlocks.team import contradictions

    return contradictions.run(run.case, run.net, run.llm)


def _statements(run: TeamRun) -> dict[str, int]:
    from sherlocks.team import statement_analyst

    return statement_analyst.run(run.case, run.net, run.llm)


def _leads(run: TeamRun) -> dict[str, int]:
    from sherlocks.team import leads

    return leads.run(run.case, run.net, run.llm)


def _summary(run: TeamRun) -> dict[str, int]:
    from sherlocks.evidence import dossiers
    from sherlocks.linkgraph.casework import summarize

    with run.case.acting("S2 Summarizer"):
        run.case.set_summary(summarize(run.case, run.graph, run.net))
    dossiers.refresh(run.case, run.net)
    return {"timeline": len((run.case.summary or {}).get("timeline") or [])}


def _assess(run: TeamRun) -> dict[str, Any]:
    from sherlocks.linkgraph.casework import assess
    from sherlocks.team import leads, matrix

    assess(run.case, run.graph, run.net, run.llm)
    conclusion = ((run.case.assessment or {}).get("answer") or "").strip()
    for asked in run.case.dialog.get("asked_of_sherlock") or []:
        if not asked.get("answer") and conclusion and run.llm is not None:
            # The officer was told "Sherlock is looking into it": his conclusion goes back through Q3.
            asked["answer"], asked["answered_version"] = conclusion[:700], run.case.version
    with run.case.acting("S8 Lead spotter"):
        leads.redescribe(run.case, run.llm)        # leads found before there was a hypothesis to tie them to
    built = matrix.build(run.case, run.llm)
    return {"hypotheses": len(run.case.hypotheses), "leader": (built or {}).get("leader"),
            "matrix": bool(built)}


def _skeptic(run: TeamRun) -> dict[str, int]:
    from sherlocks.team import matrix

    return matrix.challenge(run.case, run.llm)


def _questions(run: TeamRun) -> dict[str, int]:
    from sherlocks.team import questions

    return questions.run(run.case, run.graph, run.net, run.llm)


def _insights(run: TeamRun) -> dict[str, int]:
    from sherlocks.team import insight_desk

    waiting = insight_desk.waiting(run.case, run.case.dialog.get("focus"))
    critical = [c for c in waiting if c["severity"] == "critical"]
    # A critical finding cannot wait for the officer to ask: the board panel shows a badge.
    run.case.dialog["critical_waiting"] = [c["ref"] for c in critical]
    return {"waiting": len(waiting), "critical": len(critical)}


# -- the graph ----------------------------------------------------------------------------

ORDER = [("ingest", "A2 Card reader", _ingest), ("entities", "A5 Entity resolver", _entities),
         ("register", "S10 Fact collector", _register), ("claims", "S5 Claim extractor", _claims),
         ("presence", "Presence matrix", _presence)]
SIDE_BY_SIDE = [("contradictions", "S6 Contradiction finder", _contradictions),
                ("statements", "S7 Statement analyst", _statements), ("leads", "S8 Lead spotter", _leads)]
AFTER = [("summary", "S2 Summarizer", _summary), ("assess", "S1 Sherlock", _assess), ("skeptic", "S9 Skeptic", _skeptic),
         ("questions", "S4 Questioner", _questions), ("insights", "Q3 Insight desk", _insights)]


def build() -> Any:
    g = StateGraph(TeamState)
    for name, agent, work in [*ORDER, *SIDE_BY_SIDE, *AFTER]:
        g.add_node(name, _agent(agent, work))
    g.add_edge(START, ORDER[0][0])
    for (a, _, _), (b, _, _) in itertools.pairwise(ORDER):
        g.add_edge(a, b)
    for name, _, _ in SIDE_BY_SIDE:              # S6, S7 and S8 work the same claims at the same time
        g.add_edge(ORDER[-1][0], name)
    g.add_edge([name for name, _, _ in SIDE_BY_SIDE], AFTER[0][0])
    for (a, _, _), (b, _, _) in itertools.pairwise(AFTER):
        g.add_edge(a, b)
    g.add_edge(AFTER[-1][0], END)
    return g.compile()


_GRAPH: Any = None


def team_graph() -> Any:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build()
    return _GRAPH


def run_team(case: Any, graph: dict[str, Any], net: Any = None, llm: Any = None, *, settings: Any = None,
             reason: str = "") -> dict[str, Any]:
    """One Sherlock team run on ``case``. Returns the run record, also kept on the board
    (model, prompt version, board version, each agent's result and time) for audit."""
    from sherlocks.linkgraph.network import PersonNetwork

    run = TeamRun(case=case, graph=graph, net=net or PersonNetwork(graph), llm=llm, settings=settings, reason=reason)
    out = team_graph().invoke({"run": run, "log": []})
    record = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "reason": reason, "prompts": PROMPTS,
              "model": getattr(llm, "model", None) if llm is not None else None, "board_version": case.version,
              "seconds": round(time.monotonic() - run.started, 1), "agents": out.get("log") or []}
    case.dialog["team_runs"] = [*(case.dialog.get("team_runs") or [])[-19:], record]
    return record
