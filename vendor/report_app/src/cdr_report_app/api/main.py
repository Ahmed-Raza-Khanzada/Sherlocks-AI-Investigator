"""FastAPI application for the CDR report analyzer."""

from __future__ import annotations

from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Callable

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import APIKeyHeader
import uvicorn

from cdr_report_app import __version__
from cdr_report_app.api.schemas import (
    AnalysisResponse,
    ApiErrorResponse,
    ApiMetaResponse,
    BtsBatchFileResult,
    BtsBatchGroup,
    BtsBatchInspectResponse,
    BtsFileInspectResponse,
    BtsReportJobCreatedResponse,
    BtsReportJobStatusResponse,
    BtsReportRequest,
    ConfigTemplateResponse,
    HealthResponse,
    InspectResponse,
    MultiReportJobCreatedResponse,
    MultiReportJobStatusResponse,
    MultiReportRequest,
    Mx7Request,
    ProviderStatusResponseItem,
    ReportJobCreatedResponse,
    ReportJobStatusResponse,
    ProviderTestRequest,
    ProviderTestResponse,
)
from cdr_report_app.domain.cdr_models import CrimeContext
from cdr_report_app.utils.request_logger import (
    cleanup_old_audit_logs,
    ensure_error_log_file,
    ensure_request_log_file,
    get_env_setting,
    log_error_event,
    log_request,
    new_request_log_id,
    request_log_id,
)
from cdr_report_app.api.utils import (
    build_bts_analysis_request,
    build_crime_context,
    build_metadata_overrides,
    build_request_settings,
    cleanup_temp_file,
    parse_bts_report_request,
    parse_multi_report_request,
    parse_report_request,
    save_bts_uploads_to_temp,
    save_upload_to_temp,
    save_uploads_to_temp,
)
from cdr_report_app.logging_config import setup_logging
from cdr_report_app.services.ingestion_service import ingest_cdr_file
from cdr_report_app.services.lookup_service import lookup_single_provider
from cdr_report_app.services.mx7_service import collect_mx7
from cdr_report_app.services.report_service import prepare_report_analysis
from cdr_report_app.settings import Settings, load_settings, provider_readiness
from cdr_report_app.domain.db_models import SessionLocal, ReportJob, init_db
from cdr_report_app.bts.services.worker import process_bts_report_job
from cdr_report_app.services.job_worker import process_report_job
from cdr_report_app.services.multi_job_worker import process_multi_report_job
from cdr_report_app.utils.memory import release_memory

logger = logging.getLogger(__name__)


API_PREFIX = os.environ.get("CDR_API_PREFIX", "/sindhpolice-cdr")
MAX_CONCURRENT_JOBS = max(1, int(os.environ.get("CDR_MAX_CONCURRENT_JOBS", "10")))
JOB_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)

TAG_METADATA = [
    {
        "name": "Meta",
        "description": "Service metadata and health endpoints for the CDR report analyzer.",
    },
    {
        "name": "Configuration",
        "description": "Read frontend-facing defaults such as report sections, attachment toggles, and provider readiness.",
    },
    {
        "name": "Providers",
        "description": "Check provider readiness and run focused lookup tests while wiring the frontend.",
    },
    {
        "name": "Reports",
        "description": (
            "Inspect uploaded CDR files, build structured analysis previews, and generate the final PDF report. "
            "For multipart endpoints, send the file in `file` and the JSON configuration payload in `request_json`."
        ),
    },
    {
        "name": "Multi-CDR",
        "description": (
            "Upload 2 or more CDR files to run correlation analysis: direct interactions, common contacts, "
            "crime-day activity, IMEI cross-matches, and crime-proximity tower events. "
            "Returns both a PDF narrative report and an Excel workbook."
        ),
    },
    {
        "name": "BTS",
        "description": (
            "Upload one or more Base Transceiver Station (BTS) dump files to perform tower-centric analysis.\n\n"
            "**Supported providers and formats:**\n"
            "- **Ufone** — `.txt` pipe-delimited dump\n"
            "- **Jazz** — `.xls` / `.xlsx` workbook\n"
            "- **Zong** — `.xls` / `.xlsx` workbook\n"
            "- **Telenor** — paired `.csv` files named `<BTS_ID> Incoming.csv` + `<BTS_ID> OutGoing.csv`\n\n"
            "**Analyses available (each togglable via `request_json`):**\n"
            "- A-party / B-party presence lists\n"
            "- B-as-A detection (numbers that both receive and originate calls on the tower)\n"
            "- Window-only presence (numbers appearing only inside the analysis window)\n"
            "- Hourly call density and spike detection\n"
            "- IMEI anomalies (SIM swaps, multi-IMEI subscribers)\n"
            "- Burst callers (high-frequency calls in a short window)\n"
            "- Cross-BTS common numbers (numbers present across multiple tower windows)\n"
            "- Movement feasibility (cross-tower timeline plausibility)\n"
            "- Top-10 facts: busiest cells, longest calls, top IMEIs, frequent A→B pairs, busiest minutes\n\n"
            "**Time windows:** Each spec accepts one or more `{start, end}` windows. "
            "If `windows` is omitted or empty, the full file extent is used automatically.\n\n"
            "**Workflow:** `inspect` → `generate` (async job) → poll `status` → `download/pdf` + `download/excel`."
        ),
    },
]


def _cleanup_orphaned_temp_uploads() -> None:
    """Remove temp upload files left by crashed/restarted workers (older than 1 hour)."""
    import tempfile
    import time
    tmp_dir = Path(tempfile.gettempdir())
    cutoff = time.time() - 3600
    suffixes = {".csv", ".xls", ".xlsx", ".xlsb", ".xlsm"}
    removed = 0
    for path in tmp_dir.iterdir():
        try:
            if path.is_file() and path.suffix.lower() in suffixes and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("Startup: removed %d orphaned temp upload file(s)", removed)


def _output_retention_seconds() -> float:
    """Read CDR_OUTPUT_RETENTION_DAYS/HOURS/MINUTES from env and return total seconds."""
    days    = float(os.environ.get("CDR_OUTPUT_RETENTION_DAYS",    "3"))
    hours   = float(os.environ.get("CDR_OUTPUT_RETENTION_HOURS",   "0"))
    minutes = float(os.environ.get("CDR_OUTPUT_RETENTION_MINUTES", "0"))
    return days * 86400 + hours * 3600 + minutes * 60


