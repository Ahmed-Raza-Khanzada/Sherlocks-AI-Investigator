"""Request and response bodies for the Sherlocks API."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from sherlocks.osint.models import OsintSubject


class OsintJobRequest(BaseModel):
    """Everything known about the subject before searching.

    A CNIC alone is accepted by the schema but will produce a run that searches nothing -
    the response says so explicitly rather than returning an empty report.
    """

    full_name: str | None = None
    phone: str | None = None
    email: str | None = None
    username: str | None = None
    cnic: str | None = None
    city: str | None = None
    employer: str | None = None
    notes: str | None = None
    case_id: str | None = None
    person_id: str | None = None

    @model_validator(mode="after")
    def _require_something(self) -> OsintJobRequest:
        if not any([self.full_name, self.phone, self.email, self.username, self.cnic]):
            raise ValueError(
                "Supply at least one of: full_name, phone, email, username, cnic."
            )
        return self

    def to_subject(self) -> OsintSubject:
        return OsintSubject(
            full_name=self.full_name,
            phone=self.phone,
            email=self.email,
            username=self.username,
            cnic=self.cnic,
            city=self.city,
            employer=self.employer,
            notes=self.notes,
        )


class JobCreated(BaseModel):
    job_id: str
    status: str
    message: str
    # Present when nothing searchable was supplied, so the caller learns immediately
    # rather than after a scan that could never have found anything.
    warning: str | None = None


class JobStatus(BaseModel):
    job_id: str
    job_type: str
    status: str
    progress_pct: int
    message: str | None = None
    result_available: bool = False
    created_at: datetime
    updated_at: datetime


class ToolCapability(BaseModel):
    tool: str
    accepts: str
    description: str
    requires_binary: str | None = None
    requires_brightdata: bool = False
    runnable: bool
    skip_reason: str | None = None


class OsintHealth(BaseModel):
    osint_enabled: bool
    openosint_installed: bool
    brightdata_configured: bool
    ollama_ready: bool
    ollama_reachable: bool
    report_engine_available: bool
    tools: list[ToolCapability] = Field(default_factory=list)
