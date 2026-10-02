"""Structured analysis models for report sections."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from cdr_report_app.domain.cdr_models import CrimeContext, SubscriberMetadata
from cdr_report_app.domain.provider_models import AttachmentArtifact


class QuickStats(BaseModel):
    total_records: int = 0
    unique_numbers: int = 0
    repeated_contacts: int = 0
    duration_label: str = "-"
    duration_days: int = 0


class ActivityBin(BaseModel):
    label: str
    value: int
    color: str | None = None
    group: str | None = None


class DailyActivityAnalysis(BaseModel):
    title_suffix: str = ""
    bins: list[ActivityBin] = Field(default_factory=list)
    busiest_hour: int | None = None
    busiest_hour_count: int | None = None


class LocationVisit(BaseModel):
    order: int
    location: str
    nearest_police_station: str | None = None
    nearest_police_station_latitude: float | None = None
    nearest_police_station_longitude: float | None = None
    coordinates_text: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    visits: int = 0
    timing_window: str | None = None
    duration_pct: float | None = None


class StayRecord(BaseModel):
    time_period: str
    location: str
    coordinates_text: str | None = None
    nearest_police_station: str | None = None
    nearest_police_station_latitude: float | None = None
    nearest_police_station_longitude: float | None = None
    duration_hours: float = 0.0
    latitude: float | None = None
    longitude: float | None = None


class HeatmapAnalysis(BaseModel):
    day_labels: list[str] = Field(default_factory=list)
    hour_labels: list[str] = Field(default_factory=list)
    matrix: list[list[int]] = Field(default_factory=list)


class MovementStep(BaseModel):
    order: int
    location: str
    nearest_police_station: str | None = None
    nearest_police_station_latitude: float | None = None
    nearest_police_station_longitude: float | None = None
    coordinates_text: str | None = None
    records: int = 0
    time_window: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class BurstRecord(BaseModel):
    burst_start: str
    burst_end: str
    call_count: int
    top_numbers: list[str] = Field(default_factory=list)


class ShortCodeRecord(BaseModel):
    code: str
    count: int
    description: str = "-"


class DeviceRecord(BaseModel):
    identifier: str
    label: str | None = None
    brand: str | None = None
    specs: str | None = None
    model: str | None = None
    device_name: str | None = None
    records: int = 0
    first_seen: str | None = None
    last_seen: str | None = None


class ContactRecord(BaseModel):
    number: str
    cnic: str | None = None
    og_calls: int = 0
    og_duration: int = 0
    in_calls: int = 0
    in_duration: int = 0
    sms_in: int = 0
    sms_out: int = 0
    total_calls: int = 0


class DatabaseVerificationRow(BaseModel):
    database: str
    status: str
    summary: str
    severity: str = "neutral"


class ContactHistoryRecord(BaseModel):
    number: str
    telecom_name: str | None = None
    cnic: str | None = None
    db_rows: list[DatabaseVerificationRow] = Field(default_factory=list)
    raw_results: dict[str, Any] = Field(default_factory=dict)


class ReportAnalysis(BaseModel):
    source_operator: str | None = None
    metadata: SubscriberMetadata = Field(default_factory=SubscriberMetadata)
    crime_context: CrimeContext = Field(default_factory=CrimeContext)
    quick_stats: QuickStats = Field(default_factory=QuickStats)
    daily_activity: DailyActivityAnalysis = Field(default_factory=DailyActivityAnalysis)
    top_locations: list[LocationVisit] = Field(default_factory=list)
    long_stays: list[StayRecord] = Field(default_factory=list)
    heatmap: HeatmapAnalysis = Field(default_factory=HeatmapAnalysis)
    movement_steps: list[MovementStep] = Field(default_factory=list)
    bursts: list[BurstRecord] = Field(default_factory=list)
    short_codes: list[ShortCodeRecord] = Field(default_factory=list)
    imei_records: list[DeviceRecord] = Field(default_factory=list)
    imsi_records: list[DeviceRecord] = Field(default_factory=list)
    top_contacts: list[ContactRecord] = Field(default_factory=list)
    db_verification_rows: list[DatabaseVerificationRow] = Field(default_factory=list)
    main_db_results: dict[str, Any] = Field(default_factory=dict)
    top_contact_histories: list[ContactHistoryRecord] = Field(default_factory=list)
    attachments: list[AttachmentArtifact] = Field(default_factory=list)
    analysis_summary: dict[str, Any] = Field(default_factory=dict)


class TargetSummary(BaseModel):
    identifier: str
    target_name: str | None = None
    role: str | None = None
    operator: str | None = None
    total_calls: int = 0
    unique_contacts: int = 0
    incoming_calls: int = 0
    outgoing_calls: int = 0
    incoming_sms: int = 0
    outgoing_sms: int = 0
    frequent_locations: list[str] = Field(default_factory=list)
    top_contacts: list[str] = Field(default_factory=list)
    top_locations: list[LocationVisit] = Field(default_factory=list)
    imei_records: list[DeviceRecord] = Field(default_factory=list)
    imsi_records: list[DeviceRecord] = Field(default_factory=list)


class DirectInteraction(BaseModel):
    source_identifier: str
    target_identifier: str
    interaction_count: int
    total_duration_seconds: int = 0
    first_interaction: str | None = None
    last_interaction: str | None = None


class CommonContact(BaseModel):
    contact_number: str
    target_identifiers: list[str] = Field(default_factory=list)
    total_interactions: int = 0
    role_description: str | None = None


class TimelineEvent(BaseModel):
    timestamp: str
    event_type: str  # "Call", "SMS", "Co-location", "Crime Scene"
    target_1: str
    target_2: str | None = None
    targets: list[str] = Field(default_factory=list)
    description: str


class CrimeDayCommonContact(BaseModel):
    """Contact number seen talking to 2+ targets on crime day or adjacent days."""
    contact_number: str
    day_label: str  # "crime_day", "day_before", "day_after"
    target_identifiers: list[str] = Field(default_factory=list)
    total_interactions: int = 0


class IMEICrossMatch(BaseModel):
    """When 2+ targets share the same IMEI device."""
    imei: str
    target_identifiers: list[str] = Field(default_factory=list)
    usage_details: list[str] = Field(default_factory=list)


class TargetSubscriberInfo(BaseModel):
    """Subscriber DB lookup result for each target's A-party number."""
    identifier: str
    msisdn: str
    name: str | None = None
    cnic: str | None = None
    address: str | None = None
    operator: str | None = None


