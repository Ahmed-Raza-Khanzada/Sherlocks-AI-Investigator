"""Per-provider BTS readers.

Each public `read_*_bts` function returns a `pandas.DataFrame` with the
canonical schema defined in `cdr_report_app.bts.models.CANONICAL_BTS_COLUMNS`.

Telenor is intentionally a placeholder in Phase 1 — see `read_telenor_bts`.
"""

from __future__ import annotations

import gc
import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from cdr_report_app.bts.models import CANONICAL_BTS_COLUMNS
from cdr_report_app.ingest.loaders import read_raw_cdr_file
from cdr_report_app.ingest.normalizers import apply_column_mappings
from cdr_report_app.ingest.schema_detection import build_column_mappings, detect_header_row
from cdr_report_app.utils.phones import normalize_mobile

logger = logging.getLogger(__name__)


def _load_with_headers(path: Path, pre_rename: dict[str, str] | None = None) -> pd.DataFrame:
    """Read a file, detect the header row, and return a frame with proper columns.

    `pre_rename` (case-insensitive) applies provider-specific header renames
    BEFORE the generic column-mapping pipeline runs, for cases where a
    provider's BTS export uses different column names than its CDR export.
    """
    raw = read_raw_cdr_file(path)
    if raw.empty or len(raw) < 2:
        raise ValueError(f"BTS file is empty or too small: {path}")
    header_row = detect_header_row(raw)
    headers = [str(cell).strip() or f"COLUMN_{i + 1}" for i, cell in enumerate(raw.iloc[header_row].tolist())]
    if pre_rename:
        lower_map = {k.lower(): v for k, v in pre_rename.items()}
        headers = [lower_map.get(h.lower(), h) for h in headers]
    body = raw.iloc[header_row + 1 :].copy()
    body.columns = headers
    body = body[body.apply(lambda row: any(str(v).strip() for v in row), axis=1)].reset_index(drop=True)
    del raw
    mappings = build_column_mappings(body)
    normalized = apply_column_mappings(body, mappings)
    del body
    return normalized


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame({col: pd.Series(dtype="object") for col in CANONICAL_BTS_COLUMNS})


def _parse_timestamp_with_format(
    series: pd.Series,
    primary_format: str | None,
    *,
    provider: str,
    fallback_dayfirst: bool = False,
) -> pd.Series:
    """Parse a timestamp column with a hardcoded format, falling back to fuzzy parse if too many NaT."""
    if primary_format is None:
        return pd.to_datetime(series, errors="coerce", dayfirst=fallback_dayfirst)

    parsed = pd.to_datetime(series, format=primary_format, errors="coerce")
    nat_rate = parsed.isna().mean() if len(parsed) else 0.0
    if nat_rate > 0.5:
        logger.warning(
            "BTS timestamp primary parse low success | provider=%s format=%s nat_rate=%.2f → falling back to fuzzy parse",
            provider, primary_format, nat_rate,
        )
        parsed = pd.to_datetime(series, errors="coerce", dayfirst=fallback_dayfirst)
    return parsed


