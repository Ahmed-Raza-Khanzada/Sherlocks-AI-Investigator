"""Helpers for API request parsing and temporary files."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from cdr_report_app.api.schemas import BtsReportRequest, MultiReportRequest, ReportRequest
from cdr_report_app.domain.cdr_models import CrimeContext
from cdr_report_app.settings import Settings


ALLOWED_UPLOAD_SUFFIXES = {".csv", ".xls", ".xlsx", ".xlsb", ".xlsm"}
BTS_ALLOWED_UPLOAD_SUFFIXES = ALLOWED_UPLOAD_SUFFIXES | {".txt"}


def parse_report_request(request_json: str | None) -> ReportRequest:
    if not request_json or not request_json.strip():
        return ReportRequest()
    try:
        return ReportRequest.model_validate(json.loads(request_json))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid request_json payload: {exc}",
        ) from exc


def save_upload_to_temp(upload: UploadFile) -> Path:
    suffix = Path(upload.filename or "upload.bin").suffix.lower()
    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file type. Allowed formats: .csv, .xls, .xlsx, .xlsb, .xlsm",
        )

    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp_path = Path(temp_file.name)
    try:
        with temp_file:
            shutil.copyfileobj(upload.file, temp_file)
    finally:
        upload.file.close()
    return temp_path


def cleanup_temp_file(path: Path | None) -> None:
    if not path:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def build_request_settings(base_settings: Settings, request: ReportRequest) -> Settings:
    settings = base_settings.model_copy(deep=True)
    _apply_overrides(settings.report, request.report.model_dump(exclude_none=True))
    return settings


def build_crime_context(request: ReportRequest) -> CrimeContext:
    return CrimeContext.model_validate(request.crime.model_dump())


def build_metadata_overrides(request: ReportRequest) -> dict[str, str]:
    overrides = {}
    if request.report.suspect_name:
        overrides["name"] = request.report.suspect_name.strip()
    if request.report.msisdn:
        overrides["msisdn"] = request.report.msisdn.strip()
    if request.report.cnic:
        overrides["cnic"] = request.report.cnic.strip()
    return overrides


def resolve_output_filename(request: ReportRequest, original_name: str) -> str:
    stem = Path(original_name).stem or "cdr_report"
    return f"{stem}_report.pdf"


def save_uploads_to_temp(uploads: list[UploadFile]) -> list[Path]:
    """Save multiple uploaded files to temp paths, cleaning up on partial failure."""
    saved: list[Path] = []
    try:
        for upload in uploads:
            saved.append(save_upload_to_temp(upload))
    except Exception:
        for p in saved:
            cleanup_temp_file(p)
        raise
    return saved


def parse_multi_report_request(request_json: str | None) -> MultiReportRequest:
    if not request_json or not request_json.strip():
        return MultiReportRequest()
    try:
        return MultiReportRequest.model_validate(json.loads(request_json))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid request_json payload: {exc}",
        ) from exc


def save_bts_upload_to_temp(upload: UploadFile) -> Path:
    """Like save_upload_to_temp but also accepts .txt (Ufone BTS format)."""
    if not upload.filename or not upload.filename.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="One of the uploaded files has no filename. Ensure all file fields have a file selected.",
        )
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in BTS_ALLOWED_UPLOAD_SUFFIXES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported BTS file type '{suffix}'. Allowed: .txt, .csv, .xls, .xlsx",
        )
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp_path = Path(temp_file.name)
    try:
        with temp_file:
            shutil.copyfileobj(upload.file, temp_file)
    finally:
        upload.file.close()
    return temp_path


def save_bts_uploads_to_temp(uploads: list[UploadFile]) -> list[Path]:
    saved: list[Path] = []
    try:
        for upload in uploads:
            saved.append(save_bts_upload_to_temp(upload))
    except Exception:
        for p in saved:
            cleanup_temp_file(p)
        raise
    return saved


def parse_bts_report_request(request_json: str | None) -> BtsReportRequest:
    from datetime import datetime
    from cdr_report_app.bts.models import BtsAnalysisRequest, BtsFileSpec, BtsTimeWindow

    if not request_json or not request_json.strip():
        return BtsReportRequest(specs=[])
    try:
        raw = BtsReportRequest.model_validate(__import__("json").loads(request_json))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid BTS request_json payload: {exc}",
        ) from exc

    # Validate window ordering
    for spec in raw.specs:
        for w in spec.windows:
            try:
                start = datetime.fromisoformat(w.start)
                end = datetime.fromisoformat(w.end)
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid datetime in window: {exc}",
                ) from exc
            if end <= start:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Window end must be after start for spec '{spec.spec_id}'.",
                )

    return raw


def build_bts_analysis_request(raw: BtsReportRequest):
    """Convert API schema → domain model."""
    from datetime import datetime
    from cdr_report_app.bts.models import BtsAnalysisRequest, BtsFileSpec, BtsTimeWindow

    specs = []
    for s in raw.specs:
        windows = [
            BtsTimeWindow(
                start=datetime.fromisoformat(w.start),
                end=datetime.fromisoformat(w.end),
                label=w.label,
            )
            for w in s.windows
        ]
        specs.append(BtsFileSpec(
            spec_id=s.spec_id,
            filenames=s.filenames,
            provider=s.provider,
            bts_id=s.bts_id,
            label=s.label,
            windows=windows,
        ))
    return BtsAnalysisRequest(
        specs=specs,
        include_cross_bts_common=raw.include_cross_bts_common,
        include_b_as_a=raw.include_b_as_a,
        include_hourly_density=raw.include_hourly_density,
        include_imei_anomalies=raw.include_imei_anomalies,
        include_bursts=raw.include_bursts,
        include_window_only=raw.include_window_only,
        include_movement=raw.include_movement,
        include_top_facts=raw.include_top_facts,
        burst_threshold_calls=raw.burst_threshold_calls,
        burst_window_minutes=raw.burst_window_minutes,
    )


def _apply_overrides(target: object, values: dict[str, object]) -> None:
    for field, value in values.items():
        if hasattr(target, field):
            setattr(target, field, value)
