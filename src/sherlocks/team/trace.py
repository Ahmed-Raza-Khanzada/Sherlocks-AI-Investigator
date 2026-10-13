"""The trace of a chat turn: which agent ran, what it was given, what it answered and whom
it handed the work to.

Every question the officer sends runs through the Officer graph (``officer_graph``). The
trace keeps that run so it can be read afterwards, per question:

* ``steps`` - each node of the graph in the order it ran: the agent, how long it took,
  what it did (its reading, its draft, its lookups, its verdict) and the notes the
  agents wrote while working;
* ``model_calls`` - every call to the language model: the agent that made it, in which
  step, what it was given (the parts of its input and their size), what it answered,
  how long it took, and whether the answer was used, came from the cache or failed;
* ``flow`` - who handed what to whom, one line per hand-over;
* ``late`` - what finished after the reply had gone out.

Traces live on the case board (the last ``KEEP`` turns) and are read through
``GET /graph/runs/{run_id}/trace``. They hold what the case itself holds - nothing is
sent anywhere - and a trace never changes an answer: every hook here is best-effort.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from itertools import pairwise
from typing import Any

logger = logging.getLogger(__name__)

KEEP = 30                 # turns kept on a board
OUTPUT_CHARS = 3000       # of a model's answer
NOTE_CHARS = 600
# The node of the graph -> the agent that works in it.
AGENT = {"read": "O0 Question reader", "rules": "Rule answer (no model reading)", "record": "O3 Statement recorder",
         "brief": "Case briefer (dossiers, facts, context pack)", "/mo/": "O1 Officer agent",
         "lookup": "Toolbox (board tools and the API agent)", "desk": "Desk (not-found, Sherlock's view, Insight desk, Gatekeeper)",
         "check": "O2 Checker", "present": "Presenter"}
# The kind of a model call -> the agent that made it.
CALLER = {"question_reader": "O0 Question reader", "statement_recorder": "O3 Statement recorder",
          "officer_draft": "O1 Officer agent", "officer_answer": "Officer agent (rule path)",
          "officer_facts": "Facts agent", "knowledge_answer": "Knowledge agent", "validator": "O2 Checker",
          "revise": "O2 Checker (rewrite)", "sherlock_view": "S1 Sherlock (quick view)", "gatekeeper": "Q1 Gatekeeper",
          "gatekeeper_sweep": "Q1 Gatekeeper (sweep)", "line_translation": "Presenter (translation)",
          "quote_translation": "Presenter (translation)", "incident_time": "Statement recorder (time)",
          "role": "Statement recorder (role)"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _short(value: Any, limit: int) -> Any:
    """A value small enough to keep: long text cut, long lists and nested data summarised."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"… (+{len(value) - limit} chars)"
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= limit:
        return value
    return text[:limit] + f"… (+{len(text) - limit} chars)"


def given(prompt: str) -> dict[str, Any]:
    """What a model call was given: the parts of its input and the size of each (the
    input itself can be tens of thousands of characters and is on the board already)."""
    try:
        data = json.loads(prompt)
    except (TypeError, ValueError):
        return {"text_chars": len(prompt or "")}
    if not isinstance(data, dict):
        return {"text_chars": len(prompt or "")}
    parts: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str | int | float | bool) or value is None:
            parts[key] = _short(value, 200)
        elif isinstance(value, list):
            parts[key] = f"{len(value)} item(s), {len(json.dumps(value, ensure_ascii=False, default=str))} chars"
        else:
            parts[key] = f"{len(value)} field(s), {len(json.dumps(value, ensure_ascii=False, default=str))} chars"
    return parts


