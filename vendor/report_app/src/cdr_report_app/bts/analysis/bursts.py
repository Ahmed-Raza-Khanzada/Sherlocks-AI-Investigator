"""Burst caller detection: ≥N calls within a sliding time window."""

from __future__ import annotations

import pandas as pd

from cdr_report_app.bts.models import BurstCaller


def burst_callers(
    window_df: pd.DataFrame,
    threshold: int = 5,
    window_minutes: int = 10,
    top: int = 20,
) -> list[BurstCaller]:
    """Identify A-party numbers that placed ≥*threshold* calls within *window_minutes* minutes.

    Uses a sliding window per caller via a sorted timestamp merge-asof approach.
    Returns up to *top* callers ordered by max_calls_in_burst desc.
    """
    df = window_df.dropna(subset=["A_PARTY"]).copy()
    if df.empty:
        return []

    df["CALL_TIME"] = pd.to_datetime(df["CALL_TIME"], errors="coerce")
    df = df.dropna(subset=["CALL_TIME"]).sort_values(["A_PARTY", "CALL_TIME"])

    window_td = pd.Timedelta(minutes=window_minutes)
    results: list[BurstCaller] = []

    for msisdn, group in df.groupby("A_PARTY", observed=True):
        times = group["CALL_TIME"].values
        if len(times) < threshold:
            continue

        max_burst = 0
        burst_start_ts = None
        n = len(times)

        # O(n) sliding window using two pointers
        left = 0
        for right in range(n):
            while (times[right] - times[left]) > window_td.value:
                left += 1
            burst_size = right - left + 1
            if burst_size > max_burst:
                max_burst = burst_size
                burst_start_ts = pd.Timestamp(times[left])

        if max_burst >= threshold:
            results.append(
                BurstCaller(
                    msisdn=str(msisdn),
                    burst_count=_count_burst_windows(times, window_td.value, threshold),
                    max_calls_in_burst=max_burst,
                    first_burst_start=burst_start_ts,
                )
            )

    results.sort(key=lambda r: r.max_calls_in_burst, reverse=True)
    return results[:top]


def _count_burst_windows(times, window_ns: int, threshold: int) -> int:
    """Count how many non-overlapping burst windows exist for this caller."""
    n = len(times)
    count = 0
    i = 0
    while i < n:
        j = i
        while j < n and (times[j] - times[i]) <= window_ns:
            j += 1
        if (j - i) >= threshold:
            count += 1
            i = j  # skip past this burst
        else:
            i += 1
    return count
