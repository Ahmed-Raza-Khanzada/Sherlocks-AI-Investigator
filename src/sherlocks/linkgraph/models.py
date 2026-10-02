"""Types for the link graph.

Two node kinds and three edge kinds, and the distinction between them is the product:

* ``person`` nodes are people. ``system`` nodes are *a record about one person in one
  system* - "Ali's CRO record", not "CRO". A shared hub per system would draw a line
  between every criminal in the province.
* ``found_in`` edges join a person to their own record. ``strong`` edges join a record
  to another person that record names - the system itself asserts the relationship.
  ``weak`` edges are inferred (similar address, shared father's name, overlapping hotel
  stay, OSINT) and carry a score and the reasons behind it. A weak edge is a lead for
  an analyst, never a fact, and it never merges two identities.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

NodeKind = Literal["person", "system"]
EdgeKind = Literal["found_in", "strong", "weak"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class PersonRef(BaseModel):
    """What one record says about one person. Every field is optional."""

    name: str | None = None
    father_name: str | None = None
    cnic: str | None = None
    phones: list[str] = Field(default_factory=list)
    addresses: list[str] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)
    extra: dict[str, str] = Field(default_factory=dict)

    @property
    def searchable(self) -> bool:
        return bool(self.cnic or self.phones)

    @property
    def is_empty(self) -> bool:
        return not (self.name or self.cnic or self.phones)

    def label(self) -> str:
        return self.name or (f"CNIC {self.cnic}" if self.cnic else None) or (
            self.phones[0] if self.phones else "Unknown"
        )


class RelatedPerson(BaseModel):
    """Another person a system record names, and how the record relates them."""

    ref: PersonRef
    relation: str
    detail: str | None = None


class FirKey(BaseModel):
    fir_no: str
    fir_year: str
    police_station: str
    role: str | None = None
    offence: str | None = None
    status: str | None = None
    ps_id: str | None = None

    @field_validator("fir_no", "fir_year", "police_station", "role", "offence", "status", "ps_id",
                     mode="before")
    @classmethod
    def _stringify(cls, value: Any) -> str | None:
        """Upstream systems return these as a string, a number, or a list (charges often
        come as ``['395']`` or ``['395', '34']``). Coerce anything to clean text so one odd
        record never crashes the whole run."""
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            return ", ".join(str(v).strip() for v in value if v is not None and str(v).strip())
        return str(value).strip()

    @property
    def key(self) -> str:
        ps = (self.ps_id or self.police_station or "").strip().lower()
        return f"{self.fir_no.strip().lstrip('0')}/{self.fir_year.strip()}/{ps}"


class HotelStay(BaseModel):
    hotel: str
    district: str | None = None
    room: str | None = None
    check_in: str | None = None
    check_out: str | None = None


class InfoField(BaseModel):
    label: str
    value: str


class SystemRecord(BaseModel):
    """One system's answer about one person, normalised."""

    system: str
    status: str
    hit: bool = False
    summary: str = ""
    subject: PersonRef = Field(default_factory=PersonRef)
    fields: list[InfoField] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    related: list[RelatedPerson] = Field(default_factory=list)
    firs: list[FirKey] = Field(default_factory=list)
    stays: list[HotelStay] = Field(default_factory=list)
    organisations: list[str] = Field(default_factory=list)
    vehicles: list[str] = Field(default_factory=list)
    police_station: str | None = None
    raw: Any = None
    cached: bool = False
    errors: list[str] = Field(default_factory=list)

    def add_field(self, label: str, value: object) -> None:
        text = str(value if value is not None else "").strip()
        if text and text.lower() not in {"none", "null", "-", "n/a", "nan"}:
            self.fields.append(InfoField(label=label, value=text[:300]))


class GraphNode(BaseModel):
    id: str
    kind: NodeKind
    label: str
    data: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    kind: EdgeKind
    label: str = ""
    score: float | None = None
    reasons: list[str] = Field(default_factory=list)
    system: str | None = None


class Seed(BaseModel):
    """One starting person for a multi-person relation search."""

    cnic: str | None = None
    phone: str | None = None
    email: str | None = None

    @field_validator("cnic", "phone", "email")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        return value or None


class GraphRunParams(BaseModel):
    """What the analyst asked for. Budgets are part of the request, not hidden config:
    each searched person costs ~20 logged queries against live police systems."""

    cnic: str | None = None
    phone: str | None = None
    # An email - on its own, or alongside the CNIC/phone. On its own it is looked up on
    # the person-data APIs; a Pakistani mobile they return seeds the police-system search.
    email: str | None = None
    # Multiple starting people. When given, all are searched into one graph and the
    # relations between them are computed (the "Multiple people" mode).
    seeds: list[Seed] = Field(default_factory=list)
    depth: int = Field(default=2, ge=0, le=4)
    max_persons: int = Field(default=25, ge=1, le=200)
    systems: list[str] | None = None
    include_fir_rosters: bool = True
    include_caller_id: bool = False
    include_osint: bool = False
    # "subject": only the person searched for. "everyone": also the named people found,
    # up to max_osint_people (nearest first) - a free name search each.
    osint_scope: Literal["subject", "everyone"] = "subject"
    max_osint_people: int = Field(default=8, ge=1, le=30)
    ai_address_matching: bool = True
    related_per_record: int = Field(default=25, ge=1, le=200)
    backend: Literal["live", "demo", "ems"] | None = None
    # Who is searched first when the budget cannot cover everyone at a depth. "priority"
    # spends it on the people most likely to matter (flagged, accused, reached from
    # several directions, or - with several starting people - reached from more than
    # one of them); "breadth" keeps discovery order.
    strategy: Literal["priority", "breadth"] = "priority"
    # Several starting people: stop expanding as soon as stated links join them all.
    # The cheapest way to answer "how are these people connected".
    stop_when_connected: bool = False

    @field_validator("cnic", "phone", "email")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        value = (value or "").strip()
        return value or None


class RunEvent(BaseModel):
    at: datetime = Field(default_factory=utcnow)
    level: Literal["info", "warn", "error", "hit"] = "info"
    message: str


class GraphSnapshot(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
