"""FastAPI request and response schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from cdr_report_app.domain.analysis_models import ReportAnalysis
from cdr_report_app.domain.cdr_models import ColumnMapping, CrimeContext, IngestionWarning, SubscriberMetadata
from cdr_report_app.domain.provider_models import SearchSubject
from cdr_report_app.settings import AttachmentSettings, ReportSettings, SectionSettings


class ApiMetaResponse(BaseModel):
    app_name: str
    version: str
    environment: str
    docs_url: str
    redoc_url: str
    openapi_url: str
    endpoints: list[str] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: str = "ok"
    app_name: str
    environment: str


class ProviderStatusResponseItem(BaseModel):
    provider: str
    enabled: bool
    ready: bool
    missing: list[str] = Field(default_factory=list)


class CrimeContextInput(BaseModel):
    fir_no: str | None = None
    police_station: str | None = None
    sections_of_law: str | None = None
    crime_date: str | None = Field(None, description="Date of offence (YYYY-MM-DD), optional")
    crime_place: str | None = None
    crime_lat: str | None = None
    crime_lng: str | None = None


class ConfigTemplateResponse(BaseModel):
    report: ReportSettings
    sections: SectionSettings
    attachments: AttachmentSettings
    crime: CrimeContext
    providers: list[ProviderStatusResponseItem] = Field(default_factory=list)


class ReportOptionsInput(BaseModel):
    suspect_name: str | None = None
    msisdn: str | None = None
    cnic: str | None = None


class ReportRequest(BaseModel):
    crime: CrimeContextInput = Field(default_factory=CrimeContextInput)
    report: ReportOptionsInput = Field(default_factory=ReportOptionsInput)


class ReportJobCreatedResponse(BaseModel):
    job_id: str
    status: str
    message: str


class ReportJobStatusResponse(BaseModel):
    id: str
    job_type: str = "single"
    status: str
    progress_pct: int
    message: str
    result_file_path: str | None = None


class InspectResponse(BaseModel):
    source_name: str
    source_operator: str | None = None
    row_count: int
    metadata: SubscriberMetadata = Field(default_factory=SubscriberMetadata)
    column_mappings: list[ColumnMapping] = Field(default_factory=list)
    normalized_columns: dict[str, str] = Field(default_factory=dict)
    warnings: list[IngestionWarning] = Field(default_factory=list)


class AnalysisResponse(BaseModel):
    source_name: str
    include_live_lookups: bool
    analysis: ReportAnalysis


class ProviderTestRequest(BaseModel):
    provider: str
    subject: SearchSubject = Field(default_factory=SearchSubject)


class Mx7Request(BaseModel):
    q: str | None = None
    cnic: str | None = None
    mobile: str | None = None
    imei: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    providers: list[str] | None = None
    related: bool = True
    deep_related: bool = False
    include_caller_id: bool = True
    include_raw: bool = True


class ProviderTestResponse(BaseModel):
    provider: str
    status: str
    hit: bool
    summary: str
    errors: list[str] = Field(default_factory=list)
    data: dict[str, Any] | None = None
    raw: dict[str, Any] | list[Any] | None = None


class MultiReportRequest(BaseModel):
    crime: CrimeContextInput = Field(default_factory=CrimeContextInput)
    target_labels: list[str] = Field(
        default_factory=list,
        description="Optional label for each CDR file (same order as files). Defaults to 'Target 1', 'Target 2', …",
    )
    crime_time: str | None = Field(
        None,
        description="Approximate crime time in HH:MM or HH:MM:SS format used for proximity analysis.",
    )


class MultiReportJobCreatedResponse(BaseModel):
    job_id: str
    status: str
    message: str


class MultiReportJobStatusResponse(BaseModel):
    id: str
    job_type: str = "multi"
    status: str
    progress_pct: int
    message: str
    result_pdf_path: str | None = None
    result_excel_path: str | None = None


class ApiErrorResponse(BaseModel):
    detail: str


# ---------------------------------------------------------------------------
#  BTS Analysis schemas
# ---------------------------------------------------------------------------

class BtsTimeWindowInput(BaseModel):
    start: str = Field(..., description="ISO-8601 datetime, e.g. 2023-07-12T12:00:00")
    end: str = Field(..., description="ISO-8601 datetime, e.g. 2023-07-12T13:00:00")
    label: str | None = None


class BtsFileSpecInput(BaseModel):
    spec_id: str | None = None
    filenames: list[str]
    provider: str | None = None
    bts_id: str | None = None
    label: str | None = None
    windows: list[BtsTimeWindowInput] = Field(default_factory=list)


class BtsReportRequest(BaseModel):
    specs: list[BtsFileSpecInput]
    include_cross_bts_common: bool = True
    include_b_as_a: bool = True
    include_hourly_density: bool = True
    include_imei_anomalies: bool = True
    include_bursts: bool = True
    include_window_only: bool = True
    include_movement: bool = True
    include_top_facts: bool = True
    burst_threshold_calls: int = 5
    burst_window_minutes: int = 10


class BtsReportJobCreatedResponse(BaseModel):
    job_id: str
    status: str
    message: str


class BtsReportJobStatusResponse(BaseModel):
    id: str
    job_type: str = "bts"
    status: str
    progress_pct: int
    message: str
    result_pdf_path: str | None = None
    result_excel_path: str | None = None


class BtsFileInspectResponse(BaseModel):
    filename: str
    provider: str | None = None
    inferred_bts_id: str | None = None
    row_count: int
    time_range_start: str | None = None
    time_range_end: str | None = None
    unique_cell_count: int = 0
    unique_lac_count: int = 0


class BtsBatchFileResult(BaseModel):
    filename: str
    row_count: int
    direction_counts: dict[str, int]


class BtsBatchGroup(BaseModel):
    site_id: str | None
    provider: str
    files: list[BtsBatchFileResult]
    time_range_start: str | None = None
    time_range_end: str | None = None
    total_row_count: int
    unique_cell_count: int = 0
    unique_lac_count: int = 0


class BtsBatchInspectResponse(BaseModel):
    groups: list[BtsBatchGroup]
    unrecognized_files: list[str] = Field(default_factory=list)
