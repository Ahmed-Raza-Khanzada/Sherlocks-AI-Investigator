"""Common dataframe helpers for report analysis."""

from __future__ import annotations

from dataclasses import dataclass
import re

import pandas as pd


@dataclass
class ResolvedColumns:
    call_type: str | None = None
    media_type: str | None = None
    msisdn: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    b_number: str | None = None
    mins: str | None = None
    secs: str | None = None
    imei: str | None = None
    imsi: str | None = None
    site_address: str | None = None
    latitude: str | None = None
    longitude: str | None = None
    cell_id: str | None = None


def _find(df: pd.DataFrame, *candidates: str) -> str | None:
    for candidate in candidates:
        if candidate in df.columns:
            return candidate
    return None


def resolve_columns(df: pd.DataFrame) -> ResolvedColumns:
    return ResolvedColumns(
        call_type=_find(df, "CALL_TYPE", "CALL_TYPE_2", "CALLTYPE", "DIRECTION"),
        media_type=_find(df, "MEDIA_TYPE"),
        msisdn=_find(df, "MSISDN"),
        start_time=_find(df, "START_TIME"),
        end_time=_find(df, "END_TIME"),
        b_number=_find(df, "B_NUMBER"),
        mins=_find(df, "DURATION_MIN"),
        secs=_find(df, "DURATION_SEC"),
        imei=_find(df, "IMEI"),
        imsi=_find(df, "IMSI"),
        site_address=_find(df, "SITE_ADDRESS"),
        latitude=_find(df, "LATITUDE"),
        longitude=_find(df, "LONGITUDE"),
        cell_id=_find(df, "CELL_ID", "CELLID"),
    )


def prepare_analysis_frame(df: pd.DataFrame, cols: ResolvedColumns, *, copy_frame: bool = True) -> pd.DataFrame:
    prepared = df.copy() if copy_frame else df
    if cols.start_time and cols.start_time in prepared.columns:
        prepared["_DT"] = pd.to_datetime(prepared[cols.start_time], errors="coerce", format="mixed")
    else:
        prepared["_DT"] = pd.NaT

    prepared["_DUR_SEC"] = 0.0
    if cols.mins and cols.mins in prepared.columns:
        prepared["_DUR_SEC"] += pd.to_numeric(prepared[cols.mins], errors="coerce").fillna(0) * 60
    if cols.secs and cols.secs in prepared.columns:
        prepared["_DUR_SEC"] += prepared[cols.secs].apply(_coerce_duration_seconds).fillna(0)
    elif cols.end_time and cols.end_time in prepared.columns and cols.start_time and cols.start_time in prepared.columns:
        end_dt = pd.to_datetime(prepared[cols.end_time], errors="coerce", format="mixed")
        prepared["_DUR_SEC"] = (end_dt - prepared["_DT"]).dt.total_seconds().fillna(0).clip(lower=0)

    if cols.latitude and cols.latitude in prepared.columns:
        prepared["_LAT"] = pd.to_numeric(prepared[cols.latitude], errors="coerce")
    else:
        prepared["_LAT"] = pd.NA
    if cols.longitude and cols.longitude in prepared.columns:
        prepared["_LNG"] = pd.to_numeric(prepared[cols.longitude], errors="coerce")
    else:
        prepared["_LNG"] = pd.NA

    if cols.cell_id and cols.cell_id in prepared.columns:
        prepared[cols.cell_id] = prepared[cols.cell_id].apply(_coerce_cell_id)

    if cols.call_type and cols.call_type in prepared.columns:
        prepared[cols.call_type] = prepared[cols.call_type].astype(str).str.upper().str.strip()

    return prepared


def _coerce_duration_seconds(value: object) -> float:
    text = _safe_text(value)
    if not text or text.lower() in {"nan", "nat", "none"}:
        return 0.0
    if ":" in text:
        parts = [part for part in text.split(":") if part != ""]
        if len(parts) == 2 and all(part.strip().isdigit() for part in parts):
            return float(int(parts[0]) * 60 + int(parts[1]))
    try:
        return float(text)
    except ValueError:
        return 0.0


def _coerce_cell_id(value: object) -> object:
    text = _safe_text(value)
    if not text or text.lower() in {"nan", "none"}:
        return pd.NA
    if re.fullmatch(r"[0-9a-fA-F]+", text) and re.search(r"[a-fA-F]", text):
        try:
            return int(text, 16)
        except ValueError:
            return text
    try:
        return int(float(text))
    except ValueError:
        return text


def _safe_text(value: object) -> str:
    if value is None or value is pd.NA:
        return ""
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    return str(value).strip()
