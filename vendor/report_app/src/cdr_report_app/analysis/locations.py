"""Location and movement-related analysis."""

from __future__ import annotations

import pandas as pd

from cdr_report_app.domain.analysis_models import LocationVisit, MovementStep, StayRecord
from cdr_report_app.domain.cdr_models import CrimeContext


def _coord_text(lat: float | None, lng: float | None) -> str | None:
    if lat is None or lng is None:
        return None
    return f"{lat:.5f}, {lng:.5f}"


def build_top_locations(
    df: pd.DataFrame,
    location_col: str | None,
    limit: int,
) -> list[LocationVisit]:
    if not location_col or location_col not in df.columns:
        return []

    counts = df[location_col].dropna().astype(str).value_counts().head(limit)
    rows: list[LocationVisit] = []

    for idx, (location, count) in enumerate(counts.items(), start=1):
        subset = df[df[location_col].astype(str) == str(location)]
        lat = subset["_LAT"].dropna().iloc[0] if "_LAT" in subset.columns and subset["_LAT"].dropna().any() else None
        lng = subset["_LNG"].dropna().iloc[0] if "_LNG" in subset.columns and subset["_LNG"].dropna().any() else None
        if subset["_DT"].dropna().empty:
            timing = "N/A"
        else:
            peak_hours = subset["_DT"].dropna().dt.hour.mode()
            if peak_hours.empty:
                timing = "N/A"
            else:
                ph = int(peak_hours.iloc[0])
                timing = f"{max(0, ph - 2):02d}:00 - {min(23, ph + 2):02d}:00"
        rows.append(
            LocationVisit(
                order=idx,
                location=str(location)[:60],
                coordinates_text=_coord_text(float(lat), float(lng)) if lat is not None and lng is not None else None,
                latitude=float(lat) if lat is not None else None,
                longitude=float(lng) if lng is not None else None,
                visits=int(count),
                timing_window=timing,
                duration_pct=round((int(count) / len(df)) * 100, 1) if len(df) else None,
            )
        )

    return rows


def build_long_stays(df: pd.DataFrame, location_col: str | None, limit: int = 5) -> list[StayRecord]:
    if not location_col or location_col not in df.columns:
        return []

    ordered = df.dropna(subset=["_DT", location_col]).sort_values("_DT").copy()
    if ordered.empty:
        return []

    ordered["BLOCK"] = (ordered[location_col] != ordered[location_col].shift(1)).cumsum()
    blocks = (
        ordered.groupby(["BLOCK", location_col], dropna=False)
        .agg(
            START=("_DT", "min"),
            END=("_DT", "max"),
            LAT=("_LAT", "first"),
            LNG=("_LNG", "first"),
        )
        .reset_index()
    )
    blocks["DURATION_HOURS"] = (blocks["END"] - blocks["START"]).dt.total_seconds() / 3600
    blocks = blocks[blocks["DURATION_HOURS"] > 0].sort_values("DURATION_HOURS", ascending=False).head(limit)

    stays: list[StayRecord] = []
    for _, row in blocks.iterrows():
        lat = float(row["LAT"]) if pd.notna(row["LAT"]) else None
        lng = float(row["LNG"]) if pd.notna(row["LNG"]) else None
        stays.append(
            StayRecord(
                time_period=f"{row['START'].strftime('%d %b %H:%M')} - {row['END'].strftime('%d %b %H:%M')}",
                location=str(row[location_col])[:60],
                coordinates_text=_coord_text(lat, lng),
                duration_hours=round(float(row["DURATION_HOURS"]), 1),
                latitude=lat,
                longitude=lng,
            )
        )
    return stays


def build_crime_day_movement(df: pd.DataFrame, location_col: str | None, crime_context: CrimeContext) -> list[MovementStep]:
    if not crime_context.crime_date or not location_col or location_col not in df.columns:
        return []

    crime_day = pd.Timestamp(crime_context.crime_date).date()
    filtered = df.dropna(subset=["_DT"]).copy()
    filtered = filtered[filtered["_DT"].dt.date == crime_day].sort_values("_DT")
    filtered = filtered.dropna(subset=[location_col])
    if filtered.empty:
        return []

    filtered["LOC_CHANGE"] = filtered[location_col] != filtered[location_col].shift(1)
    filtered["SEQ"] = filtered["LOC_CHANGE"].cumsum()
    steps = (
        filtered.groupby("SEQ")
        .agg(
            LOCATION=(location_col, "first"),
            START=("_DT", "min"),
            END=("_DT", "max"),
            RECORDS=("_DT", "count"),
            LAT=("_LAT", "first"),
            LNG=("_LNG", "first"),
        )
        .reset_index()
    )

    output: list[MovementStep] = []
    for _, row in steps.iterrows():
        lat = float(row["LAT"]) if pd.notna(row["LAT"]) else None
        lng = float(row["LNG"]) if pd.notna(row["LNG"]) else None
        output.append(
            MovementStep(
                order=int(row["SEQ"]),
                location=str(row["LOCATION"])[:60],
                coordinates_text=_coord_text(lat, lng),
                records=int(row["RECORDS"]),
                time_window=f"{row['START'].strftime('%H:%M')} - {row['END'].strftime('%H:%M')}",
                latitude=lat,
                longitude=lng,
            )
        )
    return output
