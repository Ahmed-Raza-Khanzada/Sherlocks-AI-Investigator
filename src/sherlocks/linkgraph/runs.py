"""Run lifecycle: start an expansion in the background, watch it grow, keep the result.

A run lives in memory while it executes (the portal polls it and draws the graph as
it grows) and is checkpointed to ``graph_run`` as it goes, so a finished graph can be
reopened after a restart and an interrupted one still shows how far it got.

Runs are threads, not processes. The work is waiting on HTTP, the adapters already
use threads, and a graph that grows while you watch needs shared memory. The one
process-heavy piece, OSINT, isolates each tool in its own process already.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.backends import LookupBackend, build_backend
from sherlocks.linkgraph.cache import MemoryProviderCache, PostgresProviderCache, ProviderCache
from sherlocks.linkgraph.engine import Cancelled, Expansion
from sherlocks.linkgraph.graph import GraphBuilder
from sherlocks.linkgraph.images import DiskImageStore, ImageStore
from sherlocks.linkgraph.models import GraphRunParams
from sherlocks.linkgraph.normalize import cnic13, mobile11, valid_email
from sherlocks.settings import PROJECT_ROOT, Settings

logger = logging.getLogger(__name__)

_MAX_EVENTS = 3000
_CHECKPOINT_SECONDS = 3.0
# A finished run stays in memory this long (for viewers still streaming it), then is
# served from the store.
_KEEP_FINISHED_SECONDS = 1800
# LLM explanations and briefs kept for instant re-opening, newest last.
_MAX_CACHED_ANSWERS = 500


def _now() -> datetime:
    return datetime.now(UTC)


class RunHandle:
    def __init__(self, params: GraphRunParams, backend: str, created_by: str | None = None) -> None:
        self.id = str(uuid.uuid4())
        self.params = params
        self.backend = backend
        self.created_by = created_by
        self.status = "queued"
        self.progress = 0
        self.message = "Queued"
        self.events: list[dict[str, Any]] = []
        # Total events ever emitted. ``events`` is trimmed to the newest _MAX_EVENTS, so
        # a streaming viewer counts by this, not by list position.
        self.event_seq = 0
        self.stats: dict[str, Any] = {}
        self.builder = GraphBuilder()
        self.created_at = self.updated_at = _now()
        self.finished_at: datetime | None = None
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        # The case file (documents, quoted facts, evidence links, the case report) and
        # the team filling it while the run builds.
        self.case = CaseFile()
        self.agents: Any = None
        ids = [s.cnic or s.phone or s.email for s in params.seeds if (s.cnic or s.phone or s.email)]
        self.seed_label = (f"{len(ids)} people: " + ", ".join(ids)) if ids else (params.cnic or params.phone or params.email or "?")

    def emit(self, level: str, message: str) -> None:
        with self._lock:
            self.events.append({"at": _now().isoformat(), "level": level, "message": message})
            self.event_seq += 1
            del self.events[:-_MAX_EVENTS]
            self.updated_at = _now()

    def to_dict(self, *, graph: bool = True) -> dict[str, Any]:
        seed = next((n for n in self.builder.persons() if n.data.get("seed")), None)
        if seed and seed.data.get("name") and not self.params.seeds:
            self.seed_label = f"{seed.data['name']} ({self.params.cnic or self.params.phone or self.params.email})"
        out = {
            "id": self.id, "seed_label": self.seed_label, "backend": self.backend,
            "params": self.params.model_dump(mode="json"), "status": self.status,
            "progress_pct": self.progress, "message": self.message,
            "stats": {**self.builder.stats(), **self.stats}, "created_by": self.created_by,
            "created_at": self.created_at.isoformat(), "updated_at": self.updated_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "evidence": {**self.case.counts(), "report_status": self.case.report_status},
        }
        if graph:
            with self._lock:
                out["events"] = list(self.events)
            out["graph"] = self.graph()
        return out

    def graph(self) -> dict[str, Any]:
        """The graph with its case file - what is saved, exported and analysed."""
        return {**self.builder.snapshot(), "case": self.case.to_dict()}

    def events_since(self, seq: int) -> tuple[list[dict[str, Any]], int]:
        """Events emitted after event number ``seq``, and the current number."""
        with self._lock:
            missing = min(self.event_seq - seq, len(self.events))
            return (list(self.events[-missing:]) if missing > 0 else []), self.event_seq


class RunStore(Protocol):
    def save(self, run: dict[str, Any]) -> None: ...

    def load(self, run_id: str) -> dict[str, Any] | None: ...

    def list(self, limit: int) -> list[dict[str, Any]]: ...


class MemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[str, dict[str, Any]] = {}

    def save(self, run: dict[str, Any]) -> None:
        self._runs[run["id"]] = run

    def load(self, run_id: str) -> dict[str, Any] | None:
        return self._runs.get(run_id)

    def list(self, limit: int) -> list[dict[str, Any]]:
        runs = sorted(self._runs.values(), key=lambda r: r["created_at"], reverse=True)
        return [{k: v for k, v in r.items() if k not in ("graph", "events")} for r in runs[:limit]]


class PostgresRunStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def save(self, run: dict[str, Any]) -> None:
        from sherlocks.db.models import GraphRun
        from sherlocks.db.session import session_scope

        try:
            with session_scope(self.settings) as session:
                row = session.get(GraphRun, run["id"]) or GraphRun(id=run["id"])
                row.seed_label = (run.get("seed_label") or "")[:255]
                row.backend = run["backend"]
                row.params = run["params"]
                row.status = run["status"]
                row.progress_pct = run["progress_pct"]
                row.message = (run.get("message") or "")[:500]
                row.stats = run.get("stats")
                row.created_by = run.get("created_by")
                if "graph" in run:
                    row.graph = run["graph"]
                    row.events = run.get("events")
                row.created_at = datetime.fromisoformat(run["created_at"])
                row.finished_at = datetime.fromisoformat(run["finished_at"]) if run.get("finished_at") else None
                session.merge(row)
        except Exception:
            logger.exception("Could not persist graph run %s", run.get("id"))

    def load(self, run_id: str) -> dict[str, Any] | None:
        from sherlocks.db.models import GraphRun
        from sherlocks.db.session import session_scope

        try:
            with session_scope(self.settings) as session:
                row = session.get(GraphRun, run_id)
                return _row_dict(row, graph=True) if row else None
        except Exception:
            logger.exception("Could not load graph run %s", run_id)
            return None

    def list(self, limit: int) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from sherlocks.db.models import GraphRun
        from sherlocks.db.session import session_scope

        try:
            with session_scope(self.settings) as session:
                rows = session.execute(
                    select(GraphRun.id, GraphRun.seed_label, GraphRun.backend, GraphRun.params, GraphRun.status,
                           GraphRun.progress_pct, GraphRun.message, GraphRun.stats, GraphRun.created_by,
                           GraphRun.created_at, GraphRun.updated_at, GraphRun.finished_at)
                    .order_by(GraphRun.created_at.desc()).limit(limit)
                ).all()
                return [_row_dict(r, graph=False) for r in rows]
        except Exception:
            logger.exception("Could not list graph runs")
            return []


def _row_dict(row: Any, *, graph: bool) -> dict[str, Any]:
    out = {
        "id": row.id, "seed_label": row.seed_label, "backend": row.backend, "params": row.params,
        "status": row.status, "progress_pct": row.progress_pct, "message": row.message,
        "stats": row.stats or {}, "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }
    if graph:
        out["graph"] = row.graph or {"nodes": [], "edges": [], "version": 0}
        out["events"] = row.events or []
    return out


class RunManager:
    def __init__(
        self,
        settings: Settings,
        *,
        store: RunStore | None = None,
        cache: ProviderCache | None = None,
        images: ImageStore | None = None,
        backends: dict[str, LookupBackend] | None = None,
        llm_factory: Any = None,
    ) -> None:
        self.settings = settings
        ttl = settings.linkgraph.cache_ttl_hours * 3600
        self.store = store or PostgresRunStore(settings)
        self.cache = cache or PostgresProviderCache(settings, ttl)
        image_dir = Path(settings.linkgraph.image_dir)
        self.images = images or DiskImageStore(image_dir if image_dir.is_absolute() else PROJECT_ROOT / image_dir)
        self._backends: dict[str, LookupBackend] = dict(backends or {})
        self._backend_lock = threading.Lock()
        self._runs: dict[str, RunHandle] = {}
        self._briefs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._slots = threading.BoundedSemaphore(max(1, settings.api.max_concurrent_jobs))
        self._llm_factory = llm_factory
        # Uploads, incident pins and answers are worked here, apart from the graph runs.
        from concurrent.futures import ThreadPoolExecutor

        self._jobs = ThreadPoolExecutor(max_workers=3, thread_name_prefix="case")

    # -- configuration --------------------------------------------------------------

    def backend_kind(self, requested: str | None) -> str:
        if requested and self.settings.linkgraph.allow_backend_override:
            return requested
        return self.settings.linkgraph.backend

    def backend(self, kind: str) -> LookupBackend:
        with self._backend_lock:
            if kind not in self._backends:
                self._backends[kind] = build_backend(self.settings, kind)
            return self._backends[kind]

    def _llm(self) -> Any:
        if self._llm_factory is not None:
            return self._llm_factory()
        from sherlocks.agents.cache import MemoryLlmCache
        from sherlocks.agents.llm import build_llm

        client = build_llm(self.settings, cache=MemoryLlmCache())
        if client is None:
            return None
        if not client.health():
            logger.warning("LLM configured but unreachable - weak links will use rules only")
            return None
        return client

    # -- runs -----------------------------------------------------------------------

    def start(self, params: GraphRunParams, *, created_by: str | None = None, wait: bool = False) -> RunHandle:
        seeds_ok = any(cnic13(s.cnic) or mobile11(s.phone) or valid_email(s.email) for s in params.seeds)
        if not (cnic13(params.cnic) or mobile11(params.phone) or valid_email(params.email) or seeds_ok):
            raise ValueError("Give a valid 13-digit CNIC, a Pakistani mobile number (03XXXXXXXXX) or an email address.")
        emails_only = [s for s in ([params] if not params.seeds else params.seeds)
                       if valid_email(s.email) and not (cnic13(s.cnic) or mobile11(s.phone))]
        if emails_only and not self.settings.osint.enabled:
            raise ValueError("Searching by email alone needs OSINT, which is disabled on this deployment "
                             "(osint.enabled). Add a CNIC or mobile number, or enable OSINT.")
        lg = self.settings.linkgraph
        params = params.model_copy(update={
            "depth": max(1, min(params.depth, lg.max_depth)),
            "max_persons": max(1, min(params.max_persons, lg.hard_max_persons)),
        })
        handle = RunHandle(params, self.backend_kind(params.backend), created_by)
        self._evict_finished()
        self._runs[handle.id] = handle
        handle.emit("info", f"Run created · backend={handle.backend} · depth={params.depth} · "
                            f"max persons={params.max_persons}")
        self.store.save(handle.to_dict())
        if wait:
            self._execute(handle)
        else:
            threading.Thread(target=self._execute, args=(handle,), name=f"graph-{handle.id[:8]}", daemon=True).start()
        return handle

    def _execute(self, handle: RunHandle) -> None:
        last_save = [0.0]

        def checkpoint(force: bool = False) -> None:
            if force or time.monotonic() - last_save[0] >= _CHECKPOINT_SECONDS:
                last_save[0] = time.monotonic()
                self.store.save(handle.to_dict())

        def progress(pct: int, message: str) -> None:
            handle.progress, handle.message, handle.updated_at = pct, message, _now()

        with self._slots:
            handle.status = "running"
            progress(2, "Connecting to systems")
            try:
                backend = self.backend(handle.backend)
                handle.agents = self._agents(handle.case, backend, handle=handle)
                expansion = Expansion(
                    backend=backend, cache=self.cache, images=self.images, settings=self.settings,
                    params=handle.params, builder=handle.builder, emit=handle.emit, progress=progress,
                    checkpoint=checkpoint, cancelled=handle.cancel_event.is_set,
                    llm=self._llm() if handle.params.ai_address_matching else None,
                    evidence=handle.agents,
                )
                handle.stats = expansion.run()
                if handle.agents.enabled and handle.case.counts()["pending"] + len(handle.case.documents):
                    progress(97, "Reading case documents")
                    handle.emit("info", "Graph complete - finishing the case documents being read")
                    self._drain(handle)
                handle.status = "completed"
                stats = handle.builder.stats()
                progress(100, f"Done · {stats['persons']} people · {stats['records']} records · "
                              f"{stats['strong_links']} strong · {stats['weak_links']} weak links")
                handle.stats = {**handle.stats}
            except Cancelled:
                handle.status = "cancelled"
                handle.emit("warn", "Stopped by user - the case report is written from what was found")
                progress(handle.progress, "Stopped")
                if handle.agents is not None:
                    handle.agents.stop()
            except Exception as exc:
                logger.exception("Graph run %s failed", handle.id)
                handle.status = "failed"
                handle.emit("error", f"{type(exc).__name__}: {exc}")
                progress(handle.progress, f"Failed: {exc}")
            finally:
                handle.finished_at = _now()
                checkpoint(force=True)
        if handle.status in ("completed", "cancelled") and self.settings.evidence.auto_report:
            self.build_report(handle)
            checkpoint(force=True)

    # -- evidence and the case report -------------------------------------------------

    def _agents(self, case: CaseFile, backend: LookupBackend, *, handle: RunHandle | None = None,
                graph: dict[str, Any] | None = None) -> Any:
        from sherlocks.evidence.collector import CaseAgents
        from sherlocks.evidence.sources import evidence_source

        try:
            source = evidence_source(backend) if self.settings.evidence.enabled else None
        except Exception:
            logger.exception("No evidence source for backend %s", getattr(backend, "name", "?"))
            source = None
        return CaseAgents(
            case, source, settings=self.settings, cache=self.cache, images=self.images,
            builder=handle.builder if handle else None,
            graph=handle.builder.snapshot if handle else (lambda: graph or {"nodes": [], "edges": []}),
            llm=self._llm, emit=handle.emit if handle else None,
            cancelled=handle.cancel_event.is_set if handle else None, backend_name=getattr(backend, "name", "ems"))

    def _drain(self, handle: RunHandle) -> None:
        """Let the evidence team finish, unless the officer stops the run."""
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline and not handle.cancel_event.is_set():
            handle.agents.drain(timeout=2.0)
            if not any(not f.done() for f in handle.agents._futures):
                return
        if handle.cancel_event.is_set():
            handle.agents.stop()

    def build_report(self, handle: RunHandle) -> dict[str, Any] | None:
        """Write the case report for a finished (or stopped) run."""
        from sherlocks.evidence.case_report import assemble

        handle.case.set_report(handle.case.report, "building")
        handle.emit("info", "🕵 Sherlock is writing the case report")
        try:
            report = assemble(handle.builder.snapshot(), handle.case, llm=self._llm(), run=handle.to_dict(graph=False))
        except Exception as exc:
            logger.exception("Case report for %s failed", handle.id)
            handle.case.set_report(None, "failed")
            handle.emit("error", f"Case report failed: {type(exc).__name__}: {exc}")
            return None
        handle.case.set_report(report, "ready")
        handle.emit("hit", f"📑 Case report ready: {len(report['assessments'])} assessment(s), "
                           f"{len(report['cited'])} reference(s) - download it from the toolbar")
        return report

    def case_of(self, graph: dict[str, Any], run_id: str | None = None) -> CaseFile:
        handle = self._runs.get(run_id) if run_id else None
        return handle.case if handle is not None else CaseFile.from_dict(graph.get("case"))

    def document(self, run_id: str, doc_id: str) -> dict[str, Any] | None:
        graph = self.graph_for(run_id)
        if graph is None:
            return None
        return self.case_of(graph, run_id).document(doc_id)

    def report_pdf(self, graph: dict[str, Any], run_id: str | None = None, *, rebuild: bool = False) -> bytes:
        """The case report PDF: the report written at the end of the run, or written now
        (a graph posted back by the host, or ``rebuild``)."""
        from sherlocks.evidence.case_report import assemble
        from sherlocks.evidence.report_pdf import render

        case = self.case_of(graph, run_id)
        report = None if rebuild else case.report
        if report is None:
            run = self.get(run_id) if run_id else None
            report = assemble(graph, case, llm=self._llm(), run=run)
            case.set_report(report, "ready")
            if run_id and run_id not in self._runs and run is not None:
                run.setdefault("graph", graph)["case"] = case.to_dict()
                self.store.save(run)
        return render(report, case, graph, self.images)

    def live(self, run_id: str) -> RunHandle | None:
        """The in-memory handle of a run this process holds, for streaming it."""
        return self._runs.get(run_id)

    def _evict_finished(self) -> None:
        """Drop finished runs from memory once they are old enough that no viewer is
        still streaming them. They stay loadable from the store."""
        cutoff = time.time() - _KEEP_FINISHED_SECONDS
        for run_id, handle in list(self._runs.items()):
            if handle.finished_at is not None and handle.finished_at.timestamp() < cutoff:
                self._runs.pop(run_id, None)

    def get(self, run_id: str) -> dict[str, Any] | None:
        handle = self._runs.get(run_id)
        return handle.to_dict() if handle else self.store.load(run_id)

    def list(self, limit: int = 30) -> list[dict[str, Any]]:
        stored = {r["id"]: r for r in self.store.list(limit)}
        for handle in self._runs.values():
            stored[handle.id] = handle.to_dict(graph=False)
        return sorted(stored.values(), key=lambda r: r.get("created_at") or "", reverse=True)[:limit]

    # -- analysis -------------------------------------------------------------------
    #
    # Every analysis works on a graph dict, not a run: the graph is either a run held
    # here (``graph_for``) or one the host application saved and posts back, because
    # Sherlocks does not have to be where finished graphs are kept. ``scope`` names the
    # graph for caching (a run id); a posted graph has none and is not cached.

    def graph_for(self, run_id: str) -> dict[str, Any] | None:
        handle = self._runs.get(run_id)
        if handle is not None:
            return handle.graph()
        run = self.store.load(run_id)
        return run.get("graph") if run else None

    def _cached(self, key: str | None, make: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
        if key is not None and key in self._briefs:
            self._briefs.move_to_end(key)
            return self._briefs[key]
        value = make()
        if key is not None and value is not None:
            self._briefs[key] = value
            while len(self._briefs) > _MAX_CACHED_ANSWERS:
                self._briefs.popitem(last=False)
        return value

    def person_brief(self, graph: dict[str, Any], pid: str, scope: str | None = None) -> dict[str, Any] | None:
        """An intelligence brief for one person, sourced and LLM-phrased. Cached per
        (graph, person, graph version) so re-opening is instant."""
        from sherlocks.linkgraph.dossier import build_person_brief

        key = f"brief:{scope}:{pid}:{graph.get('version')}" if scope else None
        return self._cached(key, lambda: build_person_brief(graph, pid, llm=self._llm()))

    def connection(self, graph: dict[str, Any], a: str, b: str) -> dict[str, Any]:
        """How two people are connected, hop by hop."""
        from sherlocks.linkgraph.dossier import path_between

        return path_between(graph, a, b)

    def compare(self, graph: dict[str, Any], a: str, b: str, *, explain: bool = False,
                scope: str | None = None) -> dict[str, Any] | None:
        """Everything connecting two people the analyst selected. With ``explain`` the LLM
        also writes an explanation from those facts."""
        from sherlocks.linkgraph.compare import compare_people, explain_with_llm, rule_explanation

        result = compare_people(graph, a, b)
        if result is None:
            return None
        result["explanation"] = {"summary": rule_explanation(result), "key_points": [],
                                 "next_steps": [], "model": None}
        if explain:
            key = f"cmp:{scope}:{min(a, b)}:{max(a, b)}:{graph.get('version')}" if scope else None

            def make() -> dict[str, Any] | None:
                llm = self._llm()
                return explain_with_llm(result, llm) if llm is not None else None

            result["explanation"] = self._cached(key, make) or result["explanation"]
        return result

    def group(self, graph: dict[str, Any], ids: list[str] | None = None, *, explain: bool = False,
              scope: str | None = None) -> dict[str, Any]:
        """Relations among the chosen people (default: the seed people). With ``explain``
        the LLM also reads the whole group."""
        from sherlocks.linkgraph.compare import explain_group, group_relations

        result = group_relations(graph, ids)
        result["explanation"] = None
        if explain:
            members = ",".join(sorted(p["id"] for p in result["people"]))
            key = f"grp:{scope}:{graph.get('version')}:{members}" if scope else None

            def make() -> dict[str, Any] | None:
                llm = self._llm()
                return explain_group(result, llm) if llm is not None else None

            result["explanation"] = self._cached(key, make)
        return result

    def ask_pair(self, graph: dict[str, Any], a: str, b: str, question: str,
                 history: list[dict] | None = None) -> dict[str, Any] | None:
        """A question about two selected people, answered from their facts only."""
        from sherlocks.linkgraph.compare import ask_about_pair

        return ask_about_pair(graph, a, b, question, llm=self._llm(), history=history)

    def ask(self, graph: dict[str, Any], question: str, history: list[dict] | None = None) -> dict[str, Any]:
        """Answer a question about the graph, grounded in its own data."""
        from sherlocks.linkgraph.dossier import answer_question

        return answer_question(graph, question, llm=self._llm(), history=history)

    def findings(self, graph: dict[str, Any], scenario: str | None = None) -> dict[str, Any]:
        """Every linkage scenario the rules find, strongest tier first."""
        from sherlocks.linkgraph.scenarios import SCENARIOS, find_scenarios

        rows = find_scenarios(graph, only={scenario} if scenario else None)
        return {"findings": rows, "scenarios": {k: {"title": t, "meaning": m} for k, (t, m) in SCENARIOS.items()}}

    def network(self, graph: dict[str, Any]) -> dict[str, Any]:
        """Clusters, brokers, key people, hidden associates, hubs and leads."""
        from sherlocks.linkgraph.network import PersonNetwork

        return PersonNetwork(graph).overview()

    def paths(self, graph: dict[str, Any], a: str, b: str, k: int = 3) -> dict[str, Any]:
        """The ``k`` best routes between two people - stated before inferred, around hubs."""
        from sherlocks.linkgraph.network import PersonNetwork

        net = PersonNetwork(graph)
        pa, pb = net.resolve(a), net.resolve(b)
        return {"a": pa, "b": pb, "routes": net.paths(pa, pb, k=k) if pa and pb else []}

    def investigate(self, graph: dict[str, Any], question: str, history: list[dict] | None = None,
                    run_id: str | None = None) -> Any:
        """The AI investigator's events for one question (an iterator). It reads the
        case file and may fetch documents or run a lookup itself; what it fetches joins
        the case file (saved with the run, or returned as ``case`` on the final event
        for a graph posted back by the host)."""
        from sherlocks.linkgraph.sherlock_team import run_turn

        handle = self._runs.get(run_id) if run_id else None
        case = self.case_of(graph, run_id)
        kind = handle.backend if handle else self.backend_kind((self.get(run_id) or {}).get("backend") if run_id else None)
        backend = self.backend(kind)
        agents = handle.agents if handle is not None and handle.agents is not None else None
        if agents is None or not agents.enabled:
            agents = self._agents(case, backend, graph=graph)
        before = case.version

        # A graph with FIRs but no documents read yet (built before document reading): read them now.
        reading = 0
        if run_id and not case.documents and not (handle is not None and handle.status in ("queued", "running")):
            reading = self.read_documents(run_id)

        def events() -> Any:
            for event in run_turn(graph, question, llm=self._llm(), history=history, case=case, agents=agents,
                                  backend=backend, live_calls=self.settings.evidence.chat_live_calls):
                if event.get("type") == "final" and reading:
                    from sherlocks.linkgraph.conversation import READING_LINE

                    event["answer"] = (event.get("answer") or "") + "\n\n" + READING_LINE[event.get("language") or "en"].format(n=reading)
                    event["reading_documents"] = reading
                if event.get("type") == "final" and handle is not None:
                    self.store.save(handle.to_dict())
                if event.get("type") == "final" and case.version != before and handle is None:
                    event["case"] = case.to_dict()
                    run = self.store.load(run_id) if run_id else None
                    if run is not None:
                        run.setdefault("graph", graph)["case"] = case.to_dict()
                        self.store.save(run)
                yield event

        return events()

    # -- the case board: uploads, the incident, answers --------------------------------

    def _session(self, run_id: str) -> dict[str, Any] | None:
        """The case file of a run and how to save it: the live handle, or the stored run."""
        handle = self._runs.get(run_id)
        if handle is not None:
            backend = self.backend(handle.backend)
            # Fresh agents: the run's own may be stopped, and a stopped run still takes uploads.
            agents = self._agents(handle.case, backend, handle=handle)
            agents.cancelled = lambda: False
            return {"case": handle.case, "agents": agents, "graph": handle.builder.snapshot, "emit": handle.emit,
                    "save": lambda: self.store.save(handle.to_dict())}
        run = self.store.load(run_id)
        if run is None:
            return None
        graph = run.setdefault("graph", {"nodes": [], "edges": []})
        case = CaseFile.from_dict(graph.get("case"))
        agents = self._agents(case, self.backend(self.backend_kind(run.get("backend"))), graph=graph)

        def save() -> None:
            graph["case"] = case.to_dict()
            self.store.save(run)

        return {"case": case, "agents": agents, "graph": lambda: graph, "emit": lambda level, message: None, "save": save}

    def _upload_dir(self) -> Path:
        return self.settings.output_path / "uploads"

    def upload(self, run_id: str, name: str, content: bytes, *, note: str = "", owner: str | None = None) -> dict[str, Any]:
        """Accept a file (checked now) and read it in the background. Raises
        :class:`~sherlocks.evidence.uploads.UploadError` for a refused file, LookupError
        for an unknown run."""
        from sherlocks.evidence.uploads import check_upload, process_upload, safe_name

        session = self._session(run_id)
        if session is None:
            raise LookupError("Run not found")
        name = safe_name(name)
        kind = check_upload(name, content, self.settings.evidence.max_upload_mb)

        def work() -> None:
            try:
                process_upload(session["agents"], name=name, content=content, note=note, owner=owner,
                               upload_dir=self._upload_dir(), emit=session["emit"])
            except Exception as exc:  # noqa: BLE001 - reported on the case board
                logger.info("Upload %s failed: %s", name, exc)
                session["case"].attempt("upload", f"upload-failed:{name}", "error", str(exc))
            finally:
                session["save"]()

        session["case"].attempt("upload", f"upload:{name}", "accepted", f"{name} received - reading it")
        self._jobs.submit(work)
        return {"accepted": name, "kind": kind}

    def upload_file(self, run_id: str, doc_id: str) -> tuple[bytes, str, str] | None:
        session = self._session(run_id)
        doc = session["case"].documents.get(doc_id) if session else None
        path = Path((doc or {}).get("data", {}).get("path") or "")
        if not doc or not path.is_file() or self._upload_dir() not in path.resolve().parents:
            return None
        return path.read_bytes(), doc["data"].get("name") or path.name, doc["data"].get("mime") or "application/octet-stream"

    def set_incident(self, run_id: str, incident: dict[str, Any]) -> dict[str, Any]:
        """The officer pinned the incident: record it, then (in the background) find the
        nearest police station and re-read every uploaded CDR against the point."""
        from sherlocks.evidence.questioner import ask_gaps

        session = self._session(run_id)
        if session is None:
            raise LookupError("Run not found")
        case = session["case"]
        inc = case.set_incident(incident)
        where = f"{inc.get('place') or 'a pinned point'} ({inc.get('lat')}, {inc.get('lon')})" if inc.get("lat") is not None else inc.get("place")
        doc_id, line = case.officer_note(
            f"Incident location: {where}" + (f", date {inc['date']}" if inc.get("date") else "")
            + (f" {inc['time']}" if inc.get("time") else "") + (f", FIR {inc['fir']}" if inc.get("fir") else ""), source="map")
        case.add_fact(doc_id, f"Stated by the officer: the incident took place at {where}"
                      + (f" on {inc['date']}" if inc.get("date") else ""), line, kind="officer_incident_place", by="officer")
        if inc.get("lat") is not None:
            case.close_question("incident:place", where or "")
            case.close_question("incident:pin", where or "")
        if inc.get("date"):
            case.close_question("incident:when", inc["date"])

        def work() -> None:
            try:
                self._incident_followups(session)
                ask_gaps(case, session["graph"]())
            except Exception:
                logger.exception("Incident follow-up failed")
            finally:
                session["save"]()

        session["save"]()
        self._jobs.submit(work)
        return inc

    def _incident_followups(self, session: dict[str, Any]) -> None:
        from sherlocks.evidence import cdr as cdr_agent
        from sherlocks.evidence.uploads import graph_phones

        case, agents, emit = session["case"], session["agents"], session["emit"]
        inc = case.incident or {}
        if inc.get("lat") is not None and getattr(agents.source, "name", "") != "demo":
            server = cdr_agent.CdrServer(getattr(agents.source, "http", None))
            if server.ready:
                out = server.provider("nearest_ps", {"latitude": float(inc["lat"]), "longitude": float(inc["lon"])})
                if out.get("hit") or out.get("status") == "success":
                    case.set_incident({"nearest_ps": out.get("summary")})
                    emit("info", f"📍 Nearest police station to the incident: {out.get('summary')}")
        phones = {p: n for p, (_pid, n) in graph_phones(session["graph"]()).items()}
        for doc in list(case.documents.values()):
            path = Path((doc.get("data") or {}).get("path") or "")
            if doc["kind"] != "cdr" or not path.is_file():
                continue
            analysis = cdr_agent.analyse(path.read_bytes(), phones_on_graph=phones, incident=inc, llm=None,
                                         radius_km=self.settings.evidence.cdr_radius_km)
            lines = [line for line in analysis.get("lines", [])
                     if line.startswith(("Incident day", "Near the incident", "Towers within"))]
            if lines:
                case.append_text(doc["id"], "\n".join(["Against the pinned incident:", *lines]))
                for line in lines:
                    case.add_fact(doc["id"], line, line, kind="telecom", by="cdr")
                data = doc.setdefault("data", {}).setdefault("analysis", {})
                data["near_incident"] = analysis.get("near_incident") or []
                emit("hit", f"📶 {doc['title']} against the incident: {lines[0]} [{doc['id']}]")

    def answer_question(self, run_id: str, qid: str, answer: str, pid: str | None = None) -> dict[str, Any]:
        from sherlocks.evidence.questioner import ask_gaps
        from sherlocks.evidence.uploads import _link_cdr_on_graph, graph_phones

        session = self._session(run_id)
        if session is None:
            raise LookupError("Run not found")
        case = session["case"]
        q = next((q for q in case.questions if q["id"] == qid), None)
        if q is None:
            raise LookupError("Question not found")
        graph = session["graph"]()
        names = {n["id"]: n.get("label") for n in graph.get("nodes") or [] if n.get("kind") == "person"}
        label = names.get(pid or "", answer) if pid else answer
        case.answer(qid, label or answer)
        case.officer_note(f"Answer to \"{q['text']}\": {label}", source="answer")
        if pid and pid in names:
            if q["key"] == "roles:main":
                case.set_role(pid, "main suspect")
            elif q["key"].startswith("cdr_owner:"):
                doc = case.documents.get(q["key"].split(":", 1)[1])
                if doc is not None:
                    doc.setdefault("owners", []).append(pid)
                    analysis = (doc.get("data") or {}).get("analysis") or {}
                    _link_cdr_on_graph(session["agents"], doc["id"], analysis, graph_phones(graph), pid)
        ask_gaps(case, session["graph"]())
        session["save"]()
        return {"question": q, "open": case.open_questions()}

    def read_documents(self, run_id: str) -> int:
        """Read the case documents of a graph that has none yet - one built before document
        reading existed, or with it off: the FIR file (and its lab reports) of every FIR on
        the graph with a police-station id, and the CRO dossier of every CRO number. In the
        background; returns how many documents were queued."""
        session = self._session(run_id)
        if session is None or not self.settings.evidence.enabled:
            return 0
        case, agents, graph = session["case"], session["agents"], session["graph"]()
        if case.dialog.get("backfilled") or not agents.enabled:
            return 0
        firs: dict[str, tuple[str | None, str, str, str, str]] = {}
        cros: dict[str, str | None] = {}
        for node in graph.get("nodes") or []:
            d = node.get("data") or {}
            nid = str(node.get("id") or "")
            if node.get("kind") == "system" and nid.startswith("s:fir_roster:"):
                parts = nid[len("s:fir_roster:"):].split("/")
                if len(parts) == 3 and parts[2].isdigit():
                    firs.setdefault(f"{parts[0]}/{parts[1]}/{parts[2]}", (d.get("owner"), parts[0], parts[1], parts[2], nid))
            if node.get("kind") == "system":
                for f in d.get("fields") or []:
                    if str(f.get("label") or "").startswith("CRO No") and str(f.get("value") or "").strip():
                        cros.setdefault(str(f["value"]).strip(), d.get("owner"))
            if node.get("kind") == "person":
                for f in d.get("firs") or []:
                    no, _, year = str(f.get("label") or "").partition("/")
                    if f.get("ps_id") and no and year:
                        firs.setdefault(f"{no}/{year}/{f['ps_id']}", (nid, no, year, str(f["ps_id"]), None))
        case.dialog["backfilled"] = True
        if not (firs or cros):
            session["save"]()
            return 0

        def work() -> None:
            try:
                for owner, no, year, ps, sid in list(firs.values())[: self.settings.evidence.max_documents]:
                    agents._fir_task(owner, no, year, ps, None, sid)  # noqa: SLF001
                for cro_no, owner in cros.items():
                    agents._cro_task(owner, cro_no)  # noqa: SLF001
                agents.drain(timeout=600)
            except Exception:  # noqa: BLE001
                logger.exception("Reading the documents of %s failed", run_id)
            finally:
                session["save"]()

        session["save"]()
        self._jobs.submit(work)
        return len(firs) + len(cros)

    def case_index(self, run_id: str) -> dict[str, Any] | None:
        session = self._session(run_id)
        return session["case"].index() if session else None

    def cancel(self, run_id: str) -> bool:
        handle = self._runs.get(run_id)
        if not handle or handle.status not in ("queued", "running"):
            return False
        handle.cancel_event.set()
        return True


def memory_manager(settings: Settings, **kwargs: Any) -> RunManager:
    """No Postgres, no disk: for tests and ``sherlocks graph --no-db``."""
    from sherlocks.linkgraph.images import MemoryImageStore

    return RunManager(settings, store=MemoryRunStore(),
                      cache=MemoryProviderCache(settings.linkgraph.cache_ttl_hours * 3600),
                      images=MemoryImageStore(), **kwargs)
