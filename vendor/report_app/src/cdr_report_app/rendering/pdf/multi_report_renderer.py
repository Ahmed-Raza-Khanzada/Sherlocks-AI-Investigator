"""Renderer for multi-CDR correlation reports."""

from __future__ import annotations

import logging
from pathlib import Path

from cdr_report_app.domain.analysis_models import MultiReportAnalysis
from cdr_report_app.rendering.charts import (
    create_interaction_heatmap,
    create_event_timeline,
    create_network_graph,
)
from cdr_report_app.rendering.pdf.builder import ReportPdf

logger = logging.getLogger(__name__)


def render_multi_report_pdf(
    analysis: MultiReportAnalysis,
    output_path: Path | None = None,
    *,
    return_bytes: bool = True,
) -> bytes | None:
    logger.info("Multi-CDR PDF rendering started")
    pdf = ReportPdf()
    pdf.add_page()

    has_crime_date = bool(analysis.crime_context.crime_date)

    _render_title(pdf, analysis)
    _render_executive_summary(pdf, analysis)
    _render_target_subscriber_table(pdf, analysis)
    _render_interactions(pdf, analysis)
    _render_common_contacts(pdf, analysis)

    if has_crime_date:
        _render_crime_day_common(pdf, analysis)
        _render_adjacent_day_common(pdf, analysis)

    if analysis.crime_proximity_events:
        _render_crime_proximity(pdf, analysis)

    _render_imei_cross(pdf, analysis)
    _render_target_summaries(pdf, analysis)

    content: bytes | None = None
    if output_path is not None and not return_bytes:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pdf.output(str(output_path))
    else:
        content = bytes(pdf.output())
        if output_path is not None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(content)

    pdf.cleanup()
    logger.info("Multi-CDR PDF rendering completed | return_bytes=%s", return_bytes)
    return content


# ---------------------------------------------------------------------------
#  Title & Executive Summary
# ---------------------------------------------------------------------------

def _render_title(pdf: ReportPdf, analysis: MultiReportAnalysis) -> None:
    pdf.set_fill_color(41, 65, 122)
    pdf.rect(10, 10, 190, 22, style="F")
    pdf.set_xy(10, 12)
    pdf.set_font(pdf.dfont, "B", 18)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(190, 9, "Multi-CDR Correlation Analysis", align="C", ln=True)
    pdf.set_text_font("اجتماعی تجزیاتی رپورٹ", "", 9)
    pdf.set_text_color(220, 220, 240)
    pdf.cell(190, 5, pdf._safe_text("اجتماعی تجزیاتی رپورٹ"), align="C", ln=True)
    pdf.set_text_color(33, 37, 41)
    pdf.ln(6)


