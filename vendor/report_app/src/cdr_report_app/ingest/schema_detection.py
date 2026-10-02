"""Header and schema detection helpers."""

from __future__ import annotations

from collections import Counter
import re

import pandas as pd

from cdr_report_app.domain.cdr_models import ColumnMapping
from cdr_report_app.utils.text import clean_header_name, compact_whitespace


def _normalize_header_name(value: object) -> str:
    text = compact_whitespace(value).lower()
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_candidate(value: object) -> str:
    return _normalize_header_name(value)


CANONICAL_COLUMN_CANDIDATES = {
    "CALL_TYPE": {"call_type", "calltype", "call type", "direction", "inbound_outbound_ind"},
    "MEDIA_TYPE": {"type", "service provider", "media_type"},
    "MSISDN": {"msisdn", "a_number", "a number", "aparty", "a_party", "cli", "subscriber_no", "a party", "a-party"},
    "ORIG_NUMBER": {"call_org_num", "originating number", "originating_number", "calling number"},
    "B_NUMBER": {"b_number", "bnumber", "bparty", "b_party", "b number", "called_number", "call_dialed_num", "b party", "b-party", "called number"},
    "START_TIME": {"start_time", "strt_tm", "datetime", "timestamp", "call start time", "start time", "date & time", "date time", "call_start_dt_tm"},
    "END_TIME": {"end_time", "call end time", "call_end_dt_tm"},
    "DURATION_MIN": {"duration_min", "mins", "min s", "minutes", "duration minutes"},
    "DURATION_SEC": {"duration_sec", "secs", "sec", "sec s", "seconds", "duration", "duration seconds"},
    "LAC_ID": {"lac_id", "lac", "location area code"},
    "CELL_ID": {"cell_id", "cellid", "cell id", "cell", "cell site", "cell_site_id"},
    "IMEI": {"imei"},
    "IMSI": {"imsi"},
    "SITE_ADDRESS": {"site_address", "location", "address", "site address", "sitelocation", "site"},
    "LONGITUDE": {"longitude", "lng", "long"},
    "LATITUDE": {"latitude", "lat"},
    "LONGITUDE_LATITUDE": {"longitude and latitude", "lat long", "lat/lng", "lng/lat"},
    "SERIAL_NUMBER": {"sr #", "sr#", "serial number"},
}


OPERATOR_FINGERPRINTS = {
    # Unified portal export (MSISDN / B Number / Call Type / Direction / ... / Azimuth).
    # Listed first so it wins before the looser per-operator fingerprints below.
    "unified": [
        {"msisdn", "b number", "call type", "direction", "duration seconds"},
        {"msisdn", "b number", "call type", "cell sector", "azimuth"},
    ],
    "zong": [{"strt_tm", "bnumber", "msisdn_id"}, {"strt_tm", "bnumber", "msisdn"}, {"min s", "sec s", "lac_id", "cell_id"}],
    "telenor": [{"call_dialed_num"}, {"inbound_outbound_ind"}, {"cell_lac_id", "cell_site_id"}],
    "jazz": [{"sr #"}, {"a-party", "b-party"}, {"date & time"}, {"aparty", "bparty", "sitelocation"}],
    "ufone": [{"a number", "b number"}, {"cell id - a"}],
}

OPERATOR_DISPLAY_NAMES: dict[str, str] = {
    "unified": "Unified CDR",
    "jazz": "Moblink/Jazz",
    "zong": "Zong",
    "telenor": "Telenor",
    "ufone": "Ufone",
}


