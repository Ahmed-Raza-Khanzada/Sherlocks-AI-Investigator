"""Normalization routines for CDR tabular data."""

from __future__ import annotations

import re

import pandas as pd

from cdr_report_app.domain.cdr_models import ColumnMapping
from cdr_report_app.utils.phones import normalize_cnic, normalize_mobile


def apply_column_mappings(df: pd.DataFrame, mappings: list[ColumnMapping]) -> pd.DataFrame:
    rename_map = {mapping.original: mapping.normalized for mapping in mappings}
    normalized = df.rename(columns=rename_map).copy()

    for mapping in mappings:
        col = mapping.normalized
        if col not in normalized.columns:
            continue

        if mapping.dtype == "DATETIME":
            normalized[col] = pd.to_datetime(normalized[col], errors="coerce")
        elif mapping.dtype in {"INTEGER", "REAL"}:
            normalized[col] = pd.to_numeric(normalized[col], errors="coerce")
        else:
            normalized[col] = normalized[col].astype(str).str.strip()

    if "LONGITUDE_LATITUDE" in normalized.columns:
        coords = normalized["LONGITUDE_LATITUDE"].apply(_split_combined_coordinates)
        if "LONGITUDE" not in normalized.columns:
            normalized["LONGITUDE"] = [item[0] for item in coords]
        if "LATITUDE" not in normalized.columns:
            normalized["LATITUDE"] = [item[1] for item in coords]

    if "SITE_ADDRESS" in normalized.columns:
        extracted = normalized["SITE_ADDRESS"].apply(_extract_coordinates_from_site_address)
        if "LONGITUDE" not in normalized.columns:
            normalized["LONGITUDE"] = [item[0] for item in extracted]
        else:
            normalized["LONGITUDE"] = normalized["LONGITUDE"].where(normalized["LONGITUDE"].notna(), [item[0] for item in extracted])
        if "LATITUDE" not in normalized.columns:
            normalized["LATITUDE"] = [item[1] for item in extracted]
        else:
            normalized["LATITUDE"] = normalized["LATITUDE"].where(normalized["LATITUDE"].notna(), [item[1] for item in extracted])
        normalized["SITE_ADDRESS"] = [_clean_site_address(item[2]) for item in extracted]

    if "MSISDN" not in normalized.columns:
        for fallback in ["A_NUMBER", "APARTY"]:
            if fallback in normalized.columns:
                normalized["MSISDN"] = normalized[fallback]
                break

    if "B_NUMBER" not in normalized.columns:
        for fallback in ["BPARTY"]:
            if fallback in normalized.columns:
                normalized["B_NUMBER"] = normalized[fallback]
                break

    _split_combined_call_type(normalized)
    _promote_direction_column(normalized)

    if "CALL_TYPE" not in normalized.columns:
        for fallback in ["DIRECTION", "CALL_TYPE_2", "CALLTYPE"]:
            if fallback in normalized.columns:
                normalized["CALL_TYPE"] = normalized[fallback]
                break
    elif "CALL_TYPE_2" in normalized.columns:
        direction_values = normalized["CALL_TYPE_2"].astype(str).str.upper().str.strip()
        mask = direction_values.isin({"INCOMING", "OUTGOING", "SMS INCOMING", "SMS OUTGOING"})
        normalized.loc[mask, "CALL_TYPE"] = normalized.loc[mask, "CALL_TYPE_2"]

    if "CELL_ID" not in normalized.columns:
        for fallback in ["CELLID"]:
            if fallback in normalized.columns:
                normalized["CELL_ID"] = normalized[fallback]
                break

    if "SITE_ADDRESS" not in normalized.columns:
        for fallback in ["SITELOCATION"]:
            if fallback in normalized.columns:
                normalized["SITE_ADDRESS"] = normalized[fallback]
                break

    if "MSISDN" in normalized.columns:
        normalized["MSISDN"] = normalized["MSISDN"].apply(lambda value: normalize_mobile(value) or str(value).strip())
    if "ORIG_NUMBER" in normalized.columns:
        normalized["ORIG_NUMBER"] = normalized["ORIG_NUMBER"].apply(lambda value: normalize_mobile(value) or str(value).strip())
    if "B_NUMBER" in normalized.columns:
        normalized["B_NUMBER"] = normalized["B_NUMBER"].apply(lambda value: normalize_mobile(value) or str(value).strip())
    if "CNIC" in normalized.columns:
        normalized["CNIC"] = normalized["CNIC"].apply(lambda value: normalize_cnic(value) or str(value).strip())

    _apply_telenor_b_party_logic(normalized)

    normalized = normalized.replace({"": pd.NA})
    normalized = normalized.dropna(how="all").reset_index(drop=True)
    return normalized


# "Sms - Incoming", "Call - Outgoing", "Data - In" — media and direction in one cell.
_COMBINED_CALL_TYPE_PATTERN = r"^\s*(sms|mms|call|voice|data|gprs)\s*[-\u2013\u2014/]\s*(in|out)(?:coming|going)?\s*$"
_DIRECTION_WORDS = {"IN": "INCOMING", "OUT": "OUTGOING"}


