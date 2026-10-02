"""Text normalization helpers."""

from __future__ import annotations

import re


def compact_whitespace(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def clean_header_name(value: object) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", compact_whitespace(value)).strip("_")
    return cleaned.upper() or "COLUMN"
