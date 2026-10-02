"""Cross-tower movement feasibility analysis."""

from __future__ import annotations

import math
from itertools import combinations

import pandas as pd

from cdr_report_app.bts.models import BtsWindowKey, MovementFeasibility


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    if any(v is None or math.isnan(v) for v in [lat1, lon1, lat2, lon2]):
        return float("inf")
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _median_coords(df: pd.DataFrame) -> tuple[float, float] | None:
    """Median lat/lng from a canonical BTS frame slice (ignores NaN rows)."""
    lats = pd.to_numeric(df["LAT"], errors="coerce").dropna()
    lngs = pd.to_numeric(df["LNG"], errors="coerce").dropna()
    if lats.empty or lngs.empty:
        return None
    return float(lats.median()), float(lngs.median())


def movement_feasibility(
    bts_df: pd.DataFrame,
    window_keys: list[BtsWindowKey],
    bts_coords: dict[str, tuple[float, float]] | None = None,
) -> list[MovementFeasibility]:
    """For every pair of windows, check whether movement between the two towers is plausible.

    Speed limit for feasibility: 200 km/h (generous upper bound covering vehicles).
    Only pairs where there are common A-party numbers are returned.

    *bts_coords*: optional override mapping spec_id → (lat, lng). Fallback is
    the median of LAT/LNG values in the spec's rows.
    """
    if len(window_keys) < 2:
        return []

    bts_coords = bts_coords or {}
    results: list[MovementFeasibility] = []

    # Pre-compute coords and A-party sets per window key
    per_window: dict[str, dict] = {}
    for key in window_keys:
        spec_rows = bts_df[bts_df["SPEC_ID"] == key.spec_id]
        coords = bts_coords.get(key.spec_id) or _median_coords(spec_rows)
        a_nums = set(
            bts_df.loc[
                (bts_df["SPEC_ID"] == key.spec_id)
                & (bts_df["CALL_TIME"] >= pd.Timestamp(key.window_start))
                & (bts_df["CALL_TIME"] <= pd.Timestamp(key.window_end)),
                "A_PARTY",
            ]
            .dropna()
            .astype(str)
            .unique()
        )
        per_window[key.spec_id + "|" + key.window_start.isoformat()] = {
            "key": key,
            "coords": coords,
            "a_nums": a_nums,
        }

    pw_items = list(per_window.values())
    for a, b in combinations(pw_items, 2):
        common = a["a_nums"] & b["a_nums"]
        if not common:
            continue

        ka: BtsWindowKey = a["key"]
        kb: BtsWindowKey = b["key"]

        # Time gap between the end of the earlier window and start of the later
        times = sorted([(ka.window_start, ka.window_end, ka), (kb.window_start, kb.window_end, kb)], key=lambda t: t[0])
        earlier_end = times[0][1]
        later_start = times[1][0]
        gap_minutes = max(0.0, (pd.Timestamp(later_start) - pd.Timestamp(earlier_end)).total_seconds() / 60)

        ca, cb = a["coords"], b["coords"]
        if ca and cb:
            dist_m = _haversine(ca[0], ca[1], cb[0], cb[1])
        else:
            dist_m = float("inf")

        if gap_minutes > 0 and dist_m < float("inf"):
            speed = (dist_m / 1000) / (gap_minutes / 60)
        else:
            speed = float("inf")

        feasible = speed <= 200.0 or dist_m == float("inf")

        results.append(
            MovementFeasibility(
                tower_a_spec_id=ka.spec_id,
                tower_b_spec_id=kb.spec_id,
                tower_a_bts_id=ka.bts_id,
                tower_b_bts_id=kb.bts_id,
                distance_m=round(dist_m, 1) if dist_m < float("inf") else -1.0,
                time_gap_minutes=round(gap_minutes, 2),
                required_speed_kmh=round(speed, 2) if speed < float("inf") else -1.0,
                feasible=feasible,
                common_numbers=sorted(common),
            )
        )

    return results