def _split_combined_call_type(normalized: pd.DataFrame) -> None:
    """Split a combined "<media> - <direction>" CALL_TYPE into MEDIA_TYPE + CALL_TYPE."""
    if "CALL_TYPE" not in normalized.columns:
        return

    text = normalized["CALL_TYPE"].astype(str).str.strip()
    parts = text.str.extract(_COMBINED_CALL_TYPE_PATTERN, flags=re.IGNORECASE, expand=True)
    matched = parts[0].notna()
    if not matched.any():
        return

    media = parts[0].str.upper()
    direction = parts[1].str.upper().map(lambda value: _DIRECTION_WORDS.get(value, value))

    if "MEDIA_TYPE" not in normalized.columns:
        normalized["MEDIA_TYPE"] = pd.NA
    normalized.loc[matched, "MEDIA_TYPE"] = media[matched]
    normalized.loc[matched, "CALL_TYPE"] = direction[matched]


def _promote_direction_column(normalized: pd.DataFrame) -> None:
    """Use a separate DIRECTION column as CALL_TYPE, keeping the media word in MEDIA_TYPE."""
    if "DIRECTION" not in normalized.columns:
        return

    direction = normalized["DIRECTION"].astype(str).str.upper().str.strip()
    direction = direction.map(lambda value: _DIRECTION_WORDS.get(value, value))
    usable = direction.str.contains("INCOMING|OUTGOING", na=False)
    if not usable.any():
        return

    if "CALL_TYPE" not in normalized.columns:
        normalized["CALL_TYPE"] = direction
        return

    call_type = normalized["CALL_TYPE"].astype(str).str.upper().str.strip()
    carries_direction = call_type.str.contains("INCOMING|OUTGOING", na=False)
    if "MEDIA_TYPE" not in normalized.columns:
        normalized["MEDIA_TYPE"] = normalized["CALL_TYPE"]
    target = usable & ~carries_direction
    normalized.loc[target, "CALL_TYPE"] = direction[target]


def _apply_telenor_b_party_logic(normalized: pd.DataFrame) -> None:
    required = {"MSISDN", "CALL_TYPE", "B_NUMBER", "ORIG_NUMBER"}
    if not required.issubset(normalized.columns):
        return

    directions = normalized["CALL_TYPE"].astype(str).str.upper().str.strip()
    target_msisdn = normalized["MSISDN"].astype(str).str.strip()
    incoming_candidate = normalized["ORIG_NUMBER"].astype(str).str.strip()
    outgoing_candidate = normalized["B_NUMBER"].astype(str).str.strip()

    resolved_b_party: list[object] = []
    for direction, incoming, outgoing, target in zip(directions, incoming_candidate, outgoing_candidate, target_msisdn):
        candidate = ""
        if direction == "INCOMING":
            candidate = incoming
        elif direction == "OUTGOING":
            candidate = outgoing
        elif direction == "DATA":
            resolved_b_party.append(pd.NA)
            continue
        else:
            candidate = outgoing or incoming

        resolved_b_party.append(_validate_telenor_b_party(candidate, target))

    normalized["B_NUMBER"] = resolved_b_party


def _validate_telenor_b_party(candidate: object, target_msisdn: object) -> object:
    text = str(candidate or "").strip()
    target = str(target_msisdn or "").strip()
    if not text:
        return pd.NA
    if not text.isdigit():
        return pd.NA
    if len(text) < 8:
        return pd.NA
    if target and text == target:
        return pd.NA
    return normalize_mobile(text) or text


def _split_combined_coordinates(value: object) -> tuple[object, object]:
    text = str(value or "").strip()
    if not text:
        return (pd.NA, pd.NA)

    matches = re.findall(r"[-+]?\d+(?:\.\d+)?", text)
    if len(matches) < 2:
        return (pd.NA, pd.NA)

    first = float(matches[0])
    second = float(matches[1])

    # Pakistan CDR files sometimes store "Longitude and Latitude" in that order.
    if abs(first) > 40 and abs(second) < 40:
        return (first, second)
    if abs(first) < 40 and abs(second) > 40:
        return (second, first)
    return (first, second)


def _extract_coordinates_from_site_address(value: object) -> tuple[object, object, object]:
    text = str(value or "").strip()
    if not text or text == "?":
        return (pd.NA, pd.NA, pd.NA if not text else text)

    parts = [part.strip() for part in text.split("|")]
    coords = [part for part in parts if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", part)]
    if len(coords) >= 2:
        first = float(coords[0])
        second = float(coords[1])
        if abs(first) < 40 and abs(second) > 40:
            latitude, longitude = first, second
        elif abs(first) > 40 and abs(second) < 40:
            longitude, latitude = first, second
        else:
            latitude, longitude = first, second
        cleaned_parts = [part for part in parts if part not in coords[:2]]
        cleaned_text = " | ".join(cleaned_parts).strip() or parts[0]
        return (longitude, latitude, cleaned_text)

    matches = re.findall(r"[-+]?\d+(?:\.\d+)?", text)
    if len(matches) >= 2:
        first = float(matches[-2])
        second = float(matches[-1])
        if abs(first) < 40 and abs(second) > 40:
            latitude, longitude = first, second
        elif abs(first) > 40 and abs(second) < 40:
            longitude, latitude = first, second
        else:
            latitude, longitude = first, second
        cleaned_text = re.sub(r"\|?\s*[-+]?\d+(?:\.\d+)?\s*\|?\s*[-+]?\d+(?:\.\d+)?\s*$", "", text).strip(" |")
        return (longitude, latitude, cleaned_text or text)

    return (pd.NA, pd.NA, text)


def _clean_site_address(value: object) -> object:
    if value is pd.NA or pd.isna(value):
        return pd.NA
    text = str(value).strip()
    if not text:
        return pd.NA
    return text