def _finalize_canonical(
    df: pd.DataFrame,
    *,
    provider: str,
    spec_id: str,
    bts_id: str | None,
    source_file: str,
    direction_series: pd.Series | None = None,
    timestamp_format: str | None = None,
    timestamp_dayfirst: bool = False,
) -> pd.DataFrame:
    """Project a normalized frame onto the canonical BTS schema with tight dtypes."""
    if df.empty:
        return _empty_frame()

    def col(name: str) -> pd.Series:
        return df[name] if name in df.columns else pd.Series([pd.NA] * len(df))

    def _clean_number(v: Any) -> str | None:
        if pd.isna(v):
            return None
        normalized = normalize_mobile(v)
        if normalized:
            return normalized
        text = str(v).strip()
        return text or None

    a_party = col("MSISDN").apply(_clean_number)
    b_party = col("B_NUMBER").apply(_clean_number)

    if direction_series is not None:
        direction = direction_series.fillna("UNK").astype(str).str.upper().str.strip()
    else:
        direction = col("CALL_TYPE").astype(str).str.upper().str.strip()
    # Normalize substring matches first (catches "Incoming SMS", "Outgoing Call",
    # "Incomming" typo, etc.), then enforce the canonical alphabet.
    direction = direction.mask(direction.str.contains("INCOM", na=False), "IN")
    direction = direction.mask(direction.str.contains("OUTGO", na=False), "OUT")
    direction = direction.where(direction.isin(["IN", "OUT"]), "UNK")

    call_time = _parse_timestamp_with_format(
        col("START_TIME"), timestamp_format,
        provider=provider, fallback_dayfirst=timestamp_dayfirst,
    )

    duration = pd.to_numeric(col("DURATION_SEC"), errors="coerce").astype("Int32")
    lat = pd.to_numeric(col("LATITUDE"), errors="coerce").astype("float32")
    lng = pd.to_numeric(col("LONGITUDE"), errors="coerce").astype("float32")

    cell_id = col("CELL_ID").astype("string")
    lac_id = col("LAC_ID").astype("string")
    imei = col("IMEI").astype("string")
    location = col("SITE_ADDRESS").astype("string")

    canonical = pd.DataFrame(
        {
            "SPEC_ID": pd.Series([spec_id] * len(df), dtype="category"),
            "BTS_ID": pd.Series([bts_id or ""] * len(df), dtype="string"),
            "A_PARTY": pd.Series(a_party.values, dtype="string"),
            "B_PARTY": pd.Series(b_party.values, dtype="string"),
            "DIRECTION": pd.Series(direction.values, dtype="category"),
            "CALL_TIME": call_time.values,
            "DURATION_SEC": duration.values,
            "IMEI": imei.values,
            "CELL_ID": cell_id.values,
            "LAC_ID": lac_id.values,
            "LAT": lat.values,
            "LNG": lng.values,
            "LOCATION": location.values,
            "PROVIDER": pd.Series([provider] * len(df), dtype="category"),
            "SOURCE_FILE": pd.Series([source_file] * len(df), dtype="string"),
        }
    )

    # Drop rows without any party or timestamp — unusable for analysis.
    canonical = canonical[canonical["CALL_TIME"].notna()]
    mask_has_party = canonical["A_PARTY"].notna() | canonical["B_PARTY"].notna()
    canonical = canonical[mask_has_party]
    canonical = canonical.sort_values("CALL_TIME").reset_index(drop=True)

    logger.info(
        "BTS reader completed | provider=%s spec=%s rows=%d source=%s",
        provider,
        spec_id,
        len(canonical),
        source_file,
    )
    return canonical


def read_ufone_bts(
    path: Path,
    *,
    spec_id: str,
    bts_id: str | None = None,
) -> pd.DataFrame:
    """Read a Ufone .txt BTS dump.

    Tab-delimited with columns including A_Number, B_Number, Call_Start_Time,
    Cell_ID, CALL_INBOUND_OUTBOUND_DESC, LOCATION, IMEI. Multiple cell IDs
    are preserved per row. Timestamp format: DD/MM/YYYY HH:MM:SS (24h).
    """
    normalized = _load_with_headers(path)
    direction = (
        normalized["CALL_INBOUND_OUTBOUND_DESC"].astype("string").str.strip()
        if "CALL_INBOUND_OUTBOUND_DESC" in normalized.columns
        else None
    )
    return _finalize_canonical(
        normalized,
        provider="ufone",
        spec_id=spec_id,
        bts_id=bts_id,
        source_file=path.name,
        direction_series=direction,
        timestamp_format="%d/%m/%Y %H:%M:%S",
        timestamp_dayfirst=True,
    )


def read_jazz_bts(
    path: Path,
    *,
    spec_id: str,
    bts_id: str | None = None,
) -> pd.DataFrame:
    """Read a Jazz .xls BTS dump.

    Columns: Sr#, A-party, B-party, Date & Time, Duration, IMEI, Call Type, Cell ID.
    One file = one tower. No tower-id column in raw file. No LAT/LNG, no LAC.
    Timestamp format: MM/DD/YYYY HH:MM:SS (24h).
    """
    normalized = _load_with_headers(path)
    return _finalize_canonical(
        normalized,
        provider="jazz",
        spec_id=spec_id,
        bts_id=bts_id,
        source_file=path.name,
        timestamp_format="%m/%d/%Y %H:%M:%S",
    )


_ZONG_BTS_PRE_RENAME = {
    # Zong's BTS export uses different headers than its CDR export.
    # Map them to names that the generic column pipeline already understands.
    "DLG_NO": "MSISDN",
    "DLD_NO": "B_NUMBER",
    "CLG_IMEI": "IMEI",
    "DIR_FLG": "CALL_TYPE",
    "DRTN": "DURATION_SEC",
    "END_TM": "END_TIME",
    # Legacy Zong BTS layout (with strt_tm / bnumber / msisdn_id) is handled
    # automatically by OPERATOR_COLUMN_MAPS["zong"] in schema_detection.
}