class Trace:
    """One turn's trace, filled while the graph runs."""

    def __init__(self, turn: int, question: str, language: str) -> None:
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._step: dict[str, Any] | None = None
        self.data: dict[str, Any] = {"turn": turn, "at": _now(), "question": question[:2000], "language": language,
                                     "steps": [], "model_calls": [], "flow": [], "late": [], "done": False}

    def _ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    # -- the nodes --------------------------------------------------------------------------

    def begin(self, node: str) -> dict[str, Any]:
        with self._lock:
            step = {"n": len(self.data["steps"]) + 1, "node": node, "agent": AGENT.get(node, node),
                    "started_ms": self._ms(), "ms": None, "notes": []}
            self.data["steps"].append(step)
            self._step = step
            return step

    def end(self, step: dict[str, Any], did: dict[str, Any] | None) -> None:
        with self._lock:
            step["ms"] = self._ms() - step["started_ms"]
            for key, value in (did or {}).items():
                step[key] = _short(value, OUTPUT_CHARS)

    def note(self, agent: str, text: str, **extra: Any) -> None:
        """What an agent said about its own work, as it worked."""
        with self._lock:
            entry = {"agent": agent, "text": (text or "")[:NOTE_CHARS], "at_ms": self._ms()}
            if extra:
                entry["more"] = _short(extra, NOTE_CHARS)
            if self.data["done"] or self._step is None:
                self.data["late"].append(entry)
            else:
                self._step["notes"].append(entry)

    # -- the model --------------------------------------------------------------------------

    def call(self, kwargs: dict[str, Any], result: Any, error: BaseException | None, ms: int) -> None:
        kind = kwargs.get("cache_kind") or "model"
        schema = kwargs.get("schema")
        entry: dict[str, Any] = {
            "agent": CALLER.get(kind, kind), "kind": kind, "step": (self._step or {}).get("node"),
            "after_reply": self.data["done"], "at_ms": self._ms() - ms, "ms": ms,
            "schema": getattr(schema, "__name__", None), "prompt_version": kwargs.get("prompt_version"),
            "max_tokens": kwargs.get("max_tokens"), "input_chars": len(kwargs.get("prompt") or ""),
            "instructions_chars": len(kwargs.get("system") or ""), "given": given(kwargs.get("prompt") or "")}
        if error is not None:
            entry.update({"ok": False, "error": str(error)[:400]})
        else:
            validated, info = result if isinstance(result, tuple) and len(result) == 2 else (result, None)
            data = getattr(info, "data", None)
            if data is None and hasattr(validated, "model_dump"):
                data = validated.model_dump(mode="json")
            entry.update({"ok": True, "cached": bool(getattr(info, "cached", False)),
                          "attempts": getattr(info, "attempts", None), "answer": _short(data, OUTPUT_CHARS)})
        with self._lock:
            entry["n"] = len(self.data["model_calls"]) + 1
            self.data["model_calls"].append(entry)

    # -- the end ----------------------------------------------------------------------------

    def close(self, final: dict[str, Any] | None = None) -> dict[str, Any]:
        """The reply went out: the hand-overs are written down, the totals added."""
        with self._lock:
            steps = self.data["steps"]
            self.data["flow"] = [{"from": a["agent"], "to": b["agent"], "handed_over": a.get("passes") or ""}
                                 for a, b in pairwise(steps)]
            if steps:
                self.data["flow"].append({"from": steps[-1]["agent"], "to": "the officer",
                                          "handed_over": steps[-1].get("passes") or "the reply"})
            final = final or {}
            self.data.update({
                "path": [s["node"] for s in steps], "ms": self._ms(),
                "answer": _short(final.get("answer") or "", OUTPUT_CHARS),
                "writer": final.get("writer"), "shape": final.get("shape"),
                "checks": _short({k: v for k, v in (final.get("checks") or {}).items()
                                  if k in ("ok", "issues", "revised", "first_issues", "asked", "told", "intent")}, OUTPUT_CHARS),
                "slow_steps": list(final.get("late") or []), "timing_ms": final.get("timing"),
                "done": True})
            self._step = None
            calls = self.data["model_calls"]
        logger.info("Turn %s trace: %s | %d ms | %d model call(s)%s", self.data["turn"],
                    " -> ".join(f"{s['node']}({s['ms']}ms)" for s in steps), self.data["ms"], len(calls),
                    f" | failed: {[c['kind'] for c in calls if not c['ok']]}" if any(not c["ok"] for c in calls) else "")
        return self.data


def brief(trace: dict[str, Any]) -> dict[str, Any]:
    """One line of the list: the question, the path through the agents, the cost."""
    calls = trace.get("model_calls") or []
    return {"turn": trace.get("turn"), "at": trace.get("at"), "question": (trace.get("question") or "")[:300],
            "answer": str(trace.get("answer") or "")[:300], "path": trace.get("path") or [],
            "agents": list(dict.fromkeys(s["agent"] for s in trace.get("steps") or [])),
            "ms": trace.get("ms"), "model_calls": len(calls), "model_ms": sum(c.get("ms") or 0 for c in calls),
            "failed_calls": [c["kind"] for c in calls if not c.get("ok")],
            "slow_steps": trace.get("slow_steps") or [], "checks": trace.get("checks"),
            "after_reply": len(trace.get("late") or []) + sum(1 for c in calls if c.get("after_reply"))}


def overview(case: Any, turn: int | None = None) -> dict[str, Any] | None:
    """What the trace API returns. Without ``turn``: every traced question in brief, and the
    background runs of the Sherlock team. With ``turn`` (0 = the latest): that question in full."""
    traces = list(getattr(case, "traces", None) or [])
    if turn is not None:
        found = traces[-1] if turn == 0 and traces else next((t for t in traces if t.get("turn") == turn), None)
        return found
    runs = [{"at": r.get("at"), "reason": r.get("reason"), "seconds": r.get("seconds"), "model": r.get("model"),
             "board_version": r.get("board_version"), "agents": r.get("agents")}
            for r in (case.dialog.get("team_runs") or [])[-10:]]
    return {"turns": [brief(t) for t in traces], "kept": KEEP, "conversation_turns": len(case.conversation),
            "background_runs": runs,
            "how_to_read": "turns[].path is the order of the graph's nodes; GET .../trace/{turn} gives every step, "
                           "every model call (what it was given, what it answered) and who handed what to whom. "
                           "background_runs are the Sherlock team's runs between questions, agent by agent."}
