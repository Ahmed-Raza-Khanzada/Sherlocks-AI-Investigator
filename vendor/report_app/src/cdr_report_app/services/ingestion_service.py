"""CDR ingestion service."""

from __future__ import annotations

import logging
from pathlib import Path

from cdr_report_app.domain.cdr_models import IngestionResult
from cdr_report_app.ingest.loaders import read_raw_cdr_file
from cdr_report_app.ingest.metadata import extract_metadata_from_top_rows
from cdr_report_app.ingest.normalizers import apply_column_mappings
from cdr_report_app.ingest.schema_detection import OPERATOR_DISPLAY_NAMES, build_column_mappings, detect_header_row, detect_operator
from cdr_report_app.ingest.validators import validate_normalized_cdr
from cdr_report_app.utils.phones import normalize_mobile

logger = logging.getLogger(__name__)


def ingest_cdr_file(
    source_path: Path,
    *,
    include_records: bool = True,
    retain_dataframe: bool = True,
) -> IngestionResult:
    logger.info("Ingestion started | path=%s", source_path)
    raw = read_raw_cdr_file(source_path)
    if raw.empty or len(raw) < 2:
        logger.error("Ingestion failed | path=%s reason=empty_or_too_small", source_path)
        raise ValueError("CDR input file is empty or too small to process.")

    top_rows = [[str(cell) for cell in raw.iloc[i].tolist()] for i in range(min(10, len(raw)))]
    metadata = extract_metadata_from_top_rows(top_rows)

    header_row = detect_header_row(raw)
    headers = [str(cell).strip() or f"COLUMN_{idx + 1}" for idx, cell in enumerate(raw.iloc[header_row].tolist())]
    source_operator = detect_operator(headers)
    logger.info(
        "Header detected | path=%s header_row=%s operator=%s raw_rows=%s raw_columns=%s",
        source_path,
        header_row,
        source_operator or "unknown",
        len(raw),
        len(headers),
    )

    body = raw.iloc[header_row + 1 :].copy()
    body.columns = headers
    body = body[body.apply(lambda row: any(str(value).strip() for value in row), axis=1)].reset_index(drop=True)

    mappings = build_column_mappings(body)
    normalized_df = apply_column_mappings(body, mappings)
    del raw, body
    if source_operator in {"telenor", "unified"} and "MSISDN" in normalized_df.columns:
        column_msisdn = _resolve_target_msisdn_from_column(normalized_df["MSISDN"])
        if column_msisdn:
            metadata.msisdn = column_msisdn
            logger.info(
                "MSISDN resolved from MSISDN column | path=%s operator=%s msisdn=%s",
                source_path,
                source_operator,
                metadata.msisdn,
            )
    if not metadata.msisdn and "MSISDN" in normalized_df.columns:
        series = normalized_df["MSISDN"].dropna().astype(str).str.strip()
        series = series[series != ""]
        if not series.empty:
            metadata.msisdn = normalize_mobile(series.iloc[0]) or series.iloc[0]
            logger.info("Metadata MSISDN fallback applied | path=%s msisdn=%s", source_path, metadata.msisdn)
    warnings = validate_normalized_cdr(normalized_df)
    logger.info(
        "Ingestion completed | path=%s operator=%s records=%s mappings=%s warnings=%s metadata_msisdn=%s metadata_cnic=%s include_records=%s retain_dataframe=%s",
        source_path,
        source_operator or "unknown",
        len(normalized_df),
        len(mappings),
        len(warnings),
        metadata.msisdn,
        metadata.cnic,
        include_records,
        retain_dataframe,
    )
    if warnings:
        for warning in warnings:
            logger.warning("Ingestion warning | path=%s code=%s message=%s", source_path, warning.code, warning.message)

    row_count = len(normalized_df)
    records = normalized_df.to_dict(orient="records") if include_records else []
    retained_df = normalized_df if retain_dataframe else None
    if not retain_dataframe:
        del normalized_df
    return IngestionResult(
        source_path=source_path,
        source_operator=OPERATOR_DISPLAY_NAMES.get(source_operator or "", source_operator),
        metadata=metadata,
        column_mappings=mappings,
        normalized_columns={mapping.original: mapping.normalized for mapping in mappings},
        row_count=row_count,
        warnings=warnings,
        records=records,
        normalized_df=retained_df,
    )


def _resolve_target_msisdn_from_column(series) -> str | None:
    cleaned = (
        series.dropna()
        .astype(str)
        .str.strip()
        .map(lambda value: normalize_mobile(value) or value)
    )
    cleaned = cleaned[cleaned != ""]
    if cleaned.empty:
        return None
    counts = cleaned.value_counts()
    if counts.empty:
        return None
    return str(counts.index[0]).strip() or None