def _render_executive_summary(pdf: ReportPdf, analysis: MultiReportAnalysis) -> None:
    pdf.section_heading("خلاصہ (Executive Summary)")
    pdf.note("اس سیکشن میں مقدمے کا مختصر خلاصہ پیش کیا گیا ہے۔")

    total_targets = len(analysis.target_summaries)
    connected_targets = len(
        {ix.source_identifier for ix in analysis.direct_interactions}
        | {ix.target_identifier for ix in analysis.direct_interactions}
    )
    common_contacts = len(analysis.common_contacts)
    imei_matches = len(analysis.imei_cross_matches)
    has_crime_date = bool(analysis.crime_context.crime_date)

    summary_urdu = [
        f"اس رپورٹ میں {total_targets} ٹارگٹ نمبرز کا آپس میں تجزیہ کیا گیا ہے ۔",
    ]
    if connected_targets:
        summary_urdu.append(f"ان میں سے {connected_targets} نمبرز کا آپس میں ڈائریکٹ رابطہ پایا گیا ہے ۔")
        
    if common_contacts:
        summary_urdu.append(f"مجموعی طور پر {common_contacts} ایسے نمبرز (مشترکہ رابطے) ملے ہیں جن سے ٹارگٹس نے بات کی ہے ۔")

    if has_crime_date:
        crime_day_count = len([c for c in analysis.crime_day_common_contacts if c.day_label == "crime_day"])
        if crime_day_count:
            summary_urdu.append(f"واردات والے دن {crime_day_count} مشترکہ رابطے ملے ہیں ۔")

    proximity_targets = len({e.target_identifier for e in analysis.crime_proximity_events})
    if proximity_targets:
        during_count = len([e for e in analysis.crime_proximity_events if e.time_bucket == "during"])
        summary_urdu.append(
            f"{proximity_targets} ٹارگٹ کے فون نے واردات والی جگہ کے قریب موجود ٹاور استعمال کیا "
            f"(یہ ٹاور کی قربت ہے، ملزم کی عین لوکیشن نہیں) ۔"
        )
        if during_count:
            summary_urdu.append(f"واردات کے وقت {during_count} CDR ریکارڈ قریبی ٹاور سے ملے ۔")

    if imei_matches:
        summary_urdu.append(f"{imei_matches} موبائل ہینڈ سیٹس (IMEI) ایسے ہیں جو ایک سے زیادہ ٹارگٹس نے استعمال کیے ۔")

    for part in summary_urdu:
        pdf.bullet_item(part)

    pdf.ln(2)

    crime = analysis.crime_context
    if crime.crime_place or crime.crime_date:
        pdf.sub_heading("Crime Context")
        if crime.crime_date:
            pdf.key_value("Date", crime.crime_date)
        if crime.crime_time:
            pdf.key_value("Time", crime.crime_time)
        if crime.crime_place:
            pdf.key_value("Location", crime.crime_place)
        if crime.crime_lat and crime.crime_lng:
            coord_txt = f"{float(crime.crime_lat):.5f}, {float(crime.crime_lng):.5f}"
            pdf.key_value("Coordinates", (coord_txt, f"https://www.google.com/maps?q={crime.crime_lat},{crime.crime_lng}"))
    pdf.ln(3)


# ---------------------------------------------------------------------------
#  Target Subscriber Table
# ---------------------------------------------------------------------------

def _render_target_subscriber_table(pdf: ReportPdf, analysis: MultiReportAnalysis) -> None:
    pdf.section_heading("ٹارگٹس کی معلومات (Target Subscriber Info)")
    pdf.note("یہاں ہر ٹارگٹ نمبر کی بنیادی معلومات اور پیٹرن بتائے گئے ہیں۔")

    rows = []
    sub_map = {info.identifier: info for info in analysis.target_subscriber_info}
    for idx, summary in enumerate(analysis.target_summaries, start=1):
        sub = sub_map.get(summary.identifier)
        rows.append([
            str(idx),
            summary.identifier,
            sub.msisdn if sub else "-",
            sub.name or summary.target_name or "-" if sub else summary.target_name or "-",
            sub.cnic or "-" if sub else "-",
            summary.operator or (sub.operator if sub else "-"),
            str(summary.total_calls),
            str(summary.unique_contacts),
        ])

    pdf.table(
        ["#", "Target Label", "MSISDN", "Name", "CNIC", "Operator", "Calls/SMS", "Contacts"],
        rows,
        col_widths=[8, 25, 25, 30, 28, 22, 18, 18],
    )
    pdf.ln(3)


# ---------------------------------------------------------------------------
#   Raabton ka Naqsha
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
#  Direct Interactions
# ---------------------------------------------------------------------------

def _render_interactions(pdf: ReportPdf, analysis: MultiReportAnalysis) -> None:
    if pdf.get_y() > 100:  # The heatmap image is massive, it requires a full page
        pdf.add_page()
    pdf.section_heading("آپس کے رابطے (Direct Interactions / Matrix)")
    pdf.note("یہ سیکشن بتاتا ہے کہ کن ٹارگٹس نے آپس میں کتنی بار رابطہ کیا۔")

    target_ids = [s.identifier for s in analysis.target_summaries]
    if analysis.direct_interactions:
        path = create_interaction_heatmap(analysis.direct_interactions, target_ids)
        if path:
            pdf._chart_files.append(path)
            pdf.image(path, x=15, w=180)
            pdf.ln(5)

    if not analysis.direct_interactions:
        pdf.note("ٹارگٹس کے درمیان آپس میں کوئی رابطہ نہیں ہوا۔")
    else:
        pdf.sub_heading("Direct Interaction Summary")
        rows = []
        for ix in analysis.direct_interactions:
            rows.append([ix.source_identifier, ix.target_identifier, str(ix.interaction_count)])
        pdf.table(["Target 1", "Target 2", "Total Calls/SMS"], rows, col_widths=[60, 60, 30])
    pdf.ln(3)


