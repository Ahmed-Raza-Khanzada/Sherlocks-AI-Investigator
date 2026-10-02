"""Excel workbook renderer for multi-CDR correlation analysis."""

from __future__ import annotations

import gc
import io
import logging
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

from cdr_report_app.domain.analysis_models import MultiReportAnalysis

logger = logging.getLogger(__name__)

# Styling constants
HEADER_FILL = PatternFill(start_color="29417A", end_color="29417A", fill_type="solid")
HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center", wrap_text=True)
DATA_FONT = Font(name="Calibri", size=10)
DATA_ALIGNMENT = Alignment(vertical="top", wrap_text=True)
THIN_BORDER = Border(
    left=Side(style="thin", color="CCCCCC"),
    right=Side(style="thin", color="CCCCCC"),
    top=Side(style="thin", color="CCCCCC"),
    bottom=Side(style="thin", color="CCCCCC"),
)

# Alternating row colors
EVEN_FILL = PatternFill(start_color="F2F6FC", end_color="F2F6FC", fill_type="solid")
ODD_FILL = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")


def _style_header(ws, headers: list[str], widths: list[int] | None = None, start_row: int = 1) -> None:
    """Write styled header row."""
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=start_row, column=col_idx, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGNMENT
        cell.border = THIN_BORDER
        if widths and col_idx <= len(widths):
            ws.column_dimensions[get_column_letter(col_idx)].width = widths[col_idx - 1]
    ws.freeze_panes = f"A{start_row + 1}"


def _write_row(ws, row_idx: int, values: list) -> None:
    """Write a data row with alternating colors."""
    fill = EVEN_FILL if row_idx % 2 == 0 else ODD_FILL
    for col_idx, value in enumerate(values, 1):
        cell = ws.cell(row=row_idx, column=col_idx, value=value)
        cell.font = DATA_FONT
        cell.alignment = DATA_ALIGNMENT
        cell.fill = fill
        cell.border = THIN_BORDER


def render_multi_excel(
    analysis: MultiReportAnalysis,
    output_path: Path | None = None,
    *,
    return_bytes: bool = True,
) -> bytes | None:
    """Generate a structured Excel workbook from multi-CDR analysis."""
    logger.info("Multi-CDR Excel rendering started")
    wb = Workbook()

    has_crime_date = bool(analysis.crime_context.crime_date)

    # Remove default sheet
    wb.remove(wb.active)

    # --- Sheet 1: Summary ---
    _write_summary_sheet(wb, analysis)

    # --- Sheet 2: Targets ---
    _write_targets_sheet(wb, analysis)

    # --- Sheet 3: Common Contacts ---
    _write_common_contacts_sheet(wb, analysis)

    # --- Sheet 4: Crime Day Common (conditional) ---
    if has_crime_date and analysis.crime_day_common_contacts:
        _write_crime_day_common_sheet(wb, analysis)

    # --- Sheet 5: Adjacent Days Common (conditional) ---
    if has_crime_date:
        _write_adjacent_days_sheet(wb, analysis)

    # --- Sheet 6: Direct Interactions ---
    _write_direct_interactions_sheet(wb, analysis)

    # --- Sheet 7: Crime-Proximity Tower Events ---
    if analysis.crime_proximity_events:
        _write_crime_proximity_sheet(wb, analysis)

    # --- Sheet 8: IMEI Cross-Match ---
    if analysis.imei_cross_matches:
        _write_imei_cross_sheet(wb, analysis)

    content: bytes | None = None
    if output_path and not return_bytes:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(output_path)
    else:
        buffer = io.BytesIO()
        wb.save(buffer)
        content = buffer.getvalue()
        buffer.close()
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(content)

    wb.close()
    del wb
    gc.collect()

    logger.info("Multi-CDR Excel rendering completed | size=%d return_bytes=%s", len(content) if content else 0, return_bytes)
    return content


# ---------------------------------------------------------------------------
#  Individual sheet writers
# ---------------------------------------------------------------------------