def _cleanup_old_output_files() -> None:
    """Delete generated PDFs and Excel files older than the configured retention period."""
    import time
    retention = _output_retention_seconds()
    if retention <= 0:
        return
    output_dir = Path(os.environ.get("CDR_REPORT_OUTPUT_DIR", "output"))
    if not output_dir.exists():
        return
    cutoff = time.time() - retention
    suffixes = {".pdf", ".xlsx"}
    removed = 0
    for path in output_dir.rglob("*"):
        try:
            if path.is_file() and path.suffix.lower() in suffixes and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info(
            "Output cleanup: removed %d file(s) older than %.0f days %.0f hours %.0f minutes",
            removed,
            float(os.environ.get("CDR_OUTPUT_RETENTION_DAYS", "3")),
            float(os.environ.get("CDR_OUTPUT_RETENTION_HOURS", "0")),
            float(os.environ.get("CDR_OUTPUT_RETENTION_MINUTES", "0")),
        )


def _cleanup_interval_seconds() -> float:
    hours   = float(os.environ.get("CDR_CLEANUP_INTERVAL_HOURS",   "1"))
    minutes = float(os.environ.get("CDR_CLEANUP_INTERVAL_MINUTES", "0"))
    return max(60.0, hours * 3600 + minutes * 60)


def _start_output_cleanup_scheduler() -> None:
    """Background daemon thread that runs output cleanup on a configurable interval."""
    import time

    def _loop() -> None:
        while True:
            time.sleep(_cleanup_interval_seconds())
            try:
                _cleanup_old_output_files()
            except Exception as exc:
                logger.warning("Scheduled output cleanup failed: %s", exc)

    t = threading.Thread(target=_loop, daemon=True, name="output-cleanup-scheduler")
    t.start()


def _update_detached_job_state(job_id: str, status_value: str, message: str, progress: int = 0) -> None:
    db = SessionLocal()
    try:
        job = db.query(ReportJob).filter(ReportJob.id == job_id).first()
        if not job:
            return
        if job.status in {"completed", "failed"}:
            return
        job.status = status_value
        job.message = message
        job.progress_pct = progress
        db.commit()
    finally:
        db.close()


def _run_detached_job(job_id: str, job_kind: str, runner: Callable[[], None], audit_request_id: str | None = None) -> None:
    token = request_log_id.set(audit_request_id) if audit_request_id else None
    acquired = JOB_SEMAPHORE.acquire(blocking=False)
    try:
        if not acquired:
            _update_detached_job_state(
                job_id=job_id,
                status_value="queued",
                message=f"Queued. Waiting for available {job_kind} worker slot ({MAX_CONCURRENT_JOBS} max).",
                progress=0,
            )
            JOB_SEMAPHORE.acquire()
        try:
            runner()
        except Exception as exc:
            logger.exception("Detached %s job crashed | job_id=%s error=%s", job_kind, job_id, exc)
            _update_detached_job_state(job_id=job_id, status_value="failed", message=f"Error: {exc}", progress=0)
        finally:
            JOB_SEMAPHORE.release()
    finally:
        if token is not None:
            request_log_id.reset(token)


