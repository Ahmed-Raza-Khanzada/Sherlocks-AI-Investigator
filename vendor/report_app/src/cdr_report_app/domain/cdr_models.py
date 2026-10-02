"""Canonical domain models for ingested CDR data."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class SubscriberMetadata(BaseModel):
    name: str | None = None
    msisdn: str | None = None
    cnic: str | None = None


class CrimeContext(BaseModel):
    fir_no: str | None = None
    police_station: str | None = None
    sections_of_law: str | None = None
    crime_date: str | None = None
    crime_time: str | None = None
    crime_place: str | None = None
    crime_lat: str | None = None
    crime_lng: str | None = None


class ColumnMapping(BaseModel):
    original: str
    normalized: str
    dtype: str = "TEXT"


class IngestionWarning(BaseModel):
    code: str
    message: str


class IngestionResult(BaseModel):
    source_path: Path
    source_operator: str | None = None
    metadata: SubscriberMetadata = Field(default_factory=SubscriberMetadata)
    column_mappings: list[ColumnMapping] = Field(default_factory=list)
    normalized_columns: dict[str, str] = Field(default_factory=dict)
    row_count: int = 0
    warnings: list[IngestionWarning] = Field(default_factory=list)
    records: list[dict[str, Any]] = Field(default_factory=list)
    normalized_df: Any = Field(default=None, exclude=True, repr=False)
