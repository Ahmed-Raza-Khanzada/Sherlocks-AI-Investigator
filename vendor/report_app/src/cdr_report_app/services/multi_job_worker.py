"""Background job worker for async multi-CDR report generation."""

from __future__ import annotations

import logging
import multiprocessing as mp
from pathlib import Path
import sys

from cdr_report_app.api.schemas import MultiReportRequest
from cdr_report_app.domain.cdr_models import CrimeContext
from cdr_report_app.domain.db_models import ReportJob, SessionLocal
from cdr_report_app.services.multi_report_service import generate_multi_report
from cdr_report_app.settings import Settings
from cdr_report_app.utils.memory import log_memory_snapshot, release_memory

logger = logging.getLogger(__name__)


def _update_multi_job(
    job_id: str,
    status: str,
    progress: int,
    message: str,
    pdf_path: str | None = None,
    excel_path: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        job = db.query(ReportJob).filter(ReportJob.id == job_id).first()
        if job:
            job.status = status
            job.progress_pct = progress
            job.message = message
            if pdf_path:
                job.result_file_path = pdf_path
            if excel_path:
                job.result_excel_path = excel_path
            db.commit()
    finally:
        db.close()


def _cleanup_temp_files(paths: list[Path]) -> None:
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def process_multi_report_job(
    job_id: str,
    input_paths: list[Path],
    original_filenames: list[str],
    request: MultiReportRequest,
    settings: Settings,
) -> None:
    del original_filenames
    child_request = request.model_dump(mode="python")
    child_settings = settings.model_dump(mode="python")
    child_input_paths = [str(path) for path in input_paths]
    log_memory_snapshot("multi_job_parent:before_spawn", logger_instance=logger)
    context = mp.get_context(_preferred_start_method())
    proc = context.Process(
        target=_run_multi_job_child,
        args=(job_id, child_input_paths, child_request, child_settings),
        daemon=False,
    )
    proc.start()
    proc.join()
    if proc.exitcode not in (0, None):
        logger.error("Multi report child process failed | job_id=%s exit_code=%s", job_id, proc.exitcode)
        _update_multi_job(job_id, "failed", 0, f"Worker failed with exit code {proc.exitcode}")
    log_memory_snapshot("multi_job_parent:after_join", logger_instance=logger)
    _cleanup_temp_files(input_paths)
    release_memory()
    log_memory_snapshot("multi_job_parent:cleanup_done", logger_instance=logger)


def _run_multi_job_child(
    job_id: str,
    input_path_texts: list[str],
    request_payload: dict,
    settings_payload: dict,
) -> None:
    request = MultiReportRequest.model_validate(request_payload)
    settings = Settings.model_validate(settings_payload)
    input_paths = [Path(item) for item in input_path_texts]
    try:
        log_memory_snapshot("multi_job_child:start", logger_instance=logger)
        _update_multi_job(job_id, "processing", 10, "Parsing CDR files...")

        crime = CrimeContext(
            fir_no=request.crime.fir_no,
            police_station=request.crime.police_station,
            sections_of_law=request.crime.sections_of_law,
            crime_date=request.crime.crime_date,
            crime_time=request.crime_time,
            crime_place=request.crime.crime_place,
            crime_lat=request.crime.crime_lat,
            crime_lng=request.crime.crime_lng,
        )

        target_labels = request.target_labels if request.target_labels else None
        output_dir = settings.app.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = (output_dir / f"{job_id}_multi_report.pdf").resolve()
        excel_path = (output_dir / f"{job_id}_multi_analysis.xlsx").resolve()

        _update_multi_job(job_id, "processing", 30, "Running subscriber lookups...")
        log_memory_snapshot("multi_job_child:before_generate", logger_instance=logger)
        generate_multi_report(
            settings=settings,
            input_files=input_paths,
            crime_context=crime,
            output_dir=output_dir,
            target_labels=target_labels,
            output_pdf_path=pdf_path,
            output_excel_path=excel_path,
            return_bytes=False,
        )
        log_memory_snapshot("multi_job_child:after_generate", logger_instance=logger)

        _update_multi_job(
            job_id,
            "completed",
            100,
            "Multi-CDR report generated successfully.",
            pdf_path=str(pdf_path),
            excel_path=str(excel_path),
        )
    except Exception as exc:
        logger.error("Multi-CDR job %s failed: %s", job_id, exc, exc_info=True)
        _update_multi_job(job_id, "failed", 0, f"Error: {exc}")
        sys.exit(1)
    finally:
        release_memory()
        log_memory_snapshot("multi_job_child:cleanup_done", logger_instance=logger)


def _preferred_start_method() -> str:
    available = mp.get_all_start_methods()
    if "fork" in available and sys.platform != "win32":
        return "fork"
    return "spawn"
