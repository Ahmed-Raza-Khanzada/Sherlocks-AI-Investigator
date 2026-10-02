"""Classify uploaded BTS files by provider and group Telenor pairs."""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

import pandas as pd

from cdr_report_app.bts.models import BtsFileGroup, Provider
from cdr_report_app.ingest.loaders import read_raw_cdr_file
from cdr_report_app.ingest.schema_detection import detect_header_row

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BTS-specific column signatures — provider → list of column sets.
# Any one set being a subset of the file's headers → provider detected.
# All comparisons are case-insensitive and strip whitespace.
# ---------------------------------------------------------------------------
BTS_SIGNATURES: dict[str, list[set[str]]] = {
    "telenor": [
        {"inbound_outbound_ind", "site_id"},
        {"inbound_outbound_ind", "call_start_dt_tm"},
    ],
    "ufone": [
        {"call_inbound_outbound_desc", "cell_id"},
        {"a_number", "b_number", "call_start_time"},
        {"a number", "b number", "cell id"},
    ],
    "jazz": [
        {"a-party", "b-party", "date & time", "cell id"},
        {"sr #", "a-party", "b-party"},
        {"sr#", "a-party", "b-party"},
        {"a-party", "b-party", "duration"},
    ],
    "zong": [
        {"dlg_no", "dld_no", "clg_imei"},           # modern BTS layout
        {"strt_tm", "bnumber", "msisdn_id"},          # legacy layout
        {"strt_tm", "bnumber", "msisdn"},
        {"min s", "sec s", "lac_id", "cell_id"},
    ],
}

# Telenor Site_Id column — used to group multiple files by tower.
_TELENOR_SITE_ID_COL = "Site_Id"


class _SniffResult(NamedTuple):
    provider: Provider
    site_id: str | None   # Telenor only


def _read_headers(path: Path) -> list[str] | None:
    """Read the header row from any supported BTS file format.

    For Excel files, tries multiple engines so that .xls files uploaded
    with a .xlsx extension (or vice-versa) are still read correctly.
    """
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            df = pd.read_csv(path, nrows=0, dtype=str)
            return [str(c).strip() for c in df.columns]
        if suffix == ".txt":
            df = pd.read_csv(path, nrows=0, dtype=str, sep=None, engine="python")
            return [str(c).strip() for c in df.columns]
        if suffix in {".xls", ".xlsx", ".xlsb", ".xlsm"}:
            # Try engines in order: declared suffix first, then cross-fallback.
            engines = ["openpyxl", "xlrd"] if suffix == ".xlsx" else ["xlrd", "openpyxl"]
            raw = None
            for engine in engines:
                try:
                    raw = pd.read_excel(path, header=None, dtype=str, engine=engine, na_filter=False)
                    if not raw.empty:
                        break
                except Exception:
                    continue
            if raw is None or raw.empty:
                return None
            header_row = detect_header_row(raw)
            return [str(c).strip() for c in raw.iloc[header_row].tolist()]
    except Exception as exc:
        logger.debug("Header read failed for %s: %s", path, exc)
    return None


def _match_signatures(headers: list[str]) -> Provider | None:
    """Return provider name if headers match any BTS signature, else None."""
    normalized = {h.lower() for h in headers if h}
    for provider, sig_sets in BTS_SIGNATURES.items():
        if any(sig.issubset(normalized) for sig in sig_sets):
            return provider  # type: ignore[return-value]
    return None


def _common_prefix(values: list[str]) -> str:
    """Return longest common prefix of a list of strings."""
    if not values:
        return ""
    prefix = values[0]
    for v in values[1:]:
        while not v.startswith(prefix):
            prefix = prefix[:-1]
            if not prefix:
                return ""
    return prefix


