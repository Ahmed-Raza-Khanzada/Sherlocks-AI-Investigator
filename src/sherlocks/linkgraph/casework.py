"""The Sherlock team, working the case in the background.

* **S2 Summarizer** - a rolling summary and one timeline (documents, FIRs, CDRs, the
  officer's statements, hotel stays), for Sherlock, the Officer agent and the report.
* **S1 Sherlock** - hypotheses ``H#`` with a status (open, supported, weakened, ruled out),
  the evidence for and against, confidence and tier; concerns, contradictions, labelled
  speculation, next steps; and whether his quick views given in chat still hold.
  With the model he reasons over the board; without it, rules write the same sections.
* **S4 Questioner** - the questions that would move the case most, ranked; each goes
  through the Gatekeeper (``question_desk.py``) before it can reach the officer.

**What wakes them** (:class:`Casework`): the board's change log, read after a short pause
(several changes in a few seconds start one run, and runs never overlap):

* a batch from outside the team finished (an upload read, a CDR analysed, a document
  fetched, a graph phase, a correction) - one batch is one event, however many facts;
* the officer gave information or answered (statements, roles, the incident, answers);
* 15 facts written outside any batch.

The team is never woken by its own writes (summary, assessment, hypotheses, gaps, desk,
views, dossiers), and a wake with nothing new from outside is skipped and not counted.
Work still to do is on the board's agenda (``case.agenda``), so it survives a restart:
the CDR team's re-runs, dossiers, the nearest police station, and Sherlock himself.
Runs are limited per case (``linkgraph.sherlock_runs_per_case``).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS

logger = logging.getLogger(__name__)

TEAM = {"S1 Sherlock", "S2 Summarizer", "S4 Questioner", "Q1 Gatekeeper", "A4 Dossier agent", "S3 Report writer",
        "Sherlock team"}
# Entries that never wake the Sherlock team: its own output and bookkeeping.
QUIET = {"turn", "desk", "view", "followup", "call", "report", "agenda", "dossier", "summary", "gaps", "hypothesis",
         "assessment", "attempt", "board"}
FACTS_TO_WAKE = 15
DEBOUNCE_S = 2.0


# --------------------------------------------------------------------------------------
# S2 Summarizer
# --------------------------------------------------------------------------------------


def timeline(case: Any, net: Any) -> list[dict[str, str]]:
    """One timeline from every source on the board, oldest first, each line with its ref."""
    from sherlocks.evidence.case_report import _date_key, _timeline

    rows = _timeline(net, case)
    for f in case.live_facts():
        if f.get("by") == "cdr" and f["statement"].startswith(("Incident day record", "IMEI changed", "Silent around")):
            when = re.search(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?", f["statement"])
            if when:
                rows.append({"when": when.group(0), "what": f["statement"][:180], "ref": f["id"]})
    inc = case.incident or {}
    if inc.get("date"):
        stated = case.statement_for("incident:date")
        rows.append({"when": f"{inc['date']} {inc.get('time') or ''}".strip(),
                     "what": "Incident (as the officer gave it)" + (f": {inc['offence'][:80]}" if inc.get("offence") else ""),
                     "ref": stated["id"] if stated else "officer"})
    seen = set()
    out = []
    for r in sorted(rows, key=lambda r: _date_key(r["when"])):
        key = (r["when"], r["what"])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out[:200]


def summarize(case: Any, graph: dict[str, Any], net: Any) -> dict[str, Any]:
    """S2: where the case stands, in plain sentences with ids, plus the timeline."""
    inc = case.incident or {}
    targets = ", ".join(net.name(p) for p in net.seeds()) or "no targets yet"
    roles = "; ".join(f"{net.name(p)}: {r}" for p, r in case.roles.items() if p in net.people)
    strong = sorted((f for f in case.live_facts() if f.get("tier") == "fact" and f.get("by") != "officer"),
                    key=lambda f: (f.get("trust") or 4))[:6]
    parts = [(f"Targets: {targets}; {len(net.people)} people on the graph, {len(case.documents)} case document(s), "
              f"{len(case.live_facts())} fact(s) in use.")]
    if inc:
        where = inc.get("place") or (f"{inc['lat']}, {inc['lon']}" if inc.get("lat") is not None else "not pinned")
        parts.append(f"Incident: {inc.get('offence') or 'offence not given'}, at {where}"
                     + (f", on {inc['date']}" if inc.get("date") else "") + (f", FIR {inc['fir']}" if inc.get("fir") else "") + ".")
    if roles:
        parts.append(f"Roles given by the officer: {roles}.")
    if strong:
        parts.append("Key findings: " + "; ".join(f"{f['statement'][:140]} [{f['id']}]" for f in strong) + ".")
    open_q = [q["text"] for q in case.open_questions()][:3]
    if open_q:
        parts.append("Still open: " + " / ".join(open_q))
    return {"text": " ".join(parts), "timeline": timeline(case, net)}


# --------------------------------------------------------------------------------------
# S1 Sherlock
# --------------------------------------------------------------------------------------


class _Hyp(BaseModel):
    id: str | None = Field(default=None, description="The existing hypothesis id (H#) when updating one; null for new.")
    statement: str
    status: Literal["open", "supported", "weakened", "ruled out"] = "open"
    confidence: Literal["low", "medium", "high"] = "medium"
    tier: Literal["fact", "inference", "speculation"] = "inference"
    people: list[str] = Field(default_factory=list, description="Names exactly as on the graph.")
    support: list[str] = Field(default_factory=list, description="Ids of the entries for it (F#, D#, L#, Q#).")
    against: list[str] = Field(default_factory=list, description="Ids of the entries against it.")
    cause: str = Field(default="", description="What changed it this time (an id or event), if it changed.")


class _ViewCheck(BaseModel):
    view: str = Field(description="The quick view id (V#).")
    verdict: Literal["confirmed", "changed", "replaced"]
    note: str = ""


class _Question(BaseModel):
    text: str
    priority: Literal["red", "orange", "green"] = Field(
        default="orange", description="red: the new case cannot move without it; orange: important; green: helpful.")


class _Assessment(BaseModel):
    conclusion: str = Field(description="Sherlock's main conclusion so far, 2-4 sentences, ids in [brackets].")
    hypotheses: list[_Hyp] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    speculation: list[str] = Field(default_factory=list, description="Suspicions, clearly not evidence.")
    next_steps: list[str] = Field(default_factory=list)
    questions: list[_Question | str] = Field(default_factory=list, description="What to ask the officer, most useful first, "
                                       "each with how much the new case needs it.")
    views: list[_ViewCheck] = Field(default_factory=list)


_SYSTEM = ("You are Sherlock, the lead detective, working a case from its case board. Update your hypotheses: keep "
           "each one's id when it is the same idea, change its status only when the evidence moved, and cite the ids "
           "for and against it - only ids that are on the board. Keep facts, inferences and speculation apart. A "
           "tower contact is not presence; an unconfirmed CDR owner is a number, not a person. Use the answers the "
           "officer already gave (known_answers) - never ask them again. Check your quick views given in chat. "
           + DATA_NOT_INSTRUCTIONS)


def _rule_assessment(case: Any, graph: dict[str, Any], net: Any) -> dict[str, Any]:
    """S1 without the model: hypotheses from the graph's findings and the CDR team."""
    from sherlocks.evidence.trust import conflicts

    hyps = []
    for f in case.live_facts():
        if f.get("by") == "graph" and f.get("kind", "").startswith("graph_"):
            status = "supported" if f.get("tier") == "fact" else "open"
            hyps.append({"statement": f["statement"].split(" (", 1)[0] + ": " + ", ".join(f.get("people") or []),
                         "status": status, "confidence": "high" if status == "supported" else "low",
                         "tier": f.get("tier", "inference"), "people": f.get("pids") or [], "support": [f["id"]]})
        elif f.get("kind") == "cdr_cross":
            hyps.append({"statement": f["statement"], "status": "open", "confidence": "medium",
                         "tier": f.get("tier", "inference"), "people": f.get("pids") or [], "support": [f["id"]]})
    contradictions = [c["settle"] for c in conflicts(case, net)]
    return {"conclusion": "", "hypotheses": hyps[:8], "concerns": [], "contradictions": contradictions,
            "speculation": [], "next_steps": [], "questions": [], "views": []}