# ---------------------------------------------------------------------------
#  Common Contacts (All Days)
# ---------------------------------------------------------------------------

def _render_common_contacts(pdf: ReportPdf, analysis: MultiReportAnalysis) -> None:
    pdf.section_heading("مشترکہ رابطے (Common Contacts / Facilitators)")
    pdf.note("یہاں ان نمبرز کی فہرست ہے جن سے ایک سے زیادہ ٹارگٹس نے رابطہ کیا۔")
    if not analysis.common_contacts:
        pdf.note("کوئی مشترکہ تھرڈ پارٹی کانٹیکٹ نہیں ملا۔")
        return

    rows = []
    for cc in analysis.common_contacts[:20]:  # Top 20 for PDF
        t_ids = ", ".join(cc.target_identifiers)
        rows.append([cc.contact_number, str(len(cc.target_identifiers)), str(cc.total_interactions), t_ids])

    pdf.table(["Common Number", "Targets Talked", "Total Interactions", "Linked Targets"], rows, col_widths=[40, 26, 30, 94])
    if len(analysis.common_contacts) > 20:
        pdf.note(f"...اور {len(analysis.common_contacts) - 20} مشترکہ کانٹیکٹس ایکسل فائل میں ہیں۔")
    pdf.ln(3)


# ---------------------------------------------------------------------------
#  Crime Day Common Contacts
# ---------------------------------------------------------------------------

def _render_crime_day_common(pdf: ReportPdf, analysis: MultiReportAnalysis) -> None:
    crime_day_contacts = [c for c in analysis.crime_day_common_contacts if c.day_label == "crime_day"]
    if not crime_day_contacts:
        return

    pdf.section_heading("واردات والے دن کے مشترکہ رابطے (Crime Day Common Contacts)")
    pdf.note("واردات والے دن ٹارگٹس نے کن مخصوص نمبرز سے رابطہ کیا۔")

    rows = []
    for cc in crime_day_contacts[:20]:
        rows.append([
            cc.contact_number,
            str(len(cc.target_identifiers)),
            str(cc.total_interactions),
            ", ".join(cc.target_identifiers),
        ])

    pdf.table(["Common Number", "Targets", "Interactions", "Linked Targets"], rows, col_widths=[40, 22, 28, 100])
    pdf.ln(3)


# ---------------------------------------------------------------------------
#  Adjacent Day Common Contacts
# ---------------------------------------------------------------------------

def _render_adjacent_day_common(pdf: ReportPdf, analysis: MultiReportAnalysis) -> None:
    adjacent = [c for c in analysis.crime_day_common_contacts if c.day_label in ("day_before", "day_after")]
    if not adjacent:
        return

    pdf.section_heading("واردات سے ایک دن پہلے / بعد کے مشترکہ رابطے")
    pdf.note("واردات سے ایک دن پہلے اور ایک دن بعد کے مشترکہ رابطے۔")

    day_labels = {"day_before": "Day Before", "day_after": "Day After"}
    rows = []
    for cc in adjacent[:15]:
        rows.append([
            cc.contact_number,
            day_labels.get(cc.day_label, cc.day_label),
            str(len(cc.target_identifiers)),
            str(cc.total_interactions),
            ", ".join(cc.target_identifiers),
        ])

    pdf.table(["Number", "Day", "Targets", "Count", "Linked Targets"], rows, col_widths=[35, 22, 18, 18, 97])
    pdf.ln(3)



# ---------------------------------------------------------------------------
#  Timeline
# ---------------------------------------------------------------------------

