"""Phone and CNIC normalization helpers."""

from __future__ import annotations

import re

import pandas as pd


def normalize_mobile(value: object) -> str | None:
    if value is None or value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    if len(digits) == 12 and digits.startswith("92"):
        digits = "0" + digits[2:]
    elif len(digits) == 10:
        digits = "0" + digits
    if len(digits) != 11:
        return digits
    return digits


def normalize_cnic(value: object) -> str | None:
    if value is None or value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    digits = re.sub(r"\D", "", str(value))
    return digits or None
