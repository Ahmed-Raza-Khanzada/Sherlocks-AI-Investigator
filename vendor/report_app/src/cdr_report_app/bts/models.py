"""Pydantic models for the BTS analysis pipeline."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


Provider = Literal["ufone", "telenor", "jazz", "zong"]


CANONICAL_BTS_COLUMNS = [
    "SPEC_ID",
    "BTS_ID",
    "A_PARTY",
    "B_PARTY",
    "DIRECTION",
    "CALL_TIME",
    "DURATION_SEC",
    "IMEI",
    "CELL_ID",
    "LAC_ID",
    "LAT",
    "LNG",
    "LOCATION",
    "PROVIDER",
    "SOURCE_FILE",
]


class BtsTimeWindow(BaseModel):
    start: datetime
    end: datetime
    label: str | None = None


class BtsFileSpec(BaseModel):
    """One tower = one spec. Telenor pairs 2 files; others use 1."""

    spec_id: str | None = None
    filenames: list[str]
    provider: Provider | None = None
    bts_id: str | None = None
    label: str | None = None
    windows: list[BtsTimeWindow] = Field(default_factory=list)


class BtsAnalysisRequest(BaseModel):
    specs: list[BtsFileSpec]
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


class BtsFileGroup(BaseModel):
    """Resolved file group ready for a reader.

    Telenor groups may contain one or more CSV paths sharing the same Site_Id.
    Others have a single path.
    """

    model_config = {"arbitrary_types_allowed": True}

    provider: Provider
    paths: list[Path]
    spec_id: str
    bts_id: str | None = None
    label: str | None = None


# ---------------------------------------------------------------------------
# Phase 2 — Analysis output models
# ---------------------------------------------------------------------------


class BtsWindowKey(BaseModel):
    """Identifies one (spec, window) slice."""

    spec_id: str
    bts_id: str | None = None
    label: str | None = None
    window_label: str | None = None
    window_start: datetime
    window_end: datetime


class APartyRow(BaseModel):
    msisdn: str
    call_count: int
    total_duration_sec: int
    first_seen: datetime
    last_seen: datetime
    direction_in: int
    direction_out: int
    top_cell: str | None = None
    top_lac: str | None = None


class BPartyRow(BaseModel):
    msisdn: str
    call_count: int
    total_duration_sec: int
    first_seen: datetime
    last_seen: datetime
    top_cell: str | None = None
    top_lac: str | None = None


class BAsARow(BaseModel):
    """An A-party whose B-party contacts are also physically present (A-party) at the tower."""

    a_party: str
    b_parties_at_tower: list[str]
    contact_count: int


class WindowOnlyEntry(BaseModel):
    msisdn: str
    call_count: int
    b_parties: list[str] = Field(default_factory=list)


class CrossBtsCommon(BaseModel):
    """A number seen at ≥2 different towers within their respective windows."""

    msisdn: str
    tower_count: int
    towers: list[str]
    tower_call_counts: dict[str, int] = Field(default_factory=dict)


class HourlyBucket(BaseModel):
    hour: int
    call_count: int
    is_spike: bool


class HourlyDensity(BaseModel):
    window_key: BtsWindowKey
    buckets: list[HourlyBucket]
    mean_calls: float
    spike_threshold: float


class ImeiAnomaly(BaseModel):
    """A single MSISDN paired with more than one IMEI (device swap / SIM swap)."""

    msisdn: str
    imei_count: int
    imeis: list[str]
    call_count: int


class BurstCaller(BaseModel):
    msisdn: str
    burst_count: int
    max_calls_in_burst: int
    first_burst_start: datetime


class MovementFeasibility(BaseModel):
    """Whether movement between two towers in the available time is physically plausible."""

    tower_a_spec_id: str
    tower_b_spec_id: str
    tower_a_bts_id: str | None
    tower_b_bts_id: str | None
    distance_m: float
    time_gap_minutes: float
    required_speed_kmh: float
    feasible: bool
    common_numbers: list[str]


class TopCell(BaseModel):
    cell_id: str
    call_count: int
    unique_a_party: int
    total_duration_sec: int
    lac_id: str | None = None


class TopLongCall(BaseModel):
    a_party: str
    b_party: str
    duration_sec: int
    call_time: datetime
    direction: str
    cell_id: str | None = None
    lac_id: str | None = None


class TopImei(BaseModel):
    imei: str
    call_count: int
    unique_a_party: int


class TopPair(BaseModel):
    a_party: str
    b_party: str
    call_count: int
    total_duration_sec: int


class TopMinute(BaseModel):
    minute: datetime
    call_count: int


class TopFacts(BaseModel):
    """Simple top-10 aggregations for one window."""

    top_cells: list[TopCell] = Field(default_factory=list)
    top_longest_calls: list[TopLongCall] = Field(default_factory=list)
    top_imeis: list[TopImei] = Field(default_factory=list)
    top_frequent_pairs: list[TopPair] = Field(default_factory=list)
    top_busiest_minutes: list[TopMinute] = Field(default_factory=list)


class BtsWindowResult(BaseModel):
    """All analysis results for one (spec_id, window) slice."""

    key: BtsWindowKey
    total_rows: int
    a_party: list[APartyRow] = Field(default_factory=list)
    b_party: list[BPartyRow] = Field(default_factory=list)
    b_as_a: list[BAsARow] = Field(default_factory=list)
    window_only: list[WindowOnlyEntry] = Field(default_factory=list)
    hourly_density: HourlyDensity | None = None
    imei_anomalies: list[ImeiAnomaly] = Field(default_factory=list)
    burst_callers: list[BurstCaller] = Field(default_factory=list)
    top_facts: TopFacts | None = None


class BtsAnalysis(BaseModel):
    """Top-level analysis result for one BTS report job."""

    window_results: list[BtsWindowResult] = Field(default_factory=list)
    cross_bts_common: list[CrossBtsCommon] = Field(default_factory=list)
    movement_feasibility: list[MovementFeasibility] = Field(default_factory=list)
    total_bts_records: int = 0
