"""Orchestration and persistence for one OSINT run.

Persistence is not an afterthought here. A run's value to an investigation is that
months later somebody can ask "what did you search, what came back, and when" - so the
plan, every tool outcome including the ones that failed, and the raw text are all
written before the PDF is produced. The PDF is a view; the rows are the record.

Findings are written at ``confidence='low'`` and ``verified_by_user=False``, and nothing
in this module writes an ``edge``. Promotion is a human act.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from sherlocks.agents.cache import MemoryLlmCache, PostgresLlmCache
from sherlocks.agents.ollama import OllamaClient
from sherlocks.agents.osint_agent import OsintAgent
from sherlocks.db.models import OsintFinding, OsintRun
from sherlocks.db.session import session_scope
from sherlocks.osint.client import OsintClient, OsintDisabled
from sherlocks.osint.models import FindingStatus, OsintReport, OsintSubject
from sherlocks.render.osint_report import render_osint_report
from sherlocks.settings import Settings, load_settings

logger = logging.getLogger(__name__)

# Raw tool output is the evidence trail, so it is stored whole - but a runaway scrape
# should not put a megabyte into a JSONB column.
_MAX_STORED_RAW_CHARS = 200_000


@dataclass(slots=True)
class OsintRunOutcome:
    run_id: str
    report: OsintReport
    pdf_path: Path | None
    error: str | None = None


def build_agent(settings: Settings, session: Session | None = None) -> OsintAgent:
    """Wire an agent. Falls back to an in-memory LLM cache with no session."""
    import os

    # OSINT binaries (sherlock) write a <query>.txt into the CWD; keep those in one
    # scratch folder under output/, never the project root.
    os.environ.setdefault("SHERLOCKS_OSINT_WORKDIR", str(settings.output_path / "osint_work"))

    cache = PostgresLlmCache(session) if session is not None else MemoryLlmCache()
    llm = OllamaClient(settings.ollama, cache=cache) if settings.ollama.ready else None
    return OsintAgent(OsintClient(settings), llm=llm)


def run_osint(
    subject: OsintSubject,
    *,
    settings: Settings | None = None,
    session: Session | None = None,
    job_id: str | None = None,
    case_id: str | None = None,
    person_id: str | None = None,
    render_pdf: bool = True,
) -> OsintRunOutcome:
    """Execute a scan, persist it, and optionally render the PDF.

    Supplying ``session`` keeps the caller in charge of the transaction. Without one a
    session is opened per phase, so a long scan does not hold a connection open while
    waiting on the network.
    """
    settings = settings or load_settings()

    if session is not None:
        return _run(subject, settings, session, job_id, case_id, person_id, render_pdf)
    with session_scope() as owned:
        return _run(subject, settings, owned, job_id, case_id, person_id, render_pdf)


def _run(
    subject: OsintSubject,
    settings: Settings,
    session: Session,
    job_id: str | None,
    case_id: str | None,
    person_id: str | None,
    render_pdf: bool,
) -> OsintRunOutcome:
    agent = build_agent(settings, session)

    run = OsintRun(
        job_id=job_id,
        case_id=case_id,
        person_id=person_id,
        subject=subject.model_dump(mode="json"),
        status="running",
        started_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()  # we need run.id before anything references it
    run_id = run.id

    try:
        plan = agent.plan(subject)
        run.plan = plan.model_dump(mode="json")
        run.tools_planned = len(plan.tools)
        session.flush()

        results = agent.client.run_planned(plan.tools) if plan.tools else []
        discovered = agent.extract_identifiers(results, subject)
        narrative = agent.narrate(subject, results, discovered)

        report = OsintReport(
            subject=subject,
            plan=plan,
            results=results,
            narrative=narrative,
            discovered_identifiers=discovered,
            started_at=run.started_at,
            finished_at=datetime.now(UTC),
        )

        for result in results:
            raw = (result.data or {}).get("raw") or ""
            session.add(
                OsintFinding(
                    run_id=run_id,
                    person_id=person_id,
                    tool=result.tool,
                    query=result.query[:512],
                    status=result.status.value,
                    result={
                        "raw": raw[:_MAX_STORED_RAW_CHARS],
                        "truncated": len(raw) > _MAX_STORED_RAW_CHARS,
                        "discovered": [
                            item for item in discovered if item.get("tool") == result.tool
                        ],
                    },
                    summary=result.summary,
                    confidence=result.confidence,
                    verified_by_user=False,
                    error=result.error,
                    duration_ms=result.duration_ms,
                    fetched_at=result.fetched_at,
                )
            )

        run.tools_run = sum(
            1 for r in results if r.status not in (FindingStatus.SKIPPED, FindingStatus.NOT_CONFIGURED)
        )
        run.tools_succeeded = sum(1 for r in results if r.status == FindingStatus.OK)
        run.summary = narrative
        run.finished_at = report.finished_at
        run.status = "completed"
        session.flush()

        pdf_path: Path | None = None
        if render_pdf:
            pdf_path = render_osint_report(report, settings=settings)

        return OsintRunOutcome(run_id=run_id, report=report, pdf_path=pdf_path)

    except OsintDisabled as exc:
        # A configuration refusal, not a failure. Recorded so the run is not a mystery.
        _mark_terminal(run_id, "refused", str(exc), settings)
        raise
    except Exception as exc:
        logger.exception("OSINT run %s failed", run_id)
        _mark_terminal(run_id, "failed", f"{type(exc).__name__}: {exc}", settings)
        raise


def _mark_terminal(run_id: str, status: str, error: str, settings: Settings) -> None:
    """Record a run's failure in its own transaction.

    The enclosing ``session_scope`` rolls back on the exception that brought us here, so
    writing the failure on that session would discard it - the run row would be left
    saying "running" forever. A separate transaction is the only way the reason survives.
    """
    try:
        with session_scope(settings) as session:
            run = session.get(OsintRun, run_id)
            if run is None:
                return
            run.status = status
            run.error = error[:4000]
            run.finished_at = datetime.now(UTC)
    except Exception:  # pragma: no cover - never mask the original failure
        logger.exception("Could not record terminal status for OSINT run %s", run_id)
