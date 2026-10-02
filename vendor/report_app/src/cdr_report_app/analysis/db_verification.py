"""Database verification section shaping."""

from __future__ import annotations

from typing import Any

from cdr_report_app.domain.analysis_models import DatabaseVerificationRow


REPORT_DATABASE_ORDER = ["SIMSDB", "SUBSCRIBER", "WATCHLIST", "SBVS", "PRVS", "HRMIS", "EVS", "HOPE", "DLS", "HOTEL_EYE", "TRACS", "OLD_TENANT", "CRO", "PSRMS"]
ALLOWED_VERIFICATION_DATABASES = set(REPORT_DATABASE_ORDER)


def build_db_verification_rows(db_results: dict[str, Any] | None) -> list[DatabaseVerificationRow]:
    if not isinstance(db_results, dict):
        return []

    rows_by_name: dict[str, DatabaseVerificationRow] = {}
    for db_name, result in db_results.items():
        if not isinstance(result, dict):
            continue
        normalized_name = str(db_name).upper()
        if normalized_name not in ALLOWED_VERIFICATION_DATABASES:
            continue
        status_value = str(result.get("status", "")).lower()
        if result.get("error") or status_value == "error":
            status = "Error"
            severity = "neutral"
        elif result.get("hit"):
            status = "Found"
            severity = "alert"
        else:
            status = "No Record"
            severity = "safe"
        rows_by_name[normalized_name] = DatabaseVerificationRow(
            database=normalized_name,
            status=status,
            summary=str(result.get("summary", "-"))[:200],
            severity=severity,
        )

    return [rows_by_name[name] for name in REPORT_DATABASE_ORDER if name in rows_by_name]
