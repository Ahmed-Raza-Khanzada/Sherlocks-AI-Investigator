"""Validation for ingested CDR data."""

from __future__ import annotations

import pandas as pd

from cdr_report_app.domain.cdr_models import IngestionWarning


RECOMMENDED_COLUMNS = {
    "MSISDN",
    "START_TIME",
    "B_NUMBER",
    "CALL_TYPE",
    "SITE_ADDRESS",
    "LATITUDE",
    "LONGITUDE",
    "IMEI",
    "IMSI",
}


def validate_normalized_cdr(df: pd.DataFrame) -> list[IngestionWarning]:
    warnings: list[IngestionWarning] = []

    if df.empty:
        warnings.append(IngestionWarning(code="empty_dataset", message="Normalized CDR dataset is empty."))
        return warnings

    missing = sorted(RECOMMENDED_COLUMNS.difference(set(df.columns)))
    if missing:
        warnings.append(
            IngestionWarning(
                code="missing_recommended_columns",
                message=f"Recommended columns missing: {', '.join(missing)}",
            )
        )

    if "START_TIME" not in df.columns:
        warnings.append(
            IngestionWarning(
                code="missing_start_time",
                message="No START_TIME column detected. Time-based report sections may be limited.",
            )
        )

    if "B_NUMBER" not in df.columns:
        warnings.append(
            IngestionWarning(
                code="missing_b_number",
                message="No B_NUMBER column detected. Contact analysis may be limited.",
            )
        )

    return warnings