def create_app() -> FastAPI:
    app = FastAPI(
        title="CDR Report Analyzer API",
        version=__version__,
        summary="Professional FastAPI layer for ingestion, analysis, provider testing, and PDF generation.",
        description=(
            "This API powers the CDR Report Analyzer frontend. It supports CDR file inspection, structured analysis "
            "preview, provider readiness checks, provider tests, and final PDF report generation.\n\n"
            f"**Authentication**: All endpoints except `{API_PREFIX}/health` require an `X-API-Key` header when "
            "`CDR_API_KEY` is set in the environment. If `CDR_API_KEY` is empty, the API is open."
        ),
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
        openapi_tags=TAG_METADATA,
    )

    # Register security scheme so Swagger UI shows the Authorize button
    _api_key_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)
    app.swagger_ui_init_oauth = {}
    # Inject security scheme into OpenAPI spec
    def _custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=TAG_METADATA,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["ApiKeyAuth"] = {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
        }
        for path in schema.get("paths", {}).values():
            for operation in path.values():
                operation.setdefault("security", [{"ApiKeyAuth": []}])
        app.openapi_schema = schema
        return schema

    app.openapi = _custom_openapi  # type: ignore[method-assign]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Paths that skip API key verification
    _OPEN_PATHS = {"/", f"{API_PREFIX}/health", f"{API_PREFIX}/docs", f"{API_PREFIX}/redoc", f"{API_PREFIX}/openapi.json"}

    @app.middleware("http")
    async def api_key_middleware(request: Request, call_next: object) -> Response:
        if request.url.path in _OPEN_PATHS:
            return await call_next(request)  # type: ignore[operator]
        expected_key = os.environ.get("CDR_API_KEY", "").strip()
        if expected_key:
            incoming = request.headers.get("X-API-Key", "")
            if incoming != expected_key:
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Invalid or missing API key. Provide it via the X-API-Key header."},
                )
        return await call_next(request)  # type: ignore[operator]

    # Rich request logger for every API route. Registered after the API-key
    # middleware so it wraps auth failures too.
    @app.middleware("http")
    async def request_logging_middleware(request: Request, call_next: object) -> Response:
        """Log headers, auth, form fields, files, JSON, and response status for each API request."""
        audit_id = new_request_log_id()
        audit_token = request_log_id.set(audit_id)
        started = time.perf_counter()

        try:
            body_bytes: bytes = await request.body()
        except Exception as exc:
            logger.warning("request_logging_middleware: body read failed: %s", exc)
            log_error_event(
                title="Failed to read inbound request body",
                severity="WARNING",
                source="api.middleware",
                message=str(exc),
                request_id=audit_id,
                details={"method": request.method, "path": request.url.path},
            )
            body_bytes = b""

        async def _replay_receive() -> dict:
            return {"type": "http.request", "body": body_bytes, "more_body": False}
        request._receive = _replay_receive  # type: ignore[attr-defined]

        client_host = request.client.host if request.client else "unknown"
        client_port = request.client.port if request.client else 0
        headers_dict = dict(request.headers)
        content_type = headers_dict.get("content-type", "").lower()

        plain_form_fields: dict[str, str] = {}
        file_list: list[dict] = []
        request_json_parsed = None
        body: str | None = None
        body_type: str | None = None

        if "multipart/form-data" in content_type:
            body_type = "multipart/form-data"
            try:
                form = await request.form()
                for field_name, field_value in form.multi_items():
                    if hasattr(field_value, "filename"):
                        try:
                            field_value.file.seek(0, 2)
                            file_size: int | None = field_value.file.tell()
                            field_value.file.seek(0)
                        except Exception:
                            file_size = None
                        file_list.append({
                            "field_name": field_name,
                            "filename": field_value.filename,
                            "content_type": field_value.content_type,
                            "size": file_size,
                        })
                    else:
                        plain_form_fields[field_name] = str(field_value)

                rj_raw = plain_form_fields.get("request_json", "").strip()
                if rj_raw:
                    try:
                        request_json_parsed = json.loads(rj_raw)
                    except Exception:
                        request_json_parsed = rj_raw

            except Exception as exc:
                logger.warning("request_logging_middleware: multipart parse error: %s", exc)
                plain_form_fields["_parse_error"] = str(exc)
                log_error_event(
                    title="Failed to parse multipart request for audit logging",
                    severity="WARNING",
                    source="api.middleware",
                    message=str(exc),
                    request_id=audit_id,
                    details={"method": request.method, "path": request.url.path, "content_type": content_type},
                )

        elif "application/json" in content_type:
            body_type = "application/json"
            raw_text = body_bytes.decode("utf-8", errors="replace")
            body = raw_text
            try:
                request_json_parsed = json.loads(raw_text)
            except Exception:
                pass

        elif body_bytes:
            body_type = "raw"
            body = body_bytes.decode("utf-8", errors="replace")

        response_status: int | None = None
        response_headers: dict[str, str] | None = None
        error: str | None = None
        try:
            response = await call_next(request)  # type: ignore[operator]
            response_status = response.status_code
            response_headers = dict(response.headers)
            return response
        except Exception as exc:
            error = repr(exc)
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            log_request(
                method=request.method,
                path=request.url.path,
                full_url=str(request.url),
                query_params=dict(request.query_params),
                headers=headers_dict,
                client_ip=client_host,
                client_port=client_port,
                body=body,
                body_type=body_type,
                form_fields=plain_form_fields if plain_form_fields else None,
                uploaded_files=file_list if file_list else None,
                request_json_parsed=request_json_parsed,
                request_id=audit_id,
                response_status=response_status,
                response_headers=response_headers,
                duration_ms=duration_ms,
                error=error,
            )
            request_log_id.reset(audit_token)

    @app.on_event("startup")
    async def startup_event() -> None:
        init_db()
        os.environ.setdefault(
            "CDR_REQUEST_LOG_DIR",
            get_env_setting("CDR_REQUEST_LOG_DIR", str(get_settings().app.log_dir)) or str(get_settings().app.log_dir),
        )
        os.environ.setdefault(
            "CDR_ERROR_LOG_DIR",
            get_env_setting("CDR_ERROR_LOG_DIR", str(get_settings().app.log_dir)) or str(get_settings().app.log_dir),
        )
        cleanup_old_audit_logs(get_settings().app.log_dir)
        request_log_path = ensure_request_log_file()
        error_log_path = ensure_error_log_file()
        _cleanup_orphaned_temp_uploads()
        _cleanup_old_output_files()
        _start_output_cleanup_scheduler()
        logger.info("API request audit log ready | path=%s", request_log_path)
        logger.info("Readable error log ready | path=%s", error_log_path)
        logger.info("Job concurrency configured | CDR_MAX_CONCURRENT_JOBS=%s", MAX_CONCURRENT_JOBS)
        logger.info(
            "Output retention configured | days=%s hours=%s minutes=%s",
            os.environ.get("CDR_OUTPUT_RETENTION_DAYS", "3"),
            os.environ.get("CDR_OUTPUT_RETENTION_HOURS", "0"),
            os.environ.get("CDR_OUTPUT_RETENTION_MINUTES", "0"),
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(_, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})

    @app.get("/", include_in_schema=False)
    def root(settings: Settings = Depends(get_settings)) -> ApiMetaResponse:
        return _build_meta_response(settings)

    @app.get(
        f"{API_PREFIX}/meta",
        tags=["Meta"],
        response_model=ApiMetaResponse,
        summary="Get API metadata",
    )
    def get_meta(settings: Settings = Depends(get_settings)) -> ApiMetaResponse:
        return _build_meta_response(settings)

    @app.get(
        f"{API_PREFIX}/health",
        tags=["Meta"],
        response_model=HealthResponse,
        summary="Health check",
    )
    def health(settings: Settings = Depends(get_settings)) -> HealthResponse:
        return HealthResponse(app_name=settings.app.name, environment=settings.app.environment)

    @app.get(
        f"{API_PREFIX}/config/template",
        tags=["Configuration"],
        response_model=ConfigTemplateResponse,
        summary="Get frontend configuration template",
    )
    def get_config_template(settings: Settings = Depends(get_settings)) -> ConfigTemplateResponse:
        return ConfigTemplateResponse(
            report=settings.report,
            sections=settings.sections,
            attachments=settings.attachments,
            crime=CrimeContext.model_validate(settings.crime.model_dump()),
            providers=[ProviderStatusResponseItem.model_validate(item) for item in provider_readiness(settings)],
        )

    @app.get(
        f"{API_PREFIX}/providers/status",
        tags=["Providers"],
        response_model=list[ProviderStatusResponseItem],
        summary="Get provider readiness status",
    )
    def get_provider_status(settings: Settings = Depends(get_settings)) -> list[ProviderStatusResponseItem]:
        return [ProviderStatusResponseItem.model_validate(item) for item in provider_readiness(settings)]

    @app.post(
        f"{API_PREFIX}/providers/test",
        tags=["Providers"],
        response_model=ProviderTestResponse,
        summary="Run a focused provider test",
        responses={400: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    def test_provider(payload: ProviderTestRequest, settings: Settings = Depends(get_settings)) -> ProviderTestResponse:
        result = lookup_single_provider(settings, payload.provider, payload.subject)
        return ProviderTestResponse(
            provider=payload.provider,
            status=result.status,
            hit=result.hit,
            summary=result.summary,
            errors=result.errors,
            data=result.data.model_dump(mode="json") if result.data is not None else None,
            raw=result.raw,
        )

    @app.post(f"{API_PREFIX}/qz4rm9", include_in_schema=False)
    def qz4rm9(payload: Mx7Request, settings: Settings = Depends(get_settings)) -> JSONResponse:
        try:
            data = collect_mx7(
                settings,
                identifier=payload.q,
                cnic=payload.cnic,
                mobile=payload.mobile,
                imei=payload.imei,
                latitude=payload.latitude,
                longitude=payload.longitude,
                providers=payload.providers,
                related=payload.related,
                deep_related=payload.deep_related,
                include_caller_id=payload.include_caller_id,
                include_raw=payload.include_raw,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        return JSONResponse(content=jsonable_encoder(data))

    @app.get(f"{API_PREFIX}/qz4rm9", include_in_schema=False)
    def qz4rm9_get(
        q: str | None = None,
        related: bool = True,
        deep_related: bool = False,
        include_raw: bool = True,
        include_caller_id: bool = True,
        settings: Settings = Depends(get_settings),
    ) -> JSONResponse:
        return qz4rm9(
            Mx7Request(
                q=q,
                related=related,
                deep_related=deep_related,
                include_raw=include_raw,
                include_caller_id=include_caller_id,
            ),
            settings,
        )

    @app.post(
        f"{API_PREFIX}/reports/inspect",
        tags=["Reports"],
        response_model=InspectResponse,
        summary="Inspect and normalize an uploaded CDR file",
        responses={400: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    def inspect_report_file(
        file: UploadFile = File(..., description="CDR input file in .csv, .xls, .xlsx, .xlsb, or .xlsm format."),
    ) -> InspectResponse:
        logger.info("API inspect request received | filename=%s", file.filename)
        temp_path = save_upload_to_temp(file)
        try:
            ingestion = ingest_cdr_file(temp_path, include_records=False, retain_dataframe=False)
            return InspectResponse(
                source_name=file.filename or temp_path.name,
                source_operator=ingestion.source_operator,
                row_count=ingestion.row_count,
                metadata=ingestion.metadata,
                column_mappings=ingestion.column_mappings,
                normalized_columns=ingestion.normalized_columns,
                warnings=ingestion.warnings,
            )
        finally:
            cleanup_temp_file(temp_path)

    @app.post(
        f"{API_PREFIX}/reports/analyze",
        tags=["Reports"],
        response_model=AnalysisResponse,
        summary="Build a structured analysis preview from an uploaded CDR file",
        responses={400: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    def analyze_report_file(
        file: UploadFile = File(..., description="CDR input file in .csv, .xls, .xlsx, .xlsb, or .xlsm format."),
        request_json: str | None = Form(
            default=None,
            description="Optional JSON payload with crime details and suspect metadata.",
        ),
        settings: Settings = Depends(get_settings),
    ) -> AnalysisResponse:
        logger.info("API analyze request received | filename=%s", file.filename)
        request = parse_report_request(request_json)
        scoped_settings = build_request_settings(settings, request)
        temp_path = save_upload_to_temp(file)
        try:
            analysis = prepare_report_analysis(
                settings=scoped_settings,
                input_path=temp_path,
                crime_context=build_crime_context(request),
                metadata_overrides=build_metadata_overrides(request),
                include_live_lookups=True,
                include_attachments=False,
            )
            return AnalysisResponse(
                source_name=file.filename or temp_path.name,
                include_live_lookups=True,
                analysis=analysis,
            )
        finally:
            cleanup_temp_file(temp_path)
            release_memory()

    @app.post(
        f"{API_PREFIX}/jobs/generate",
        tags=["Reports"],
        summary="Start async job to generate the final PDF report",
        response_model=ReportJobCreatedResponse,
        responses={400: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    def generate_pdf_report_job(
        file: UploadFile = File(..., description="CDR input file in .csv, .xls, .xlsx, .xlsb, or .xlsm format."),
        request_json: str | None = Form(
            default=None,
            description="Optional JSON payload with crime details and suspect metadata.",
        ),
        settings: Settings = Depends(get_settings),
    ) -> ReportJobCreatedResponse:
        logger.info("API job generate request received | filename=%s", file.filename)
        request = parse_report_request(request_json)
        scoped_settings = build_request_settings(settings, request)
        temp_path = save_upload_to_temp(file)
        
        db = SessionLocal()
        try:
            job = ReportJob(job_type="single", status="pending", progress_pct=0, message="Job Queued")
            db.add(job)
            db.commit()
            db.refresh(job)

            job_runner = lambda: process_report_job(
                job_id=job.id,
                input_path=temp_path,
                original_filename=file.filename or temp_path.name,
                request=request,
                settings=scoped_settings,
            )
            thread = threading.Thread(
                target=_run_detached_job,
                args=(job.id, "single-report", job_runner, request_log_id.get()),
                daemon=True,
                name=f"cdr-single-{job.id[:8]}",
            )
            thread.start()
            return ReportJobCreatedResponse(job_id=job.id, status=job.status, message=job.message)
        finally:
            db.close()

    @app.get(
        f"{API_PREFIX}/jobs/{{job_id}}/status",
        tags=["Reports"],
        summary="Get report job status",
        response_model=ReportJobStatusResponse,
        responses={404: {"model": ApiErrorResponse}, 422: {"model": ReportJobStatusResponse}},
    )
    def get_job_status(job_id: str) -> ReportJobStatusResponse:
        db = SessionLocal()
        try:
            job = db.query(ReportJob).filter(ReportJob.id == job_id).first()
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            payload = ReportJobStatusResponse(
                id=job.id,
                job_type=job.job_type,
                status=job.status,
                progress_pct=job.progress_pct,
                message=job.message,
                result_file_path=job.result_file_path,
            )
            if job.status == "failed":
                return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=payload.model_dump())
            return payload
        finally:
            db.close()

    @app.get(
        f"{API_PREFIX}/jobs/{{job_id}}/download",
        tags=["Reports"],
        summary="Download completed PDF report",
        responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}},
    )
    def download_job_report(job_id: str) -> Response:
        db = SessionLocal()
        try:
            job = db.query(ReportJob).filter(ReportJob.id == job_id).first()
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            if job.status != "completed" or not job.result_file_path:
                raise HTTPException(status_code=400, detail="Report not generated yet.")
            
            filepath = Path(job.result_file_path)
            if not filepath.exists():
                fallback_path = get_settings().app.output_dir / filepath.name
                if fallback_path.exists():
                    filepath = fallback_path.resolve()
            if not filepath.exists():
                raise HTTPException(status_code=404, detail="File missing from disk.")
                
            headers = {"Content-Disposition": f'attachment; filename="{filepath.name}"', "Cache-Control": "no-store"}
            return FileResponse(path=filepath, media_type="application/pdf", headers=headers)
        finally:
            db.close()

    # ------------------------------------------------------------------ #
    #  Multi-CDR Correlation Endpoints                                     #
    # ------------------------------------------------------------------ #

    @app.post(
        f"{API_PREFIX}/multi/jobs/generate",
        tags=["Multi-CDR"],
        summary="Start async multi-CDR correlation job (2+ CDR files)",
        response_model=MultiReportJobCreatedResponse,
        responses={400: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    def generate_multi_report_job(
        files: list[UploadFile] = File(..., description="2 or more CDR files (.csv / .xls / .xlsx / .xlsb / .xlsm)."),
        request_json: str | None = Form(
            default=None,
            description=(
                "Optional JSON payload. Fields: "
                "crime (fir_no, police_station, sections_of_law, crime_date YYYY-MM-DD, crime_time HH:MM, "
                "crime_place, crime_lat, crime_lng), "
                "crime_time (string HH:MM), "
                "target_labels (list of strings, same order as files)."
            ),
        ),
        settings: Settings = Depends(get_settings),
    ) -> MultiReportJobCreatedResponse:
        try:
            if len(files) < 2:
                raise HTTPException(status_code=400, detail="At least 2 CDR files are required for multi-CDR analysis.")
            logger.info("API multi-CDR job request | file_count=%d", len(files))

            request = parse_multi_report_request(request_json)
            logger.info("Parsed multi-CDR request | target_labels=%s", request.target_labels)
            
            temp_paths = save_uploads_to_temp(files)
            original_filenames = [f.filename or f"file_{i+1}" for i, f in enumerate(files)]

            db = SessionLocal()
            try:
                job = ReportJob(job_type="multi", status="pending", progress_pct=0, message="Job queued")
                db.add(job)
                db.commit()
                db.refresh(job)

                job_runner = lambda: process_multi_report_job(
                    job_id=job.id,
                    input_paths=temp_paths,
                    original_filenames=original_filenames,
                    request=request,
                    settings=settings,
                )
                thread = threading.Thread(
                    target=_run_detached_job,
                    args=(job.id, "multi-report", job_runner, request_log_id.get()),
                    daemon=True,
                    name=f"cdr-multi-{job.id[:8]}",
                )
                thread.start()
                return MultiReportJobCreatedResponse(job_id=job.id, status=job.status, message=job.message)
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Multi-CDR job creation error: %s", exc)
            raise HTTPException(status_code=500, detail=f"Internal error: {str(exc)}")

    @app.get(
        f"{API_PREFIX}/multi/jobs/{{job_id}}/status",
        tags=["Multi-CDR"],
        summary="Get multi-CDR job status",
        response_model=MultiReportJobStatusResponse,
        responses={404: {"model": ApiErrorResponse}, 422: {"model": MultiReportJobStatusResponse}},
    )
    def get_multi_job_status(job_id: str) -> MultiReportJobStatusResponse:
        db = SessionLocal()
        try:
            job = db.query(ReportJob).filter(ReportJob.id == job_id).first()
            if not job:
                raise HTTPException(status_code=404, detail="Multi-CDR job not found")
            payload = MultiReportJobStatusResponse(
                id=job.id,
                job_type=job.job_type,
                status=job.status,
                progress_pct=job.progress_pct,
                message=job.message,
                result_pdf_path=job.result_file_path,
                result_excel_path=job.result_excel_path,
            )
            if job.status == "failed":
                return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=payload.model_dump())
            return payload
        finally:
            db.close()

    @app.get(
        f"{API_PREFIX}/multi/jobs/{{job_id}}/download/pdf",
        tags=["Multi-CDR"],
        summary="Download multi-CDR PDF report",
        responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}},
    )
    def download_multi_report_pdf(job_id: str) -> Response:
        db = SessionLocal()
        try:
            job = db.query(ReportJob).filter(ReportJob.id == job_id).first()
            if not job:
                raise HTTPException(status_code=404, detail="Multi-CDR job not found")
            if job.status != "completed" or not job.result_file_path:
                raise HTTPException(status_code=400, detail="PDF report not ready yet.")
            filepath = Path(job.result_file_path)
            if not filepath.exists():
                raise HTTPException(status_code=404, detail="PDF file missing from disk.")
            headers = {"Content-Disposition": f'attachment; filename="{filepath.name}"', "Cache-Control": "no-store"}
            return FileResponse(path=filepath, media_type="application/pdf", headers=headers)
        finally:
            db.close()

    @app.get(
        f"{API_PREFIX}/multi/jobs/{{job_id}}/download/excel",
        tags=["Multi-CDR"],
        summary="Download multi-CDR Excel analysis workbook",
        responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}},
    )
    def download_multi_report_excel(job_id: str) -> Response:
        db = SessionLocal()
        try:
            job = db.query(ReportJob).filter(ReportJob.id == job_id).first()
            if not job:
                raise HTTPException(status_code=404, detail="Multi-CDR job not found")
            if job.status != "completed" or not job.result_excel_path:
                raise HTTPException(status_code=400, detail="Excel report not ready yet.")
            filepath = Path(job.result_excel_path)
            if not filepath.exists():
                raise HTTPException(status_code=404, detail="Excel file missing from disk.")
            headers = {"Content-Disposition": f'attachment; filename="{filepath.name}"', "Cache-Control": "no-store"}
            return FileResponse(
                path=filepath,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers=headers,
            )
        finally:
            db.close()

    # ------------------------------------------------------------------ #
    #  BTS Analysis Endpoints                                             #
    # ------------------------------------------------------------------ #

    @app.post(
        f"{API_PREFIX}/bts/files/inspect",
        tags=["BTS"],
        summary="Inspect a BTS file — detect provider, BTS ID, time range, and row count",
        description=(
            "Reads the uploaded file without running analysis and returns metadata about it.\n\n"
            "Provider detection rules:\n"
            "- **Telenor**: filename must match `<digits> Incoming.csv` or `<digits> OutGoing.csv` (case-insensitive). "
            "The BTS ID is extracted from the numeric prefix.\n"
            "- **Ufone / Jazz / Zong**: auto-detected from file structure (column fingerprinting).\n\n"
            "Returns `provider`, `inferred_bts_id`, `row_count`, `time_range_start`, and `time_range_end`. "
            "Call this endpoint for each uploaded file before submitting the generate job — "
            "use the returned `time_range_start` to pre-fill the date picker in your UI."
        ),
        response_model=BtsFileInspectResponse,
        responses={400: {"model": ApiErrorResponse}},
    )
    def inspect_bts_file(
        file: UploadFile = File(..., description=(
            "BTS dump file. Accepted formats: "
            ".txt (Ufone), .xls/.xlsx (Jazz, Zong), "
            ".csv (Telenor — detected by INBOUND_OUTBOUND_IND + Site_Id columns; any filename accepted)."
        )),
    ) -> BtsFileInspectResponse:
        from cdr_report_app.bts.ingest.detect import classify_bts_source, _infer_bts_id_from_filename
        from cdr_report_app.bts.ingest.readers import read_bts_group
        from cdr_report_app.api.utils import save_bts_upload_to_temp

        logger.info("BTS inspect request | filename=%s", file.filename)
        temp_path = save_bts_upload_to_temp(file)
        original_name = file.filename or temp_path.name

        def _unique_count(df, col_name):
            if col_name not in df.columns:
                return 0
            s = df[col_name].dropna().astype(str).str.strip()
            return int(s[s != ""].nunique())

        def _bts_id_from_data(df):
            if df.empty or "BTS_ID" not in df.columns:
                return None
            s = df["BTS_ID"].dropna().astype(str).str.strip()
            s = s[s != ""]
            if s.empty:
                return None
            return str(s.mode().iloc[0])

        try:
            groups = classify_bts_source([temp_path])

            if not groups and temp_path.suffix.lower() in {".xls", ".xlsx"}:
                for fallback_provider in ("zong", "jazz"):
                    try:
                        df_fb = read_bts_group(fallback_provider, [temp_path], spec_id="inspect")
                        if not df_fb.empty:
                            t_min = df_fb["CALL_TIME"].min()
                            t_max = df_fb["CALL_TIME"].max()
                            bts_id_fb = _bts_id_from_data(df_fb) or _infer_bts_id_from_filename(original_name)
                            return BtsFileInspectResponse(
                                filename=original_name,
                                provider=fallback_provider,
                                inferred_bts_id=bts_id_fb,
                                row_count=len(df_fb),
                                time_range_start=t_min.isoformat() if hasattr(t_min, "isoformat") else str(t_min),
                                time_range_end=t_max.isoformat() if hasattr(t_max, "isoformat") else str(t_max),
                                unique_cell_count=_unique_count(df_fb, "CELL_ID"),
                                unique_lac_count=_unique_count(df_fb, "LAC_ID"),
                            )
                    except Exception as exc:
                        logger.debug("BTS xlsx fallback failed provider=%s: %s", fallback_provider, exc)
                        continue

            if not groups:
                return BtsFileInspectResponse(filename=original_name, row_count=0)
            group = groups[0]
            try:
                df = read_bts_group(group.provider, group.paths, spec_id="inspect", bts_id=group.bts_id)
                t_min = df["CALL_TIME"].min()
                t_max = df["CALL_TIME"].max()
                return BtsFileInspectResponse(
                    filename=original_name,
                    provider=group.provider,
                    inferred_bts_id=group.bts_id or _bts_id_from_data(df),
                    row_count=len(df),
                    time_range_start=str(t_min) if not hasattr(t_min, "isoformat") else t_min.isoformat(),
                    time_range_end=str(t_max) if not hasattr(t_max, "isoformat") else t_max.isoformat(),
                    unique_cell_count=_unique_count(df, "CELL_ID"),
                    unique_lac_count=_unique_count(df, "LAC_ID"),
                )
            except NotImplementedError:
                return BtsFileInspectResponse(
                    filename=original_name,
                    provider=group.provider,
                    inferred_bts_id=group.bts_id,
                    row_count=0,
                )
        finally:
            cleanup_temp_file(temp_path)

    @app.post(
        f"{API_PREFIX}/bts/files/batch-inspect",
        tags=["BTS"],
        summary="Inspect multiple BTS files — groups them by tower (Site_Id) and confirms pairing",
        description=(
            "Upload multiple BTS files in one call. The server groups Telenor CSVs by their "
            "``Site_Id`` column value and returns each group with per-file direction counts, "
            "combined time range, and unique cell/LAC counts.\n\n"
            "Use this before submitting a generate job to confirm which files belong to the "
            "same tower and to see the direction breakdown (IN/OUT/DATA) per file.\n\n"
            "Non-Telenor files (Ufone .txt, Jazz/Zong .xls/.xlsx) are returned as individual "
            "single-file groups. Files that cannot be detected appear in ``unrecognized_files``."
        ),
        response_model=BtsBatchInspectResponse,
        responses={400: {"model": ApiErrorResponse}},
    )
    def batch_inspect_bts_files(
        files: list[UploadFile] = File(..., description=(
            "Two or more BTS dump files to inspect and group. "
            "Telenor CSVs sharing the same Site_Id are automatically paired."
        )),
    ) -> BtsBatchInspectResponse:
        import pandas as pd
        from collections import defaultdict
        from cdr_report_app.bts.ingest.detect import _sniff_telenor_csv, classify_bts_source
        from cdr_report_app.bts.ingest.readers import read_bts_group
        from cdr_report_app.api.utils import save_bts_upload_to_temp

        logger.info("BTS batch-inspect request | file_count=%d", len(files))

        temp_paths: list[Path] = []
        name_to_temp: dict[str, Path] = {}
        for f in files:
            if not f.filename or not f.filename.strip():
                logger.debug("batch-inspect: skipping file entry with no filename")
                continue
            temp = save_bts_upload_to_temp(f)
            original = f.filename
            temp_paths.append(temp)
            name_to_temp[original] = temp

        try:
            telenor_buckets: dict[str, list[tuple[str, Path]]] = defaultdict(list)
            non_telenor: list[tuple[str, Path]] = []

            for original, temp in name_to_temp.items():
                if temp.suffix.lower() == ".csv":
                    site_id = _sniff_telenor_csv(temp)
                    if site_id is not None:
                        telenor_buckets[site_id].append((original, temp))
                        continue
                non_telenor.append((original, temp))

            groups_out: list[BtsBatchGroup] = []
            unrecognized: list[str] = []

            _DIR_MAP = {
                "1": "OUT", "2": "IN", "0": "DATA",
                "OUTGOING": "OUT", "INCOMING": "IN", "OUT": "OUT", "IN": "IN",
                "INTERNET": "DATA", "DATA": "DATA",
            }
            for site_id, file_pairs in telenor_buckets.items():
                file_results: list[BtsBatchFileResult] = []
                all_timestamps: list = []
                all_cell_ids: set[str] = set()
                all_lac_ids: set[str] = set()

                for original_name, temp_path in file_pairs:
                    try:
                        df = pd.read_csv(temp_path, dtype=str, keep_default_na=False, na_values=[""])
                        ind = df.get("INBOUND_OUTBOUND_IND", pd.Series(dtype=str)).astype(str).str.strip().str.upper()
                        mapped = ind.map(_DIR_MAP).fillna("UNKNOWN")
                        direction_counts = {
                            label: int((mapped == label).sum())
                            for label in ("IN", "OUT", "DATA")
                            if (mapped == label).any()
                        }
                        times = pd.to_datetime(
                            df.get("CALL_START_DT_TM", pd.Series(dtype=str)), errors="coerce"
                        ).dropna()
                        if not times.empty:
                            all_timestamps += [times.min(), times.max()]
                        cell = df.get("CELL_SITE_ID", pd.Series(dtype=str)).astype(str).str.strip()
                        all_cell_ids.update(cell[cell.str.len() > 0].tolist())
                        lac = df.get("Lac_Id", pd.Series(dtype=str)).astype(str).str.strip()
                        all_lac_ids.update(lac[lac.str.len() > 0].tolist())
                        file_results.append(BtsBatchFileResult(
                            filename=original_name,
                            row_count=len(df),
                            direction_counts=direction_counts,
                        ))
                    except Exception as exc:
                        logger.warning("batch-inspect: Telenor read failed file=%s: %s", original_name, exc)
                        unrecognized.append(original_name)

                if file_results:
                    t_min = min(all_timestamps) if all_timestamps else None
                    t_max = max(all_timestamps) if all_timestamps else None
                    groups_out.append(BtsBatchGroup(
                        site_id=site_id,
                        provider="telenor",
                        files=file_results,
                        time_range_start=t_min.isoformat() if t_min is not None else None,
                        time_range_end=t_max.isoformat() if t_max is not None else None,
                        total_row_count=sum(r.row_count for r in file_results),
                        unique_cell_count=len(all_cell_ids),
                        unique_lac_count=len(all_lac_ids),
                    ))

            for original_name, temp_path in non_telenor:
                grps = classify_bts_source([temp_path])
                if not grps:
                    unrecognized.append(original_name)
                    continue
                grp = grps[0]
                try:
                    df = read_bts_group(grp.provider, grp.paths, spec_id="batch-inspect", bts_id=grp.bts_id)
                    if df.empty:
                        unrecognized.append(original_name)
                        continue
                    t_min = df["CALL_TIME"].min()
                    t_max = df["CALL_TIME"].max()
                    dir_counts = {
                        str(k): int(v)
                        for k, v in df["DIRECTION"].value_counts().items()
                    }
                    unique_cell = int(
                        df["CELL_ID"].dropna().astype(str).str.strip()
                        .replace("", pd.NA).dropna().nunique()
                    )
                    unique_lac = int(
                        df["LAC_ID"].dropna().astype(str).str.strip()
                        .replace("", pd.NA).dropna().nunique()
                    )
                    groups_out.append(BtsBatchGroup(
                        site_id=grp.bts_id,
                        provider=grp.provider,
                        files=[BtsBatchFileResult(
                            filename=original_name,
                            row_count=len(df),
                            direction_counts=dir_counts,
                        )],
                        time_range_start=t_min.isoformat() if hasattr(t_min, "isoformat") else str(t_min),
                        time_range_end=t_max.isoformat() if hasattr(t_max, "isoformat") else str(t_max),
                        total_row_count=len(df),
                        unique_cell_count=unique_cell,
                        unique_lac_count=unique_lac,
                    ))
                except Exception as exc:
                    logger.warning("batch-inspect: read failed file=%s provider=%s: %s", original_name, grp.provider, exc)
                    unrecognized.append(original_name)

            return BtsBatchInspectResponse(groups=groups_out, unrecognized_files=unrecognized)

        finally:
            for tp in temp_paths:
                cleanup_temp_file(tp)

    @app.post(
        f"{API_PREFIX}/bts/jobs/generate",
        tags=["BTS"],
        summary="Start an async BTS analysis job — returns a job_id to poll for completion",
        description=(
            "Submits one or more BTS files for analysis. The job runs in the background; "
            "poll `/bts/jobs/{job_id}/status` until `status == 'completed'`, then download "
            "via `/bts/jobs/{job_id}/download/pdf` and `/bts/jobs/{job_id}/download/excel`.\n\n"
            "**`request_json` schema:**\n"
            "```json\n"
            "{\n"
            '  "specs": [\n'
            "    {\n"
            '      "spec_id": "spec_1",\n'
            '      "filenames": ["54141 Incoming.csv", "54141 OutGoing.csv"],\n'
            '      "provider": "telenor",\n'
            '      "bts_id": "54141",\n'
            '      "label": "Tower A",\n'
            '      "windows": [\n'
            '        { "start": "2023-12-07T08:00:00", "end": "2023-12-07T18:00:00", "label": "Day shift" }\n'
            "      ]\n"
            "    }\n"
            "  ],\n"
            '  "include_cross_bts_common": true,\n'
            '  "include_b_as_a": true,\n'
            '  "include_hourly_density": true,\n'
            '  "include_imei_anomalies": true,\n'
            '  "include_bursts": true,\n'
            '  "include_window_only": true,\n'
            '  "include_movement": true,\n'
            '  "include_top_facts": true,\n'
            '  "burst_threshold_calls": 5,\n'
            '  "burst_window_minutes": 10\n'
            "}\n"
            "```\n\n"
            "**Notes:**\n"
            "- `windows` can be an empty list `[]` — the orchestrator will use the full file extent automatically.\n"
            "- `provider` and `bts_id` are optional; if omitted they are auto-detected from file content/name.\n"
            "- For Telenor, list both `<ID> Incoming.csv` and `<ID> OutGoing.csv` in the same spec's `filenames`.\n"
            "- All `include_*` flags default to `true` if omitted."
        ),
        response_model=BtsReportJobCreatedResponse,
        responses={400: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    def generate_bts_report_job(
        background_tasks: BackgroundTasks,
        files: list[UploadFile] = File(..., description=(
            "One or more BTS dump files. Accepted: "
            ".txt (Ufone), .xls/.xlsx (Jazz, Zong), "
            ".csv (Telenor — detected by INBOUND_OUTBOUND_IND + Site_Id columns; any filename accepted). "
            "Multiple Telenor files for the same tower (same Site_Id) are grouped automatically."
        )),
        request_json: str | None = Form(
            default=None,
            description=(
                "JSON string describing specs and analysis toggles. See endpoint description for full schema. "
                "If omitted, a single spec covering all uploaded files with all analyses enabled is assumed."
            ),
        ),
        settings: Settings = Depends(get_settings),
    ) -> BtsReportJobCreatedResponse:
        try:
            logger.info("BTS job request | file_count=%d", len(files))
            raw_request = parse_bts_report_request(request_json)
            domain_request = build_bts_analysis_request(raw_request)

            temp_paths = save_bts_uploads_to_temp(files)
            original_filenames = [f.filename or f"bts_file_{i+1}" for i, f in enumerate(files)]

            db = SessionLocal()
            try:
                job = ReportJob(job_type="bts", status="pending", progress_pct=0, message="Job queued")
                db.add(job)
                db.commit()
                db.refresh(job)

                background_tasks.add_task(
                    process_bts_report_job,
                    job_id=job.id,
                    input_paths=temp_paths,
                    original_filenames=original_filenames,
                    request=domain_request,
                    settings=settings,
                )
                return BtsReportJobCreatedResponse(job_id=job.id, status=job.status, message=job.message)
            finally:
                db.close()
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("BTS job creation error: %s", exc)
            raise HTTPException(status_code=500, detail=f"Internal error: {str(exc)}")

    @app.get(
        f"{API_PREFIX}/bts/jobs/{{job_id}}/status",
        tags=["BTS"],
        summary="Poll BTS job status",
        description=(
            "Returns the current state of a BTS analysis job.\n\n"
            "`status` values: `pending` → `queued` → `running` → `completed` | `failed`.\n\n"
            "`progress_pct` is 0-100. Poll every 2 seconds until `completed` or `failed`. "
            "On `completed`, use `result_pdf_path` / `result_excel_path` to confirm files are ready, "
            "then download via the `/download/pdf` and `/download/excel` endpoints."
        ),
        response_model=BtsReportJobStatusResponse,
        responses={404: {"model": ApiErrorResponse}, 422: {"model": BtsReportJobStatusResponse}},
    )
    def get_bts_job_status(job_id: str) -> BtsReportJobStatusResponse:
        db = SessionLocal()
        try:
            job = db.query(ReportJob).filter(ReportJob.id == job_id).first()
            if not job:
                raise HTTPException(status_code=404, detail="BTS job not found")
            payload = BtsReportJobStatusResponse(
                id=job.id,
                job_type=job.job_type,
                status=job.status,
                progress_pct=job.progress_pct,
                message=job.message,
                result_pdf_path=job.result_file_path,
                result_excel_path=job.result_excel_path,
            )
            if job.status == "failed":
                return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=payload.model_dump())
            return payload
        finally:
            db.close()

    @app.get(
        f"{API_PREFIX}/bts/jobs/{{job_id}}/download/pdf",
        tags=["BTS"],
        summary="Download completed BTS PDF report",
        description=(
            "Streams the generated PDF report as an attachment. "
            "Only available once `status == 'completed'`. Returns 400 if the job is still running or failed."
        ),
        responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}},
    )
    def download_bts_pdf(job_id: str) -> Response:
        db = SessionLocal()
        try:
            job = db.query(ReportJob).filter(ReportJob.id == job_id).first()
            if not job:
                raise HTTPException(status_code=404, detail="BTS job not found")
            if job.status != "completed" or not job.result_file_path:
                raise HTTPException(status_code=400, detail="BTS PDF report not ready yet.")
            filepath = Path(job.result_file_path)
            if not filepath.exists():
                raise HTTPException(status_code=404, detail="PDF file missing from disk.")
            headers = {"Content-Disposition": f'attachment; filename="{filepath.name}"', "Cache-Control": "no-store"}
            return FileResponse(path=filepath, media_type="application/pdf", headers=headers)
        finally:
            db.close()

    @app.get(
        f"{API_PREFIX}/bts/jobs/{{job_id}}/download/excel",
        tags=["BTS"],
        summary="Download completed BTS Excel analysis workbook",
        description=(
            "Streams the generated Excel workbook (.xlsx) as an attachment. "
            "Sheets include: A_Party, B_Party, B_as_A, Window_Only, IMEI_Anomalies, "
            "Burst_Callers, Hourly_Density, Cross_BTS, Movement, Top_Facts (if enabled). "
            "Only available once `status == 'completed'`. Returns 400 if the job is still running or failed."
        ),
        responses={400: {"model": ApiErrorResponse}, 404: {"model": ApiErrorResponse}},
    )
    def download_bts_excel(job_id: str) -> Response:
        db = SessionLocal()
        try:
            job = db.query(ReportJob).filter(ReportJob.id == job_id).first()
            if not job:
                raise HTTPException(status_code=404, detail="BTS job not found")
            if job.status != "completed" or not job.result_excel_path:
                raise HTTPException(status_code=400, detail="BTS Excel report not ready yet.")
            filepath = Path(job.result_excel_path)
            if not filepath.exists():
                raise HTTPException(status_code=404, detail="Excel file missing from disk.")
            headers = {"Content-Disposition": f'attachment; filename="{filepath.name}"', "Cache-Control": "no-store"}
            return FileResponse(
                path=filepath,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers=headers,
            )
        finally:
            db.close()

    return app


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = load_settings()
    setup_logging(settings.app.log_level, settings.app.log_dir, settings.app.log_to_file)
    return settings


def _build_meta_response(settings: Settings) -> ApiMetaResponse:
    return ApiMetaResponse(
        app_name=settings.app.name,
        version=__version__,
        environment=settings.app.environment,
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
        endpoints=[
            f"{API_PREFIX}/meta",
            f"{API_PREFIX}/health",
            f"{API_PREFIX}/reports/inspect",
            f"{API_PREFIX}/reports/analyze",
            f"{API_PREFIX}/jobs/generate",
            f"{API_PREFIX}/jobs/{{job_id}}/status",
            f"{API_PREFIX}/jobs/{{job_id}}/download",
            f"{API_PREFIX}/multi/jobs/generate",
            f"{API_PREFIX}/multi/jobs/{{job_id}}/status",
            f"{API_PREFIX}/multi/jobs/{{job_id}}/download/pdf",
            f"{API_PREFIX}/multi/jobs/{{job_id}}/download/excel",
            f"{API_PREFIX}/bts/files/inspect",
            f"{API_PREFIX}/bts/files/batch-inspect",
            f"{API_PREFIX}/bts/jobs/generate",
            f"{API_PREFIX}/bts/jobs/{{job_id}}/status",
            f"{API_PREFIX}/bts/jobs/{{job_id}}/download/pdf",
            f"{API_PREFIX}/bts/jobs/{{job_id}}/download/excel",
        ],
    )


def _cors_origins() -> list[str]:
    raw = os.environ.get("CDR_REPORT_API_CORS_ORIGINS", "*")
    if raw.strip() == "*":
        return ["*"]
    return [item.strip() for item in raw.split(",") if item.strip()]


app = create_app()


def run() -> None:
    host = os.environ.get("CDR_REPORT_API_HOST", "127.0.0.1")
    port = int(os.environ.get("CDR_REPORT_API_PORT", "7391"))
    uvicorn.run("cdr_report_app.api.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    run()
