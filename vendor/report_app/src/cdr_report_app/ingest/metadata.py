"""Metadata extraction helpers."""

from __future__ import annotations

from typing import Iterable

from cdr_report_app.domain.cdr_models import SubscriberMetadata
from cdr_report_app.utils.phones import normalize_cnic, normalize_mobile
from cdr_report_app.utils.text import compact_whitespace


NAME_KEYS = ("name", "customer name", "subscriber name")
MSISDN_KEYS = ("msisdn", "mobile", "phone", "customer msisdn", "subscriber msisdn")
CNIC_KEYS = ("cnic", "nic", "subscriber cnic", "customer cnic")


def _match_label(cell: object, keys: Iterable[str]) -> bool:
    text = compact_whitespace(cell).lower()
    return any(key in text for key in keys)


def extract_metadata_from_top_rows(rows: list[list[str]]) -> SubscriberMetadata:
    metadata = SubscriberMetadata()

    for row in rows:
        cleaned = [compact_whitespace(cell) for cell in row if compact_whitespace(cell)]
        if not cleaned:
            continue

        row_text = " | ".join(cleaned).lower()
        if metadata.name is None and any(key in row_text for key in NAME_KEYS) and len(cleaned) >= 2:
            metadata.name = cleaned[1]
        if metadata.msisdn is None and any(key in row_text for key in MSISDN_KEYS) and len(cleaned) >= 2:
            # Guard against a header row ("MSISDN | B Number | ...") being read as a
            # label/value pair: only take the cell if it really looks like a number.
            candidate = normalize_mobile(cleaned[1])
            if candidate and candidate.isdigit() and 10 <= len(candidate) <= 13:
                metadata.msisdn = candidate
        if metadata.cnic is None and any(key in row_text for key in CNIC_KEYS) and len(cleaned) >= 2:
            candidate = normalize_cnic(cleaned[1])
            if candidate and candidate.isdigit():
                metadata.cnic = candidate

    return metadata
