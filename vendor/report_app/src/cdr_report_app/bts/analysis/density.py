"""Hourly call density analysis with mean+2σ spike detection."""

from __future__ import annotations

import numpy as np
import pandas as pd

from cdr_report_app.bts.models import HourlyBucket, HourlyDensity, BtsWindowKey


def hourly_density(window_df: pd.DataFrame, key: BtsWindowKey) -> HourlyDensity:
    """Compute per-hour call counts and flag spikes (mean + 2σ)."""
    if window_df.empty:
        return HourlyDensity(
            window_key=key,
            buckets=[],
            mean_calls=0.0,
            spike_threshold=0.0,
        )

    times = pd.to_datetime(window_df["CALL_TIME"], errors="coerce").dropna()
    if times.empty:
        return HourlyDensity(window_key=key, buckets=[], mean_calls=0.0, spike_threshold=0.0)

    hour_counts = times.dt.hour.value_counts().sort_index()

    # Fill in all 24 hours so gaps are visible
    all_hours = pd.Series(0, index=range(24))
    all_hours.update(hour_counts)

    values = all_hours.values.astype(float)
    mean = float(np.mean(values))
    std = float(np.std(values))
    threshold = mean + 2 * std

    buckets = [
        HourlyBucket(hour=int(h), call_count=int(c), is_spike=float(c) > threshold)
        for h, c in enumerate(values)
    ]

    return HourlyDensity(
        window_key=key,
        buckets=buckets,
        mean_calls=round(mean, 2),
        spike_threshold=round(threshold, 2),
    )
