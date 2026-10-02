"""End-to-end report service."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from pathlib import Path
from typing import Any

from cdr_report_app.domain.cdr_models import CrimeContext
from cdr_report_app.domain.analysis_models import ContactHistoryRecord, DatabaseVerificationRow, ReportAnalysis
from cdr_report_app.domain.provider_models import SearchSubject
from cdr_report_app.integrations.caller_id import CallerIdClient, to_intl_number
from cdr_report_app.integrations.providers import CroPdfAttachmentProvider, IDENTITY_ENRICHMENT_PROVIDERS, PsrmsFirAttachmentProvider, REPORT_LOOKUP_PROVIDERS, UnifiedLookupService
from cdr_report_app.rendering.pdf.report_renderer import render_report_pdf
from cdr_report_app.services.analysis_service import build_report_analysis, extract_top_contacts_for_lookup
from cdr_report_app.services.ingestion_service import ingest_cdr_file
from cdr_report_app.settings import Settings
from cdr_report_app.analysis.db_verification import build_db_verification_rows
from cdr_report_app.utils.memory import log_memory_snapshot

logger = logging.getLogger(__name__)


def _ordered_fir_refs(firs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    items = [item for item in (firs or []) if isinstance(item, dict)]

    def fir_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        year_text = str(item.get("fir_year") or "").strip()
        no_text = str(item.get("fir_no") or "").strip()
        try:
            year_value = int("".join(ch for ch in year_text if ch.isdigit()) or "0")
        except ValueError:
            year_value = 0
        try:
            no_value = int("".join(ch for ch in no_text if ch.isdigit()) or "0")
        except ValueError:
            no_value = 0
        return (year_value, no_value, no_text)

    return sorted(items, key=fir_sort_key)


def generate_report(
    settings: Settings,
    input_path: Path,
    output_path: Path | None = None,
    crime_context: CrimeContext | None = None,
    db_results: dict[str, Any] | None = None,
    metadata_overrides: dict[str, Any] | None = None,
) -> bytes:
    content = generate_report_with_mode(
        settings=settings,
        input_path=input_path,
        output_path=output_path,
        crime_context=crime_context,
        db_results=db_results,
        metadata_overrides=metadata_overrides,
        return_bytes=True,
    )
    return content or b""


def generate_report_with_mode(
    settings: Settings,
    input_path: Path,
    output_path: Path | None = None,
    crime_context: CrimeContext | None = None,
    db_results: dict[str, Any] | None = None,
    metadata_overrides: dict[str, Any] | None = None,
    *,
    return_bytes: bool = True,
) -> bytes | None:
    logger.info("Report generation started | input=%s output=%s", input_path, output_path)
    log_memory_snapshot("single_report:start", logger_instance=logger)
    analysis = prepare_report_analysis(
        settings=settings,
        input_path=input_path,
        crime_context=crime_context,
        db_results=db_results,
        metadata_overrides=metadata_overrides,
        include_live_lookups=True,
        include_attachments=True,
    )
    log_memory_snapshot("single_report:analysis_ready", logger_instance=logger)
    content = render_report_pdf(analysis, output_path=output_path, return_bytes=return_bytes)
    bytes_written = len(content) if isinstance(content, (bytes, bytearray)) else 0
    logger.info("Report generation completed | input=%s output=%s bytes=%s return_bytes=%s", input_path, output_path, bytes_written, return_bytes)
    log_memory_snapshot("single_report:completed", logger_instance=logger)
    return content


def prepare_report_analysis(
    settings: Settings,
    input_path: Path,
    crime_context: CrimeContext | None = None,
    db_results: dict[str, Any] | None = None,
    metadata_overrides: dict[str, Any] | None = None,
    include_live_lookups: bool = True,
    include_attachments: bool = False,
) -> ReportAnalysis:
    logger.info(
        "Preparing report analysis | input=%s include_live_lookups=%s include_attachments=%s",
        input_path,
        include_live_lookups,
        include_attachments,
    )
    log_memory_snapshot("single_prepare:start", logger_instance=logger)
    ingestion = ingest_cdr_file(input_path, include_records=False, retain_dataframe=True)
    log_memory_snapshot("single_prepare:ingested", logger_instance=logger)
    if metadata_overrides:
        if metadata_overrides.get("msisdn"):
            ingestion.metadata.msisdn = str(metadata_overrides["msisdn"]).strip()
        if metadata_overrides.get("cnic"):
            ingestion.metadata.cnic = str(metadata_overrides["cnic"]).strip()
        if metadata_overrides.get("name"):
            ingestion.metadata.name = str(metadata_overrides["name"]).strip()
    if not include_live_lookups:
        logger.info("Skipping live lookups | input=%s", input_path)
        analysis = build_report_analysis(
            ingestion_result=ingestion,
            settings=settings,
            crime_context=crime_context,
            db_results=db_results or {},
        )
        ingestion.normalized_df = None
        log_memory_snapshot("single_prepare:completed_no_lookups", logger_instance=logger)
        return analysis

    lookup_service = UnifiedLookupService(settings)
    try:
        resolved_db_results = db_results or _lookup_main_subject(lookup_service, ingestion)
        _annotate_hotel_eye_result(
            resolved_db_results.get("hotel_eye"),
            explicit_cnic=ingestion.metadata.cnic,
            subscriber_cnic=_extract_subscriber_cnic(resolved_db_results),
        )
        _annotate_hrmis_result(
            resolved_db_results.get("hrmis"),
            explicit_cnic=ingestion.metadata.cnic,
            subscriber_cnic=_extract_subscriber_cnic(resolved_db_results),
        )
        _annotate_employment_db_result(
            resolved_db_results.get("evs"),
            explicit_cnic=ingestion.metadata.cnic,
            subscriber_cnic=_extract_subscriber_cnic(resolved_db_results),
        )
        _annotate_employment_db_result(
            resolved_db_results.get("hope"),
            explicit_cnic=ingestion.metadata.cnic,
            subscriber_cnic=_extract_subscriber_cnic(resolved_db_results),
        )
        _annotate_old_tenant_result(
            resolved_db_results.get("old_tenant"),
            explicit_cnic=ingestion.metadata.cnic,
            subscriber_cnic=_extract_subscriber_cnic(resolved_db_results),
        )
        _annotate_watchlist_result(
            resolved_db_results.get("watchlist"),
            explicit_cnic=ingestion.metadata.cnic,
            subscriber_cnic=_extract_subscriber_cnic(resolved_db_results),
        )
        _annotate_sbvs_result(
            resolved_db_results.get("sbvs"),
            explicit_cnic=ingestion.metadata.cnic,
            subscriber_cnic=_extract_subscriber_cnic(resolved_db_results),
        )
        logger.info("Main subject lookups resolved | providers=%s", list(resolved_db_results))
        log_memory_snapshot("single_prepare:main_lookups_done", logger_instance=logger)

        top_contact_numbers = extract_top_contacts_for_lookup(ingestion, settings)
        contact_histories = _build_contact_histories(lookup_service, top_contact_numbers)
        logger.info("Top contact histories built | count=%s", len(contact_histories))
        log_memory_snapshot("single_prepare:contact_histories_done", logger_instance=logger)

        analysis = build_report_analysis(
            ingestion_result=ingestion,
            settings=settings,
            crime_context=crime_context,
            db_results=resolved_db_results,
            top_contact_histories=contact_histories,
        )
        log_memory_snapshot("single_prepare:analysis_done", logger_instance=logger)
        try:
            _enrich_location_nearest_ps(lookup_service, analysis)
        except Exception as exc:
            logger.error(f"Error enriching locations with nearest PS: {exc}", exc_info=True)

        try:
            _enrich_imei_labels(lookup_service, analysis)
        except Exception as exc:
            logger.error(f"Error enriching IMEI labels: {exc}", exc_info=True)

        try:
            _enrich_caller_id(settings, lookup_service, analysis)
        except Exception as exc:
            logger.error(f"Error enriching caller ID: {exc}", exc_info=True)

        if include_attachments:
            try:
                analysis.attachments = _collect_attachments(settings, analysis, lookup_service)
                logger.info("Attachments collected | count=%s", len(analysis.attachments))
            except Exception as exc:
                logger.error(f"Error collecting attachments: {exc}", exc_info=True)
                analysis.attachments = []
        log_memory_snapshot("single_prepare:attachments_done", logger_instance=logger)
        return analysis
    finally:
        lookup_service._lookup_cache.clear()
        lookup_service.http.session.close()
        ingestion.normalized_df = None
        log_memory_snapshot("single_prepare:cleanup_done", logger_instance=logger)


def _lookup_main_subject(lookup_service: UnifiedLookupService, ingestion) -> dict[str, Any]:
    subject = SearchSubject(cnic=ingestion.metadata.cnic, mobile=ingestion.metadata.msisdn)
    logger.info("Looking up main subject | mobile=%s cnic=%s providers=%s", subject.mobile, subject.cnic, REPORT_LOOKUP_PROVIDERS)
    results = lookup_service.lookup_all(subject, provider_names=REPORT_LOOKUP_PROVIDERS)
    for name, result in results.items():
        logger.info("Main subject provider result | provider=%s status=%s hit=%s summary=%s", name, result.status, result.hit, result.summary)
    return {name: result.model_dump(mode="python") for name, result in results.items()}


def _build_contact_histories(lookup_service: UnifiedLookupService, top_contact_numbers: list[str]) -> list[ContactHistoryRecord]:
    if not top_contact_numbers:
        return []

    histories_by_number: dict[str, ContactHistoryRecord] = {}

    def build_history(number: str) -> ContactHistoryRecord:
        subject = SearchSubject(mobile=number)
        logger.info("Looking up top contact | number=%s", number)
        results = lookup_service.lookup_all(subject, provider_names=REPORT_LOOKUP_PROVIDERS)
        raw_results = {name: result.model_dump(mode="python") for name, result in results.items()}
        identity = _identity_data(raw_results)
        _annotate_hotel_eye_result(
            raw_results.get("hotel_eye"),
            explicit_cnic=None,
            subscriber_cnic=identity.get("cnic"),
        )
        _annotate_hrmis_result(
            raw_results.get("hrmis"),
            explicit_cnic=None,
            subscriber_cnic=identity.get("cnic"),
        )
        _annotate_employment_db_result(
            raw_results.get("evs"),
            explicit_cnic=None,
            subscriber_cnic=identity.get("cnic"),
        )
        _annotate_employment_db_result(
            raw_results.get("hope"),
            explicit_cnic=None,
            subscriber_cnic=identity.get("cnic"),
        )
        _annotate_old_tenant_result(
            raw_results.get("old_tenant"),
            explicit_cnic=None,
            subscriber_cnic=identity.get("cnic"),
        )
        _annotate_watchlist_result(
            raw_results.get("watchlist"),
            explicit_cnic=None,
            subscriber_cnic=identity.get("cnic"),
        )
        _annotate_sbvs_result(
            raw_results.get("sbvs"),
            explicit_cnic=None,
            subscriber_cnic=identity.get("cnic"),
        )
        logger.info(
            "Top contact lookup completed | number=%s hit_providers=%s",
            number,
            [name for name, result in results.items() if result.hit],
        )
        return ContactHistoryRecord(
            number=number,
            telecom_name=identity.get("name"),
            cnic=identity.get("cnic"),
            db_rows=build_db_verification_rows(raw_results),
            raw_results=raw_results,
        )
    with ThreadPoolExecutor(max_workers=min(4, len(top_contact_numbers))) as executor:
        future_map = {executor.submit(build_history, number): number for number in top_contact_numbers}
        for future in as_completed(future_map):
            number = future_map[future]
            try:
                histories_by_number[number] = future.result()
            except Exception as exc:
                logger.error(f"Failed to build contact history for {number}: {exc}", exc_info=True)
    return [histories_by_number[number] for number in top_contact_numbers if number in histories_by_number]


def _identity_data(raw_results: dict[str, Any]) -> dict[str, Any]:
    """Owner identity for the number: SIMs database first, telecom subscriber DB as fallback."""
    for provider_name in IDENTITY_ENRICHMENT_PROVIDERS:
        result = raw_results.get(provider_name)
        if not isinstance(result, dict) or not result.get("hit"):
            continue
        data = result.get("data") or {}
        if isinstance(data, dict) and (data.get("cnic") or data.get("name")):
            return data
    return {}


def _extract_subscriber_cnic(raw_results: dict[str, Any]) -> str | None:
    cnic = str(_identity_data(raw_results).get("cnic") or "").strip()
    return cnic or None


def _annotate_hotel_eye_result(result: dict[str, Any] | None, explicit_cnic: str | None, subscriber_cnic: str | None) -> None:
    if not isinstance(result, dict):
        return
    raw = result.get("raw")
    if not isinstance(raw, dict):
        return
    query = raw.get("query")
    if not isinstance(query, dict):
        return

    used_branch = query.get("used_branch")
    if used_branch != "cnic":
        return

    used_cnic = str(query.get("cnic") or "").strip()
    explicit = str(explicit_cnic or "").strip()
    subscriber = str(subscriber_cnic or "").strip()
    source_label = None
    if used_cnic and subscriber and used_cnic == subscriber and (not explicit or explicit != used_cnic):
        source_label = "subscriber"
    elif used_cnic:
        source_label = "input"

    if source_label:
        query["cnic_source"] = source_label

    data = result.get("data")
    if isinstance(data, dict):
        details = data.get("details")
        if isinstance(details, dict):
            if used_cnic:
                details["cnic"] = used_cnic
            if source_label:
                details["cnic_source"] = source_label

    summary = str(result.get("summary") or "").strip()
    if source_label == "subscriber" and "Subscriber DB CNIC" not in summary:
        result["summary"] = f"{summary} using Subscriber DB CNIC".strip()


def _annotate_hrmis_result(result: dict[str, Any] | None, explicit_cnic: str | None, subscriber_cnic: str | None) -> None:
    if not isinstance(result, dict) or not result.get("hit"):
        return

    data = result.get("data")
    if not isinstance(data, dict):
        return

    if str(data.get("search_branch") or "").strip().lower() != "cnic":
        return

    used_cnic = str(data.get("cnic_used") or "").strip()
    explicit = str(explicit_cnic or "").strip()
    subscriber = str(subscriber_cnic or "").strip()
    source_label = None
    if used_cnic and subscriber and used_cnic == subscriber and (not explicit or explicit != used_cnic):
        source_label = "subscriber"
    elif used_cnic:
        source_label = "input"

    if source_label:
        data["cnic_source"] = source_label

    summary = str(result.get("summary") or "").strip()
    if source_label == "subscriber" and "Subscriber DB CNIC" not in summary:
        result["summary"] = f"{summary} using Subscriber DB CNIC".strip()


def _annotate_employment_db_result(result: dict[str, Any] | None, explicit_cnic: str | None, subscriber_cnic: str | None) -> None:
    if not isinstance(result, dict) or not result.get("hit"):
        return

    data = result.get("data")
    if not isinstance(data, dict):
        return

    used_cnic = str(data.get("cnic") or "").strip()
    explicit = str(explicit_cnic or "").strip()
    subscriber = str(subscriber_cnic or "").strip()
    if not used_cnic:
        return

    source_label = None
    if subscriber and used_cnic == subscriber and (not explicit or explicit != used_cnic):
        source_label = "subscriber"
    elif explicit and used_cnic == explicit:
        source_label = "input"

    if source_label:
        data["cnic_source"] = source_label

    summary = str(result.get("summary") or "").strip()
    if source_label == "subscriber" and "Subscriber DB CNIC" not in summary:
        result["summary"] = f"{summary} using Subscriber DB CNIC".strip()


def _annotate_old_tenant_result(result: dict[str, Any] | None, explicit_cnic: str | None, subscriber_cnic: str | None) -> None:
    if not isinstance(result, dict) or not result.get("hit"):
        return

    data = result.get("data")
    if not isinstance(data, dict):
        return

    sb = data.get("search_branch")
    if sb != "by_cnic":
        return

    used_cnic = str(data.get("owner_cnic") or "").strip()
    explicit = str(explicit_cnic or "").strip()
    subscriber = str(subscriber_cnic or "").strip()
    
    source_label = None
    if used_cnic and subscriber and used_cnic == subscriber and (not explicit or explicit != used_cnic):
        source_label = "subscriber"
    elif used_cnic:
        source_label = "input"

    if source_label:
        data["cnic_source"] = source_label

    summary = str(result.get("summary") or "").strip()
    if source_label == "subscriber" and "Subscriber DB CNIC" not in summary:
        result["summary"] = f"{summary} using Subscriber DB CNIC".strip()


def _annotate_watchlist_result(result: dict[str, Any] | None, explicit_cnic: str | None, subscriber_cnic: str | None) -> None:
    if not isinstance(result, dict) or not result.get("hit"):
        return

    raw = result.get("raw")
    if not isinstance(raw, dict):
        return

    meta = raw.get("_meta")
    if not isinstance(meta, dict):
        return

    used_cnic = str(meta.get("used_cnic") or "").strip()
    explicit = str(explicit_cnic or "").strip()
    subscriber = str(subscriber_cnic or "").strip()
    
    if not used_cnic:
        return

    source_label = None
    if subscriber and used_cnic == subscriber and (not explicit or explicit != used_cnic):
        source_label = "subscriber"
    elif used_cnic:
        source_label = "input"

    if source_label:
        data = result.get("data")
        if isinstance(data, dict):
            data["cnic_source"] = source_label

    summary = str(result.get("summary") or "").strip()
    if source_label == "subscriber" and "Subscriber DB CNIC" not in summary:
        result["summary"] = f"{summary} using Subscriber DB CNIC".strip()


def _annotate_sbvs_result(result: dict[str, Any] | None, explicit_cnic: str | None, subscriber_cnic: str | None) -> None:
    if not isinstance(result, dict) or not result.get("hit"):
        return

    raw = result.get("raw")
    if not isinstance(raw, dict):
        return

    meta = raw.get("_meta")
    if not isinstance(meta, dict):
        return

    used_val = str(meta.get("used_value") or "").strip()
    used_method = str(meta.get("used_method") or "").strip()
    
    explicit = str(explicit_cnic or "").strip()
    subscriber = str(subscriber_cnic or "").strip()
    
    if not used_val:
        return

    source_label = None
    if used_method == "phone":
        source_label = "phone"
    elif used_method == "cnic":
        if subscriber and used_val == subscriber and (not explicit or explicit != used_val):
            source_label = "subscriber"
        else:
            source_label = "input"

    if source_label:
        data = result.get("data")
        # Ensure it has a dict-compatible form or just store dynamically
        # Since it's a Pydantic model parsed outside, we'll store on dict
        if isinstance(data, dict):
            data["lookup_source"] = source_label
        elif hasattr(data, "__dict__"):
            setattr(data, "lookup_source", source_label)

    summary = str(result.get("summary") or "").strip()
    
    if source_label == "phone" and "using Phone" not in summary:
        result["summary"] = f"{summary} using Phone".strip()
    elif source_label == "subscriber" and "using Subscriber DB CNIC" not in summary:
        result["summary"] = f"{summary} using Subscriber DB CNIC".strip()


def _enrich_caller_id(settings: Settings, lookup_service: UnifiedLookupService, analysis) -> None:
    """Run Caller ID (Truecaller-style) lookups for the suspect + top contacts.

    Sequential + rate-limit aware (the client rotates 3 API keys with a delay).
    - Suspect result is injected into ``main_db_results['caller_id']`` and a row is
      added to the A-Party DB verification table.
    - Each top-contact result is injected into that contact's ``raw_results['caller_id']``.
    - If EVERY lookup fails (all keys rate-limited / down), no caller-id detail is
      shown and the A-Party table row reads "Unavailable".
    """
    cfg = settings.caller_id
    if not cfg.ready:
        logger.info("Caller ID skipped | enabled=%s ready=%s", cfg.enabled, cfg.ready)
        return

    client = CallerIdClient(
        lookup_service.http,
        cfg.base_url,
        cfg.api_keys,
        delay_seconds=cfg.delay_seconds,
        timeout_seconds=cfg.timeout_seconds,
    )

    suspect_intl = to_intl_number(analysis.metadata.msisdn)
    contact_intls = [(history, to_intl_number(history.number)) for history in analysis.top_contact_histories]

    # Lookup suspect first, then each contact (key round-robin spans all of them).
    cache: dict[str, Any] = {}

    def lookup(intl: str | None):
        if not intl:
            return None
        if intl not in cache:
            cache[intl] = client.lookup(intl)
        return cache[intl]

    logger.info("Caller ID enrichment started | suspect=%s contacts=%s", bool(suspect_intl), len(contact_intls))
    suspect_outcome = lookup(suspect_intl)

    any_success = bool(suspect_outcome and suspect_outcome.ok)
    contact_outcomes = []
    for history, intl in contact_intls:
        outcome = lookup(intl)
        contact_outcomes.append((history, outcome))
        if outcome and outcome.ok:
            any_success = True

    # Inject suspect detail (only on a successful hit).
    if suspect_outcome and suspect_outcome.ok and suspect_outcome.hit and suspect_outcome.result:
        analysis.main_db_results["caller_id"] = suspect_outcome.result

    # Inject each contact's detail.
    for history, outcome in contact_outcomes:
        if outcome and outcome.ok and outcome.hit and outcome.result:
            history.raw_results["caller_id"] = outcome.result

    # A-Party DB verification table row for the suspect.
    if suspect_intl:
        if suspect_outcome is None or not suspect_outcome.ok:
            status, summary, severity = "Unavailable", "Caller ID service unavailable", "neutral"
        elif suspect_outcome.hit and suspect_outcome.result:
            data = suspect_outcome.result.get("data", {})
            status = "Found"
            summary = str(suspect_outcome.result.get("summary") or "-")[:200]
            severity = "alert" if data.get("spam") else "neutral"
        else:
            status, summary, severity = "No Record", "No caller ID record found", "safe"
        analysis.db_verification_rows.insert(
            0,
            DatabaseVerificationRow(database="CALLER_ID", status=status, summary=summary, severity=severity),
        )

    logger.info(
        "Caller ID enrichment completed | any_success=%s suspect_hit=%s contacts_hit=%s",
        any_success,
        bool(suspect_outcome and suspect_outcome.hit),
        sum(1 for _, o in contact_outcomes if o and o.hit),
    )


def _enrich_location_nearest_ps(lookup_service: UnifiedLookupService, analysis) -> None:
    location_items = [*analysis.top_locations, *analysis.long_stays, *analysis.movement_steps]
    coords_to_lookup = []
    seen_coords: set[tuple[float, float]] = set()
    for item in location_items:
        if item.latitude is None or item.longitude is None:
            continue
        key = (round(float(item.latitude), 4), round(float(item.longitude), 4))
        if key in seen_coords:
            continue
        seen_coords.add(key)
        coords_to_lookup.append((key, float(item.latitude), float(item.longitude)))

    nearest_ps_map: dict[tuple[float, float], dict[str, float | str | None]] = {}
    if coords_to_lookup:
        logger.info("Nearest PS enrichment started | coordinates=%s", len(coords_to_lookup))
        with ThreadPoolExecutor(max_workers=min(4, len(coords_to_lookup))) as executor:
            future_map = {
                executor.submit(
                    lookup_service.lookup_one,
                    "nearest_ps",
                    SearchSubject(latitude=lat, longitude=lng),
                ): key
                for key, lat, lng in coords_to_lookup
            }
            for future in as_completed(future_map):
                key = future_map[future]
                try:
                    result = future.result(timeout=15)
                except Exception:
                    logger.warning("Nearest PS lookup timed out or failed | key=%s", key)
                    continue
                if result.hit and result.data and getattr(result.data, "name", None):
                    name = result.data.name
                    district = getattr(result.data, "district", None)
                    nearest_ps_map[key] = {
                        "label": f"{name}/{district}" if district else name,
                        "latitude": getattr(result.data, "latitude", None),
                        "longitude": getattr(result.data, "longitude", None),
                    }
        logger.info("Nearest PS enrichment completed | resolved=%s", len(nearest_ps_map))

    for item in location_items:
        if item.latitude is None or item.longitude is None:
            continue
        key = (round(float(item.latitude), 4), round(float(item.longitude), 4))
        if key in nearest_ps_map:
            station = nearest_ps_map[key]
            item.nearest_police_station = str(station.get("label") or "").strip() or None
            item.nearest_police_station_latitude = _coerce_optional_float(station.get("latitude"))
            item.nearest_police_station_longitude = _coerce_optional_float(station.get("longitude"))


def _enrich_imei_labels(lookup_service: UnifiedLookupService, analysis) -> None:
    from cdr_report_app.services.tac_lookup import lookup_by_imei

    imei_records = [item for item in analysis.imei_records if item.identifier]
    if not imei_records:
        return

    seen_identifiers: set[str] = set()
    identifiers: list[str] = []
    for item in imei_records:
        normalized = "".join(ch for ch in str(item.identifier) if ch.isdigit())
        if not normalized or normalized in seen_identifiers:
            continue
        seen_identifiers.add(normalized)
        identifiers.append(normalized)

    lookup_map: dict[str, tuple[str, str | None, str | None]] = {}
    logger.info("IMEI enrichment started (TAC local DB) | identifiers=%d", len(identifiers))
    for identifier in identifiers:
        result = lookup_by_imei(identifier)
        if result:
            brand, specs = result
            label = " — ".join(p for p in [brand, specs] if p) or "Unknown Device"
            lookup_map[identifier] = (label, brand, specs)
        else:
            lookup_map[identifier] = ("Unknown Device", None, None)
    logger.info("IMEI enrichment completed | labeled=%d", len(lookup_map))

    # NOTE: The API-based ImeiProvider (ImeiProvider class in providers.py) is intentionally
    # disabled here. The local TAC database is used instead. To re-enable the API lookup,
    # replace the TAC lookup block above with the ThreadPoolExecutor block that calls
    # lookup_service.lookup_one("imei", ...).

    for item in imei_records:
        normalized = "".join(ch for ch in str(item.identifier) if ch.isdigit())
        entry = lookup_map.get(normalized, ("Unknown Device", None, None))
        item.label = entry[0]
        item.brand = entry[1]
        item.specs = entry[2]


def _coerce_optional_float(value: object) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _collect_attachments(settings: Settings, analysis, lookup_service: UnifiedLookupService) -> list:
    logger.info("Attachment collection started")
    attachments = []
    cro_provider = CroPdfAttachmentProvider(settings, lookup_service.http)
    fir_provider = PsrmsFirAttachmentProvider(settings.providers.psrms, lookup_service.http)
    cro_cache: dict[str, Any] = {}
    fir_cache: dict[tuple[str, str, str], Any] = {}

    def _shallow_attachment_copy(cached):
        """Return a new ProviderResult with a fresh data+metadata but shared bytes/raw."""
        if not hasattr(cached, "model_copy") or not cached.data:
            return cached
        fresh_data = cached.data.model_copy(update={"metadata": dict(cached.data.metadata)})
        return cached.model_copy(update={"data": fresh_data, "raw": None})

    def fetch_cro(cro_no: str):
        if cro_no not in cro_cache or not getattr(cro_cache[cro_no], "hit", False):
            cro_cache[cro_no] = cro_provider.fetch_attachment(str(cro_no))
        return _shallow_attachment_copy(cro_cache[cro_no])

    def fetch_fir(fir_no: str, fir_year: str, ps_id: str):
        key = (str(fir_no), str(fir_year), str(ps_id))
        if key not in fir_cache or not getattr(fir_cache[key], "hit", False):
            fir_cache[key] = fir_provider.fetch_attachment(*key)
        return _shallow_attachment_copy(fir_cache[key])

    cro_refs: set[str] = set()
    fir_refs: set[tuple[str, str, str]] = set()

    main_cro = analysis.main_db_results.get("cro", {})
    if settings.attachments.embed_cro_pdf and isinstance(main_cro, dict):
        cro_data = main_cro.get("data", {})
        cro_no = cro_data.get("cro_no") if isinstance(cro_data, dict) else None
        if cro_no:
            cro_refs.add(str(cro_no))

    if settings.attachments.embed_fir_reports:
        main_psrms = analysis.main_db_results.get("psrms", {})
        psrms_data = main_psrms.get("data", {})
        firs = psrms_data.get("firs", []) if isinstance(psrms_data, dict) else []
        for fir in _ordered_fir_refs(firs):
            fir_refs.add((str(fir.get("fir_no", "")), str(fir.get("fir_year", "")), str(fir.get("ps_id", ""))))

    for history in analysis.top_contact_histories:
        cro = history.raw_results.get("cro", {})
        cro_data = cro.get("data", {}) if isinstance(cro, dict) else {}
        cro_no = cro_data.get("cro_no") if isinstance(cro_data, dict) else None
        if settings.attachments.embed_cro_pdf and cro_no:
            cro_refs.add(str(cro_no))

        psrms = history.raw_results.get("psrms", {})
        psrms_data = psrms.get("data", {}) if isinstance(psrms, dict) else {}
        firs = psrms_data.get("firs", []) if isinstance(psrms_data, dict) else []
        if settings.attachments.embed_fir_reports:
            for fir in _ordered_fir_refs(firs):
                fir_refs.add((str(fir.get("fir_no", "")), str(fir.get("fir_year", "")), str(fir.get("ps_id", ""))))

    prefetch_jobs: list[tuple[str, object]] = [("cro", cro_no) for cro_no in sorted(cro_refs)]
    prefetch_jobs.extend(("fir", key) for key in sorted(fir_refs))
    if prefetch_jobs:
        logger.info("Attachment prefetch started | cro=%s fir=%s", len(cro_refs), len(fir_refs))
        with ThreadPoolExecutor(max_workers=min(6, len(prefetch_jobs))) as executor:
            future_map = {}
            for kind, ref in prefetch_jobs:
                if kind == "cro":
                    future_map[executor.submit(fetch_cro, str(ref))] = (kind, ref)
                else:
                    fir_no, fir_year, ps_id = ref
                    future_map[executor.submit(fetch_fir, fir_no, fir_year, ps_id)] = (kind, ref)
            for future in as_completed(future_map):
                kind, ref = future_map[future]
                try:
                    future.result()
                    logger.info("Attachment prefetch completed | kind=%s ref=%s", kind, ref)
                except Exception as exc:
                    logger.exception("Attachment prefetch failed | kind=%s ref=%s error=%s", kind, ref, exc)

    if settings.attachments.embed_cro_pdf and isinstance(main_cro, dict):
        cro_data = main_cro.get("data", {})
        cro_no = cro_data.get("cro_no") if isinstance(cro_data, dict) else None
        if cro_no:
            result = fetch_cro(str(cro_no))
            if result.hit and result.data:
                result.data.label = f"CRO Report - A Party (CRO#{cro_no})"
                result.data.metadata.update(
                    {
                        "insert_after": "main_db_verification:cro",
                        "attachment_kind": "cro_pdf",
                        "subject_label": analysis.metadata.name or analysis.metadata.msisdn or "A Party",
                        "cro_no": cro_no,
                    }
                )
                attachments.append(result.data)
                logger.info("Main CRO attachment added | cro_no=%s", cro_no)

    if settings.attachments.embed_fir_reports:
        main_psrms = analysis.main_db_results.get("psrms", {})
        psrms_data = main_psrms.get("data", {})
        firs = psrms_data.get("firs", []) if isinstance(psrms_data, dict) else []
        for fir in _ordered_fir_refs(firs):
            result = fetch_fir(str(fir.get("fir_no", "")), str(fir.get("fir_year", "")), str(fir.get("ps_id", "")))
            if result.hit and result.data:
                result.data.label = f"FIR Report - A Party ({fir.get('fir_no', '-')}/{fir.get('fir_year', '-')})"
                result.data.metadata.update(
                    {
                        "insert_after": "main_db_verification:psrms",
                        "attachment_kind": "fir_report",
                        "show_summary_page": settings.attachments.include_custom_fir_summary,
                        "subject_label": analysis.metadata.name or analysis.metadata.msisdn or "A Party",
                        "fir_no": fir.get("fir_no"),
                        "fir_year": fir.get("fir_year"),
                        "ps_id": fir.get("ps_id"),
                    }
                )
                attachments.append(result.data)
                logger.info("Main FIR attachment added | fir_no=%s fir_year=%s ps_id=%s", fir.get("fir_no"), fir.get("fir_year"), fir.get("ps_id"))

    for history in analysis.top_contact_histories:
        cro = history.raw_results.get("cro", {})
        cro_data = cro.get("data", {}) if isinstance(cro, dict) else {}
        cro_no = cro_data.get("cro_no") if isinstance(cro_data, dict) else None
        if settings.attachments.embed_cro_pdf and cro_no:
            result = fetch_cro(str(cro_no))
            if result.hit and result.data:
                result.data.label = f"Contact {history.number} CRO#{cro_no}"
                result.data.metadata.update(
                    {
                        "insert_after": f"contact_history:{history.number}:cro",
                        "attachment_kind": "cro_pdf",
                        "subject_label": history.number,
                        "cro_no": cro_no,
                    }
                )
                attachments.append(result.data)
                logger.info("Contact CRO attachment added | contact=%s cro_no=%s", history.number, cro_no)

        psrms = history.raw_results.get("psrms", {})
        psrms_data = psrms.get("data", {}) if isinstance(psrms, dict) else {}
        firs = psrms_data.get("firs", []) if isinstance(psrms_data, dict) else []
        if settings.attachments.embed_fir_reports:
            for fir in _ordered_fir_refs(firs):
                result = fetch_fir(str(fir.get("fir_no", "")), str(fir.get("fir_year", "")), str(fir.get("ps_id", "")))
                if result.hit and result.data:
                    result.data.label = f"Contact {history.number} FIR {fir.get('fir_no', '-')}/{fir.get('fir_year', '-')}"
                    result.data.metadata.update(
                        {
                            "insert_after": f"contact_history:{history.number}:psrms",
                            "attachment_kind": "fir_report",
                            "show_summary_page": settings.attachments.include_custom_fir_summary,
                            "subject_label": history.number,
                            "fir_no": fir.get("fir_no"),
                            "fir_year": fir.get("fir_year"),
                            "ps_id": fir.get("ps_id"),
                        }
                    )
                    attachments.append(result.data)
                    logger.info("Contact FIR attachment added | contact=%s fir_no=%s fir_year=%s ps_id=%s", history.number, fir.get("fir_no"), fir.get("fir_year"), fir.get("ps_id"))

    logger.info("Attachment collection completed | count=%s", len(attachments))
    cro_cache.clear()
    fir_cache.clear()
    return attachments
