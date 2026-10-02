"""SQLAlchemy models.

Phase 1 covers the identity spine (person / identifier / case) plus everything the
OSINT layer needs to record what it did and what it found. Claims, edges, evidence
and the provider caches arrive in Phase 2 as new migrations.

Two rules hold across the whole schema and should survive every later change:

1. Every assertion is traceable. A row that states something about a human being
   carries the tool or provider that produced it, when, and the raw payload.
2. OSINT findings are never evidence. They are stored in their own table, always at
   low confidence, and are not permitted to create links on their own.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Every table lives in this Postgres schema. Keeping it a module constant (rather
# than reading settings here) keeps the model layer import-time side-effect free;
# ``session.py`` ensures the schema exists before creating anything in it.
SCHEMA = "sherlocks"


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA)


# --------------------------------------------------------------------------------------
# Identity spine
# --------------------------------------------------------------------------------------


class Person(Base):
    """A resolved human being.

    ``merged_into`` rather than deletion: an identity merge must always be reversible,
    because a wrong merge invents a relationship between two real citizens.
    """

    __tablename__ = "person"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    display_name: Mapped[str | None] = mapped_column(String(255))
    canonical_cnic: Mapped[str | None] = mapped_column(String(13), index=True)
    # How sure we are this cluster is one person, not two people merged by accident.
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    merged_into: Mapped[str | None] = mapped_column(
        String(36), ForeignKey(f"{SCHEMA}.person.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    identifiers: Mapped[list[Identifier]] = relationship(
        back_populates="person", cascade="all, delete-orphan"
    )


class Identifier(Base):
    """A handle that points at a person: CNIC, MSISDN, IMEI, email, username...

    ``value_norm`` is the comparison key and is unique per kind, so the same phone
    number can never anchor two different people. ``value_raw`` keeps what the source
    actually said, for the audit trail.
    """

    __tablename__ = "identifier"
    __table_args__ = (
        UniqueConstraint("kind", "value_norm", name="uq_identifier_kind_value"),
        Index("ix_identifier_person", "person_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    person_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey(f"{SCHEMA}.person.id", ondelete="CASCADE")
    )
    # cnic | msisdn | imei | imsi | passport | licence | email | username | url
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    value_norm: Mapped[str] = mapped_column(String(255), nullable=False)
    value_raw: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str | None] = mapped_column(String(64))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    person: Mapped[Person | None] = relationship(back_populates="identifiers")


class Case(Base):
    """An investigation. Groups the people under examination together."""

    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    fir_no: Mapped[str | None] = mapped_column(String(64))
    police_station: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CasePerson(Base):
    __tablename__ = "case_person"
    __table_args__ = (UniqueConstraint("case_id", "person_id", name="uq_case_person"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(f"{SCHEMA}.cases.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(f"{SCHEMA}.person.id", ondelete="CASCADE"), nullable=False
    )
    # seed = the analyst supplied them; discovered = the pipeline found them
    role: Mapped[str] = mapped_column(String(32), default="seed", nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --------------------------------------------------------------------------------------
# OSINT
# --------------------------------------------------------------------------------------


class OsintRun(Base):
    """One OSINT investigation against one subject.

    The plan is stored alongside the results so an analyst can see not just what was
    found but what was searched for - including tools that were skipped, and why.
    """

    __tablename__ = "osint_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    case_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey(f"{SCHEMA}.cases.id", ondelete="SET NULL")
    )
    person_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey(f"{SCHEMA}.person.id", ondelete="SET NULL")
    )
    subject: Mapped[dict] = mapped_column(JSONB, nullable=False)
    plan: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    tools_planned: Mapped[int] = mapped_column(Integer, default=0)
    tools_run: Mapped[int] = mapped_column(Integer, default=0)
    tools_succeeded: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    findings: Mapped[list[OsintFinding]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class OsintFinding(Base):
    """One tool's result.

    ``confidence`` defaults to ``low`` and the report renderer stamps every one of
    these as unverified. Promoting a finding to something stronger requires a human
    setting ``verified_by_user``.
    """

    __tablename__ = "osint_finding"
    __table_args__ = (Index("ix_osint_finding_run_tool", "run_id", "tool"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey(f"{SCHEMA}.osint_run.id", ondelete="CASCADE"), nullable=False
    )
    person_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey(f"{SCHEMA}.person.id", ondelete="SET NULL")
    )
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    # ok | no_result | skipped | error | not_configured
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB)
    summary: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[str] = mapped_column(String(16), default="low", nullable=False)
    verified_by_user: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    run: Mapped[OsintRun] = relationship(back_populates="findings")


# --------------------------------------------------------------------------------------
# Link graph
# --------------------------------------------------------------------------------------


class GraphRun(Base):
    """One link-graph expansion: the request, the resulting graph, and its run log.

    The graph is stored whole as JSONB. It is a view over provider answers that are
    themselves cached in ``provider_cache``, so it is cheap to rebuild - but an analyst
    reopening last week's graph should see exactly what was seen then.
    """

    __tablename__ = "graph_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    seed_label: Mapped[str | None] = mapped_column(String(255))
    backend: Mapped[str] = mapped_column(String(16), default="demo", nullable=False)
    params: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # queued | running | completed | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    progress_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[str | None] = mapped_column(String(500))
    stats: Mapped[dict | None] = mapped_column(JSONB)
    graph: Mapped[dict | None] = mapped_column(JSONB)
    events: Mapped[list | None] = mapped_column(JSONB)
    created_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderCacheEntry(Base):
    """A provider's answer for one (backend, system, cnic, phone). See linkgraph.cache."""

    __tablename__ = "provider_cache"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    system: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


# --------------------------------------------------------------------------------------
# Infrastructure
# --------------------------------------------------------------------------------------


class LlmCache(Base):
    """Memoised LLM output.

    Address and name normalisation sees the same strings over and over. Keyed on a
    hash of (kind, model, prompt version, input) so a prompt change invalidates
    cleanly instead of silently serving stale parses.
    """

    __tablename__ = "llm_cache"

    input_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(16), nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    output: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Job(Base):
    """Async job tracking.

    Same shape as ``report_jobs`` in cdr_report_app so the two services' job APIs feel
    identical from a client's point of view.
    """

    __tablename__ = "sherlocks_job"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # osint | dossier | case_analysis
    job_type: Mapped[str] = mapped_column(String(32), default="osint", nullable=False)
    # pending | queued | processing | completed | failed
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    progress_pct: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[str | None] = mapped_column(String(500))
    request: Mapped[dict | None] = mapped_column(JSONB)
    result_file_path: Mapped[str | None] = mapped_column(String(500))
    result_excel_path: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