class CrimeProximityEvent(BaseModel):
    """A CDR ping from a tower that is close to the crime location, around the crime time.

    IMPORTANT: tower_distance_meters is the distance from the CELL TOWER to the crime
    location — NOT the suspect's actual distance. The suspect could be anywhere within
    the tower's coverage area (typically 200m–2km radius in urban Pakistan).
    """
    target_identifier: str
    time_bucket: str                   # "before" | "during" | "after"
    timestamp: str                     # "YYYY-MM-DD HH:MM:SS"
    minutes_from_crime: int            # negative = before crime, 0 = at crime time, positive = after
    tower_distance_meters: float       # TOWER-to-crime-scene distance (not suspect distance)
    proximity_strength: str            # "Very Strong" | "Strong" | "Moderate" | "Weak"
    tower_location: str                # SITE_ADDRESS or cell description
    tower_latitude: float | None = None
    tower_longitude: float | None = None
    call_type: str | None = None       # Incoming / Outgoing / SMS
    b_number: str | None = None        # Who they were communicating with at this moment


class MultiReportAnalysis(BaseModel):
    report_title: str = "Multi-CDR Correlation Analysis"
    crime_context: CrimeContext = Field(default_factory=CrimeContext)
    target_summaries: list[TargetSummary] = Field(default_factory=list)
    direct_interactions: list[DirectInteraction] = Field(default_factory=list)
    common_contacts: list[CommonContact] = Field(default_factory=list)
    timeline_events: list[TimelineEvent] = Field(default_factory=list)
    crime_day_common_contacts: list[CrimeDayCommonContact] = Field(default_factory=list)
    imei_cross_matches: list[IMEICrossMatch] = Field(default_factory=list)
    target_subscriber_info: list[TargetSubscriberInfo] = Field(default_factory=list)
    crime_proximity_events: list[CrimeProximityEvent] = Field(default_factory=list)
    executive_summary: str | None = None