def _write_summary_sheet(wb: Workbook, analysis: MultiReportAnalysis) -> None:
    ws = wb.create_sheet("Summary")
    ws.sheet_properties.tabColor = "29417A"

    # Title
    title_cell = ws.cell(row=1, column=1, value="Multi-CDR Correlation Analysis — Summary")
    title_cell.font = Font(name="Calibri", bold=True, size=14, color="29417A")
    ws.merge_cells("A1:D1")

    # Key metrics
    metrics = [
        ("Total Targets Analyzed", len(analysis.target_summaries)),
        ("Common Contacts Found", len(analysis.common_contacts)),
        ("Direct Interactions (Target↔Target)", len(analysis.direct_interactions)),
        ("IMEI Cross-Matches", len(analysis.imei_cross_matches)),
        ("Crime-Proximity Tower Events", len(analysis.crime_proximity_events)),
    ]

    if analysis.crime_context.crime_date:
        metrics.append(("Crime Date", analysis.crime_context.crime_date))
        metrics.append(("Crime Time", analysis.crime_context.crime_time or "-"))
        metrics.append((
            "Crime Day Common Contacts",
            len([c for c in analysis.crime_day_common_contacts if c.day_label == "crime_day"]),
        ))

    if analysis.crime_context.crime_place:
        metrics.append(("Crime Location", analysis.crime_context.crime_place))

    row = 3
    for label, value in metrics:
        ws.cell(row=row, column=1, value=label).font = Font(name="Calibri", bold=True, size=11)
        ws.cell(row=row, column=2, value=str(value)).font = DATA_FONT
        row += 1

    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 30


def _write_targets_sheet(wb: Workbook, analysis: MultiReportAnalysis) -> None:
    ws = wb.create_sheet("Targets")
    ws.sheet_properties.tabColor = "4472C4"

    headers = [
        "#", 
        "Target Label", 
        "MSISDN", 
        "Name", 
        "CNIC", 
        "Operator", 
        "Total Calls/SMS", 
        "Incoming Calls",
        "Outgoing Calls",
        "Incoming SMS",
        "Outgoing SMS",
        "Unique Contacts", 
        "Top Locations"
    ]
    widths = [5, 30, 18, 25, 20, 15, 16, 14, 14, 14, 14, 16, 50]
    _style_header(ws, headers, widths)

    # Build subscriber info lookup
    sub_map = {info.identifier: info for info in analysis.target_subscriber_info}

    for idx, summary in enumerate(analysis.target_summaries, start=1):
        sub = sub_map.get(summary.identifier)
        if summary.frequent_locations:
            locs = "\n".join([f"{i}. {loc}" for i, loc in enumerate(summary.frequent_locations, 1)])
        else:
            locs = "-"
            
        _write_row(ws, idx + 1, [
            idx,
            summary.identifier,
            sub.msisdn if sub else "-",
            sub.name if sub else summary.target_name or "-",
            sub.cnic if sub else "-",
            summary.operator or (sub.operator if sub else "-"),
            summary.total_calls,
            summary.incoming_calls,
            summary.outgoing_calls,
            summary.incoming_sms,
            summary.outgoing_sms,
            summary.unique_contacts,
            locs,
        ])


def _write_common_contacts_sheet(wb: Workbook, analysis: MultiReportAnalysis) -> None:
    ws = wb.create_sheet("Common_Contacts")
    ws.sheet_properties.tabColor = "ED7D31"

    headers = ["#", "Contact Number", "Linked Targets", "Target Count", "Total Interactions"]
    widths = [5, 20, 60, 14, 18]
    _style_header(ws, headers, widths)

    for idx, cc in enumerate(analysis.common_contacts, start=1):
        _write_row(ws, idx + 1, [
            idx,
            cc.contact_number,
            ", ".join(cc.target_identifiers),
            len(cc.target_identifiers),
            cc.total_interactions,
        ])


def _write_crime_day_common_sheet(wb: Workbook, analysis: MultiReportAnalysis) -> None:
    ws = wb.create_sheet("CrimeDay_Common")
    ws.sheet_properties.tabColor = "FF0000"

    headers = ["#", "Contact Number", "Day", "Linked Targets", "Total Interactions"]
    widths = [5, 20, 15, 60, 18]
    _style_header(ws, headers, widths)

    crime_day_items = [c for c in analysis.crime_day_common_contacts if c.day_label == "crime_day"]
    for idx, cc in enumerate(crime_day_items, start=1):
        _write_row(ws, idx + 1, [
            idx,
            cc.contact_number,
            "Crime Day",
            ", ".join(cc.target_identifiers),
            cc.total_interactions,
        ])