def read_zong_bts(
    path: Path,
    *,
    spec_id: str,
    bts_id: str | None = None,
) -> pd.DataFrame:
    """Read a Zong .xls BTS dump.

    Supports two column layouts:
      - legacy: strt_tm, bnumber, msisdn_id, min_s, sec_s, lac_id, cell_id,
        imei, site_address, lng, lat
      - modern: DLG_NO, DLD_NO, CLG_IMEI, DIR_FLG, DRTN, START_TIME, END_TM,
        ACTVY_TYPE_CD, LAC_ID, CELL_ID
    """
    normalized = _load_with_headers(path, pre_rename=_ZONG_BTS_PRE_RENAME)

    # Normalize Zong direction text ("Incomming" / "Outgoing") — the canonical
    # coercion in _finalize_canonical handles INCOMING/OUTGOING, so fold the
    # typo'd variant into INCOMING here.
    if "CALL_TYPE" in normalized.columns:
        normalized["CALL_TYPE"] = (
            normalized["CALL_TYPE"].astype("string").str.upper().str.strip().replace({"INCOMMING": "INCOMING"})
        )

    # Zong splits duration into minutes + seconds; combine if DURATION_SEC
    # wasn't set directly by the column map.
    if "DURATION_SEC" in normalized.columns and "DURATION_MIN" in normalized.columns:
        secs = pd.to_numeric(normalized["DURATION_SEC"], errors="coerce").fillna(0)
        mins = pd.to_numeric(normalized["DURATION_MIN"], errors="coerce").fillna(0)
        normalized["DURATION_SEC"] = mins * 60 + secs
    elif "DURATION_MIN" in normalized.columns and "DURATION_SEC" not in normalized.columns:
        normalized["DURATION_SEC"] = pd.to_numeric(normalized["DURATION_MIN"], errors="coerce").fillna(0) * 60

    return _finalize_canonical(
        normalized,
        provider="zong",
        spec_id=spec_id,
        bts_id=bts_id,
        source_file=path.name,
        timestamp_format="%m/%d/%Y %I:%M:%S %p",
    )


# Direction mapping for Telenor INBOUND_OUTBOUND_IND field.
# Numeric: 1=OUT, 2=IN, 0=DATA. Text variant used in incoming-only files.
_TELENOR_DIRECTION_MAP = {
    "1": "OUT", "2": "IN", "0": "DATA",
    "OUTGOING": "OUT", "INCOMING": "IN", "OUT": "OUT", "IN": "IN",
    "INTERNET": "DATA", "DATA": "DATA",
}
_TELENOR_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _read_telenor_one(
    path: Path,
    *,
    spec_id: str,
    bts_id: str | None,
) -> tuple[pd.DataFrame, str | None]:
    """Read one Telenor CSV and return (canonical frame, site_id_from_data).

    Direction is determined per-row from ``INBOUND_OUTBOUND_IND`` (1=OUT, 2=IN,
    0=DATA/internet). B-party is selected per-row: outgoing rows use
    ``CALL_DIALED_NUM``, incoming rows use ``CALL_ORIG_NUM``. No filename
    convention is required or assumed.
    """
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    if df.empty:
        return _empty_frame(), None

    n = len(df)
    site_id_series = df.get("Site_Id", pd.Series([pd.NA] * n)).astype("string").str.strip()
    inferred_site_id = None
    if not site_id_series.dropna().empty:
        most_common = site_id_series.dropna().mode()
        if len(most_common):
            inferred_site_id = str(most_common.iloc[0])

    effective_bts_id = bts_id or inferred_site_id or ""

    call_start = _parse_timestamp_with_format(
        df["CALL_START_DT_TM"], _TELENOR_TIMESTAMP_FORMAT,
        provider="telenor",
    )

    # Compute duration from CALL_END_DT_TM when available (present in outgoing files).
    if "CALL_END_DT_TM" in df.columns:
        call_end = _parse_timestamp_with_format(
            df["CALL_END_DT_TM"], _TELENOR_TIMESTAMP_FORMAT,
            provider="telenor",
        )
        duration = (call_end - call_start).dt.total_seconds()
    else:
        duration = pd.Series([pd.NA] * n)

    # Direction from INBOUND_OUTBOUND_IND — numeric (1/2/0) or text (INCOMING/OUTGOING).
    ind_series = df.get("INBOUND_OUTBOUND_IND", pd.Series([pd.NA] * n)).astype(str).str.strip().str.upper()
    direction = ind_series.map(_TELENOR_DIRECTION_MAP).fillna("UNK")

    # B-party selection per row: outgoing → CALL_DIALED_NUM, incoming → CALL_ORIG_NUM.
    is_outgoing = direction.values == "OUT"
    call_dialed = df.get("CALL_DIALED_NUM", pd.Series([pd.NA] * n))
    call_orig = df.get("CALL_ORIG_NUM", pd.Series([pd.NA] * n))
    b_party_src = pd.Series(
        np.where(is_outgoing, call_dialed.values, call_orig.values),
        index=df.index,
    )

    def _to_party(v):
        if pd.isna(v):
            return None
        text = str(v).strip()
        if not text or text.lower() == "internet":
            return None
        return normalize_mobile(text) or text

    canonical = pd.DataFrame({
        "SPEC_ID": pd.Series([spec_id] * n, dtype="category"),
        "BTS_ID": pd.Series([effective_bts_id] * n, dtype="string"),
        "A_PARTY": df["MSISDN"].apply(_to_party),
        "B_PARTY": b_party_src.apply(_to_party),
        "DIRECTION": pd.Series(direction.values, dtype="category"),
        "CALL_TIME": call_start.values,
        "DURATION_SEC": pd.to_numeric(duration, errors="coerce").astype("Int32").values,
        "IMEI": df.get("IMEI", pd.Series([pd.NA] * n)).astype("string"),
        "CELL_ID": df.get("CELL_SITE_ID", pd.Series([pd.NA] * n)).astype("string"),
        "LAC_ID": df.get("Lac_Id", pd.Series([pd.NA] * n)).astype("string"),
        "LAT": pd.to_numeric(df.get("LAT"), errors="coerce").astype("float32").values,
        "LNG": pd.to_numeric(df.get("LONGITUDE"), errors="coerce").astype("float32").values,
        "LOCATION": df.get("LOCATION", pd.Series([pd.NA] * n)).astype("string"),
        "PROVIDER": pd.Series(["telenor"] * n, dtype="category"),
        "SOURCE_FILE": pd.Series([path.name] * n, dtype="string"),
    })

    # Keep DATA (GPRS/internet) rows — surfaced in raw output as INTERNET.
    canonical = canonical[canonical["DIRECTION"].isin(["IN", "OUT", "DATA"])]
    canonical = canonical[canonical["CALL_TIME"].notna()]
    mask = canonical["A_PARTY"].notna() | canonical["B_PARTY"].notna()
    canonical = canonical[mask].reset_index(drop=True)
    return canonical, inferred_site_id


