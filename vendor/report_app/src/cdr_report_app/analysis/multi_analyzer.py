"""Multi-CDR Analysis engine for correlating multiple records."""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd

from cdr_report_app.domain.cdr_models import CrimeContext
from cdr_report_app.domain.analysis_models import (
    MultiReportAnalysis,
    TargetSummary,
    DirectInteraction,
    CommonContact,
    TimelineEvent,
    CrimeDayCommonContact,
    IMEICrossMatch,
    CrimeProximityEvent,
    LocationVisit,
    DeviceRecord,
)
from cdr_report_app.services.tac_lookup import lookup_by_imei
from cdr_report_app.settings import ReportSettings
from cdr_report_app.utils.phones import normalize_mobile

logger = logging.getLogger(__name__)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two lat/lng points."""
    if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in [lat1, lon1, lat2, lon2]):
        return float("inf")
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _proximity_strength(dist_m: float) -> str:
    """Label how close a tower is to the crime scene."""
    if dist_m <= 100:
        return "Very Strong"
    if dist_m <= 500:
        return "Strong"
    if dist_m <= 1000:
        return "Moderate"
    return "Weak"


def _normalize_b_number(val: object) -> str:
    """Normalize B_NUMBER for cross-telecom comparison."""
    normed = normalize_mobile(val)
    if normed:
        return normed
    raw = str(val or "").strip()
    return raw if raw and raw != "nan" else ""


def _is_regular_number(val: str) -> bool:
    """Filter out short codes and non-phone identifiers."""
    cleaned = val.replace("+", "").replace("-", "")
    return len(cleaned) >= 8 and not cleaned.startswith("*")


# ---------------------------------------------------------------------------
#  Main entry point
# ---------------------------------------------------------------------------

def run_multi_analysis(
    labeled_cdrs: dict[str, pd.DataFrame],
    crime_context: CrimeContext | None = None,
    report_settings: ReportSettings | None = None,
) -> MultiReportAnalysis:
    """
    Run correlation analysis across multiple CDRs.

    Args:
        labeled_cdrs: Mapping of target label -> DataFrame with columns like
                      MSISDN, B_NUMBER, DATETIME, LATITUDE, LONGITUDE, IMEI, SITE_ADDRESS.
        crime_context: Optional crime context. If crime_date is provided, crime-day
                       specific analysis is executed.
    """
    if crime_context is None:
        crime_context = CrimeContext()
    if report_settings is None:
        report_settings = ReportSettings()

    analysis = MultiReportAnalysis(crime_context=crime_context)
    if not labeled_cdrs:
        return analysis

    has_crime_date = bool(crime_context.crime_date)

    # --- Phase 1: Pre-process each CDR, build target summaries ---
    target_msisdns: set[str] = set()
    msisdn_to_target: dict[str, str] = {}
    normalized_cdrs: dict[str, pd.DataFrame] = {}

    for target_id, df in labeled_cdrs.items():
        if df.empty:
            continue

        # Normalize B_NUMBER
        df = df.copy()
        if "B_NUMBER" in df.columns:
            df["_B_NORM"] = df["B_NUMBER"].apply(_normalize_b_number)
        else:
            df["_B_NORM"] = ""

        # Parse datetime
        if "DATETIME" in df.columns:
            df["_DT"] = pd.to_datetime(df["DATETIME"], errors="coerce", format="mixed")
        elif "START_TIME" in df.columns:
            df["_DT"] = pd.to_datetime(df["START_TIME"], errors="coerce", format="mixed")
        else:
            df["_DT"] = pd.NaT

        # Parse lat/lng
        for col_pair in [("LATITUDE", "_LAT"), ("LONGITUDE", "_LNG")]:
            if col_pair[0] in df.columns:
                df[col_pair[1]] = pd.to_numeric(df[col_pair[0]], errors="coerce")
            else:
                df[col_pair[1]] = pd.NA

        normalized_cdrs[target_id] = df

        # Track MSISDN
        if "MSISDN" in df.columns and not df.empty:
            raw_msisdn = str(df["MSISDN"].iloc[0]).strip()
            normed_msisdn = normalize_mobile(raw_msisdn) or raw_msisdn
            target_msisdns.add(normed_msisdn)
            msisdn_to_target[normed_msisdn] = target_id

        # Target summary
        b_numbers = (
            df["_B_NORM"].loc[df["_B_NORM"] != ""].unique().tolist()
            if "_B_NORM" in df.columns
            else []
        )
        site_locs = (
            df["SITE_ADDRESS"].value_counts().head(3).index.tolist()
            if "SITE_ADDRESS" in df.columns
            else []
        )

        in_calls = 0
        out_calls = 0
        in_sms = 0
        out_sms = 0
        if "CALL_TYPE" in df.columns:
            types = df["CALL_TYPE"].astype(str).str.upper()
            in_mask = types.str.contains("IN", na=False)
            out_mask = types.str.contains("OUT", na=False)
            sms_mask = types.str.contains("SMS", na=False)

            in_calls = int((in_mask & ~sms_mask).sum())
            out_calls = int((out_mask & ~sms_mask).sum())
            in_sms = int((in_mask & sms_mask).sum())
            out_sms = int((out_mask & sms_mask).sum())

            # Fallback for simple "Incoming"/"Outgoing" without explicit SMS flag
            if in_calls + out_calls + in_sms + out_sms == 0:
                in_calls = int(in_mask.sum())
                out_calls = int(out_mask.sum())

        top_locations = _build_target_top_locations(df, limit=5)
        imei_records = _build_target_devices(df, "IMEI")
        imsi_records = _build_target_devices(df, "IMSI")

        analysis.target_summaries.append(TargetSummary(
            identifier=target_id,
            total_calls=len(df),
            unique_contacts=len(b_numbers),
            incoming_calls=in_calls,
            outgoing_calls=out_calls,
            incoming_sms=in_sms,
            outgoing_sms=out_sms,
            frequent_locations=[str(loc) for loc in site_locs],
            top_locations=top_locations,
            imei_records=imei_records,
            imsi_records=imsi_records,
        ))

    # --- Phase 2: Direct Interactions & Contact Graph (vectorized) ---
    logger.info("Building contact graph for %d targets", len(normalized_cdrs))
    contact_to_targets: dict[str, set[str]] = defaultdict(set)
    contact_interaction_count: dict[str, int] = defaultdict(int)
    direct_ix_counts: dict[tuple[str, str], int] = defaultdict(int)

    for target_id, df in normalized_cdrs.items():
        valid = df[df["_B_NORM"] != ""]
        if valid.empty:
            continue

        for b_num, group in valid.groupby("_B_NORM"):
            b_str = str(b_num)
            count = len(group)

            if b_str in msisdn_to_target:
                other_target = msisdn_to_target[b_str]
                if other_target != target_id:
                    pair = tuple(sorted([target_id, other_target]))
                    direct_ix_counts[pair] += count

                    # Timeline events on crime date
                    if has_crime_date:
                        crime_date_rows = group[group["_DT"].dt.date.astype(str) == crime_context.crime_date]
                        event_limit = getattr(report_settings, "multi_timeline_events_per_pair", None)
                        timeline_sample = (
                            crime_date_rows
                            if event_limit is None
                            else crime_date_rows.head(max(event_limit, 0))
                        )
                        for _, row in timeline_sample.iterrows():
                            dt = row["_DT"]
                            if pd.notna(dt):
                                analysis.timeline_events.append(TimelineEvent(
                                    timestamp=dt.strftime("%Y-%m-%d %H:%M:%S"),
                                    event_type="Call/SMS",
                                    target_1=target_id,
                                    target_2=other_target,
                                    targets=[target_id, other_target],
                                    description=f"{target_id} communicated with {other_target}",
                                ))
            else:
                if _is_regular_number(b_str):
                    contact_to_targets[b_str].add(target_id)
                    contact_interaction_count[b_str] += count

    # Direct interactions
    for pair, count in sorted(direct_ix_counts.items(), key=lambda x: x[1], reverse=True):
        analysis.direct_interactions.append(DirectInteraction(
            source_identifier=pair[0],
            target_identifier=pair[1],
            interaction_count=count,
        ))

    # Common contacts (numbers contacted by 2+ targets)
    for contact, t_set in contact_to_targets.items():
        if len(t_set) > 1:
            analysis.common_contacts.append(CommonContact(
                contact_number=contact,
                target_identifiers=sorted(t_set),
                total_interactions=contact_interaction_count.get(contact, 0),
            ))
    analysis.common_contacts.sort(key=lambda x: x.total_interactions, reverse=True)

    logger.info("Found %d common contacts, %d direct interactions", len(analysis.common_contacts), len(analysis.direct_interactions))

    # --- Phase 3: Crime Day ±1 Common Contacts (conditional) ---
    if has_crime_date:
        analysis.crime_day_common_contacts = _build_crime_day_common_contacts(
            normalized_cdrs, crime_context.crime_date, msisdn_to_target  # type: ignore[arg-type]
        )
        logger.info("Crime day ±1 common contacts: %d", len(analysis.crime_day_common_contacts))

    # --- Phase 4: IMEI Cross-Match ---
    analysis.imei_cross_matches = _build_imei_cross_matches(normalized_cdrs)
    if analysis.imei_cross_matches:
        logger.info("IMEI cross-matches found: %d", len(analysis.imei_cross_matches))

    # --- Phase 5: Crime-Proximity Tower Analysis ---
    has_crime_loc = bool(crime_context.crime_lat and crime_context.crime_lng and crime_context.crime_time)
    if has_crime_loc:
        analysis.crime_proximity_events = _build_crime_proximity_events(
            normalized_cdrs, crime_context, report_settings
        )
        logger.info("Crime-proximity tower events: %d", len(analysis.crime_proximity_events))

    # Sort timeline chronologically
    analysis.timeline_events.sort(key=lambda x: x.timestamp)

    # Release all intermediate DataFrames and dicts — no longer needed after analysis
    normalized_cdrs.clear()
    contact_to_targets.clear()
    contact_interaction_count.clear()
    direct_ix_counts.clear()

    return analysis


# ---------------------------------------------------------------------------
#  Crime Day ±1 Common Contacts
# ---------------------------------------------------------------------------

def _build_crime_day_common_contacts(
    cdrs: dict[str, pd.DataFrame],
    crime_date_str: str,
    msisdn_to_target: dict[str, str],
) -> list[CrimeDayCommonContact]:
    """Find contacts shared by 2+ targets on crime day, day before, and day after."""
    try:
        crime_date = pd.Timestamp(crime_date_str).date()
    except Exception:
        return []

    day_offsets = {
        "day_before": crime_date - timedelta(days=1),
        "crime_day": crime_date,
        "day_after": crime_date + timedelta(days=1),
    }

    results: list[CrimeDayCommonContact] = []
    target_msisdns = set(msisdn_to_target.keys())

    for day_label, target_date in day_offsets.items():
        # For each target, collect the B_NUMBERs contacted on this day
        day_contacts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for target_id, df in cdrs.items():
            if "_DT" not in df.columns or "_B_NORM" not in df.columns:
                continue

            day_mask = df["_DT"].dt.date == target_date
            day_rows = df[day_mask & (df["_B_NORM"] != "")]
            if day_rows.empty:
                continue

            for b_num, count in day_rows["_B_NORM"].value_counts().items():
                b_str = str(b_num)
                if b_str in target_msisdns:
                    continue  # skip direct target-to-target
                if _is_regular_number(b_str):
                    day_contacts[b_str][target_id] = int(count)

        # Find contacts shared by 2+ targets
        for contact, targets_map in day_contacts.items():
            if len(targets_map) > 1:
                results.append(CrimeDayCommonContact(
                    contact_number=contact,
                    day_label=day_label,
                    target_identifiers=sorted(targets_map.keys()),
                    total_interactions=sum(targets_map.values()),
                ))

    results.sort(key=lambda x: (
        {"crime_day": 0, "day_before": 1, "day_after": 2}.get(x.day_label, 3),
        -x.total_interactions,
    ))
    return results


# ---------------------------------------------------------------------------
#  IMEI Cross-Match
# ---------------------------------------------------------------------------

def _build_imei_cross_matches(cdrs: dict[str, pd.DataFrame]) -> list[IMEICrossMatch]:
    """Find IMEI devices used by 2+ different targets."""
    imei_to_targets: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)

    for target_id, df in cdrs.items():
        if "IMEI" not in df.columns or "_DT" not in df.columns:
            continue

        for imei_val, group in df.dropna(subset=["IMEI"]).groupby("IMEI"):
            imei_str = str(imei_val).split(".")[0].strip()
            if len(imei_str) < 10:  # skip garbage
                continue

            dates = group["_DT"].dropna()
            if dates.empty:
                imei_to_targets[imei_str][target_id] = ("Unknown", "Unknown")
            else:
                first = dates.min().strftime("%Y-%m-%d")
                last = dates.max().strftime("%Y-%m-%d")
                imei_to_targets[imei_str][target_id] = (first, last)

    results: list[IMEICrossMatch] = []
    for imei, targets_map in imei_to_targets.items():
        if len(targets_map) > 1:
            details = [f"{tid}: {dates[0]} to {dates[1]}" for tid, dates in targets_map.items()]
            results.append(IMEICrossMatch(
                imei=imei,
                target_identifiers=sorted(targets_map.keys()),
                usage_details=details,
            ))

    return results


# ---------------------------------------------------------------------------
#  Crime-Proximity Tower Analysis
# ---------------------------------------------------------------------------

def _build_crime_proximity_events(
    cdrs: dict[str, pd.DataFrame],
    crime_context: CrimeContext,
    report_settings: "ReportSettings",
) -> list[CrimeProximityEvent]:
    """
    Find CDR pings from towers that are physically close to the crime location,
    recorded around the crime time (before, during, after).

    NOTE: The distance computed here is TOWER-to-crime-scene distance, NOT the
    suspect's actual distance. A suspect making a call could be anywhere within
    that tower's coverage area (typically 200m–2km in urban areas).
    """
    # --- Validate required inputs ---
    try:
        clat = float(crime_context.crime_lat)   # type: ignore[arg-type]
        clng = float(crime_context.crime_lng)   # type: ignore[arg-type]
    except (TypeError, ValueError):
        return []

    if not crime_context.crime_date or not crime_context.crime_time:
        return []

    try:
        crime_ts = datetime.strptime(
            f"{crime_context.crime_date} {crime_context.crime_time}", "%Y-%m-%d %H:%M"
        )
    except ValueError:
        try:
            crime_ts = datetime.strptime(
                f"{crime_context.crime_date} {crime_context.crime_time}", "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            return []

    before_min = report_settings.crime_before_window_minutes
    after_min = report_settings.crime_after_window_minutes
    during_tol = report_settings.crime_during_tolerance_minutes
    radius_m = report_settings.crime_proximity_radius_meters

    window_start = crime_ts - timedelta(minutes=before_min)
    window_end   = crime_ts + timedelta(minutes=after_min)
    during_start = crime_ts - timedelta(minutes=during_tol)
    during_end   = crime_ts + timedelta(minutes=during_tol)

    results: list[CrimeProximityEvent] = []

    for target_id, df in cdrs.items():
        required = {"_DT", "_LAT", "_LNG"}
        if not required.issubset(df.columns):
            continue

        # Fast pre-filter: rows within the outer time window with valid coordinates
        time_mask = (df["_DT"] >= window_start) & (df["_DT"] <= window_end)
        candidates = df[time_mask].dropna(subset=["_LAT", "_LNG", "_DT"])
        if candidates.empty:
            continue

        for _, row in candidates.iterrows():
            tower_lat = float(row["_LAT"])
            tower_lng = float(row["_LNG"])

            # Distance is TOWER to crime scene — not suspect to crime scene
            dist = _haversine(clat, clng, tower_lat, tower_lng)
            if dist > radius_m:
                continue

            dt: datetime = row["_DT"].to_pydatetime()
            minutes_delta = int((dt - crime_ts).total_seconds() / 60)

            # Classify time bucket — "during" takes priority
            if during_start <= dt <= during_end:
                bucket = "during"
            elif dt < crime_ts:
                bucket = "before"
            else:
                bucket = "after"

            b_raw = str(row.get("_B_NORM") or row.get("B_NUMBER") or "").strip()
            b_number = b_raw if b_raw and b_raw != "nan" else None

            call_type_raw = str(row.get("CALL_TYPE") or "").strip()
            call_type = call_type_raw if call_type_raw and call_type_raw != "nan" else None

            site = str(row.get("SITE_ADDRESS") or "").strip()
            tower_location = site if site and site != "nan" else f"Tower @ {tower_lat:.5f},{tower_lng:.5f}"

            results.append(CrimeProximityEvent(
                target_identifier=target_id,
                time_bucket=bucket,
                timestamp=dt.strftime("%Y-%m-%d %H:%M:%S"),
                minutes_from_crime=minutes_delta,
                tower_distance_meters=round(dist, 1),
                proximity_strength=_proximity_strength(dist),
                tower_location=tower_location,
                tower_latitude=tower_lat,
                tower_longitude=tower_lng,
                call_type=call_type,
                b_number=b_number,
            ))

    _BUCKET_ORDER = {"before": 0, "during": 1, "after": 2}
    results.sort(key=lambda e: (_BUCKET_ORDER.get(e.time_bucket, 3), e.timestamp))
    return results


# ---------------------------------------------------------------------------
#  Per-target Location + Device Builders
# ---------------------------------------------------------------------------

def _build_target_top_locations(df: pd.DataFrame, limit: int = 5) -> list[LocationVisit]:
    """Top-N SITE_ADDRESS locations for a suspect with visit count and coords."""
    if "SITE_ADDRESS" not in df.columns:
        return []

    grouped = df.groupby("SITE_ADDRESS", dropna=True)
    scored = []
    for loc, group in grouped:
        loc_str = str(loc).strip()
        if not loc_str or loc_str.lower() == "nan":
            continue
        visits = len(group)
        lat = group["_LAT"].dropna().iloc[0] if "_LAT" in group.columns and group["_LAT"].notna().any() else None
        lng = group["_LNG"].dropna().iloc[0] if "_LNG" in group.columns and group["_LNG"].notna().any() else None
        scored.append((visits, loc_str, lat, lng))

    scored.sort(key=lambda x: x[0], reverse=True)

    out: list[LocationVisit] = []
    for order, (visits, loc_str, lat, lng) in enumerate(scored[:limit], start=1):
        coords_text = f"{float(lat):.5f}, {float(lng):.5f}" if lat is not None and lng is not None else None
        out.append(LocationVisit(
            order=order,
            location=loc_str,
            visits=int(visits),
            latitude=float(lat) if lat is not None else None,
            longitude=float(lng) if lng is not None else None,
            coordinates_text=coords_text,
        ))
    return out


def _build_target_devices(df: pd.DataFrame, column: str) -> list[DeviceRecord]:
    """Build IMEI/IMSI DeviceRecord list for a single target. Applies TAC lookup for IMEI."""
    if column not in df.columns or "_DT" not in df.columns:
        return []

    valid = df.dropna(subset=[column])
    if valid.empty:
        return []

    records: list[DeviceRecord] = []
    for device_val, group in valid.groupby(column):
        identifier = str(device_val).split(".")[0].strip()
        if len(identifier) < 8:
            continue

        dates = group["_DT"].dropna()
        first = dates.min().strftime("%d %b %Y") if not dates.empty else None
        last = dates.max().strftime("%d %b %Y") if not dates.empty else None

        brand: str | None = None
        specs: str | None = None
        label: str | None = None
        if column == "IMEI":
            result = lookup_by_imei(identifier)
            if result:
                brand, specs = result
                label = " — ".join(p for p in [brand, specs] if p) or "Unknown Device"
            else:
                label = "Unknown Device"

        records.append(DeviceRecord(
            identifier=identifier,
            label=label,
            brand=brand,
            specs=specs,
            records=len(group),
            first_seen=first,
            last_seen=last,
        ))

    records.sort(key=lambda r: r.records, reverse=True)
    return records