def _write_adjacent_days_sheet(wb: Workbook, analysis: MultiReportAnalysis) -> None:
    adjacent = [c for c in analysis.crime_day_common_contacts if c.day_label in ("day_before", "day_after")]
    if not adjacent:
        return

    ws = wb.create_sheet("Adjacent_Days")
    ws.sheet_properties.tabColor = "FFC000"

    headers = ["#", "Contact Number", "Day", "Linked Targets", "Total Interactions"]
    widths = [5, 20, 15, 60, 18]
    _style_header(ws, headers, widths)

    day_labels = {"day_before": "Day Before", "day_after": "Day After"}
    for idx, cc in enumerate(adjacent, start=1):
        _write_row(ws, idx + 1, [
            idx,
            cc.contact_number,
            day_labels.get(cc.day_label, cc.day_label),
            ", ".join(cc.target_identifiers),
            cc.total_interactions,
        ])


def _write_direct_interactions_sheet(wb: Workbook, analysis: MultiReportAnalysis) -> None:
    ws = wb.create_sheet("Direct_Interactions")
    ws.sheet_properties.tabColor = "70AD47"

    headers = ["#", "Target 1", "Target 2", "Total Calls/SMS"]
    widths = [5, 40, 40, 18]
    _style_header(ws, headers, widths)

    for idx, ix in enumerate(analysis.direct_interactions, start=1):
        _write_row(ws, idx + 1, [
            idx,
            ix.source_identifier,
            ix.target_identifier,
            ix.interaction_count,
        ])


_BUCKET_LABEL = {"before": "Before Crime", "during": "At Crime Time", "after": "After Crime"}


def _write_crime_proximity_sheet(wb: Workbook, analysis: MultiReportAnalysis) -> None:
    ws = wb.create_sheet("CrimeProximity_Towers")
    ws.sheet_properties.tabColor = "C00000"

    # Warning header
    warn = ws.cell(row=1, column=1, value=(
        "IMPORTANT: Tower Distance — NOT Suspect Distance. "
        "Suspect could be anywhere within tower coverage area (200m–2km). "
        "This shows which TOWERS are near the crime scene, used by suspects around crime time."
    ))
    warn.font = Font(name="Calibri", bold=True, size=10, color="C00000")
    ws.merge_cells("A1:J1")
    ws.row_dimensions[1].height = 30

    ctx = analysis.crime_context
    info = ws.cell(row=2, column=1, value=(
        f"Crime Location: {ctx.crime_place or 'N/A'}  |  "
        f"Coordinates: {ctx.crime_lat}, {ctx.crime_lng}  |  "
        f"Crime Time: {ctx.crime_date} {ctx.crime_time}"
    ))
    info.font = Font(name="Calibri", italic=True, size=9)
    ws.merge_cells("A2:J2")

    headers = [
        "#", "Target", "Time Bucket", "Timestamp", "Minutes From Crime",
        "Tower Distance (m)", "Proximity Strength", "Tower Location",
        "Tower Coordinates", "Call Type", "B-Number (Communicating With)",
    ]
    widths = [5, 20, 18, 22, 20, 20, 18, 44, 26, 16, 30]
    _style_header(ws, headers, widths, start_row=3)

    for idx, e in enumerate(analysis.crime_proximity_events, start=1):
        mins = e.minutes_from_crime
        offset_txt = (
            f"-{abs(mins)} min (before)" if mins < 0
            else f"+{mins} min (after)" if mins > 0
            else "0 (crime time)"
        )
        coord_txt = (
            f"{e.tower_latitude:.5f}, {e.tower_longitude:.5f}"
            if e.tower_latitude and e.tower_longitude else "-"
        )
        _write_row(ws, idx + 3, [
            idx,
            e.target_identifier,
            _BUCKET_LABEL.get(e.time_bucket, e.time_bucket),
            e.timestamp,
            offset_txt,
            e.tower_distance_meters,
            e.proximity_strength,
            e.tower_location,
            coord_txt,
            e.call_type or "-",
            e.b_number or "-",
        ])


def _write_imei_cross_sheet(wb: Workbook, analysis: MultiReportAnalysis) -> None:
    ws = wb.create_sheet("IMEI_CrossMatch")
    ws.sheet_properties.tabColor = "7030A0"

    headers = ["#", "IMEI", "Targets Sharing", "Usage Details"]
    widths = [5, 22, 50, 60]
    _style_header(ws, headers, widths)

    for idx, match in enumerate(analysis.imei_cross_matches, start=1):
        _write_row(ws, idx + 1, [
            idx,
            match.imei,
            ", ".join(match.target_identifiers),
            " | ".join(match.usage_details),
        ])

