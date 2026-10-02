"""Overview and quick stats analysis."""

from __future__ import annotations

import pandas as pd

from cdr_report_app.domain.analysis_models import QuickStats


def build_quick_stats(df: pd.DataFrame, b_number_col: str | None) -> QuickStats:
    total_records = len(df)
    unique_numbers = 0
    repeated_contacts = 0
    duration_label = "-"
    duration_days = 0

    if b_number_col and b_number_col in df.columns:
        counts = df[b_number_col].dropna().astype(str).value_counts()
        unique_numbers = int(counts.shape[0])
        repeated_contacts = int((counts > 1).sum())

    valid_dates = df["_DT"].dropna()
    if not valid_dates.empty:
        duration_days = int((valid_dates.max() - valid_dates.min()).days + 1)
        duration_label = f"{valid_dates.min().strftime('%d %b %Y')} - {valid_dates.max().strftime('%d %b %Y')}"

    return QuickStats(
        total_records=total_records,
        unique_numbers=unique_numbers,
        repeated_contacts=repeated_contacts,
        duration_label=duration_label,
        duration_days=duration_days,
    )
