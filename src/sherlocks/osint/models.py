"""Types for the OSINT layer.

These are deliberately separate from the identity models in ``db.models``. An OSINT
finding is a lead, not a fact, and the type system should make that hard to forget.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class IdentifierKind(StrEnum):
    PHONE = "phone"
    EMAIL = "email"
    USERNAME = "username"
    FULL_NAME = "full_name"
    CNIC = "cnic"
    DOMAIN = "domain"
    URL = "url"


class FindingStatus(StrEnum):
    OK = "ok"
    NO_RESULT = "no_result"
    SKIPPED = "skipped"
    ERROR = "error"
    NOT_CONFIGURED = "not_configured"


_PK_MOBILE_RE = re.compile(r"^(?:\+?92|0)?3\d{9}$")


class OsintSubject(BaseModel):
    """What we know about the person before searching.

    At least one field must be set. A CNIC is accepted because it is how an
    investigator identifies someone here, but note that **no OSINT tool can search by
    CNIC** - it only ever travels along to label the report and tie findings back to a
    person record.
    """

    full_name: str | None = None
    phone: str | None = None
    email: str | None = None
    username: str | None = None
    cnic: str | None = None
    city: str | None = None
    employer: str | None = None
    notes: str | None = None

    @field_validator("phone")
    @classmethod
    def _normalise_phone(cls, value: str | None) -> str | None:
        """Store Pakistani mobiles in E.164 - phoneinfoga requires it."""
        if not value:
            return None
        digits = re.sub(r"\D", "", value)
        if not digits:
            return None
        if digits.startswith("92"):
            digits = digits[2:]
        elif digits.startswith("0"):
            digits = digits[1:]
        if len(digits) == 10 and digits.startswith("3"):
            return f"+92{digits}"
        # Not a recognisable PK mobile; keep it usable rather than dropping it.
        return f"+{digits}" if not value.strip().startswith("+") else value.strip()

    @field_validator("cnic")
    @classmethod
    def _normalise_cnic(cls, value: str | None) -> str | None:
        if not value:
            return None
        digits = re.sub(r"\D", "", value)
        return digits or None

    def is_empty(self) -> bool:
        return not any(
            [self.full_name, self.phone, self.email, self.username, self.cnic]
        )

    def available_kinds(self) -> set[IdentifierKind]:
        kinds: set[IdentifierKind] = set()
        if self.full_name:
            kinds.add(IdentifierKind.FULL_NAME)
        if self.phone:
            kinds.add(IdentifierKind.PHONE)
        if self.email:
            kinds.add(IdentifierKind.EMAIL)
        if self.username:
            kinds.add(IdentifierKind.USERNAME)
        if self.cnic:
            kinds.add(IdentifierKind.CNIC)
        return kinds

    def label(self) -> str:
        """A human-readable name for the report header."""
        return (
            self.full_name
            or self.username
            or self.email
            or self.phone
            or (f"CNIC {self.cnic}" if self.cnic else "Unknown subject")
        )


class PlannedTool(BaseModel):
    """One intended tool invocation, decided before anything runs."""

    tool: str
    query: str
    rationale: str = ""
    # Set when the tool is in the plan but cannot run - missing key, missing binary.
    skip_reason: str | None = None

    @property
    def runnable(self) -> bool:
        return self.skip_reason is None


class OsintPlan(BaseModel):
    subject_label: str
    tools: list[PlannedTool] = Field(default_factory=list)
    reasoning: str = ""

    @property
    def runnable_tools(self) -> list[PlannedTool]:
        return [t for t in self.tools if t.runnable]


class ToolResult(BaseModel):
    """The outcome of one tool invocation.

    ``confidence`` is fixed at ``low`` for everything produced here. Raising it is a
    human decision recorded on the database row, never something this layer does.
    """

    tool: str
    query: str
    status: FindingStatus
    data: dict[str, Any] | None = None
    summary: str = ""
    error: str | None = None
    duration_ms: int = 0
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    confidence: str = "low"

    @property
    def is_hit(self) -> bool:
        return self.status == FindingStatus.OK


class OsintReport(BaseModel):
    """Everything one run produced, ready to render."""

    subject: OsintSubject
    plan: OsintPlan
    results: list[ToolResult] = Field(default_factory=list)
    narrative: str = ""
    # Identifiers the run surfaced that were not supplied - candidate leads only.
    discovered_identifiers: list[dict[str, str]] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def hits(self) -> list[ToolResult]:
        return [r for r in self.results if r.is_hit]

    @property
    def counts(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for result in self.results:
            tally[result.status.value] = tally.get(result.status.value, 0) + 1
        return tally
