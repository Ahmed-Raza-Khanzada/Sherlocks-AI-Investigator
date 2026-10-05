"""HTTP surface for the link graph, and the portal that uses it.

Images and the live stream accept the credential as a query parameter as well as a
header, because neither ``<img src>`` nor ``EventSource`` can send a header. Everything
else wants the header.

Two ways to use a graph:

* **While it builds** - ``POST /runs`` then ``GET /runs/{id}/stream`` (Server-Sent
  Events): the graph arrives node by node as the systems answer.
* **Afterwards** - ``POST /analyze/{action}`` with either ``run_id`` (a run this server
  still holds) or ``graph`` (one the host application saved and posts back). Sherlocks
  does not have to keep finished graphs; the host can.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from sherlocks.agents.llm import llm_label
from sherlocks.api.auth import AuthError, SessionAuth
from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.demo_data import DEMO_SEEDS
from sherlocks.linkgraph.images import _MAGIC
from sherlocks.linkgraph.models import GraphRunParams
from sherlocks.linkgraph.runs import RunManager
from sherlocks.linkgraph.stream import GraphDiffer
from sherlocks.linkgraph.systems import DEFAULT_SYSTEMS, SYSTEMS
from sherlocks.settings import Settings

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_MIME = {ext: mime for _, mime, ext in _MAGIC}

# How often a stream checks its run for changes, and how long it may stay silent before
# a keep-alive comment (proxies drop idle connections).
_STREAM_TICK_SECONDS = 0.5
_STREAM_KEEPALIVE_SECONDS = 15.0
_TERMINAL = {"completed", "failed", "cancelled"}
_STATUS_KEYS = ("id", "seed_label", "backend", "params", "status", "progress_pct", "message", "stats",
                "created_by", "created_at", "finished_at", "evidence")


class ChatTurn(BaseModel):
    q: str = ""
    a: str = ""


class AnalyzeRequest(BaseModel):
    """One analysis of one graph. Give ``run_id`` for a run this server holds, or
    ``graph`` (the ``graph`` object of a saved run export) for one kept elsewhere."""

    run_id: str | None = None
    graph: dict[str, Any] | None = None
    question: str | None = Field(default=None, description="ask, ask_pair")
    history: list[ChatTurn] = Field(default_factory=list, description="ask, ask_pair: the conversation so far")
    pid: str | None = Field(default=None, description="brief: the person")
    a: str | None = Field(default=None, description="compare, ask_pair, connection: first person")
    b: str | None = Field(default=None, description="compare, ask_pair, connection: second person")
    ids: list[str] | None = Field(default=None, description="relations: the people (default: the seeds)")
    explain: bool = Field(default=False, description="compare, relations: add the AI reading")
    k: int = Field(default=3, ge=1, le=10, description="paths: how many routes")
    scenario: str | None = Field(default=None, description="findings: only this scenario")


class PairAskRequest(BaseModel):
    a: str
    b: str
    question: str = ""
    history: list[ChatTurn] = Field(default_factory=list)


class AskRequest(BaseModel):
    question: str = ""
    history: list[ChatTurn] = Field(default_factory=list)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def build_router(settings: Settings, manager: Callable[[], RunManager],
                 session_auth: SessionAuth | None = None) -> APIRouter:
    router = APIRouter(prefix=f"{settings.api.prefix}/graph", tags=["link graph"])

    def auth(x_api_key: str | None = Header(default=None), key: str | None = Query(default=None),
             x_session_token: str | None = Header(default=None),
             token: str | None = Query(default=None)) -> str | None:
        """A signed-in operator, or a machine holding the API key. Either is enough.
        Returns the user the token names (``None`` for the API key or an open API), so
        each run records who started it.

        ``key``/``token`` in the query string exist for the browser's own requests -
        <img>, EventSource and download links cannot carry headers.
        """
        expected = settings.api.api_key
        if expected and expected in (x_api_key, key):
            return None
        if session_auth and session_auth.verifies_tokens:
            try:
                return session_auth.verify(x_session_token or token) or None
            except AuthError as exc:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
        if expected:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
        return None

    def _systems_for(backend: str | None) -> list[dict]:
        """Every system the chosen backend actually queries. EMS runs systems the shared
        registry's default list does not carry (NADRA, ARMS, Excise, AVLC); listing only
        the defaults left them unchecked in the portal, so they were never searched."""
        try:
            mgr = manager()
            rows = mgr.backend(mgr.backend_kind(backend)).systems_status()
            keys = [r["system"] for r in rows if r["system"] not in {"fir_roster", "caller_id", "osint"}]
        except Exception:  # noqa: BLE001 - backend down must not empty the system list
            logger.warning("systems_status unavailable for backend %s; using defaults", backend)
            keys = list(DEFAULT_SYSTEMS)
        return [{"system": k, "label": SYSTEMS[k].label, "category": SYSTEMS[k].category,
                 "description": SYSTEMS[k].description, "needs": SYSTEMS[k].needs}
                for k in keys if k in SYSTEMS]

    @router.get("/config")
    def config(backend: str | None = None, _: None = Depends(auth)) -> dict:
        from sherlocks.linkgraph.backends import report_app_available

        lg = settings.linkgraph
        demo_ok = report_app_available(settings)
        # On a server without cdr_report_app, demo/live cannot run - present EMS instead
        # of a default the operator cannot use.
        default_backend = lg.backend
        if default_backend in ("demo", "live") and not demo_ok:
            default_backend = "ems"
        return {
            "backend": default_backend,
            "demo_available": demo_ok,
            "allow_backend_override": lg.allow_backend_override,
            "depth": {"default": lg.default_depth, "max": lg.max_depth},
            "max_persons": {"default": lg.default_max_persons, "max": lg.hard_max_persons},
            "include_fir_rosters": lg.include_fir_rosters,
            "osint_enabled": settings.osint.enabled,
            "ai_enabled": llm_label(settings) is not None,
            "ai_model": llm_label(settings),
            "api_key_required": bool(settings.api.api_key),
            "demo_seeds": DEMO_SEEDS,
            "systems": _systems_for(backend or lg.backend),
            "categories": sorted({info.category for info in SYSTEMS.values()}),
        }

    @router.get("/systems")
    def systems(backend: str | None = None, _: None = Depends(auth)) -> list[dict]:
        mgr = manager()
        try:
            return mgr.backend(mgr.backend_kind(backend)).systems_status()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"Backend unavailable: {exc}") from exc

    @router.post("/runs", status_code=202)
    def create_run(params: GraphRunParams, user: str | None = Depends(auth)) -> dict:
        try:
            handle = manager().start(params, created_by=user)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        logger.info("Graph run %s started by %s (backend=%s, depth=%s)",
                    handle.id, user or "api-key", handle.backend, handle.params.depth)
        return {"run_id": handle.id, "status": handle.status, "backend": handle.backend}

    @router.get("/runs")
    def list_runs(limit: int = Query(default=30, le=200), _: None = Depends(auth)) -> list[dict]:
        return manager().list(limit)

    @router.get("/runs/{run_id}")
    def get_run(run_id: str, _: None = Depends(auth)) -> dict:
        run = manager().get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    @router.get("/runs/{run_id}/stream")
    async def stream(run_id: str, request: Request, _: None = Depends(auth)) -> StreamingResponse:
        """The run as it builds, as Server-Sent Events.

        ``graph``  - ``{version, full, nodes, edges, removed_nodes, removed_edges}``: what
                     changed since the last ``graph`` event (everything, with ``full``,
                     on the first). Upsert nodes/edges by ``id``, drop the removed ids.
        ``status`` - the run's summary (``id, seed_label, backend, params, status,
                     progress_pct, message, stats``…) whenever any of it changes.
        ``log``    - ``{events: [{at, level, message}]}``, new log lines.
        ``case``   - the case file's index whenever it changes: ``{counts, documents,
                     facts, links, attempts, report_status}`` (document texts are fetched
                     with ``/runs/{id}/documents/{doc}``).
        ``done``   - ``{status}``; the stream then ends. Close the EventSource here, or
                     the browser reconnects. Fetch ``/runs/{id}/export`` to save it.
        """
        mgr = manager()
        if mgr.live(run_id) is None and mgr.get(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")

        async def events() -> AsyncIterator[str]:
            handle = mgr.live(run_id)
            if handle is None:
                # Finished and no longer in memory (or started by another process): the
                # stored run in one go.
                run = await asyncio.to_thread(mgr.get, run_id)
                if run is None:
                    return
                graph = run.get("graph") or {"nodes": [], "edges": [], "version": 0}
                yield _sse("graph", {**GraphDiffer().diff(graph)})
                yield _sse("case", CaseFile.from_dict(graph.get("case")).index())
                yield _sse("status", {k: run.get(k) for k in _STATUS_KEYS})
                yield _sse("log", {"events": run.get("events") or []})
                yield _sse("done", {"status": run.get("status")})
                return
            differ, version, seq, last_status, last_sent = GraphDiffer(), -1, 0, None, time.monotonic()
            case_version = -1
            while True:
                if await request.is_disconnected():
                    return
                finished = handle.status in _TERMINAL
                chunks: list[str] = []
                if handle.builder.version != version or finished:
                    version = handle.builder.version
                    delta = differ.diff(await asyncio.to_thread(handle.builder.snapshot))
                    if delta is not None:
                        chunks.append(_sse("graph", delta))
                if handle.case.version != case_version or finished:
                    case_version = handle.case.version
                    chunks.append(_sse("case", handle.case.index()))
                summary = handle.to_dict(graph=False)
                current = {k: summary.get(k) for k in _STATUS_KEYS}
                if current != last_status:
                    last_status = current
                    chunks.append(_sse("status", current))
                new_events, seq = handle.events_since(seq)
                if new_events:
                    chunks.append(_sse("log", {"events": new_events}))
                if finished:
                    chunks.append(_sse("done", {"status": handle.status}))
                if chunks:
                    last_sent = time.monotonic()
                    yield "".join(chunks)
                elif time.monotonic() - last_sent > _STREAM_KEEPALIVE_SECONDS:
                    last_sent = time.monotonic()
                    yield ": keep-alive\n\n"
                if finished:
                    return
                await asyncio.sleep(_STREAM_TICK_SECONDS)

        return StreamingResponse(events(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx: do not buffer the stream
        })

    # -- analysis ---------------------------------------------------------------------

    def _graph(run_id: str) -> dict[str, Any]:
        graph = manager().graph_for(run_id)
        if graph is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return graph

    def _history(turns: list[ChatTurn]) -> list[dict]:
        return [t.model_dump() for t in turns]

    def _source(body: AnalyzeRequest) -> tuple[dict[str, Any], str | None]:
        if body.graph is not None:
            graph, scope = body.graph, None
        elif body.run_id:
            graph, scope = _graph(body.run_id), body.run_id
        else:
            raise HTTPException(status_code=422, detail="Give run_id or graph")
        if not isinstance(graph.get("nodes"), list) or not isinstance(graph.get("edges"), list):
            raise HTTPException(status_code=422, detail="graph must have nodes and edges lists")
        return graph, scope

    @router.post("/analyze/{action}")
    def analyze(action: Literal["ask", "brief", "compare", "ask_pair", "relations", "connection",
                                "findings", "network", "paths", "investigate"],
                body: AnalyzeRequest, _: None = Depends(auth)) -> dict:
        """Chat with a graph, and every per-person/per-pair analysis the portal offers -
        on a run held here (``run_id``) or on a graph posted back by the host (``graph``).

        ask        - {question, history}           -> {answer, confident, model}
        brief      - {pid}                         -> sourced intelligence brief
        compare    - {a, b, explain}               -> what connects two people
        ask_pair   - {a, b, question, history}     -> {answer, confident, model}
        relations  - {ids?, explain}               -> how a group relates, pair by pair
        connection - {a, b}                        -> the chain between two people
        findings   - {scenario?}                   -> linkage scenarios found by rule, tiered
        network    - {}                            -> clusters, brokers, key people, hidden associates
        paths      - {a, b, k}                     -> the k best routes (stated first, around hubs)
        investigate- {question, history}           -> the AI investigator's steps and conclusion
                                                      (use POST /investigate to watch it live)
        """
        mgr = manager()
        graph, scope = _source(body)

        def need(*names: str) -> None:
            missing = [n for n in names if not getattr(body, n)]
            if missing:
                raise HTTPException(status_code=422, detail=f"{action} needs: {', '.join(missing)}")

        result: dict | None
        if action == "ask":
            result = mgr.ask(graph, body.question or "", history=_history(body.history))
        elif action == "brief":
            need("pid")
            result = mgr.person_brief(graph, body.pid, scope=scope)
        elif action == "compare":
            need("a", "b")
            result = mgr.compare(graph, body.a, body.b, explain=body.explain, scope=scope)
        elif action == "ask_pair":
            need("a", "b")
            result = mgr.ask_pair(graph, body.a, body.b, body.question or "", history=_history(body.history))
        elif action == "relations":
            result = mgr.group(graph, body.ids, explain=body.explain, scope=scope)
        elif action == "findings":
            result = mgr.findings(graph, body.scenario)
        elif action == "network":
            result = mgr.network(graph)
        elif action == "paths":
            need("a", "b")
            result = mgr.paths(graph, body.a, body.b, k=body.k)
        elif action == "investigate":
            events = list(mgr.investigate(graph, body.question or "", history=_history(body.history), run_id=scope))
            result = {**events[-1], "steps": [e for e in events if e["type"] == "step"]}
        else:
            need("a", "b")
            result = mgr.connection(graph, body.a, body.b)
        if result is None:
            raise HTTPException(status_code=404, detail="Those people are not in this graph")
        return result

    @router.post("/investigate")
    def investigate_stream(body: AnalyzeRequest, _: None = Depends(auth)) -> StreamingResponse:
        """The AI investigator, live: Server-Sent Events over a POST (read it with fetch()
        and a stream reader - EventSource cannot POST). Events: ``start``, one ``step`` per
        tool call {tool, args, thought, summary}, then ``final`` {answer, hypotheses,
        key_people, next_steps, suggestions, findings}. Body as for /analyze."""
        graph, scope = _source(body)
        events = manager().investigate(graph, body.question or "", history=_history(body.history), run_id=scope)

        def stream() -> Any:
            for event in events:
                # Tool results can be large; the portal shows the summary line.
                slim = {k: v for k, v in event.items() if k != "result"}
                yield _sse(event["type"], slim)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -- case file and case report ------------------------------------------------------

    @router.get("/runs/{run_id}/documents/{doc_id}")
    def document(run_id: str, doc_id: str, _: None = Depends(auth)) -> dict:
        """One case document in full: text, quoted facts, people found, pictures (as
        image ids for ``/images/{id}``)."""
        doc = manager().document(run_id, doc_id.upper())
        if doc is None:
            raise HTTPException(status_code=404, detail="Run or document not found")
        return doc

    def _pdf_response(pdf: bytes, name: str) -> Response:
        return Response(pdf, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @router.get("/runs/{run_id}/report.pdf")
    def report_pdf(run_id: str, rebuild: bool = False, _: None = Depends(auth)) -> Response:
        """The case report as a PDF: written when the run finished or was stopped (or now,
        with ``rebuild=true`` - e.g. after the chat fetched more documents). A link the
        browser can open directly: pass the token as ``?token=``."""
        mgr = manager()
        graph = mgr.graph_for(run_id)
        if graph is None:
            raise HTTPException(status_code=404, detail="Run not found")
        try:
            pdf = mgr.report_pdf(graph, run_id, rebuild=rebuild)
        except Exception as exc:
            logger.exception("Case report PDF for %s failed", run_id)
            raise HTTPException(status_code=500, detail=f"Case report failed: {exc}") from exc
        return _pdf_response(pdf, f"sherlocks_case_report_{run_id[:8]}.pdf")

    @router.post("/report")
    def report_from_graph(body: AnalyzeRequest, _: None = Depends(auth)) -> Response:
        """The case report PDF for a graph the host saved (``graph`` with its ``case``),
        or for a run held here (``run_id``)."""
        graph, scope = _source(body)
        try:
            pdf = manager().report_pdf(graph, scope, rebuild=body.explain)
        except Exception as exc:
            logger.exception("Case report PDF failed")
            raise HTTPException(status_code=500, detail=f"Case report failed: {exc}") from exc
        return _pdf_response(pdf, "sherlocks_case_report.pdf")

    # The per-run forms below predate /analyze and are kept for existing callers.

    @router.get("/runs/{run_id}/persons/{pid}/brief")
    def person_brief(run_id: str, pid: str, _: None = Depends(auth)) -> dict:
        brief = manager().person_brief(_graph(run_id), pid, scope=run_id)
        if brief is None:
            raise HTTPException(status_code=404, detail="Run or person not found")
        return brief

    @router.get("/runs/{run_id}/connection")
    def connection(run_id: str, a: str, b: str, _: None = Depends(auth)) -> dict:
        """How person ``a`` is connected to person ``b`` in this graph, hop by hop."""
        return manager().connection(_graph(run_id), a, b)

    @router.get("/runs/{run_id}/relations")
    def relations(run_id: str, explain: bool = False, ids: str | None = None, _: None = Depends(auth)) -> dict:
        """How the chosen people (default: the ones searched) relate to each other -
        every pair, sourced, plus an optional AI read of the whole group."""
        id_list = [x for x in (ids or "").split(",") if x] or None
        return manager().group(_graph(run_id), id_list, explain=explain, scope=run_id)

    @router.get("/runs/{run_id}/compare")
    def compare(run_id: str, a: str, b: str, explain: bool = False, _: None = Depends(auth)) -> dict:
        """What connects two selected people: direct links, shared records, route,
        mutual contacts and shared details - each with its source system."""
        result = manager().compare(_graph(run_id), a, b, explain=explain, scope=run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Run not found, or those are not two different people in it")
        return result

    @router.post("/runs/{run_id}/compare/ask")
    def compare_ask(run_id: str, body: PairAskRequest, _: None = Depends(auth)) -> dict:
        """Chat about two selected people."""
        answer = manager().ask_pair(_graph(run_id), body.a, body.b, body.question, history=_history(body.history))
        if answer is None:
            raise HTTPException(status_code=404, detail="Run not found, or those are not two different people in it")
        return answer

    @router.post("/runs/{run_id}/ask")
    def ask(run_id: str, body: AskRequest, _: None = Depends(auth)) -> dict:
        """Ask a question about this graph; answered only from the graph's own data."""
        return manager().ask(_graph(run_id), body.question, history=_history(body.history))

    @router.post("/runs/{run_id}/cancel")
    def cancel_run(run_id: str, _: None = Depends(auth)) -> dict:
        return {"cancelled": manager().cancel(run_id)}

    @router.get("/runs/{run_id}/export")
    def export_run(run_id: str, _: None = Depends(auth)) -> Response:
        run = manager().get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        body = json.dumps(run, ensure_ascii=False, indent=1, default=str)
        return Response(body, media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="linkgraph_{run_id[:8]}.json"'})

    @router.get("/images/{image_id}")
    def image(image_id: str, _: None = Depends(auth)) -> Response:
        item = manager().images.get(image_id)
        if item is None:
            return JSONResponse({"detail": "Image not found"}, status_code=404)
        data, ext = item
        return Response(data, media_type=_MIME.get(ext, "application/octet-stream"),
                        headers={"Cache-Control": "private, max-age=86400"})

    return router