def assess(case: Any, graph: dict[str, Any], net: Any, llm: Any = None) -> dict[str, Any]:
    """S1: the assessment, written onto the board. Returns it."""
    from sherlocks.evidence.question_desk import known_answers
    from sherlocks.linkgraph.briefer import build

    known = case.known_ids()
    out: dict[str, Any] | None = None
    if llm is not None:
        pack = build(case, graph, net, " ".join(h["statement"] for h in case.hypotheses.values())[:400] or "the case",
                     people=list(case.roles) or net.seeds(), chars=12000)
        payload = {"summary": (case.summary or {}).get("text"), "timeline": (case.summary or {}).get("timeline", [])[:40],
                   "facts": [{"id": f["id"], "fact": f["statement"][:220], "tier": f.get("tier"), "trust": f.get("trust")}
                             for f in case.live_facts()][-80:],
                   "hypotheses": [{k: h.get(k) for k in ("id", "statement", "status", "confidence", "for", "against")}
                                  for h in case.hypotheses.values()],
                   "conflicts": pack.get("conflicts"), "officer_said": pack.get("statements"),
                   "known_answers": known_answers(case),
                   "quick_views": [{"id": v["id"], "question": v["question"], "view": v["view"]} for v in case.views
                                   if v.get("status") == "given"][-6:]}
        try:
            got, _ = llm.generate_structured(prompt=json.dumps(payload, ensure_ascii=False, default=str), schema=_Assessment,
                                             system=_SYSTEM, cache_kind="sherlock_assessment", prompt_version="v1",
                                             max_tokens=2500)
            out = got.model_dump()
        except Exception as exc:  # noqa: BLE001 - the rules below still assess
            logger.info("Sherlock's assessment by model failed: %s", exc)
    out = out or _rule_assessment(case, graph, net)
    cause = ", ".join(sorted({c.get("kind") or c["entry"] for c in case.changes[-40:]
                              if c.get("agent") not in TEAM and c["entry"] not in QUIET}))[:200]
    with case.acting("S1 Sherlock"):
        ids = []
        for h in out["hypotheses"][:10]:
            support = [i for i in h.get("support") or [] if i in known and case.usable(i)]
            against = [i for i in h.get("against") or [] if i in known and case.usable(i)]
            if not (support or against) and h.get("tier") != "speculation":
                continue                      # a hypothesis that rests on nothing on the board is not kept
            people = [net.resolve(p) or p for p in h.get("people") or []]
            ids.append(case.set_hypothesis(h["statement"], status=h.get("status", "open"),
                                           confidence=h.get("confidence", "medium"), tier=h.get("tier", "inference"),
                                           people=[p for p in people if p in net.people], support=support,
                                           against=against, hid=h.get("id") if h.get("id") in case.hypotheses else None,
                                           cause=h.get("cause") or cause))
        for check in out.get("views") or []:
            v = next((v for v in case.views if v["id"] == check["view"]), None)
            if v is not None:
                v["status"], v["checked"] = check["verdict"], check.get("note", "")[:300]
        case.set_assessment({"answer": out.get("conclusion") or "", "hypotheses": [case.hypotheses[i] for i in ids],
                             "concerns": out.get("concerns") or [], "contradictions": out.get("contradictions") or [],
                             "speculation": out.get("speculation") or [], "next_steps": out.get("next_steps") or [],
                             "questions": out.get("questions") or [], "board_version": case.version,
                             "model": getattr(llm, "model", None) if llm is not None else None})
    return case.assessment