def _render_timeline(pdf: ReportPdf, analysis: MultiReportAnalysis) -> None:
    pdf.section_heading("وقت کے مطابق سرگرمی (Event Timeline)")
    pdf.note("واردات والے دن ٹارگٹس کی سرگرمیوں کی ٹائم لائن۔")

    target_ids = [s.identifier for s in analysis.target_summaries]
    if analysis.timeline_events:
        path = create_event_timeline(analysis.timeline_events, target_ids)
        if path:
            pdf._chart_files.append(path)
            if pdf.get_y() > 140:
                pdf.add_page()
            pdf.image(path, x=15, w=180)
            pdf.ln(5)

    if not analysis.timeline_events:
        pdf.note("واردات کی تاریخ پر آپس کی کوئی سرگرمی یا ہم مقامی (co-location) نہیں ملی۔")
    else:
        pdf.sub_heading("Timeline Event Log")
        rows = []
        for ev in analysis.timeline_events[:15]:
            rows.append([ev.timestamp, ev.event_type, ev.target_1, ev.description])
        pdf.table(["Timestamp", "Type", "Source", "Event Details"], rows, col_widths=[40, 30, 40, 80])
        if len(analysis.timeline_events) > 15:
            pdf.note(f"...اور {len(analysis.timeline_events) - 15} ایونٹس ایکسل فائل میں ہیں۔")
    pdf.ln(3)


# ---------------------------------------------------------------------------
#  Crime-Proximity Tower Analysis
# ---------------------------------------------------------------------------

_BUCKET_LABELS = {"before": "Before Crime", "during": "At Crime Time", "after": "After Crime"}
_STRENGTH_COLORS = {
    "Very Strong": (180, 0, 0),
    "Strong":      (200, 80, 0),
    "Moderate":    (180, 140, 0),
    "Weak":        (100, 100, 100),
}


def _render_crime_proximity(pdf: ReportPdf, analysis: MultiReportAnalysis) -> None:
    ctx = analysis.crime_context
    events = analysis.crime_proximity_events

    pdf.section_heading("جائے وقوعہ کے قریب ٹاور (Crime Scene Proximity)")
    pdf.note(
        "ضروری نوٹ: یہ ٹاور کی لوکیشن ہے، ملزم کی عین لوکیشن نہیں۔ "
        "ملزم اس ٹاور کے کوریج ایریا (200m–2km) میں کہیں بھی ہو سکتا تھا۔"
    )
    pdf.note(
        f"Crime Location: {ctx.crime_place or 'N/A'}  |  "
        f"Coordinates: {ctx.crime_lat}, {ctx.crime_lng}  |  "
        f"Crime Time: {ctx.crime_date} {ctx.crime_time}"
    )

    if not events:
        pdf.note("کسی بھی ملزم کا ٹاور جائے وقوعہ کے قریب نہیں ملا۔")
        return

    for bucket_key in ("before", "during", "after"):
        bucket_events = [e for e in events if e.time_bucket == bucket_key]
        if not bucket_events:
            continue

        label = _BUCKET_LABELS[bucket_key]
        pdf.sub_heading(f"{label} ({len(bucket_events)} records)")

        rows = []
        for e in bucket_events[:20]:
            mins = e.minutes_from_crime
            offset_txt = (
                f"{abs(mins)} min pehle" if mins < 0
                else f"{mins} min baad" if mins > 0
                else "Crime waqt"
            )
            dist_txt = f"{e.tower_distance_meters:.0f}m — {e.proximity_strength}"
            call_txt = e.call_type or "-"
            if e.tower_latitude is not None and e.tower_longitude is not None:
                coord_text = f"{e.tower_latitude:.5f}, {e.tower_longitude:.5f}"
                tower_coords = (coord_text, f"https://www.google.com/maps?q={e.tower_latitude},{e.tower_longitude}")
            else:
                tower_coords = "-"
            rows.append([
                e.timestamp[11:16],    # HH:MM
                offset_txt,
                e.target_identifier,
                dist_txt,
                e.tower_location,
                tower_coords,
                call_txt,
            ])

        pdf.table(
            ["Time", "Offset", "Target", "Tower Dist. (Strength)", "Tower Location", "Coordinates", "Call Type"],
            rows,
        )
        if len(bucket_events) > 20:
            pdf.note(f"...اور {len(bucket_events) - 20} ریکارڈز ایکسل فائل میں ہیں۔")
        pdf.ln(2)

    pdf.ln(1)


# ---------------------------------------------------------------------------
#  IMEI Cross-Match
# ---------------------------------------------------------------------------

