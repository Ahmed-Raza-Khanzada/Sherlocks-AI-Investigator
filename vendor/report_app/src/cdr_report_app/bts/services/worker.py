"""Background job worker for async BTS report generation."""

from __future__ import annotations

import logging
from pathlib import Path

from cdr_report_app.bts.models import BtsAnalysisRequest
from cdr_report_app.bts.services.orchestrator import generate_bts_report
from cdr_report_app.domain.db_models import ReportJob, SessionLocal
from cdr_report_app.settings import Settings
from cdr_report_app.utils.memory import release_memory

logger = logging.getLogger(__name__)


def _update_bts_job(
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


def process_bts_report_job(
    job_id: str,
    input_paths: list[Path],
    original_filenames: list[str],
    request: BtsAnalysisRequest,
    settings: Settings,
) -> None:
    """Background task: run the full BTS pipeline and save outputs to disk."""
    try:
        _update_bts_job(job_id, "processing", 10, "Classifying and ingesting BTS files...")

        output_dir = settings.app.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        _update_bts_job(job_id, "processing", 30, "Running BTS analysis...")

        pdf_bytes, excel_bytes, _ = generate_bts_report(
            settings=settings,
            temp_paths=input_paths,
            original_filenames=original_filenames,
            request=request,
            output_dir=output_dir,
        )

        _update_bts_job(job_id, "processing", 80, "Saving output files...")

        pdf_path = (output_dir / f"{job_id}_bts_report.pdf").resolve()
        excel_path = (output_dir / f"{job_id}_bts_analysis.xlsx").resolve()

        pdf_path.write_bytes(pdf_bytes)
        excel_path.write_bytes(excel_bytes)

        _update_bts_job(
            job_id, "completed", 100, "BTS report generated successfully.",
            pdf_path=str(pdf_path), excel_path=str(excel_path),
        )

    except Exception as exc:
        logger.error("BTS job %s failed: %s", job_id, exc, exc_info=True)
        _update_bts_job(job_id, "failed", 0, f"Error: {exc}")
    finally:
        _cleanup_temp_files(input_paths)
        release_memory()