def _read_telenor_site_id(path: Path) -> str | None:
    """Extract Telenor site/tower ID from a CSV using multiple approaches in order.

    1. ``Site_Id`` column — direct read (outgoing files always have this).
    2. Common numeric prefix of all unique ``CELL_SITE_ID`` values — robust,
       works regardless of suffix length (e.g. ``54141277``, ``54141312`` → ``54141``).
    3. Strip last 3 digits of modal ``CELL_SITE_ID`` — simple last resort.

    Incoming-only Telenor files often lack ``Site_Id`` but always have
    ``CELL_SITE_ID`` encoding both site and cell in one numeric string.
    """
    try:
        sample = pd.read_csv(path, nrows=500, dtype=str, keep_default_na=False, na_values=[""])

        # Approach 1: Site_Id column
        col = sample.get(_TELENOR_SITE_ID_COL, pd.Series(dtype=str)).astype(str).str.strip()
        non_empty = col[col.str.len() > 0]
        if not non_empty.empty:
            return str(non_empty.mode().iloc[0])

        # Approaches 2 & 3: derive from CELL_SITE_ID
        cell_col = sample.get("CELL_SITE_ID", pd.Series(dtype=str)).astype(str).str.strip()
        cell_vals = sorted(cell_col[cell_col.str.match(r"^\d+$")].unique().tolist())
        if cell_vals:
            # Approach 2: longest common numeric prefix across 2+ distinct cell IDs.
            # With only one unique value the prefix equals itself (useless) — skip.
            if len(cell_vals) >= 2:
                prefix = _common_prefix(cell_vals)
                if 4 <= len(prefix) < len(cell_vals[0]):  # shorter than the full cell ID
                    return prefix

            # Approach 3: strip last 3 digits from modal CELL_SITE_ID.
            # Telenor convention: CELL_SITE_ID = <site_id><3-digit cell suffix>.
            modal = cell_col.mode().iloc[0] if not cell_col.empty else ""
            if len(modal) > 3 and modal.isdigit():
                return modal[:-3]

        return None
    except Exception as exc:
        logger.debug("Telenor Site_Id read failed for %s: %s", path, exc)
        return None


def _sniff_bts_provider(path: Path) -> _SniffResult | None:
    """Detect BTS provider from file content alone — no filename or extension used.

    Returns a ``_SniffResult(provider, site_id)`` where ``site_id`` is only
    set for Telenor files (used to group multiple files by tower).
    Returns ``None`` if the file cannot be identified.
    """
    headers = _read_headers(path)
    if not headers:
        return None

    provider = _match_signatures(headers)
    if provider is None:
        logger.debug("No BTS signature matched for %s (headers=%s)", path.name, headers[:10])
        return None

    site_id = _read_telenor_site_id(path) if provider == "telenor" else None
    return _SniffResult(provider=provider, site_id=site_id)


def _infer_bts_id_from_filename(name: str) -> str | None:
    import re
    match = re.search(r"\d{4,8}", Path(name).stem)
    return match.group(0) if match else None


def classify_bts_source(paths: list[Path], spec_id_prefix: str = "auto") -> list[BtsFileGroup]:
    """Sort uploaded files into per-provider BTS groups.

    Detection is fully content-based — no filename or extension assumptions.
    Telenor files sharing the same ``Site_Id`` are grouped together.
    """
    telenor_buckets: dict[str, list[Path]] = defaultdict(list)
    others: list[tuple[Provider, Path]] = []
    unrecognized: list[Path] = []

    for path in paths:
        result = _sniff_bts_provider(path)
        if result is None:
            logger.warning("Could not detect BTS provider for %s — skipping", path.name)
            unrecognized.append(path)
            continue

        if result.provider == "telenor":
            # Group by Site_Id; fall back to "unknown" so file is not lost.
            bucket_key = result.site_id or "unknown"
            telenor_buckets[bucket_key].append(path)
        else:
            others.append((result.provider, path))

    groups: list[BtsFileGroup] = []

    for idx, (bts_id, telenor_paths) in enumerate(telenor_buckets.items()):
        groups.append(BtsFileGroup(
            provider="telenor",
            paths=sorted(telenor_paths),
            spec_id=f"{spec_id_prefix}_telenor_{idx}",
            bts_id=bts_id if bts_id != "unknown" else None,
            label=f"Telenor {bts_id}",
        ))

    for idx, (provider, path) in enumerate(others):
        groups.append(BtsFileGroup(
            provider=provider,
            paths=[path],
            spec_id=f"{spec_id_prefix}_{provider}_{idx}",
            bts_id=None,
            label=provider.title(),
        ))

    if unrecognized:
        logger.warning(
            "Unrecognized BTS files (skipped): %s",
            [p.name for p in unrecognized],
        )

    return groups


# Keep for backward-compat (used in single inspect endpoint).
def _sniff_telenor_csv(path: Path) -> str | None:
    """Return Site_Id if file is a Telenor BTS CSV, else None."""
    result = _sniff_bts_provider(path)
    if result and result.provider == "telenor":
        return result.site_id
    return None
