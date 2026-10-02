"""IMEI anomaly detection: MSISDNs paired with multiple IMEIs."""

from __future__ import annotations

import pandas as pd

from cdr_report_app.bts.models import ImeiAnomaly


def imei_anomalies(window_df: pd.DataFrame, min_calls: int = 2, top: int = 20) -> list[ImeiAnomaly]:
    """Return MSISDNs seen with more than one distinct IMEI in *window_df*.

    Rows without an IMEI or A_PARTY are excluded. Requires *min_calls* total
    rows for the MSISDN to filter low-signal noise.
    """
    df = window_df.dropna(subset=["A_PARTY", "IMEI"])
    df = df[df["IMEI"].astype(str).str.strip().ne("")]
    if df.empty:
        return []

    # Count distinct IMEIs and total calls per A_PARTY
    imei_counts = df.groupby("A_PARTY", observed=True)["IMEI"].nunique().rename("imei_count")
    call_counts = df.groupby("A_PARTY", observed=True)["IMEI"].count().rename("call_count")
    summary = pd.concat([imei_counts, call_counts], axis=1)
    summary = summary[(summary["imei_count"] > 1) & (summary["call_count"] >= min_calls)]
    summary = summary.sort_values("imei_count", ascending=False).head(top)

    # Collect the actual IMEI values per MSISDN
    imei_lists = (
        df[df["A_PARTY"].isin(summary.index)]
        .groupby("A_PARTY", observed=True)["IMEI"]
        .apply(lambda s: sorted(s.dropna().astype(str).unique().tolist()))
    )

    result = []
    for msisdn, row in summary.iterrows():
        imeis = imei_lists.get(msisdn, [])
        result.append(
            ImeiAnomaly(
                msisdn=str(msisdn),
                imei_count=int(row["imei_count"]),
                imeis=imeis,
                call_count=int(row["call_count"]),
            )
        )
    return result