# --------------------------------------------------------------------------------------
# S4 Questioner
# --------------------------------------------------------------------------------------


def rank_gaps(case: Any, graph: dict[str, Any]) -> list[dict[str, Any]]:
    """S4: the gaps that would move the case most - the board's rules first, then
    Sherlock's own questions - each through the Gatekeeper."""
    from sherlocks.evidence.question_desk import Gatekeeper
    from sherlocks.evidence.questioner import ask_gaps, gaps

    with case.acting("S4 Questioner"):          # the team's own writes: they never wake it again
        ask_gaps(case, graph)
        desk = Gatekeeper(case, graph)
        ranked = [{"key": g["key"], "text": g["text"], "why": "the board needs it"} for g in gaps(case, graph)]
        for item in ((case.assessment or {}).get("questions") or [])[:4]:
            text, priority = (item.get("text"), item.get("priority")) if isinstance(item, dict) else (item, "orange")
            q = desk.admit({"text": text, "priority": priority}, by="S4 Questioner")
            decision = case.desk[-1] if case.desk else {}
            ranked.append({"key": (q or {}).get("key") or decision.get("key"), "text": text, "why": "Sherlock's question",
                           "decision": decision.get("decision"), "reason": decision.get("reason")})
        case.set_gaps(ranked)
    return ranked