def _render_imei_cross(pdf: ReportPdf, analysis: MultiReportAnalysis) -> None:
    if not analysis.imei_cross_matches:
        return

    pdf.section_heading("IMEI کراس میچ (مشترکہ ڈیوائس)")
    pdf.note("یہ سیکشن بتاتا ہے کہ ایک ہی موبائل فون (IMEI) کو کن ٹارگٹس نے مل کر استعمال کیا۔")

    rows = []
    for match in analysis.imei_cross_matches:
        rows.append([
            match.imei,
            ", ".join(match.target_identifiers),
            " | ".join(match.usage_details),
        ])

    pdf.table(["IMEI", "Targets", "Usage Period"], rows, col_widths=[40, 60, 90])
    pdf.ln(3)


# ---------------------------------------------------------------------------
#  Individual Target Summaries
# ---------------------------------------------------------------------------

def _render_target_summaries(pdf: ReportPdf, analysis: MultiReportAnalysis) -> None:
    pdf.section_heading("Individual Target Summaries")
    pdf.note("ہر ٹارگٹ نمبر کا الگ الگ تفصیلی تجزیہ۔")
    if not analysis.target_summaries:
        return

    sub_map = {info.identifier: info for info in analysis.target_subscriber_info}

    for idx, summary in enumerate(analysis.target_summaries, start=1):
        # Each suspect starts on a fresh page for clear separation
        if idx > 1:
            pdf.add_page()

        sub = sub_map.get(summary.identifier)
        title = summary.identifier
        if sub and sub.name:
            title = f"{summary.identifier} — {sub.name}"

        # Colored banner per suspect for strong visual separation
        pdf.ln(1)
        banner_y = pdf.get_y()
        pdf.set_fill_color(41, 65, 122)
        pdf.rect(pdf.l_margin, banner_y, pdf.w - pdf.l_margin - pdf.r_margin, 9, style="F")
        pdf.set_xy(pdf.l_margin + 2, banner_y + 1.5)
        pdf.set_font(pdf.dfont, "B", 11)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(0, 6, pdf._safe_text(f"Target #{idx}: {title}"), ln=True)
        pdf.set_text_color(33, 37, 41)
        pdf.ln(4)

        # --- Basic stats ---
        pdf.key_value("Total Records", str(summary.total_calls))
        pdf.key_value("Unique Contacts", str(summary.unique_contacts))
        pdf.ln(2)

        # --- Top 5 Frequent Locations with clickable coordinates ---
        if summary.top_locations:
            pdf.sub_heading("Top 5 Frequent Locations")
            loc_rows = []
            for loc in summary.top_locations:
                if loc.coordinates_text and loc.latitude is not None and loc.longitude is not None:
                    coords = (loc.coordinates_text, f"https://www.google.com/maps?q={loc.latitude},{loc.longitude}")
                else:
                    coords = "-"
                loc_rows.append([str(loc.order), loc.location, coords, str(loc.visits)])
            pdf.table(["#", "Location", "Coordinates", "Visits"], loc_rows)
            pdf.ln(2)

        # --- IMEI Devices (same layout as single-CDR, with TAC legend) ---
        if summary.imei_records:
            pdf.sub_heading("ڈیوائس (IMEI) کی تفصیل")
            pdf.note(
                "نوٹ: ڈیوائس کی پہچان IMEI کے پہلے 8 ہندسوں (TAC ID) سے کی گئی ہے، "
                "جو بنانے والی کمپنی اور ماڈل ظاہر کرتے ہیں۔ باقی ہندسے ہر ڈیوائس کا منفرد نمبر ہوتے ہیں۔"
            )
            imei_rows = [
                [item.identifier, str(item.records), item.first_seen or "?", item.last_seen or "?", item.brand or "-", item.specs or "-"]
                for item in summary.imei_records
            ]
            pdf.table(["IMEI", "Records", "پہلی بار", "آخری بار", "Brand", "Specs"], imei_rows)
            pdf.ln(2)

        # --- IMSI ---
        if summary.imsi_records:
            pdf.sub_heading("سم (IMSI) کی تفصیل")
            imsi_rows = [
                [item.identifier, str(item.records), item.first_seen or "?", item.last_seen or "?"]
                for item in summary.imsi_records
            ]
            pdf.table(["IMSI", "Records", "پہلی بار", "آخری بار"], imsi_rows)
            pdf.ln(2)