OPERATOR_COLUMN_MAPS = {
    "unified": {
        "msisdn": "MSISDN",
        "b number": "B_NUMBER",
        # "Call Type" carries either the media alone ("Call" / "SMS") or media plus
        # direction ("Sms - Incoming"); normalizers split it against "Direction".
        "call type": "CALL_TYPE",
        "direction": "DIRECTION",
        "start time": "START_TIME",
        "end date": "END_DATE",
        "end time": "END_TIME",
        "duration seconds": "DURATION_SEC",
        "imsi": "IMSI",
        "imei": "IMEI",
        "lac id": "LAC_ID",
        "cell id": "CELL_ID",
        "cell sector": "CELL_SECTOR",
        # "Site ID" holds the tower address text in this export, not a numeric id.
        "site id": "SITE_ADDRESS",
        "latitude": "LATITUDE",
        "longitude": "LONGITUDE",
        "network volume": "NETWORK_VOLUME",
        "node id": "NODE_ID",
        "azimuth": "AZIMUTH",
    },
    "zong": {
        "call_type": "CALL_TYPE",
        "msisdn_id": "MSISDN",
        "msisdn": "MSISDN",
        "bnumber": "B_NUMBER",
        "strt_tm": "START_TIME",
        "min s": "DURATION_MIN",
        "mins": "DURATION_MIN",
        "sec s": "DURATION_SEC",
        "secs": "DURATION_SEC",
        "lac_id": "LAC_ID",
        "cell_id": "CELL_ID",
        "imei": "IMEI",
        "site_address": "SITE_ADDRESS",
        "lng": "LONGITUDE",
        "lat": "LATITUDE",
    },
    "telenor": {
        "msisdn": "MSISDN",
        "call_org_num": "ORIG_NUMBER",
        "call_dialed_num": "B_NUMBER",
        "call_start_dt_tm": "START_TIME",
        "call_end_dt_tm": "END_TIME",
        "inbound_outbound_ind": "CALL_TYPE",
        "call_type": "MEDIA_TYPE",
        "call_network_volume": "DURATION_SEC",
        "imei": "IMEI",
        "imsi": "IMSI",
        "lac_id": "LAC_ID",
        "lac id": "LAC_ID",
        "cell_lac_id": "LAC_ID",
        "site_id": "CELL_ID",
        "site id": "CELL_ID",
        "cell_site_id": "CELL_ID",
        "location": "SITE_ADDRESS",
        "lat": "LATITUDE",
        "latitude": "LATITUDE",
        "longitude": "LONGITUDE",
        "long": "LONGITUDE",
    },
    "jazz": {
        # old Jazz format
        "sr #": "SERIAL_NUMBER",
        "a-party": "MSISDN",
        "b-party": "B_NUMBER",
        "date & time": "START_TIME",
        "call type": "CALL_TYPE",
        "cell id": "CELL_ID",
        "site": "SITE_ADDRESS",
        # new Moblink/Jazz format (CallType, Aparty, Bparty, Datetime, SiteLocation, ...)
        "calltype": "CALL_TYPE",
        "aparty": "MSISDN",
        "bparty": "B_NUMBER",
        "datetime": "START_TIME",
        "cellid": "CELL_ID",
        "sitelocation": "SITE_ADDRESS",
        "longitude": "LONGITUDE",
        # shared across both formats
        "duration": "DURATION_SEC",
        "imei": "IMEI",
        "imsi": "IMSI",
    },
    "ufone": {
        "a number": "MSISDN",
        "b number": "B_NUMBER",
        "call start time": "START_TIME",
        "call end time": "END_TIME",
        "call duration": "DURATION_SEC",
        "call type": "CALL_TYPE",
        "imei": "IMEI",
        "imsi": "IMSI",
        "cell id - a": "CELL_ID",
        "location - a": "SITE_ADDRESS",
    },
}


CUSTOM_VARIANT_MAP = {
    "calltype": "CALL_TYPE",
    "aparty": "MSISDN",
    "bparty": "B_NUMBER",
    "datetime": "START_TIME",
    "duration": "DURATION_SEC",
    "cellid": "CELL_ID",
    "imsi": "IMSI",
    "imei": "IMEI",
    "sitelocation": "SITE_ADDRESS",
    "longitude and latitude": "LONGITUDE_LATITUDE",
}

