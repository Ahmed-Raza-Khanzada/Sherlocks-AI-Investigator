"""Orchestration service for multi-CDR analysis, subscriber lookups, and output generation."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

from cdr_report_app.analysis.multi_analyzer import run_multi_analysis
from cdr_report_app.domain.analysis_models import MultiReportAnalysis, TargetSubscriberInfo
from cdr_report_app.domain.cdr_models import CrimeContext
from cdr_report_app.domain.provider_models import SearchSubject
from cdr_report_app.rendering.multi_excel_renderer import render_multi_excel
from cdr_report_app.rendering.pdf.multi_report_renderer import render_multi_report_pdf
from cdr_report_app.services.ingestion_service import ingest_cdr_file
from cdr_report_app.settings import Settings
from cdr_report_app.utils.memory import log_memory_snapshot
from cdr_report_app.utils.phones import normalize_mobile

logger = logging.getLogger(__name__)


def _lookup_subscriber(settings: Settings, msisdn: str, target_id: str) -> TargetSubscriberInfo:
    """Resolve one target MSISDN: SIMs database first, telecom subscriber DB as fallback."""
    info = TargetSubscriberInfo(identifier=target_id, msisdn=msisdn)
    service = None
    try:
        from cdr_report_app.integrations.providers import IDENTITY_ENRICHMENT_PROVIDERS, UnifiedLookupService
        service = UnifiedLookupService(settings)
        subject = SearchSubject(mobile=msisdn)
        for provider_name in IDENTITY_ENRICHMENT_PROVIDERS:
            result = service.lookup_one(provider_name, subject)
            if result.hit and result.data:
                data = result.data
                info.name = getattr(data, "name", None)
                info.cnic = getattr(data, "cnic", None)
                info.address = getattr(data, "address", None)
                info.operator = getattr(data, "operator", None)
                logger.info("Identity lookup hit | provider=%s target=%s msisdn=%s name=%s", provider_name, target_id, msisdn, info.name)
                break
            logger.info("Identity lookup miss | provider=%s target=%s msisdn=%s", provider_name, target_id, msisdn)
    except Exception as exc:
        logger.exception("Subscriber lookup error | target=%s error=%s", target_id, exc)
    finally:
        if service is not None:
            service.http.close()
    return info


def generate_multi_report(
    settings: Settings,
    input_files: list[Path],
    crime_context: CrimeContext | None = None,
    output_dir: Path | None = None,
    target_labels: list[str] | None = None,
    max_concurrent_lookups: int = 5,
    output_pdf_path: Path | None = None,
    output_excel_path: Path | None = None,
    *,
    return_bytes: bool = True,
) -> tuple[bytes | None, bytes | None, MultiReportAnalysis]:
    """
    Full multi-CDR pipeline: ingest → subscriber lookups → analysis → render PDF + Excel.
    """
    if crime_context is None:
        crime_context = CrimeContext()
    if output_dir is None:
        output_dir = settings.app.output_dir

    output_dir.mkdir(parents=True, exist_ok=True)
    log_memory_snapshot("multi_report:start", logger_instance=logger)

    # --- Step 1: Ingest all CDR files ---
    logger.info("Multi-CDR ingestion started | files=%d", len(input_files))
    labeled_cdrs: dict[str, pd.DataFrame] = {}
    msisdn_map: dict[str, str] = {}  # target_id -> msisdn
    operator_map: dict[str, str] = {}  # target_id -> operator
    metadata_names: dict[str, str] = {} # target_id -> name from metadata

    for i, file_path in enumerate(input_files):
        try:
            ingestion = ingest_cdr_file(file_path, include_records=False, retain_dataframe=True)
            df = ingestion.normalized_df if isinstance(ingestion.normalized_df, pd.DataFrame) else pd.DataFrame(ingestion.records)
            msisdn = ingestion.metadata.msisdn or f"Unknown_{i+1}"
            normed = normalize_mobile(msisdn) or msisdn
            
            # Labeling priority: Manual > Metadata > Target N
            # We want to show the name in the label if available.
            meta_name = ingestion.metadata.name
            manual_label = target_labels[i] if target_labels and i < len(target_labels) else None
            
            if manual_label:
                label = manual_label
            elif meta_name:
                label = meta_name
            else:
                label = f"Target {i+1}"
            
            # Ensure unique labels if they collide
            base_label = label
            counter = 1
            while label in labeled_cdrs:
                label = f"{base_label} ({counter})"
                counter += 1

            labeled_cdrs[label] = df
            msisdn_map[label] = normed
            operator_map[label] = ingestion.source_operator or "Unknown"
            if meta_name:
                metadata_names[label] = meta_name

            logger.info(
                "Ingested CDR | target=%s file=%s rows=%d operator=%s",
                label, file_path.name, len(df), ingestion.source_operator,
            )
            ingestion.normalized_df = None
        except Exception as exc:
            logger.exception("Failed to ingest CDR | file=%s error=%s", file_path.name, exc)
    log_memory_snapshot("multi_report:ingestion_done", logger_instance=logger)

    if len(labeled_cdrs) < 2:
        logger.error("Need at least 2 CDRs for multi-analysis, got %d", len(labeled_cdrs))
        raise ValueError(f"At least 2 valid CDR files required, only {len(labeled_cdrs)} could be parsed.")

    # --- Step 2: Subscriber lookups (concurrent, capped) ---
    logger.info("Subscriber lookups started | targets=%d max_concurrent=%d", len(msisdn_map), max_concurrent_lookups)
    subscriber_infos: list[TargetSubscriberInfo] = []

    with ThreadPoolExecutor(max_workers=min(max_concurrent_lookups, len(msisdn_map))) as executor:
        futures = {
            executor.submit(_lookup_subscriber, settings, msisdn, target_id): target_id
            for target_id, msisdn in msisdn_map.items()
        }
        for future in as_completed(futures):
            target_id = futures[future]
            try:
                info = future.result()
                # If we don't have an operator from ingestion, use the one from subscriber lookup
                if not operator_map.get(target_id) and info.operator:
                    operator_map[target_id] = info.operator
                subscriber_infos.append(info)
            except Exception as exc:
                logger.exception("Subscriber future error | target=%s error=%s", target_id, exc)
                subscriber_infos.append(TargetSubscriberInfo(
                    identifier=target_id, msisdn=msisdn_map.get(target_id, ""),
                ))
    log_memory_snapshot("multi_report:subscriber_lookups_done", logger_instance=logger)

    # Sort subscriber infos to match target order
    target_order = list(labeled_cdrs.keys())
    subscriber_infos.sort(key=lambda x: target_order.index(x.identifier) if x.identifier in target_order else 999)

    # --- Step 3: Run multi-CDR analysis ---
    logger.info("Multi-CDR analysis started | targets=%d", len(labeled_cdrs))
    analysis = run_multi_analysis(
        labeled_cdrs,
        crime_context=crime_context,
        report_settings=settings.report,
    )
    log_memory_snapshot("multi_report:analysis_done", logger_instance=logger)
    analysis.target_subscriber_info = subscriber_infos

    # Enrich target summaries
    sub_name_map = {info.identifier: info.name for info in subscriber_infos if info.name}
    for summary in analysis.target_summaries:
        # Operator info
        summary.operator = operator_map.get(summary.identifier)
        
        # Name info: prioritize Lookup Name > Metadata Name > self label if it's not "Target N"
        lookup_name = sub_name_map.get(summary.identifier)
        meta_name = metadata_names.get(summary.identifier)
        
        if lookup_name:
            summary.target_name = lookup_name
        elif meta_name:
            summary.target_name = meta_name
        # If the identifier itself was set to a name, it's already there in the UI/reports

    # --- Step 4: Render outputs ---
    pdf_path = output_pdf_path or (output_dir / "multi_correlation_report.pdf")
    excel_path = output_excel_path or (output_dir / "multi_correlation_analysis.xlsx")

    logger.info("Rendering multi-CDR PDF | path=%s", pdf_path)
    pdf_bytes = render_multi_report_pdf(analysis, output_path=pdf_path, return_bytes=return_bytes)

    logger.info("Rendering multi-CDR Excel | path=%s", excel_path)
    excel_bytes = render_multi_excel(analysis, output_path=excel_path, return_bytes=return_bytes)

    labeled_cdrs.clear()
    log_memory_snapshot("multi_report:render_done", logger_instance=logger)
    return pdf_bytes, excel_bytes, analysis
