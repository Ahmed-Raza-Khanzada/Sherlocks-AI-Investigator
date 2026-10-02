"""FastAPI application.

Scope for Phase 1 is deliberately small: submit an OSINT job, poll it, download the PDF,
and ask what the deployment is actually capable of. The capability endpoint matters more
than it looks - most confusing OSINT results trace back to a missing binary or an unset
Bright Data key, and it is far better to answer that with a request than with a
support call.

Jobs run in a background thread that forks a child process. That is enough for the
volumes this handles; the plan's note about moving to a real queue stands for later.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sherlocks.api.auth import AuthError, SessionAuth
from sherlocks.api.linkgraph import WEB_DIR, build_router
from sherlocks.api.schemas import (
    JobCreated,
    JobStatus,
    OsintHealth,
    OsintJobRequest,
    ToolCapability,
)
from sherlocks.db.models import Job
from sherlocks.db.session import init_db, session_scope
from sherlocks.linkgraph.runs import RunManager
from sherlocks.logging_config import configure_logging
from sherlocks.osint.client import OsintClient
from sherlocks.render._reportlib import report_app_available
from sherlocks.services.job_worker import run_osint_job
from sherlocks.settings import Settings, load_settings

logger = logging.getLogger(__name__)

settings: Settings = load_settings()
configure_logging(settings)

app = FastAPI(
    title="Sherlocks",
    description="Person intelligence and link analysis. Phase 1: online footprint.",
    version="0.1.0",
)

_cors_origins = [o.strip() for o in settings.api.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # Credentials travel in headers, never cookies - and a wildcard origin must not be
    # paired with credentials. Set SHERLOCKS_CORS_ORIGINS to the host portal's origin.
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

_semaphore = threading.BoundedSemaphore(max(1, settings.api.max_concurrent_jobs))


class LoginRequest(BaseModel):
    username: str
    password: str

# Built on first use rather than at import, so importing the app never needs Postgres.
_graph_manager: RunManager | None = None
_graph_manager_lock = threading.Lock()


def graph_manager() -> RunManager:
    global _graph_manager
    with _graph_manager_lock:
        if _graph_manager is None:
            _graph_manager = RunManager(settings)
        return _graph_manager


session_auth = SessionAuth(settings)
app.include_router(build_router(settings, graph_manager, session_auth))


class _NoStorePortal(StaticFiles):
    """Serve the portal with no browser caching.

    The page, its script and its stylesheet keep the same names across deploys, so a
    cached copy would keep an operator on yesterday's UI after an update. The files are
    small and local; re-fetching them costs nothing worth keeping a stale copy for.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response


def _asset_version() -> str:
    """Changes whenever the portal's script or stylesheet changes, so the browser is
    forced onto the new UI after a deploy instead of running a cached copy."""
    stamps = [int((WEB_DIR / name).stat().st_mtime) for name in ("app.js", "app.css") if (WEB_DIR / name).exists()]
    return str(max(stamps, default=0))


@app.get(f"{settings.api.prefix}/portal/", include_in_schema=False)
@app.get(f"{settings.api.prefix}/portal/index.html", include_in_schema=False)
def _portal_index() -> HTMLResponse:
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8").replace("__ASSET_VERSION__", _asset_version())
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})


app.mount(f"{settings.api.prefix}/portal", _NoStorePortal(directory=WEB_DIR, html=True), name="portal")


@app.get("/", include_in_schema=False)
def _root() -> RedirectResponse:
    return RedirectResponse(f"{settings.api.prefix}/portal/")


@app.post(f"{settings.api.prefix}/auth/login", tags=["auth"])
def login(body: LoginRequest) -> dict:
    try:
        return session_auth.login(body.username, body.password)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@app.get(f"{settings.api.prefix}/auth/session", tags=["auth"])
def session_info(x_session_token: str | None = Header(default=None)) -> dict:
    """Who am I, and is my session still valid? The portal calls this on load."""
    if not session_auth.verifies_tokens:
        return {"login_required": False, "authenticated": True, "username": None}
    if not session_auth.enabled:
        # Accounts belong to the host application: there is no sign-in form here.
        try:
            return {"login_required": False, "external_auth": True, "authenticated": True,
                    "username": session_auth.verify(x_session_token)}
        except AuthError as exc:
            return {"login_required": False, "external_auth": True, "authenticated": False,
                    "reason": str(exc)}
    try:
        return {"login_required": True, "authenticated": True,
                "username": session_auth.verify(x_session_token)}
    except AuthError as exc:
        return {"login_required": True, "authenticated": False, "reason": str(exc)}


def require_api_key(x_api_key: str | None = Header(default=None),
                    x_session_token: str | None = Header(default=None)) -> None:
    """A signed-in operator, or the API key. Either is enough.

    With neither ``api.api_key`` nor a sign-in password set the API is open. That is a
    deliberate development default, logged loudly at startup so it cannot reach
    production by accident.
    """
    expected = settings.api.api_key
    if expected and x_api_key == expected:
        return
    if session_auth.verifies_tokens:
        try:
            session_auth.verify(x_session_token)
            return
        except AuthError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    if expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