NORMALIZED_CANONICAL_COLUMN_CANDIDATES = {
    canonical: {_normalize_candidate(alias) for alias in aliases}
    for canonical, aliases in CANONICAL_COLUMN_CANDIDATES.items()
}

NORMALIZED_OPERATOR_FINGERPRINTS = {
    operator: [{_normalize_candidate(item) for item in fingerprint} for fingerprint in fingerprints]
    for operator, fingerprints in OPERATOR_FINGERPRINTS.items()
}

NORMALIZED_OPERATOR_COLUMN_MAPS = {
    operator: {_normalize_candidate(key): value for key, value in mapping.items()}
    for operator, mapping in OPERATOR_COLUMN_MAPS.items()
}

NORMALIZED_CUSTOM_VARIANT_MAP = {
    _normalize_candidate(key): value for key, value in CUSTOM_VARIANT_MAP.items()
}


def detect_header_row(raw: pd.DataFrame) -> int:
    for i in range(min(15, len(raw))):
        row = raw.iloc[i]
        cells = [compact_whitespace(value) for value in row if compact_whitespace(value)]
        if len(cells) < 4:
            continue
        label_like = sum(1 for cell in cells if any(ch.isalpha() for ch in cell) and len(cell) < 50)
        if label_like >= len(cells) * 0.6:
            return i
    return 0


def detect_operator(headers: list[object]) -> str | None:
    normalized_headers = {_normalize_header_name(header) for header in headers if _normalize_header_name(header)}
    for operator, fingerprint_sets in NORMALIZED_OPERATOR_FINGERPRINTS.items():
        if any(fingerprint.issubset(normalized_headers) for fingerprint in fingerprint_sets):
            return operator

    if {_normalize_candidate(item) for item in {"calltype", "aparty", "bparty", "datetime"}}.issubset(normalized_headers):
        return "custom"
    return None


def _infer_dtype(series: pd.Series) -> str:
    non_empty = series.astype(str).str.strip()
    non_empty = non_empty[non_empty != ""]
    if non_empty.empty:
        return "TEXT"

    numeric = pd.to_numeric(non_empty, errors="coerce")
    if numeric.notna().mean() > 0.9:
        return "INTEGER" if (numeric.fillna(0) % 1 == 0).all() else "REAL"

    common_formats = [
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%m/%d/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d-%m-%Y %H:%M:%S",
        "%d-%m-%y %H:%M",
        "%d-%m-%y %H:%M:%S",
        "%d-%m-%y %H:%M",
        "%m/%d/%Y %H:%M",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d %H:%M",
        "%d-%m-%Y %H:%M",
        "%m/%d/%Y",
        "%d/%m/%Y",
        "%Y-%m-%d",
    ]
    for fmt in common_formats:
        parsed_dates = pd.to_datetime(non_empty, format=fmt, errors="coerce")
        if parsed_dates.notna().mean() > 0.7:
            return "DATETIME"

    return "TEXT"


def build_column_mappings(df: pd.DataFrame) -> list[ColumnMapping]:
    operator = detect_operator(list(df.columns))
    used_names: Counter[str] = Counter()
    mappings: list[ColumnMapping] = []

    for original in df.columns:
        normalized = clean_header_name(original)
        lowered = _normalize_header_name(original)

        operator_map = NORMALIZED_OPERATOR_COLUMN_MAPS.get(operator or "", {})
        if lowered in operator_map:
            normalized = operator_map[lowered]
        elif lowered in NORMALIZED_CUSTOM_VARIANT_MAP:
            normalized = NORMALIZED_CUSTOM_VARIANT_MAP[lowered]
        else:
            for canonical, aliases in NORMALIZED_CANONICAL_COLUMN_CANDIDATES.items():
                if lowered in aliases:
                    normalized = canonical
                    break

        used_names[normalized] += 1
        if used_names[normalized] > 1:
            normalized = f"{normalized}_{used_names[normalized]}"

        mappings.append(
            ColumnMapping(
                original=str(original),
                normalized=normalized,
                dtype=_infer_dtype(df[original]),
            )
        )

    return mappings
