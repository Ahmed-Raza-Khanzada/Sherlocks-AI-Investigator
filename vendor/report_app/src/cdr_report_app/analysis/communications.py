"""Communication pattern analysis."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

import pandas as pd

from cdr_report_app.domain.analysis_models import BurstRecord, ContactRecord, DeviceRecord, ShortCodeRecord
from cdr_report_app.settings import ReportSettings
from cdr_report_app.utils.phones import normalize_mobile

logger = logging.getLogger(__name__)

_SHORT_CODES_PATH = Path(__file__).parent / "data" / "pakistan_short_codes.json"


@lru_cache(maxsize=1)
def load_short_codes() -> dict[str, str]:
    """Load Pakistani short codes from JSON as a flat {code: name} map.

    Cached so the file is read once per process.
    """
    try:
        with _SHORT_CODES_PATH.open(encoding="utf-8") as fh:
            payload = json.load(fh)
        return {code: entry["name"] for code, entry in payload.get("codes", {}).items()}
    except (OSError, ValueError, KeyError) as exc:
        logger.error("Failed to load short codes from %s: %s", _SHORT_CODES_PATH, exc)
        return {}


# Backwards-compatible module-level mapping (other modules import this name).
PAKISTAN_SHORT_CODES = load_short_codes()


def build_bursts(df: pd.DataFrame, b_number_col: str | None, report: ReportSettings) -> list[BurstRecord]:
    valid = df.dropna(subset=["_DT"]).sort_values("_DT").reset_index(drop=True)
    if len(valid) < 2:
        return []

    bursts: list[BurstRecord] = []
    window = pd.Timedelta(minutes=report.burst_window_minutes)
    i = 0
    while i < len(valid):
        window_end = valid.loc[i, "_DT"] + window
        j = i
        while j < len(valid) and valid.loc[j, "_DT"] <= window_end:
            j += 1
        count = j - i
        if count >= report.burst_threshold:
            top_numbers: list[str] = []
            if b_number_col and b_number_col in valid.columns:
                top_numbers = (
                    valid.loc[i : j - 1, b_number_col]
                    .dropna()
                    .astype(str)
                    .value_counts()
                    .head(3)
                    .index.tolist()
                )
            bursts.append(
                BurstRecord(
                    burst_start=valid.loc[i, "_DT"].strftime("%d %b %Y, %I:%M %p"),
                    burst_end=valid.loc[j - 1, "_DT"].strftime("%I:%M %p"),
                    call_count=int(count),
                    top_numbers=top_numbers,
                )
            )
            i = j
        else:
            i += 1

    return sorted(bursts, key=lambda item: item.call_count, reverse=True)[:5]


def build_short_codes(df: pd.DataFrame, b_number_col: str | None, limit: int) -> list[ShortCodeRecord]:
    if not b_number_col or b_number_col not in df.columns:
        return []

    def is_short_code(value: object) -> bool:
        text = str(value).strip().replace("+", "").replace("-", "")
        return len(text) < 8 or text.startswith("*")

    filtered = df[df[b_number_col].apply(is_short_code)]
    if filtered.empty:
        return []

    stats = filtered[b_number_col].astype(str).value_counts().head(limit)
    output: list[ShortCodeRecord] = []
    for code, count in stats.items():
        cleaned = code.replace("+", "").replace("-", "").lstrip("0")
        output.append(
            ShortCodeRecord(
                code=str(code),
                count=int(count),
                description=PAKISTAN_SHORT_CODES.get(cleaned, PAKISTAN_SHORT_CODES.get(str(code), "-")),
            )
        )
    return output


def build_device_records(df: pd.DataFrame, device_col: str | None, label: str) -> list[DeviceRecord]:
    if not device_col or device_col not in df.columns:
        return []

    grouped = (
        df.groupby(device_col)
        .agg(RECORDS=("_DUR_SEC", "count"), FIRST=("_DT", "min"), LAST=("_DT", "max"))
        .sort_values("RECORDS", ascending=False)
    )
    records: list[DeviceRecord] = []
    for identifier, row in grouped.iterrows():
        records.append(
            DeviceRecord(
                identifier=str(identifier).split(".")[0].strip(),
                label=label,
                records=int(row["RECORDS"]),
                first_seen=row["FIRST"].strftime("%d %b %Y") if pd.notna(row["FIRST"]) else None,
                last_seen=row["LAST"].strftime("%d %b %Y") if pd.notna(row["LAST"]) else None,
            )
        )
    return records


def build_top_contacts(df: pd.DataFrame, b_number_col: str | None, call_type_col: str | None, media_type_col: str | None, limit: int) -> list[ContactRecord]:
    if not b_number_col or b_number_col not in df.columns:
        return []

    def is_regular_number(value: object) -> bool:
        normalized = str(value).split(".")[0].strip().replace("+", "").replace("-", "")
        return len(normalized) >= 8 and not normalized.startswith("*")

    filtered = df[df[b_number_col].apply(is_regular_number)].copy()
    if filtered.empty:
        return []

    # Identify most frequent contacts
    top_numbers = filtered[b_number_col].astype(str).value_counts().head(limit).index

    contacts: list[ContactRecord] = []
    for number in top_numbers:
        subset = filtered[filtered[b_number_col].astype(str) == str(number)]
        
        # OG/IN split
        og_mask = subset[call_type_col].str.contains("OUTGOING|OG", na=False, case=False) if call_type_col else pd.Series(False, index=subset.index)
        in_mask = subset[call_type_col].str.contains("INCOMING|IC", na=False, case=False) if call_type_col else pd.Series(False, index=subset.index)
        
        # SMS vs Call split (often duration=0 means SMS if MEDIA_TYPE not explicit)
        is_sms = subset[media_type_col].str.contains("SMS", na=False, case=False) if media_type_col else (subset["_DUR_SEC"] == 0)
        is_call = ~is_sms

        contacts.append(ContactRecord(
            number=normalize_mobile(number) or str(number).split(".")[0].strip(),
            og_calls=int(subset[og_mask & is_call].shape[0]),
            og_duration=int(subset[og_mask & is_call]["_DUR_SEC"].sum()),
            in_calls=int(subset[in_mask & is_call].shape[0]),
            in_duration=int(subset[in_mask & is_call]["_DUR_SEC"].sum()),
            sms_in=int(subset[in_mask & is_sms].shape[0]),
            sms_out=int(subset[og_mask & is_sms].shape[0]),
            total_calls=int(subset.shape[0])
        ))
    return contacts
