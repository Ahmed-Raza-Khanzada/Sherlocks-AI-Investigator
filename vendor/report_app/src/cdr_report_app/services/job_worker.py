import logging
import multiprocessing as mp
from pathlib import Path
import sys

from cdr_report_app.domain.db_models import ReportJob, SessionLocal
from cdr_report_app.settings import Settings
from cdr_report_app.api.schemas import ReportRequest
from cdr_report_app.api.utils import build_crime_context, build_metadata_overrides, cleanup_temp_file, resolve_output_filename
from cdr_report_app.services.report_service import generate_report_with_mode
from cdr_report_app.utils.memory import log_memory_snapshot, release_memory

logger = logging.getLogger(__name__)


def update_job_status(job_id: str, status: str, progress: int, message: str, result_path: str | None = None) -> None:
    db = SessionLocal()
    try:
        job = db.query(ReportJob).filter(ReportJob.id == job_id).first()
        if job:
            job.status = status
            job.progress_pct = progress
            job.message = message
            if result_path:
                job.result_file_path = result_path
            db.commit()
    finally:
        db.close()


def process_report_job(job_id: str, input_path: Path, original_filename: str, request: ReportRequest, settings: Settings) -> None:
    child_request = request.model_dump(mode="python")
    child_settings = settings.model_dump(mode="python")
    child_input_path = str(input_path)
    log_memory_snapshot("single_job_parent:before_spawn", logger_instance=logger)
    context = mp.get_context(_preferred_start_method())
    proc = context.Process(
        target=_run_report_job_child,
        args=(job_id, child_input_path, original_filename, child_request, child_settings),
        daemon=False,
    )
    proc.start()
    proc.join()
    if proc.exitcode not in (0, None):
        logger.error("Single report child process failed | job_id=%s exit_code=%s", job_id, proc.exitcode)
        update_job_status(job_id, "failed", 0, f"Worker failed with exit code {proc.exitcode}")
    log_memory_snapshot("single_job_parent:after_join", logger_instance=logger)
    cleanup_temp_file(input_path)
    release_memory()
    log_memory_snapshot("single_job_parent:cleanup_done", logger_instance=logger)


def _run_report_job_child(
    job_id: str,
    input_path_text: str,
    original_filename: str,
    request_payload: dict,
    settings_payload: dict,
) -> None:
    request = ReportRequest.model_validate(request_payload)
    settings = Settings.model_validate(settings_payload)
    input_path = Path(input_path_text)
    try:
        log_memory_snapshot("single_job_child:start", logger_instance=logger)
        update_job_status(job_id, "processing", 10, "Processing started. Parsing CDR...")

        crime_context = build_crime_context(request)
        overrides = build_metadata_overrides(request)

        update_job_status(job_id, "processing", 20, "Cross-referencing live databases and FIRs...")

        output_dir = settings.app.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        final_filename = resolve_output_filename(request, original_filename)
        output_path = (output_dir / f"{job_id}_{final_filename}").resolve()

        update_job_status(job_id, "processing", 50, "Generating analysis and visual charts...")
        log_memory_snapshot("single_job_child:before_generate", logger_instance=logger)

        generate_report_with_mode(
            settings=settings,
            input_path=input_path,
            output_path=output_path,
            crime_context=crime_context,
            metadata_overrides=overrides,
            return_bytes=False,
        )

        log_memory_snapshot("single_job_child:after_generate", logger_instance=logger)
        update_job_status(job_id, "completed", 100, "Report generation successful.", str(output_path))
    except Exception as exc:
        logger.error("Job %s failed: %s", job_id, exc, exc_info=True)
        update_job_status(job_id, "failed", 0, f"Error: {exc}")
        sys.exit(1)
    finally:
        release_memory()
        log_memory_snapshot("single_job_child:cleanup_done", logger_instance=logger)


def _preferred_start_method() -> str:
    available = mp.get_all_start_methods()
    if "fork" in available and sys.platform != "win32":
        return "fork"
    return "spawn"
