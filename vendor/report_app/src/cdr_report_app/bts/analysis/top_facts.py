"""Simple top-10 fact analyzers — pure pandas group-bys, no anomaly logic."""

from __future__ import annotations

import pandas as pd

from cdr_report_app.bts.models import (
    TopCell,
    TopFacts,
    TopImei,
    TopLongCall,
    TopMinute,
    TopPair,
)


def _safe_int(v) -> int:
    try:
        if pd.isna(v):
            return 0
        return int(v)
    except (TypeError, ValueError):
        return 0


def top_cells(window_df: pd.DataFrame, n: int = 10) -> list[TopCell]:
    if window_df.empty or "CELL_ID" not in window_df.columns:
        return []
    df = window_df.dropna(subset=["CELL_ID"])
    df = df[df["CELL_ID"].astype(str).str.strip() != ""]
    if df.empty:
        return []

    # If LAC_ID is present and non-empty, group by (LAC, CELL) — the same
    # cell_id under different LACs is a different physical sector.
    has_lac = (
        "LAC_ID" in df.columns
        and df["LAC_ID"].dropna().astype(str).str.strip().ne("").any()
    )
    group_cols = ["LAC_ID", "CELL_ID"] if has_lac else ["CELL_ID"]
    if has_lac:
        df = df.assign(LAC_ID=df["LAC_ID"].astype("string").fillna(""))

    g = df.groupby(group_cols, observed=True).agg(
        call_count=("A_PARTY", "size"),
        unique_a_party=("A_PARTY", lambda s: s.dropna().nunique()),
        total_duration_sec=("DURATION_SEC", "sum"),
    ).reset_index()
    g = g.sort_values("call_count", ascending=False).head(n)

    rows: list[TopCell] = []
    for _, row in g.iterrows():
        lac_value: str | None = None
        if has_lac:
            raw_lac = str(row["LAC_ID"]).strip()
            lac_value = raw_lac or None
        rows.append(TopCell(
            cell_id=str(row["CELL_ID"]),
            call_count=_safe_int(row["call_count"]),
            unique_a_party=_safe_int(row["unique_a_party"]),
            total_duration_sec=_safe_int(row["total_duration_sec"]),
            lac_id=lac_value,
        ))
    return rows


def top_longest_calls(window_df: pd.DataFrame, n: int = 10) -> list[TopLongCall]:
    if window_df.empty or "DURATION_SEC" not in window_df.columns:
        return []
    df = window_df.dropna(subset=["DURATION_SEC"])
    if df.empty:
        return []
    df = df.sort_values("DURATION_SEC", ascending=False).head(n)

    def _opt(v):
        if v is None:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        text = str(v).strip()
        if not text or text.lower() in {"<na>", "nan", "none"}:
            return None
        return text

    return [
        TopLongCall(
            a_party=str(row.get("A_PARTY") or "-"),
            b_party=str(row.get("B_PARTY") or "-"),
            duration_sec=_safe_int(row.get("DURATION_SEC")),
            call_time=pd.Timestamp(row["CALL_TIME"]).to_pydatetime(),
            direction=str(row.get("DIRECTION") or "UNK"),
            cell_id=_opt(row.get("CELL_ID")),
            lac_id=_opt(row.get("LAC_ID")),
        )
        for _, row in df.iterrows()
    ]


def top_imeis(window_df: pd.DataFrame, n: int = 10) -> list[TopImei]:
    if window_df.empty or "IMEI" not in window_df.columns:
        return []
    df = window_df.dropna(subset=["IMEI"])
    df = df[df["IMEI"].astype(str).str.strip() != ""]
    if df.empty:
        return []
    g = df.groupby("IMEI", observed=True).agg(
        call_count=("A_PARTY", "size"),
        unique_a_party=("A_PARTY", lambda s: s.dropna().nunique()),
    ).reset_index()
    g = g.sort_values("call_count", ascending=False).head(n)
    return [
        TopImei(
            imei=str(row["IMEI"]),
            call_count=_safe_int(row["call_count"]),
            unique_a_party=_safe_int(row["unique_a_party"]),
        )
        for _, row in g.iterrows()
    ]


def top_frequent_pairs(window_df: pd.DataFrame, n: int = 10) -> list[TopPair]:
    if window_df.empty:
        return []
    df = window_df.dropna(subset=["A_PARTY", "B_PARTY"])
    if df.empty:
        return []
    g = df.groupby(["A_PARTY", "B_PARTY"], observed=True).agg(
        call_count=("CALL_TIME", "size"),
        total_duration_sec=("DURATION_SEC", "sum"),
    ).reset_index()
    g = g.sort_values("call_count", ascending=False).head(n)
    return [
        TopPair(
            a_party=str(row["A_PARTY"]),
            b_party=str(row["B_PARTY"]),
            call_count=_safe_int(row["call_count"]),
            total_duration_sec=_safe_int(row["total_duration_sec"]),
        )
        for _, row in g.iterrows()
    ]


def top_busiest_minutes(window_df: pd.DataFrame, n: int = 10) -> list[TopMinute]:
    if window_df.empty or "CALL_TIME" not in window_df.columns:
        return []
    minutes = pd.to_datetime(window_df["CALL_TIME"]).dt.floor("min")
    counts = minutes.value_counts().head(n).sort_values(ascending=False)
    return [
        TopMinute(minute=pd.Timestamp(ts).to_pydatetime(), call_count=int(c))
        for ts, c in counts.items()
    ]


def compute_top_facts(window_df: pd.DataFrame, n: int = 10) -> TopFacts:
    """Run all five top-N analyzers on the window and bundle the results."""
    return TopFacts(
        top_cells=top_cells(window_df, n),
        top_longest_calls=top_longest_calls(window_df, n),
        top_imeis=top_imeis(window_df, n),
        top_frequent_pairs=top_frequent_pairs(window_df, n),
        top_busiest_minutes=top_busiest_minutes(window_df, n),
    )