@app.on_event("startup")
def _startup() -> None:
    init_db(settings)
    # Retire cache rows that can never be read again (expired, or written under an older
    # CACHE_VERSION), so the table does not grow forever with answers nobody can use.
    pruner = getattr(graph_manager().cache, "prune", None)
    if callable(pruner) and (dropped := pruner()):
        logger.info("Provider cache: pruned %s stale entries", dropped)
    if session_auth.enabled:
        logger.info("Portal sign-in is ON (user %s, %sh sessions)",
                    session_auth.username, settings.auth.session_hours)
    elif session_auth.verifies_tokens:
        logger.info("Host-issued tokens are ON (signed with SHERLOCKS_AUTH_SECRET); no local sign-in")
    elif not settings.api.api_key:
        logger.warning(
            "Neither SHERLOCKS_AUTH_PASSWORD nor SHERLOCKS_API_KEY is set - this API is "
            "OPEN. Set one before exposing this service beyond localhost."
        )
    if not settings.osint.enabled:
        logger.info("OSINT is disabled (osint.enabled=false). Scans will be refused.")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": settings.app.name, "environment": settings.app.environment}


@app.get(f"{settings.api.prefix}/osint/capabilities", response_model=OsintHealth)
def capabilities(_: None = Depends(require_api_key)) -> OsintHealth:
    """What this deployment can actually do right now."""
    client = OsintClient(settings)
    ollama_reachable = False
    if settings.ollama.ready:
        from sherlocks.agents.ollama import OllamaClient

        with OllamaClient(settings.ollama) as llm:
            ollama_reachable = llm.health()

    return OsintHealth(
        osint_enabled=client.enabled,
        openosint_installed=client.library_available,
        brightdata_configured=settings.osint.brightdata_ready,
        ollama_ready=settings.ollama.ready,
        ollama_reachable=ollama_reachable,
        report_engine_available=report_app_available(settings),
        tools=[ToolCapability(**row) for row in client.capability_report()],
    )


@app.post(f"{settings.api.prefix}/osint/jobs", response_model=JobCreated, status_code=202)
def create_osint_job(request: OsintJobRequest, _: None = Depends(require_api_key)) -> JobCreated:
    if not settings.osint.enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "OSINT is disabled on this deployment. Reaching the public internet on "
                "behalf of an investigation is a policy decision; set osint.enabled."
            ),
        )

    subject = request.to_subject()

    # Say this at submission time, not after a scan that could never have found anything.
    warning = None
    if not any([subject.full_name, subject.phone, subject.email, subject.username]):
        warning = (
            "Only a CNIC was supplied. No OSINT tool can search by CNIC, so this scan "
            "will run nothing. Add a name, phone, email or username."
        )

    with session_scope(settings) as session:
        job = Job(
            job_type="osint",
            status="queued",
            progress_pct=0,
            message="Queued.",
            request=request.model_dump(mode="json"),
        )
        session.add(job)
        session.flush()
        job_id = job.id

    payload = subject.model_dump(mode="json")

    def _worker() -> None:
        with _semaphore:
            run_osint_job(job_id, payload, settings)

    threading.Thread(target=_worker, name=f"osint-job-{job_id}", daemon=True).start()

    return JobCreated(
        job_id=job_id,
        status="queued",
        message="OSINT job accepted.",
        warning=warning,
    )


@app.get(f"{settings.api.prefix}/osint/jobs/{{job_id}}/status", response_model=JobStatus)
def job_status(job_id: str, _: None = Depends(require_api_key)) -> JobStatus:
    with session_scope(settings) as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return JobStatus(
            job_id=job.id,
            job_type=job.job_type,
            status=job.status,
            progress_pct=job.progress_pct,
            message=job.message,
            result_available=bool(job.result_file_path and Path(job.result_file_path).exists()),
            created_at=job.created_at,
            updated_at=job.updated_at,
        )


@app.get(f"{settings.api.prefix}/osint/jobs/{{job_id}}/download")
def download(job_id: str, _: None = Depends(require_api_key)) -> FileResponse:
    with session_scope(settings) as session:
        job = session.get(Job, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if job.status != "completed":
            raise HTTPException(status_code=409, detail=f"Job is {job.status}, not completed")
        path_text = job.result_file_path

    if not path_text or not Path(path_text).exists():
        raise HTTPException(status_code=404, detail="Report file is missing from disk")

    path = Path(path_text)
    return FileResponse(path, media_type="application/pdf", filename=path.name)


def run() -> None:
    """Console-script entrypoint: ``sherlocks-api``."""
    import uvicorn

    uvicorn.run(
        "sherlocks.api.main:app",
        host=settings.api.host,
        port=settings.api.port,
        reload=False,
    )