# --------------------------------------------------------------------------------------
# The scheduler
# --------------------------------------------------------------------------------------


class Board:
    """One case board's background work: its graph, agents and save, and its run state."""

    def __init__(self, run_id: str, case: Any, graph: Callable[[], dict[str, Any]], save: Callable[[], None],
                 tools: Callable[[], Any] | None = None) -> None:
        self.run_id, self.case, self.graph, self.save, self.tools = run_id, case, graph, save, tools
        self.lock = threading.Lock()
        self.running = False
        self.again = False
        self.timer: threading.Timer | None = None
        self.last_seq = case.dialog.get("sherlock_seq", 0)
        self.batches: list[dict[str, Any]] = []


class Casework:
    def __init__(self, settings: Any, llm: Callable[[], Any], *, debounce: float = DEBOUNCE_S) -> None:
        self.settings = settings
        self.llm = llm
        self.debounce = debounce
        self.boards: dict[str, Board] = {}
        self._lock = threading.Lock()

    # -- wiring -----------------------------------------------------------------------

    def attach(self, run_id: str, case: Any, graph: Callable[[], dict[str, Any]], save: Callable[[], None],
               tools: Callable[[], Any] | None = None) -> Board:
        with self._lock:
            board = self.boards.get(run_id)
            if board is not None and board.case is case:
                return board
            board = Board(run_id, case, graph, save, tools)
            self.boards[run_id] = board
        case.listen(lambda change, b=board: self._heard(b, change))
        if case.agenda:
            self._schedule(board)                # work left from before a restart
        return board

    def _heard(self, board: Board, change: dict[str, Any]) -> None:
        if change.get("agent") in TEAM:
            return                               # never woken by its own writes
        if change["entry"] == "batch":
            board.batches.append(change)
        elif change["entry"] in QUIET:
            return
        self._schedule(board)

    def _schedule(self, board: Board, delay: float | None = None) -> None:
        with board.lock:
            if board.timer is not None:
                board.timer.cancel()             # debounce: several events, one run
            board.timer = threading.Timer(self.debounce if delay is None else delay, self._fire, args=(board,))
            board.timer.daemon = True
            board.timer.start()

    # -- deciding ---------------------------------------------------------------------

    def _outside(self, board: Board) -> list[dict[str, Any]]:
        return [c for c in board.case.changes_since(board.last_seq)
                if c.get("agent") not in TEAM and c["entry"] not in QUIET]

    def due(self, board: Board) -> str | None:
        """Why Sherlock should run now, or None."""
        case = board.case
        if any(a["task"] == "sherlock" for a in case.agenda):
            return "planned"
        new = self._outside(board)
        batches = [b for b in board.batches if b["seq"] > board.last_seq]
        if any(b.get("entries") and set(b["entries"]) - QUIET for b in batches):
            return f"batch finished: {batches[-1].get('kind')}"
        officer = [c for c in new if c["entry"] in ("role", "incident", "question", "statement", "stale")
                   and not c.get("event")]
        if officer:
            return "the officer gave information"
        loose = [c for c in new if c["entry"] == "fact" and not c.get("event")]
        if len(loose) >= FACTS_TO_WAKE:
            return f"{len(loose)} new facts"
        return None

    def _fire(self, board: Board) -> None:
        with board.lock:
            board.timer = None
            if board.running:
                board.again = True               # one follow-up run, never two at once
                return
            board.running = True
        try:
            self.work(board)
        except Exception:  # noqa: BLE001 - background work must not kill the server
            logger.exception("Sherlock team run on %s failed", board.run_id)
        finally:
            with board.lock:
                board.running = False
                again, board.again = board.again, False
            if again:
                self._schedule(board)

    # -- working ----------------------------------------------------------------------

    def run_agenda(self, board: Board) -> list[str]:
        """The board's planned work other than Sherlock himself."""
        from sherlocks.evidence import cdr_team, dossiers
        from sherlocks.linkgraph.network import PersonNetwork

        case, done = board.case, []
        graph = board.graph()
        net = PersonNetwork(graph)
        radius = getattr(getattr(self.settings, "evidence", None), "cdr_radius_km", 2.0)
        for item in list(case.agenda):
            task = item["task"]
            if task == "sherlock":
                continue
            try:
                if task.startswith("cdr:enrich:"):
                    tools = board.tools() if board.tools else None
                    if tools is not None:
                        cdr_team.enrich(case, task.split(":", 2)[2], tools, graph)
                elif task.startswith("cdr:"):
                    cdr_team.rerun(case, graph, task, radius_km=radius)
                elif task == "dossiers":
                    dossiers.refresh(case, net)
                elif task == "incident:ps" and case.incident:
                    case.incident.pop("nearest_ps", None)     # found again when the officer pins the new place
                # "summary" and "gaps" are part of every Sherlock team run below.
            except Exception:  # noqa: BLE001
                logger.exception("Agenda task %s on %s failed", task, board.run_id)
            case.done(task)
            done.append(task)
        return done

    def work(self, board: Board, *, force: bool = False) -> dict[str, Any] | None:
        """One Sherlock team run: the agenda, then S2 -> S1 -> S4. Skipped when nothing
        new came from outside (unless ``force``) or the case's run budget is used up."""
        from sherlocks.evidence import dossiers
        from sherlocks.linkgraph.network import PersonNetwork

        case = board.case
        self.run_agenda(board)
        reason = "requested" if force else self.due(board)
        if reason is None:
            return None
        limit = getattr(getattr(self.settings, "evidence", None), "sherlock_runs_per_case", 20)
        runs = int(case.dialog.get("sherlock_runs") or 0)
        if runs >= limit and not force:
            logger.info("Case %s: Sherlock's %d background runs used - waiting for the report or the officer",
                        board.run_id, limit)
            return None
        started = time.monotonic()
        graph = board.graph()
        net = PersonNetwork(graph)
        seq = case.change_seq
        with case.acting("S2 Summarizer"):
            case.set_summary(summarize(case, graph, net))
        dossiers.refresh(case, net)
        llm = self.llm() if self.llm else None
        assessment = assess(case, graph, net, llm)
        rank_gaps(case, graph)
        case.done("sherlock")
        case.dialog["sherlock_runs"] = runs + 1
        case.dialog["sherlock_seq"] = board.last_seq = max(seq, board.last_seq)
        board.batches = [b for b in board.batches if b["seq"] > board.last_seq]
        board.save()
        logger.info("Sherlock team on %s (%s): %d hypothesis(es), %.1fs", board.run_id, reason,
                    len(case.hypotheses), time.monotonic() - started)
        return assessment

    def bring_up_to_date(self, board: Board, timeout: float = 90.0) -> bool:
        """Before the report: make the assessment cover the board as it is now. True if it
        does; False if the run did not finish in time (the report says "as of")."""
        if (case_covered(board.case)):
            return True
        done = threading.Event()
        until = time.monotonic() + timeout

        def run() -> None:
            try:
                while time.monotonic() < until:      # a run in progress finishes first
                    with board.lock:
                        if not board.running:
                            board.running = True
                            break
                    time.sleep(0.2)
                else:
                    return
                try:
                    self.work(board, force=True)
                finally:
                    with board.lock:
                        board.running = False
            finally:
                done.set()

        threading.Thread(target=run, name=f"report-{board.run_id[:8]}", daemon=True).start()
        done.wait(timeout)
        return case_covered(board.case)


def case_covered(case: Any) -> bool:
    """Does Sherlock's assessment cover every outside change on the board?"""
    covered = int((case.assessment or {}).get("board_version") or -1)
    later = [c for c in case.changes if c["version"] > covered and c.get("agent") not in TEAM
             and c["entry"] not in QUIET]
    return case.assessment is not None and not later


def not_covered(case: Any) -> list[dict[str, Any]]:
    """The outside changes the assessment does not cover yet (for the report's appendix)."""
    covered = int((case.assessment or {}).get("board_version") or -1)
    return [c for c in case.changes if c["version"] > covered and c.get("agent") not in TEAM and c["entry"] not in QUIET]
