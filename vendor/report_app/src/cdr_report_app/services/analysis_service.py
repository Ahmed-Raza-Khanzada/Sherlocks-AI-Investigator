"""Build structured report analysis from ingested CDR data."""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from cdr_report_app.analysis.activity import build_daily_activity, build_heatmap
from cdr_report_app.analysis.common import prepare_analysis_frame, resolve_columns
from cdr_report_app.analysis.communications import (
    build_bursts,
    build_device_records,
    build_short_codes,
    build_top_contacts,
)
from cdr_report_app.analysis.db_verification import build_db_verification_rows
from cdr_report_app.analysis.locations import build_crime_day_movement, build_long_stays, build_top_locations
from cdr_report_app.analysis.overview import build_quick_stats
from cdr_report_app.domain.analysis_models import ContactHistoryRecord, ReportAnalysis
from cdr_report_app.domain.cdr_models import CrimeContext, IngestionResult, SubscriberMetadata
from cdr_report_app.settings import Settings
from cdr_report_app.utils.phones import normalize_mobile

logger = logging.getLogger(__name__)


def extract_top_contacts_for_lookup(ingestion_result: IngestionResult, settings: Settings) -> list[str]:
    df = _analysis_frame_from_ingestion(ingestion_result)
    cols = resolve_columns(df)
    if not cols.b_number or cols.b_number not in df.columns:
        return []

    top_counts: dict[str, int] = {}
    for value in df[cols.b_number].tolist():
        normalized = _normalize_contact_number(value)
        if not normalized:
            continue
        top_counts[normalized] = top_counts.get(normalized, 0) + 1

    ordered = sorted(top_counts.items(), key=lambda item: item[1], reverse=True)
    return [number for number, _ in ordered[: settings.report.top_contacts_count]]


def compact_hit_results_for_report(raw_results: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw_results, dict):
        return {}

    compacted: dict[str, Any] = {}
    for provider_name, result in raw_results.items():
        if not isinstance(result, dict):
            continue

        provider_key = str(provider_name or "").strip().lower()
        compact_result: dict[str, Any] = {
            "provider": result.get("provider") or provider_key,
            "hit": bool(result.get("hit")),
            "status": str(result.get("status") or ""),
            "summary": str(result.get("summary") or ""),
        }

        if result.get("errors"):
            compact_result["errors"] = [str(item)[:200] for item in (result.get("errors") or [])[:5]]

        data = result.get("data")
        if isinstance(data, dict):
            compact_result["data"] = _compact_provider_data(provider_key, data)
        elif data is not None:
            compact_result["data"] = _prune_nested(data, max_depth=2, max_items=20)

        raw = _compact_provider_raw(provider_key, result.get("raw"))
        if raw not in (None, {}, []):
            compact_result["raw"] = raw

        compacted[provider_key] = compact_result

    return compacted


def build_report_analysis(
    ingestion_result: IngestionResult,
    settings: Settings,
    crime_context: CrimeContext | None = None,
    db_results: dict[str, Any] | None = None,
    top_contact_histories: list[ContactHistoryRecord] | None = None,
) -> ReportAnalysis:
    logger.info(
        "Analysis started | source=%s operator=%s row_count=%s include_db_results=%s include_top_contact_histories=%s",
        ingestion_result.source_path,
        ingestion_result.source_operator or "unknown",
        ingestion_result.row_count,
        bool(db_results),
        bool(top_contact_histories),
    )
    metadata = SubscriberMetadata.model_validate(ingestion_result.metadata.model_dump())
    crime = crime_context or CrimeContext()
    df = _analysis_frame_from_ingestion(ingestion_result)
    cols = resolve_columns(df)
    prepared = prepare_analysis_frame(df, cols, copy_frame=False)

    quick_stats = build_quick_stats(prepared, cols.b_number)
    daily_activity = build_daily_activity(prepared, crime)
    top_locations = build_top_locations(prepared, cols.site_address, settings.report.top_locations_count)
    long_stays = build_long_stays(prepared, cols.site_address or cols.cell_id)
    heatmap = build_heatmap(prepared)
    movement_steps = build_crime_day_movement(prepared, cols.site_address, crime)
    bursts = build_bursts(prepared, cols.b_number, settings.report)
    short_codes = build_short_codes(prepared, cols.b_number, settings.report.top_short_codes_count)
    imei_records = build_device_records(prepared, cols.imei, "IMEI")
    imsi_records = build_device_records(prepared, cols.imsi, "IMSI")
    top_contacts = build_top_contacts(prepared, cols.b_number, cols.call_type, cols.media_type, settings.report.top_contacts_count)

    compact_histories = _compact_contact_histories(top_contact_histories or [])
    if compact_histories:
        cnic_map = {h.number: h.cnic for h in compact_histories if h.cnic}
        for contact in top_contacts:
            if contact.number in cnic_map:
                contact.cnic = cnic_map[contact.number]

    compact_db_results = compact_hit_results_for_report(db_results or {})
    db_rows = build_db_verification_rows(compact_db_results)

    summary = {
        "subscriber_name": metadata.name,
        "msisdn": metadata.msisdn,
        "total_records": quick_stats.total_records,
        "unique_contacts": quick_stats.unique_numbers,
        "repeated_contacts": quick_stats.repeated_contacts,
        "duration": quick_stats.duration_label,
        "top_locations": [item.model_dump() for item in top_locations],
        "long_stays": [item.model_dump() for item in long_stays],
        "burst_count": len(bursts),
        "short_codes_count": len(short_codes),
        "unique_imei_count": len(imei_records),
        "unique_imsi_count": len(imsi_records),
        "top_contacts": [item.model_dump() for item in top_contacts],
        "db_verification": [item.model_dump() for item in db_rows],
    }

    logger.info(
        "Analysis completed | total_records=%s unique_numbers=%s repeated_contacts=%s top_locations=%s long_stays=%s movement_steps=%s top_contacts=%s db_rows=%s",
        quick_stats.total_records,
        quick_stats.unique_numbers,
        quick_stats.repeated_contacts,
        len(top_locations),
        len(long_stays),
        len(movement_steps),
        len(top_contacts),
        len(db_rows),
    )

    return ReportAnalysis(
        source_operator=ingestion_result.source_operator,
        metadata=metadata,
        crime_context=crime,
        quick_stats=quick_stats,
        daily_activity=daily_activity,
        top_locations=top_locations,
        long_stays=long_stays,
        heatmap=heatmap,
        movement_steps=movement_steps,
        bursts=bursts,
        short_codes=short_codes,
        imei_records=imei_records,
        imsi_records=imsi_records,
        top_contacts=top_contacts,
        db_verification_rows=db_rows,
        main_db_results=compact_db_results,
        top_contact_histories=compact_histories,
        analysis_summary=summary,
    )


