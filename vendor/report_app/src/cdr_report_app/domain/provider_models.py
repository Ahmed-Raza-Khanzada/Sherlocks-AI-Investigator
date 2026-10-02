"""Normalized provider models for external integrations."""

from __future__ import annotations

from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, Field


ProviderStatus = Literal["success", "no_record", "invalid_input", "error", "partial"]

T = TypeVar("T")


class SearchSubject(BaseModel):
    cnic: str | None = None
    mobile: str | None = None
    imei: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class AttachmentArtifact(BaseModel):
    source: str
    label: str
    media_type: Literal["pdf", "html", "text"]
    bytes_content: bytes | None = None
    text_content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderResult(BaseModel, Generic[T]):
    provider: str
    hit: bool = False
    status: ProviderStatus
    summary: str = ""
    raw: dict[str, Any] | list[Any] | None = None
    data: T | None = None
    errors: list[str] = Field(default_factory=list)


class SubscriberRecord(BaseModel):
    name: str | None = None
    cnic: str | None = None
    mobile: str | None = None
    activation_date: str | None = None
    address: str | None = None


class SimsDbSimEntry(BaseModel):
    number: str | None = None
    name: str | None = None
    cnic: str | None = None
    address: str | None = None


class SimsDbRecord(BaseModel):
    name: str | None = None
    cnic: str | None = None
    mobile: str | None = None
    address: str | None = None
    sim_count: int = 0
    sims: list[SimsDbSimEntry] = Field(default_factory=list)


class PrvsRecord(BaseModel):
    name: str | None = None
    police_station: str | None = None
    record_reference: str | None = None
    remarks: str | None = None


class CroFirRecord(BaseModel):
    fir_no: str | None = None
    fir_year: str | None = None
    police_station: str | None = None
    offence: str | None = None
    status: str | None = None


class CroRecord(BaseModel):
    cro_no: str | None = None
    name: str | None = None
    father_name: str | None = None
    age: str | None = None
    category: str | None = None
    district: str | None = None
    fir_count: int = 0
    firs: list[CroFirRecord] = Field(default_factory=list)


class PsrmsFirReference(BaseModel):
    fir_no: str | None = None
    fir_year: str | None = None
    ps_id: str | None = None
    fir_status: str | None = None
    person_name: str | None = None
    person_father: str | None = None
    person_cnic: str | None = None
    person_phone: str | None = None
    person_address: str | None = None
    person_type: str | None = None


class PsrmsFirDetails(BaseModel):
    title: str = ""
    header_fields: list[dict[str, str]] = Field(default_factory=list)
    fir_sections: list[dict[str, Any]] = Field(default_factory=list)
    main_narrative: str = ""
    investigation_result: str = ""
    case_positions: list[dict[str, str]] = Field(default_factory=list)
    investigating_officers: list[dict[str, str]] = Field(default_factory=list)
    unknown_suspects: list[dict[str, str]] = Field(default_factory=list)
    nominated_suspects: list[dict[str, str]] = Field(default_factory=list)
    witnesses: list[dict[str, str]] = Field(default_factory=list)
    stolen_property: list[dict[str, str]] = Field(default_factory=list)
    detail_tables: list[dict[str, Any]] = Field(default_factory=list)


class PsrmsRecord(BaseModel):
    record_count: int = 0
    person_name: str | None = None
    person_cnic: str | None = None
    person_phone: str | None = None
    firs: list[PsrmsFirReference] = Field(default_factory=list)


class WatchlistRecord(BaseModel):
    matched: bool = False
    source: str | None = None
    remarks: str | None = None


class NearestPoliceStation(BaseModel):
    name: str | None = None
    district: str | None = None
    distance_km: float | None = None
    latitude: float | None = None
    longitude: float | None = None


class ImeiRecord(BaseModel):
    imei: str | None = None
    brand: str | None = None
    model: str | None = None
    device_name: str | None = None
    status_text: str | None = None


class HrmisRecord(BaseModel):
    officer_name: str | None = None
    officer_phone: str | None = None
    officer_cnic: str | None = None
    officer_belt_no: str | None = None
    date_of_birth: str | None = None
    current_posting: str | None = None
    officer_address: str | None = None
    officer_city: str | None = None
    rank: str | None = None
    police_station_name: str | None = None
    district: str | None = None
    search_branch: str | None = None
    cnic_used: str | None = None
    cnic_source: str | None = None


class EmploymentDatabaseRecord(BaseModel):
    name: str | None = None
    father_name: str | None = None
    cnic: str | None = None
    contact: str | None = None
    other_contact: str | None = None
    permanent_address: str | None = None
    designation: str | None = None
    cnic_source: str | None = None


class DlsLicenseRecord(BaseModel):
    license_no: str | None = None
    category: str | None = None
    license_type: str | None = None
    expiry_date: str | None = None
    issued_date: str | None = None
    issued_office: str | None = None
    status: str | None = None


class DlsRecord(BaseModel):
    firstname: str | None = None
    lastname: str | None = None
    phone: str | None = None
    cnic: str | None = None
    address: str | None = None
    licenses: list[DlsLicenseRecord] = Field(default_factory=list)


class GenericDatabaseRecord(BaseModel):
    matched: bool = False
    title: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    source: str | None = None


class OldTenantTenantInfo(BaseModel):
    tenant_id: int | None = None
    tenant_name: str | None = None
    tenant_cnic: str | None = None
    tenant_phone: str | None = None
    house_no: str | None = None
    street_mohalla: str | None = None
    address: str | None = None


class OldTenantRecord(BaseModel):
    owner_name: str | None = None
    owner_phone: str | None = None
    owner_cnic: str | None = None
    search_branch: str | None = None
    tenants: list[OldTenantTenantInfo] = Field(default_factory=list)
    schema_version: int = 1


class SbvsEntry(BaseModel):
    id: int | None = None
    organization_name: str | None = None
    profession: str | None = None
    purpose: str | None = None
    district: str | None = None
    ps: str | None = None
    status: str | None = None


class SbvsRecord(BaseModel):
    name: str | None = None
    father_name: str | None = None
    cnic: str | None = None
    phone: str | None = None
    passport: str | None = None
    address: str | None = None
    lookup_source: str | None = None
    entries: list[SbvsEntry] = Field(default_factory=list)
