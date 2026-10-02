"""Slice the canonical BTS frame by (spec_id, time window)."""

from __future__ import annotations

import pandas as pd

from cdr_report_app.bts.models import BtsTimeWindow, BtsWindowKey


def slice_window(df: pd.DataFrame, spec_id: str, window: BtsTimeWindow, bts_id: str | None = None) -> pd.DataFrame:
    """Return rows in *df* that belong to *spec_id* within the window's time range."""
    mask = (
        (df["SPEC_ID"] == spec_id)
        & (df["CALL_TIME"] >= pd.Timestamp(window.start))
        & (df["CALL_TIME"] <= pd.Timestamp(window.end))
    )
    return df.loc[mask].copy()


def make_window_key(spec_id: str, bts_id: str | None, label: str | None, window: BtsTimeWindow) -> BtsWindowKey:
    return BtsWindowKey(
        spec_id=spec_id,
        bts_id=bts_id,
        label=label,
        window_label=window.label,
        window_start=window.start,
        window_end=window.end,
    )