def _analysis_frame_from_ingestion(ingestion_result: IngestionResult) -> pd.DataFrame:
    if isinstance(ingestion_result.normalized_df, pd.DataFrame):
        return ingestion_result.normalized_df
    return pd.DataFrame(ingestion_result.records)


def _compact_contact_histories(histories: list[ContactHistoryRecord]) -> list[ContactHistoryRecord]:
    compacted: list[ContactHistoryRecord] = []
    for history in histories:
        compacted.append(
            ContactHistoryRecord(
                number=history.number,
                telecom_name=history.telecom_name,
                cnic=history.cnic,
                db_rows=history.db_rows,
                raw_results=compact_hit_results_for_report(history.raw_results),
            )
        )
    return compacted


def _normalize_contact_number(value: object) -> str | None:
    if value is None or value is pd.NA:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    text = str(value).strip().replace("+", "").replace("-", "")
    if "." in text:
        text = text.split(".", 1)[0]
    if not text or text.startswith("*") or len(text) < 8:
        return None
    return normalize_mobile(text) or text


def _compact_provider_data(provider_name: str, data: dict[str, Any]) -> dict[str, Any]:
    compact_data = _prune_nested(data, max_depth=4, max_items=60)
    if not isinstance(compact_data, dict):
        return {}

    if provider_name in {"cro", "psrms"} and isinstance(compact_data.get("firs"), list):
        compact_data["firs"] = compact_data["firs"][:40]
    if provider_name == "old_tenant" and isinstance(compact_data.get("tenants"), list):
        compact_data["tenants"] = compact_data["tenants"][:50]
    if provider_name == "sbvs" and isinstance(compact_data.get("entries"), list):
        compact_data["entries"] = compact_data["entries"][:50]
    if provider_name == "dls" and isinstance(compact_data.get("licenses"), list):
        compact_data["licenses"] = compact_data["licenses"][:40]
    return compact_data


def _compact_provider_raw(provider_name: str, raw: Any) -> Any:
    if provider_name == "hotel_eye" and isinstance(raw, dict):
        return {
            "query": _prune_nested(raw.get("query"), max_depth=2, max_items=20),
            "records": _prune_nested(raw.get("records"), max_depth=3, max_items=12),
        }
    if provider_name == "watchlist" and isinstance(raw, dict):
        compact: dict[str, Any] = {}
        if isinstance(raw.get("_meta"), dict):
            compact["_meta"] = _prune_nested(raw.get("_meta"), max_depth=2, max_items=20)
        compact["data"] = _prune_nested(raw.get("data"), max_depth=3, max_items=10)
        return compact
    if provider_name == "tracs":
        if isinstance(raw, dict):
            compact = _prune_nested(raw, max_depth=4, max_items=15)
            if isinstance(compact, dict) and isinstance(compact.get("data"), list):
                compact["data"] = compact["data"][:12]
            return compact
        if isinstance(raw, list):
            return _prune_nested(raw, max_depth=4, max_items=12)
    return None


def _prune_nested(value: Any, *, max_depth: int, max_items: int, max_text: int = 300) -> Any:
    if max_depth <= 0:
        return None
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                break
            pruned = _prune_nested(item, max_depth=max_depth - 1, max_items=max_items, max_text=max_text)
            if pruned is not None:
                output[str(key)] = pruned
        return output
    if isinstance(value, list):
        output_list: list[Any] = []
        for item in value[:max_items]:
            pruned = _prune_nested(item, max_depth=max_depth - 1, max_items=max_items, max_text=max_text)
            if pruned is not None:
                output_list.append(pruned)
        return output_list
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:max_text]