def read_telenor_bts(
    paths: list[Path],
    *,
    spec_id: str,
    bts_id: str | None = None,
) -> pd.DataFrame:
    """Read one or more Telenor BTS CSV files into the canonical frame.

    Files are matched by Site_Id content — no filename convention required.
    Direction (IN/OUT/DATA) and B-party selection are determined per row from
    ``INBOUND_OUTBOUND_IND`` (1=OUT, 2=IN, 0=DATA). BTS ID is read from the
    ``Site_Id`` column when not supplied by the caller.
    """
    if not paths:
        raise ValueError(f"read_telenor_bts requires at least one file for spec {spec_id}.")

    frames: list[pd.DataFrame] = []
    site_ids: list[str] = []
    for path in paths:
        try:
            frame, site_id = _read_telenor_one(path, spec_id=spec_id, bts_id=bts_id)
            frames.append(frame)
            if site_id:
                site_ids.append(site_id)
            logger.info("Telenor file read | spec=%s rows=%d site_id=%s source=%s", spec_id, len(frame), site_id, path.name)
        except Exception as exc:
            logger.error("Failed to read Telenor file %s: %s", path, exc, exc_info=True)

    if not frames:
        raise ValueError(f"Telenor reader: all files failed to load for spec {spec_id}.")

    combined = pd.concat(frames, ignore_index=True)

    # Re-stamp BTS_ID with the data-derived Site_Id when caller didn't supply one.
    if not bts_id and site_ids:
        common = max(set(site_ids), key=site_ids.count)
        combined["BTS_ID"] = pd.Series([common] * len(combined), dtype="string")

    combined = combined.sort_values("CALL_TIME").reset_index(drop=True)
    logger.info(
        "BTS reader completed | provider=telenor spec=%s rows=%d sources=%s",
        spec_id, len(combined),
        ", ".join(p.name for p in paths),
    )
    return combined


def read_bts_group(
    provider: str,
    paths: list[Path],
    *,
    spec_id: str,
    bts_id: str | None = None,
) -> pd.DataFrame:
    """Dispatch to the appropriate per-provider reader."""
    if provider == "ufone":
        if len(paths) != 1:
            raise ValueError(f"Ufone group requires exactly 1 file, got {len(paths)}")
        return read_ufone_bts(paths[0], spec_id=spec_id, bts_id=bts_id)
    if provider == "jazz":
        if len(paths) != 1:
            raise ValueError(f"Jazz group requires exactly 1 file, got {len(paths)}")
        return read_jazz_bts(paths[0], spec_id=spec_id, bts_id=bts_id)
    if provider == "zong":
        if len(paths) != 1:
            raise ValueError(f"Zong group requires exactly 1 file, got {len(paths)}")
        return read_zong_bts(paths[0], spec_id=spec_id, bts_id=bts_id)
    if provider == "telenor":
        return read_telenor_bts(paths, spec_id=spec_id, bts_id=bts_id)
    raise ValueError(f"Unknown BTS provider: {provider}")
