"""High-level PDF renderer for report analysis."""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from cdr_report_app.domain.analysis_models import ReportAnalysis
from cdr_report_app.domain.provider_models import AttachmentArtifact
from cdr_report_app.integrations.helpers import extract_scalar_details
from cdr_report_app.rendering.charts import create_daily_activity_chart, create_heatmap_chart, create_movement_map
from cdr_report_app.rendering.pdf.builder import CLR_TEXT, ReportPdf
from cdr_report_app.rendering.theme import CLR_ACCENT

logger = logging.getLogger(__name__)


def render_report_pdf(
    analysis: ReportAnalysis,
    output_path: Path | None = None,
    *,
    return_bytes: bool = True,
) -> bytes | None:
    logger.info(
        "PDF rendering started | output=%s top_locations=%s long_stays=%s movement_steps=%s top_contacts=%s attachments=%s",
        output_path,
        len(analysis.top_locations),
        len(analysis.long_stays),
        len(analysis.movement_steps),
        len(analysis.top_contacts),
        len(analysis.attachments),
    )
    pdf = ReportPdf()
    pdf.add_page()
    pdf_insertions: list[dict[str, object]] = []

    def safe_call(func, *args, **kwargs):
        try:
            func(*args, **kwargs)
        except Exception as exc:
            logger.error(f"Error rendering {func.__name__}: {exc}", exc_info=True)
            try:
                pdf.ln(5)
                pdf.note(f"(Section Error: {func.__name__}) اس حصے کو رینڈر کرنے میں مسئلہ پیش آیا ہے۔", color=(200, 0, 0))
            except Exception:
                pass

    safe_call(_render_title, pdf)
    safe_call(_render_urdu_summary, pdf, analysis)
    safe_call(_render_cdr_info, pdf, analysis)
    safe_call(_render_crime_info, pdf, analysis)
    safe_call(_render_quick_stats, pdf, analysis)
    if analysis.crime_context.crime_date:
        safe_call(_render_daily_activity, pdf, analysis)
    safe_call(_render_top_locations, pdf, analysis)
    safe_call(_render_long_stays, pdf, analysis)
    safe_call(_render_heatmap, pdf, analysis)
    safe_call(_render_movement, pdf, analysis)
    safe_call(_render_bursts, pdf, analysis)
    safe_call(_render_short_codes, pdf, analysis)
    safe_call(_render_devices, pdf, analysis)
    safe_call(_render_top_contacts, pdf, analysis)
    safe_call(_render_db_verification, pdf, analysis, pdf_insertions)
    safe_call(_render_top_contacts_history, pdf, analysis, pdf_insertions)
    safe_call(_render_text_attachments, pdf, analysis, placement_key=None)

    try:
        content = bytes(pdf.output())
        content = _splice_pdf_attachments(content, pdf_insertions)
    except Exception as exc:
        logger.error(f"Error finalizing PDF output: {exc}", exc_info=True)
        # Fallback to base PDF if splicing fails
        content = bytes(pdf.output())

    if output_path is not None:
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(content)
        except Exception as exc:
            logger.error(f"Error saving PDF to disk: {exc}", exc_info=True)

    pdf.cleanup()
    pdf_insertions.clear()
    del pdf, pdf_insertions
    logger.info("PDF rendering completed | output=%s bytes=%s return_bytes=%s", output_path, len(content), return_bytes)
    return content if return_bytes else None


def _reshape_urdu(text: str) -> str:
    """Reshape + apply BiDi algorithm so FPDF renders Urdu correctly."""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except ImportError:
        return text  # Graceful fallback if libs missing

def _render_title(pdf: ReportPdf) -> None:
    pdf.set_fill_color(41, 65, 122)
    pdf.rect(10, 10, 190, 22, style="F")
    pdf.set_xy(10, 12)
    pdf.set_font(pdf.dfont, "B", 18)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(190, 9, "Single CDR Analysis Report", align="C", ln=True)
    pdf.set_text_font("مکمل تجزیاتی رپورٹ", "", 9)
    pdf.set_text_color(220, 220, 240)
    pdf.cell(190, 5, pdf._safe_text("مکمل تجزیاتی رپورٹ"), align="C", ln=True)
    pdf.set_text_color(33, 37, 41)
    pdf.ln(6)


def _render_urdu_summary(pdf: ReportPdf, analysis: ReportAnalysis) -> None:
    """Single cohesive Urdu summary using proper RTL Arabic font with bullet points."""
    pdf.section_heading("تحقیقاتی خلاصہ (Executive Summary)")

    name = analysis.metadata.name
    msisdn = analysis.metadata.msisdn or "نامعلوم"
    total_calls = analysis.quick_stats.total_records
    days = max(1, analysis.quick_stats.duration_days)

    def print_urdu_bullets(parts: list[str]) -> None:
        for part in parts:
            pdf.bullet_item(part)
            
        pdf.ln(2)

    # Target Sub-section
    pdf.sub_heading("ٹارگٹ کی معلومات", align="C", size=11)
    
    target_parts = []
    if name:
        target_parts.append(f"ٹارگٹ نمبر {msisdn} بنام {name} کے کال ڈیٹا ریکارڈ کا تجزیہ کیا گیا ہے ۔")
    else:
        target_parts.append(f"ٹارگٹ نمبر {msisdn} کے کال ڈیٹا ریکارڈ کا تجزیہ کیا گیا ہے ۔")

    target_parts.append(f"یہ ریکارڈ {days} دنوں پر محیط ہے اور اس میں کل {total_calls} کالز شامل ہیں ۔")

    if analysis.top_contacts:
        tc = analysis.top_contacts[0]
        target_parts.append(f"سب سے زیادہ رابطہ نمبر {tc.number} سے ہوا جو کہ کل {tc.total_calls} مرتبہ ملا ۔")

    if analysis.top_locations:
        loc = analysis.top_locations[0]
        clean_loc = loc.location.replace("(", "").replace(")", "").strip()
        if clean_loc:
            target_parts.append(f"ٹارگٹ کی سب سے زیادہ موجودگی {clean_loc} کے قریب پائی گئی ہے ۔")

    db_hits = [row for row in analysis.db_verification_rows if row.database != "CALLER_ID" and any(k in row.status.lower() for k in ("hit", "found", "matched"))]
    if db_hits:
        for row in db_hits:
            hit_desc = _summarize_db_hit_urdu(row.database, row.summary)
            target_parts.append(f"{hit_desc} ۔")
    else:
        target_parts.append("ڈیٹا بیس تصدیق میں ٹارگٹ کا کوئی سابقہ مجرمانہ ریکارڈ نہیں ملا ۔")

    print_urdu_bullets(target_parts)

    # Top Contacts Sub-section
    pdf.sub_heading("ٹاپ کانٹیکٹس کی معلومات", align="C", size=11)
    
    top_contacts_parts = []
    for ch in analysis.top_contact_histories:
        hits = [row for row in ch.db_rows if any(k in row.status.lower() for k in ("hit", "found", "matched"))]
        if hits:
            db_descs = []
            for row in hits:
                db_descs.append(_summarize_db_hit_urdu(row.database, row.summary))
            
            joined_descs = " ، ".join(db_descs)
            top_contacts_parts.append(f"رابطہ نمبر {ch.number} کے {joined_descs} میں ریکارڈ ملے ۔")

    if not top_contacts_parts:
        top_contacts_parts.append("کسی بھی ٹاپ کانٹیکٹ کا کوئی سابقہ مجرمانہ ریکارڈ نہیں ملا ۔")

    print_urdu_bullets(top_contacts_parts)


def _summarize_db_hit_urdu(database: str, summary: str | None) -> str:
    text = str(summary or "").strip()
    if not text:
        return "ریکارڈ ملا"

    upper_db = str(database or "").upper().strip()

    if upper_db == "PSRMS":
        text = text.replace("FIR record(s) found in PSRMS", "مقدمات ملے")
        text = text.replace("as suspect", "بحیثیت ملزم")
        text = text.replace("as witness", "بحیثیت گواہ")
        text = text.replace("as complainant", "بحیثیت مدعی")
        return text

    if upper_db == "SUBSCRIBER":
        if "Telecom record found" in text:
            return "ٹیلی کام ریکارڈ ملا"
        return "ٹیلی کام ریکارڈ ملا"

    if upper_db == "HOTEL_EYE":
        if "found" in text.lower():
            return "ہوٹل آئی ریکارڈ ملا"
        return "ہوٹل آئی میں اندراج ملا"

    if upper_db == "DLS":
        if "driving license" in text.lower():
            return "ڈرائیونگ لائسنس ریکارڈ ملا"
        return "ڈرائیونگ لائسنس کا ریکارڈ ملا"

    if upper_db == "HRMIS":
        if "Officer record found" in text:
            return "ملازمت کا ریکارڈ ملا"
        return "ملازمت کا ریکارڈ ملا"

    if upper_db == "OLD_TENANT":
        mention = ""
        if "using Subscriber DB CNIC" in text:
            mention = " (ٹیلی کام ڈیٹا بیس سے حاصل کردہ شناختی کارڈ کی مدد سے)"
        
        tenants = ""
        if "tenants" in text:
            match = re.search(r'\((\d+) tenants\)', text)
            if match:
                tenants = f" ({match.group(1)} کرایہ دار)"
        
        return f"اولڈ ٹیننٹ ریکارڈ ملا{mention}{tenants}"

    if upper_db == "EVS":
        return "ای وی ایس میں ملازمت کا ریکارڈ ملا"

    if upper_db in {"HOPE", "HOPE EMPLOYEE"}:
        return "ہوپ میں ملازمت کا ریکارڈ ملا"

    if upper_db == "CRO":
        if "FIR" in text.upper():
            return text.replace("registered", "درج").replace("FIR(s)", "مقدمات")
        return "سابقہ ریکارڈ ملا"

    if upper_db == "WATCHLIST":
        mention = ""
        if "using Subscriber DB CNIC" in text:
            mention = " (ٹیلی کام ڈیٹا بیس سے حاصل کردہ شناختی کارڈ کی مدد سے)"
        
        count_match = re.search(r'\((\d+)\s+records\)', text)
        if count_match:
            count_val = count_match.group(1)
            return f"واچ لسٹ میں {count_val} ریکارڈز ملے{mention}"
            
        return f"واچ لسٹ میں اندراج ملا{mention}"

    if upper_db == "SBVS":
        mention = ""
        if "using Subscriber DB CNIC" in text:
            mention = " (ٹیلی کام ڈیٹا بیس سے حاصل کردہ شناختی کارڈ کی مدد سے)"
        elif "using Phone" in text:
            mention = " (فون نمبر کی مدد سے)"
            
        count_match = re.search(r'\((\d+)\s+records\)', text)
        if count_match:
            count_val = count_match.group(1)
            return f"SBVS میں {count_val} ریکارڈز ملے{mention}"
            
        return f"SBVS میں اندراج ملا{mention}"

    if upper_db == "PRVS":
        return "PRVS میں ریکارڈ ملا"

    if upper_db == "TRACS":
        return "ٹریفک چالان کا ریکارڈ ملا"

    return text

def _render_cdr_info(pdf: ReportPdf, analysis: ReportAnalysis) -> None:
    pdf.section_heading("ٹارگٹ کی سی ڈی آر معلومات", align='C')
    pdf.key_value("آپریٹر", (analysis.source_operator or "نامعلوم").upper())
    pdf.key_value("نام", analysis.metadata.name or "نامعلوم", bold_value=True)
    pdf.key_value("شناختی کارڈ نمبر", analysis.metadata.cnic or "نامعلوم")
    pdf.key_value("موبائل نمبر (MSISDN)", analysis.metadata.msisdn or "نامعلوم")
    pdf.ln(3)


def _render_crime_info(pdf: ReportPdf, analysis: ReportAnalysis) -> None:
    crime = analysis.crime_context
    has_crime_info = any([crime.fir_no, crime.police_station, crime.sections_of_law, crime.crime_date, crime.crime_time, crime.crime_place, crime.crime_lat and crime.crime_lng])
    if not has_crime_info:
        return
    pdf.section_heading("جرم کی معلومات")
    if crime.fir_no:
        pdf.key_value("ایف آئی آر نمبر", crime.fir_no)
    if crime.police_station:
        pdf.key_value("تھانہ", crime.police_station)
    if crime.sections_of_law:
        pdf.key_value("دفعاتِ قانون", crime.sections_of_law)
    if crime.crime_date:
        pdf.key_value("تاریخِ وقوعہ", crime.crime_date)
    if crime.crime_time:
        pdf.key_value("وقتِ وقوعہ", crime.crime_time)
    if crime.crime_place:
        pdf.key_value("جائے وقوعہ", crime.crime_place)
    if crime.crime_lat and crime.crime_lng:
        try:
            coord_txt = f"{float(crime.crime_lat):.5f}, {float(crime.crime_lng):.5f}"
            pdf.key_value("کوآرڈینیٹس", (coord_txt, f"https://www.google.com/maps?q={crime.crime_lat},{crime.crime_lng}"))
        except Exception:
            pass
    pdf.ln(3)


def _render_quick_stats(pdf: ReportPdf, analysis: ReportAnalysis) -> None:
    # Separator line before stat boxes
    pdf.set_draw_color(180, 180, 180)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(4)
    stats = analysis.quick_stats
    box_y = pdf.get_y()
    stat_items = [
        ("Total Records", str(stats.total_records), None, "کل ریکارڈز"),
        ("Repeated Contacts", str(stats.repeated_contacts), None, "بار بار رابطے"),
        ("Unique Numbers", str(stats.unique_numbers), None, "مختلف نمبرز"),
        ("Duration", stats.duration_label, f"({stats.duration_days} دن)" if stats.duration_days else None, "مدت"),
    ]
    box_count = len(stat_items)
    box_h = 22
    usable = pdf.w - pdf.l_margin - pdf.r_margin
    gap = 6
    box_w = max(34, (usable - (gap * (box_count + 1))) / box_count)
    start_x = pdf.l_margin + gap
    for index, (label_en, value, sub_value, label_ur) in enumerate(stat_items):
        x = start_x + index * (box_w + gap)
        pdf.stat_box(label_en, value, x, box_y, box_w, box_h, sub_value=sub_value, label_ur=label_ur)
    pdf.set_y(box_y + box_h + 6)


def _render_daily_activity(pdf: ReportPdf, analysis: ReportAnalysis) -> None:
    pdf.section_heading("روزانہ کی سرگرمی", min_height=100)
    path = create_daily_activity_chart(analysis.daily_activity)
    if path:
        pdf._chart_files.append(path)
        pdf.image(path, x=10, w=190)
        pdf.ln(3)
    if analysis.daily_activity.busiest_hour is not None and analysis.daily_activity.busiest_hour_count is not None:
        pdf.note(f"سب سے مصروف وقت: {analysis.daily_activity.busiest_hour:02d}:00 ({analysis.daily_activity.busiest_hour_count} کالز)")


def _render_top_locations(pdf: ReportPdf, analysis: ReportAnalysis) -> None:
    if not analysis.top_locations:
        return
    pdf.section_heading("سب سے زیادہ کالز کرنے کی جگہیں")
    rows = []
    for item in analysis.top_locations:
        ps_value = _nearest_ps_link(item.nearest_police_station, item.nearest_police_station_latitude, item.nearest_police_station_longitude)
        coords = (item.coordinates_text, f"https://www.google.com/maps?q={item.latitude},{item.longitude}") if item.coordinates_text and item.latitude is not None and item.longitude is not None else "-"
        rows.append([str(item.order), item.location, coords, ps_value, str(item.visits), item.timing_window or "N/A", f"{item.duration_pct:.1f}%" if item.duration_pct is not None else "-"])
    pdf.table(["S.No", "Location", "Coordinates", "Jurisdiction of PS", "Visits", "Timing", "Duration %"], rows, col_widths=[10, 47, 35, 30, 15, 23, 20])
    pdf.ln(3)


def _render_long_stays(pdf: ReportPdf, analysis: ReportAnalysis) -> None:
    pdf.section_heading("سب سے لمبے قیام کی جگہیں")
    if not analysis.long_stays:
        pdf.note("کوئی قابلِ ذکر قیام نہیں ملا۔")
        return
    rows = []
    for item in analysis.long_stays:
        ps_value = _nearest_ps_link(item.nearest_police_station, item.nearest_police_station_latitude, item.nearest_police_station_longitude)
        coords = (item.coordinates_text, f"https://www.google.com/maps?q={item.latitude},{item.longitude}") if item.coordinates_text and item.latitude is not None and item.longitude is not None else "-"
        rows.append([item.time_period, item.location, coords, ps_value, f"{item.duration_hours:.1f} hrs"])
    pdf.table(["Time Period", "Location", "Coordinates", "Jurisdiction of PS", "Duration"], rows, col_widths=[35, 60, 35, 40, 20])
    pdf.ln(3)


def _render_heatmap(pdf: ReportPdf, analysis: ReportAnalysis) -> None:
    pdf.section_heading("موبائل فون استعمال کرنے کا معمول", min_height=110)
    path = create_heatmap_chart(analysis.heatmap)
    if path:
        pdf._chart_files.append(path)
        pdf.image(path, x=15, w=180)
        pdf.ln(5)
    else:
        pdf.note("گھنٹہ وار سرگرمی کا ڈیٹا دستیاب نہیں۔")


def _render_movement(pdf: ReportPdf, analysis: ReportAnalysis) -> None:
    if not analysis.movement_steps:
        return
    pdf.section_heading("جرم کے دن کی نقل و حرکت (ترتیب وار)", min_height=100)
    rows = []
    dir_coords = []
    for item in analysis.movement_steps:
        ps_value = _nearest_ps_link(item.nearest_police_station, item.nearest_police_station_latitude, item.nearest_police_station_longitude)
        coords = "-"
        if item.coordinates_text and item.latitude is not None and item.longitude is not None:
             coords = (item.coordinates_text, f"https://www.google.com/maps?q={item.latitude},{item.longitude}")
             dir_coords.append(f"{item.latitude},{item.longitude}")
        rows.append([str(item.order), item.location, ps_value, coords, str(item.records), item.time_window or "-"])
    pdf.table(["Order", "Location", "Jurisdiction of PS", "Coordinates", "Records", "Time window"], rows, col_widths=[12, 50, 32, 35, 15, 41])
    pdf.ln(3)
    
    maps_link_added = False
    if dir_coords:
        # Google Maps URL can be quite long, limit to max 40 waypoints to be safe with URL limits
        maps_url = "https://www.google.com/maps/dir/" + "/".join(dir_coords[:40])
        maps_link_added = True

    path = create_movement_map(analysis.movement_steps, analysis.crime_context.crime_lat, analysis.crime_context.crime_lng)
    if path:
        if pdf.get_y() > 120:
            pdf.add_page()
        pdf.sub_heading("نقشہ")
        pdf.note("نیچے دیے گئے نقشے میں ملزم کا سفر اور جائے وقوعہ دکھائی گئی ہے۔", italic=False)
        pdf._chart_files.append(path)
        pdf.image(path, x=15, w=180)
        pdf.ln(5)
        
        if maps_link_added:
            pdf.set_text_font("مکمل نقشہ گوگل میپس پر دیکھیں", "", 10)
            pdf.set_text_color(0, 0, 255)
            pdf.cell(0, 5, pdf._safe_text("مکمل نقشہ گوگل میپس پر دیکھیں"), link=maps_url, ln=True, align="C")
            pdf.set_text_color(*CLR_TEXT)
            pdf.ln(5)


def _render_bursts(pdf: ReportPdf, analysis: ReportAnalysis) -> None:
    pdf.section_heading("برسٹ ایکٹیویٹی — کم وقت میں بہت زیادہ کالز")
    if not analysis.bursts:
        pdf.note("کوئی غیر معمولی برسٹ پیٹرن نہیں ملا۔")
        return
    burst_line = f"{len(analysis.bursts)} برسٹ کی نشاندہی ہوئی۔"
    pdf.set_text_font(burst_line, "B", 8)
    pdf.cell(0, 5, pdf._safe_text(burst_line), ln=True, align="R")
    pdf.ln(1)
    rows = [[item.burst_start, item.burst_end, str(item.call_count), ", ".join(item.top_numbers) if item.top_numbers else "-"] for item in analysis.bursts]
    pdf.table(["Burst Start", "Burst End", "Calls", "Top Numbers involved"], rows, col_widths=[50, 40, 25, 65])
    pdf.ln(3)


def _render_short_codes(pdf: ReportPdf, analysis: ReportAnalysis) -> None:
    pdf.section_heading("شارٹ کوڈز اور سروس نمبرز")
    if not analysis.short_codes:
        pdf.note("کوئی شارٹ کوڈ استعمال نہیں ہوا۔")
        return
    rows = [[item.code, str(item.count), item.description] for item in analysis.short_codes]
    pdf.table(["Short Code", "Usage Count", "Details"], rows, col_widths=[40, 30, 120])
    pdf.ln(3)


def _render_devices(pdf: ReportPdf, analysis: ReportAnalysis) -> None:
    if not analysis.imei_records and not analysis.imsi_records:
        return
    pdf.section_heading("ڈیوائس اور سم کی معلومات (IMEI / IMSI)")
    if analysis.imei_records:
        pdf.sub_heading("ڈیوائس (IMEI) کی تفصیل")
        pdf.note(
            "نوٹ: ڈیوائس کی پہچان IMEI کے پہلے 8 ہندسوں (TAC ID) سے کی گئی ہے، "
            "جو بنانے والی کمپنی اور ماڈل ظاہر کرتے ہیں۔ باقی ہندسے ہر ڈیوائس کا منفرد نمبر ہوتے ہیں۔"
        )
        rows = [
            [item.identifier, str(item.records), item.first_seen or "?", item.last_seen or "?", item.brand or "-", item.specs or "-"]
            for item in analysis.imei_records
        ]
        pdf.table(["IMEI", "Records", "پہلی بار", "آخری بار", "Brand", "Specs"], rows)
        pdf.ln(3)
    if analysis.imsi_records:
        pdf.sub_heading("سم (IMSI) کی تفصیل")
        rows = [[item.identifier, str(item.records), item.first_seen or "?", item.last_seen or "?"] for item in analysis.imsi_records]
        pdf.table(["IMSI", "Records", "پہلی بار", "آخری بار"], rows, col_widths=[45, 25, 30, 30])
        pdf.ln(3)


def _render_top_contacts(pdf: ReportPdf, analysis: ReportAnalysis) -> None:
    pdf.section_heading("سب سے زیادہ رابطے والے نمبرز (Top Contacts)")
    if not analysis.top_contacts:
        pdf.note("کوئی ٹاپ کانٹیکٹ ڈیٹا نہیں ملا۔")
        return
    rows = []
    for item in analysis.top_contacts:
        og_time = f"{item.og_duration // 60}m {item.og_duration % 60}s"
        in_time = f"{item.in_duration // 60}m {item.in_duration % 60}s"
        rows.append([
            item.number,
            item.cnic or "-",
            str(item.og_calls),
            og_time,
            str(item.in_calls),
            in_time,
            str(item.sms_in),
            str(item.sms_out)
        ])
    pdf.table(
        ["Number", "CNIC", "OG calls", "OG time", "In calls", "In time", "SMS In", "SMS Out"], 
        rows, 
        col_widths=[40, 37, 16, 20, 16, 20, 13, 13]
    )
    pdf.ln(2)
    pdf.note("OG: آؤٹ گوئنگ کالز | In: اِن کمنگ کالز", italic=True)
    pdf.ln(3)


def _render_db_verification(pdf: ReportPdf, analysis: ReportAnalysis, pdf_insertions: list[dict[str, object]]) -> None:
    pdf.section_heading("اے پارٹی کی ڈیٹابیس تصدیق کے نتائج")
    if not analysis.db_verification_rows:
        pdf.note("ڈیٹابیس تصدیق کے نتائج ابھی منسلک نہیں کیے گئے۔")
        return
    rows = [[_verification_database_label(row.database), row.status, row.summary] for row in analysis.db_verification_rows]
    severity_rows = []
    for idx, row in enumerate(analysis.db_verification_rows):
        if row.severity == "alert":
            severity_rows.append((idx, (255, 200, 200)))
        elif row.severity == "safe":
            severity_rows.append((idx, (200, 255, 200)))
        else:
            severity_rows.append((idx, (240, 240, 240)))
    pdf.table(["Database", "Status", "Key Finding"], rows, col_widths=[40, 40, 110], severity_rows=severity_rows)
    pdf.ln(3)
    _render_database_hit_details(pdf, analysis.main_db_results, analysis=analysis, pdf_insertions=pdf_insertions, placement_prefix="main_db_verification")


def _render_top_contacts_history(pdf: ReportPdf, analysis: ReportAnalysis, pdf_insertions: list[dict[str, object]]) -> None:
    pdf.ln(4)
    pdf.section_heading("ٹاپ کانٹیکٹس کی ہسٹری", align='C', min_height=100)
    if not analysis.top_contact_histories:
        pdf.note("اس سیکشن کے لیے ابھی کوئی کانٹیکٹ ہسٹری دستیاب نہیں ہے۔", color=CLR_ACCENT)
        return

    for i, history in enumerate(analysis.top_contact_histories, 1):
        if i > 1:
            pdf.ln(8)
            pdf.set_draw_color(150, 150, 150)
            pdf.set_line_width(0.5)
            pdf.line(pdf.l_margin + 5, pdf.get_y(), pdf.w - pdf.r_margin - 5, pdf.get_y())
            pdf.set_line_width(0.2)
            pdf.ln(8)

        pdf.set_text_font(f"{i}) {history.number}", "B", 12)
        pdf.cell(0, 10, f"{i}) {history.number}", ln=True)
        pdf.ln(2)

        has_data = any(res.get("hit") for res in (history.raw_results or {}).values() if isinstance(res, dict))
        if has_data:
            pdf.section_heading("Detailed Records Found", align='C', min_height=10)
            _render_database_hit_details(
                pdf,
                history.raw_results,
                skip_heading=True,
                analysis=analysis,
                pdf_insertions=pdf_insertions,
                placement_prefix=f"contact_history:{history.number}",
            )
        else:
            pdf.note("اس کانٹیکٹ کا کسی ڈیٹابیس میں ریکارڈ نہیں ملا۔")

    pdf.note("* نوٹ: سبسکرائبر ڈیٹابیس 2020 تک اپ ڈیٹ ہے۔ شناختی کارڈ کی بنیاد پر ملنے والا مجرمانہ ریکارڈ سم کے اصل مالک کا ہو سکتا ہے، جو ممکن ہے موجودہ استعمال کنندہ نہ ہو۔", italic=True)


def _render_database_hit_details(
    pdf: ReportPdf,
    raw_results: dict[str, object] | None,
    skip_heading: bool = False,
    analysis: ReportAnalysis | None = None,
    pdf_insertions: list[dict[str, object]] | None = None,
    placement_prefix: str | None = None,
) -> None:
    if not isinstance(raw_results, dict):
        return

    preferred = [
        "caller_id",
        "simsdb",
        "subscriber",
        "watchlist",
        "prvs",
        "hrmis",
        "evs",
        "hope",
        "dls",
        "hotel_eye",
        "tracs",
        "old_tenant",
        "cro",
        "psrms",
    ]
    ordered = [name for name in preferred if isinstance(raw_results.get(name), dict) and raw_results.get(name, {}).get("hit")]
    ordered.extend(
        name
        for name, result in raw_results.items()
        if name not in ordered and isinstance(result, dict) and result.get("hit")
    )
    if not ordered:
        return

    if not skip_heading:
        pdf.sub_heading("Detailed Records Found")
    for index, provider_name in enumerate(ordered):
        if pdf.get_y() > 220:
            pdf.add_page()
        result = raw_results.get(provider_name, {})
        if not isinstance(result, dict):
            continue
        details = _provider_detail_pairs(provider_name, result)
        extra_lines = _provider_extra_lines(provider_name, result)
        table_specs = _provider_table_specs(provider_name, result)
        if not details and not extra_lines and not table_specs:
            continue
        pdf.sub_heading(f"{_provider_display_name(provider_name)} Details")
        align = "R" if provider_name in ["cro", "psrms"] else "L"
        for label, value in details:
            pdf.key_value(label, value, align=align)
        for title, headers, rows, col_widths in table_specs:
            if title:
                pdf.sub_heading(title)
            pdf.table(headers, rows, col_widths=col_widths, align=align)
            pdf.ln(1)
        if not table_specs:
            for line in extra_lines:
                pdf.note(line, italic=False, color=(33, 37, 41))
        if analysis is not None and placement_prefix:
            placement_key = f"{placement_prefix}:{provider_name}"
            _render_text_attachments(pdf, analysis, placement_key=placement_key)
            if pdf_insertions is not None:
                has_pdf_attachments = _queue_pdf_attachment_placeholders(pdf, analysis, placement_key, pdf_insertions)
                if has_pdf_attachments and provider_name in {"cro", "psrms"}:
                    pdf.add_page()
        pdf.ln(1)
        if index < len(ordered) - 1:
            pdf.set_draw_color(205, 213, 229)
            pdf.set_line_width(0.35)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.set_line_width(0.2)
            pdf.ln(4)


def _provider_display_name(provider_name: str) -> str:
    labels = {
        "caller_id": "Caller ID Lookup",
        "simsdb": "SIMs Database",
        "subscriber": "Telecom",
        "hotel_eye": "Hotel Eye",
        "prvs": "PRVS",
        "cro": "CRO",
        "psrms": "PSRMS",
        "watchlist": "Watchlist",
        "cfms": "CFMS",
        "sbvs": "SBVS",
        "pfc": "PFC",
        "hrmis": "HRMIS",
        "igp_cms": "IGP CMS",
        "evs": "EVS",
        "hope": "HOPE Employee",
        "dls": "DLS",
        "tracs": "TRACS",
        "old_tenant": "Old Tenant",
        "trust": "TRUST",
        "milap": "MILAP",
    }
    return labels.get(provider_name, provider_name.upper())


def _verification_database_label(name: str) -> str:
    labels = {
        "CALLER_ID": "Caller ID Lookup",
        "SIMSDB": "SIMs Database",
        "SUBSCRIBER": "Telecom",
        "HOTEL_EYE": "Hotel Eye",
        "HRMIS": "HRMIS",
        "EVS": "EVS",
        "HOPE": "Hope Employee",
        "OLD_TENANT": "Old Tenant",
        "WATCHLIST": "Watchlist",
        "SBVS": "SBVS",
    }
    return labels.get(str(name or "").upper().strip(), str(name or "").upper().strip())


def _provider_detail_pairs(provider_name: str, result: dict[str, object]) -> list[tuple[str, str]]:
    data = result.get("data", {})
    pairs: list[tuple[str, str]] = []

    def add(label: str, value: object) -> None:
        text = str(value or "").strip()
        if text and text not in {"-", "None"}:
            pairs.append((label, text[:220]))

    if provider_name == "caller_id" and isinstance(data, dict):
        add("Naam (Primary Name)", data.get("primary_name"))
        if data.get("spam"):
            add("Spam Status", "Reported as Spam")
        if data.get("facebook_link"):
            add("Facebook", data.get("facebook_link"))
        elif data.get("facebook_id"):
            add("Facebook ID", data.get("facebook_id"))
        return pairs

    # Skip summary for telecom as it usually duplicates the data
    if result.get("summary") and provider_name not in {"subscriber", "simsdb"}:
        add("Summary", result.get("summary"))

    if provider_name == "simsdb" and isinstance(data, dict):
        add("Name", data.get("name"))
        add("CNIC", data.get("cnic"))
        add("Mobile", data.get("mobile"))
        add("Address", data.get("address"))
        add("SIMs On This CNIC", data.get("sim_count"))
        return pairs

    if provider_name == "subscriber" and isinstance(data, dict):
        add("Name", data.get("name"))
        add("CNIC", data.get("cnic"))
        add("Mobile", data.get("mobile"))
        add("Activation Date", data.get("activation_date"))
        add("Address", data.get("address"))
        return pairs

    if provider_name == "prvs" and isinstance(data, dict):
        add("Name", data.get("name"))
        add("Police Station", data.get("police_station"))
        add("Record Reference", data.get("record_reference"))
        add("Remarks", data.get("remarks"))
        return pairs

    if provider_name == "cro" and isinstance(data, dict):
        add("CRO No.", data.get("cro_no"))
        add("Name", data.get("name"))
        add("Father Name", data.get("father_name"))
        add("Age", data.get("age"))
        add("Category", data.get("category"))
        add("District", data.get("district"))
        add("FIR Count", data.get("fir_count"))
        return pairs

    if provider_name == "psrms" and isinstance(data, dict):
        add("Record Count", data.get("record_count"))
        add("Person Name", data.get("person_name"))
        add("CNIC", data.get("person_cnic"))
        add("Phone", data.get("person_phone"))
        first_status = None
        firs = data.get("firs") or []
        if firs and isinstance(firs[0], dict):
            first_status = firs[0].get("fir_status")
        add("Latest FIR Status", first_status)
        return pairs

    if provider_name == "watchlist" and isinstance(data, dict):
        raw_data = result.get("raw", {})
        if isinstance(raw_data, dict) and isinstance(raw_data.get("data"), list) and raw_data.get("data"):
            first = raw_data["data"][0]
            if isinstance(first, dict):
                add("Name", first.get("name"))
                if first.get("phone") or first.get("mobile"):
                    add("Phone", first.get("phone") or first.get("mobile"))
                add("CNIC", first.get("cnic"))
                
                cnic_source = data.get("cnic_source")
                if cnic_source == "subscriber":
                    add("Search Method", "Using Telecom Provided CNIC")
        return pairs

    if provider_name == "tracs" and isinstance(data, dict):
        latest = _latest_tracs_challan(result)
        add("Owner Name", latest.get("ownerName") if isinstance(latest, dict) else None)
        add("CNIC", latest.get("ownerCNIC") if isinstance(latest, dict) else None)
        return pairs

    if provider_name == "hrmis" and isinstance(data, dict):
        add("Officer Name", data.get("officer_name"))
        add("Officer Phone", data.get("officer_phone"))
        add("Officer CNIC", data.get("officer_cnic"))
        if data.get("cnic_source") == "subscriber":
            add("Lookup CNIC Source", "Subscriber DB")
        add("Belt No.", data.get("officer_belt_no"))
        add("Date of Birth", data.get("date_of_birth"))
        add("Current Posting", data.get("current_posting"))
        add("Officer Address", data.get("officer_address"))
        add("Officer City", data.get("officer_city"))
        add("Rank", data.get("rank"))
        ps_name = data.get("police_station_name")
        district = data.get("district")
        if ps_name or district:
            add("PS / District", " / ".join(part for part in [str(ps_name or "").strip(), str(district or "").strip()] if part))
        return pairs

    if provider_name in {"evs", "hope"} and isinstance(data, dict):
        add("Name", data.get("name"))
        add("Father Name", data.get("father_name"))
        add("CNIC", data.get("cnic"))
        if data.get("cnic_source") == "subscriber":
            add("Lookup CNIC Source", "Subscriber DB")
        add("Contact", data.get("contact"))
        add("Other Contact", data.get("other_contact"))
        add("Permanent Address", data.get("permanent_address"))
        add("Designation", data.get("designation"))
        return pairs

    if provider_name == "dls" and isinstance(data, dict):
        add("First Name", data.get("firstname"))
        add("Last Name", data.get("lastname"))
        add("Phone", data.get("phone"))
        add("CNIC", data.get("cnic"))
        add("Address", data.get("address"))
        return pairs

    if provider_name == "hotel_eye":
        raw = result.get("raw", {})
        query = raw.get("query", {}) if isinstance(raw, dict) else {}
        records = raw.get("records", []) if isinstance(raw, dict) else []
        if isinstance(query, dict):
            phone = query.get("phone")
            cnic = query.get("cnic")
            cnic_source = query.get("cnic_source")
            if phone:
                add("Phone", phone)
            if cnic:
                add("CNIC (Subscriber DB)" if cnic_source == "subscriber" else "CNIC", cnic)
        add("Total Records", len(records) if isinstance(records, list) else 0)
        return pairs

    if provider_name == "old_tenant" and isinstance(data, dict):
        add("Owner Name", data.get("owner_name"))
        add("Owner Phone", data.get("owner_phone"))
        add("Owner CNIC", data.get("owner_cnic"))
        sb = data.get("search_branch")
        if sb == "by_cnic":
            add("Search Method", "CNIC Search")
        elif sb == "by_mobile":
            add("Search Method", "Mobile Search")
        return pairs

    if provider_name == "sbvs" and isinstance(data, dict):
        add("Name", data.get("name"))
        add("CNIC", data.get("cnic"))
        add("Phone", data.get("phone"))
        add("Passport", data.get("passport"))
        add("Address", data.get("address"))
        
        # Check source if injected
        src = data.get("lookup_source")
        if src == "subscriber":
            add("Lookup Source", "Telecom (Subscriber DB)")
        elif src == "phone":
            add("Lookup Source", "Phone Number")
        elif src == "input":
            add("Lookup Source", "Input CNIC")
            
        return pairs

    details = {}
    if isinstance(data, dict) and isinstance(data.get("details"), dict):
        details = {str(key): str(value) for key, value in data.get("details", {}).items() if str(value).strip()}
    if not details:
        details = extract_scalar_details(result.get("raw"), max_items=10)
    for key, value in details.items():
        add(key, value)
    return pairs[:10]


def _provider_extra_lines(provider_name: str, result: dict[str, object]) -> list[str]:
    data = result.get("data", {})
    lines: list[str] = []

    if provider_name == "cro" and isinstance(data, dict):
        for fir in (data.get("firs") or [])[:6]:
            if not isinstance(fir, dict):
                continue
            fir_label = f"{fir.get('fir_no', '-')}/{fir.get('fir_year', '-')}"
            station = fir.get("police_station") or "-"
            offence = fir.get("offence") or "-"
            status = fir.get("status") or "-"
            lines.append(f"CRO FIR: {fir_label} | PS: {station} | Offence: {offence} | Status: {status}")

    if provider_name == "psrms" and isinstance(data, dict):
        for fir in (data.get("firs") or [])[:6]:
            if not isinstance(fir, dict):
                continue
            fir_label = f"{fir.get('fir_no', '-')}/{fir.get('fir_year', '-')}"
            ps_id = fir.get("ps_id") or "-"
            status = fir.get("fir_status") or "-"
            
            ptype = str(fir.get("person_type") or "").upper().strip()
            role = "-"
            if ptype == "WIT":
                role = "Suspect"
            elif ptype == "SUS":
                role = "Witness"
            elif ptype == "FIR":
                role = "Complainant"
                
            lines.append(f"PSRMS FIR: {fir_label} | PS: {ps_id} | Status: {status} | Role: {role}")

    if provider_name == "tracs":
        payload = _tracs_payload(result)
        for item in payload[:6]:
            if not isinstance(item, dict):
                continue
            challan_no = item.get("challanNumber") or item.get("challan_no") or item.get("reference_no") or item.get("id") or "-"
            vehicle = item.get("vehicleNumPlate") or item.get("vehicle_no") or item.get("registration_no") or "-"
            challan_type = _tracs_challan_type(item) or "-"
            lines.append(f"TRACS Challan: {challan_no} | Vehicle: {vehicle} | Type: {challan_type}")

    return lines


def _provider_table_specs(provider_name: str, result: dict[str, object]) -> list[tuple[str | None, list[str], list[list[object]], list[float] | None]]:
    data = result.get("data", {})
    specs: list[tuple[str | None, list[str], list[list[object]], list[float] | None]] = []

    if provider_name == "caller_id" and isinstance(data, dict):
        account_rows = []
        for acc in (data.get("accounts") or [])[:15]:
            if not isinstance(acc, dict):
                continue
            account_rows.append(
                [
                    str(acc.get("name") or "-"),
                    "Yes" if acc.get("spam") else "-",
                    str(acc.get("type") or "-"),
                ]
            )
        if account_rows:
            specs.append(("Known Names / Accounts", ["Name", "Spam?", "Type"], account_rows, [110, 25, 35]))
        return specs

    if provider_name == "simsdb" and isinstance(data, dict):
        sim_rows = []
        for sim in (data.get("sims") or [])[:20]:
            if not isinstance(sim, dict):
                continue
            sim_rows.append(
                [
                    sim.get("number") or "-",
                    sim.get("name") or "-",
                    sim.get("cnic") or "-",
                    sim.get("address") or "-",
                ]
            )
        if sim_rows:
            specs.append(("All SIMs Against This CNIC", ["Number", "Name", "CNIC", "Address"], sim_rows, [28, 42, 32, 78]))
        return specs

    if provider_name == "cro" and isinstance(data, dict):
        fir_rows = []
        for fir in (data.get("firs") or [])[:12]:
            if not isinstance(fir, dict):
                continue
            fir_rows.append(
                [
                    fir.get("fir_no") or "-",
                    fir.get("fir_year") or "-",
                    fir.get("police_station") or "-",
                    fir.get("offence") or "-",
                    fir.get("status") or "-",
                ]
            )
        if fir_rows:
            specs.append(("CRO FIRs", ["FIR No", "Year", "Police Station", "Offence", "Status"], fir_rows, [20, 16, 48, 68, 28]))

    if provider_name == "psrms" and isinstance(data, dict):
        fir_rows = []
        for fir in (data.get("firs") or [])[:12]:
            if not isinstance(fir, dict):
                continue
            
            ptype = str(fir.get("person_type") or "").upper().strip()
            role = "-"
            if ptype == "WIT":
                role = "Suspect"
            elif ptype == "SUS":
                role = "Witness"
            elif ptype == "FIR":
                role = "Complainant"

            fir_rows.append(
                [
                    fir.get("fir_no") or "-",
                    fir.get("fir_year") or "-",
                    fir.get("fir_status") or "-",
                    fir.get("person_address") or "-",
                    role,
                ]
            )
        if fir_rows:
            specs.append(("PSRMS FIRs", ["FIR No", "Year", "Case Status", "Address", "Person Type"], fir_rows, [18, 14, 40, 78, 30]))

    if provider_name == "tracs":
        payload = _tracs_payload(result)
        challan_rows = []
        for item in payload[:12]:
            if not isinstance(item, dict):
                continue
            challan_rows.append(
                [
                    item.get("challanNumber") or item.get("challan_no") or item.get("reference_no") or item.get("id") or "-",
                    (item.get("location") or {}).get("name") if isinstance(item.get("location"), dict) else "-",
                    item.get("vehicleNumPlate") or item.get("vehicle_no") or item.get("registration_no") or "-",
                    _tracs_challan_type(item) or "-",
                    (item.get("vehicleType") or {}).get("title") if isinstance(item.get("vehicleType"), dict) else "-",
                    item.get("status") or "-",
                ]
            )
        if challan_rows:
            specs.append(("TRACS Challans", ["Challan ID", "Location", "Vehicle No.", "Challan Type", "Vehicle Type", "Status"], challan_rows, [38, 34, 28, 34, 25, 21]))

    if provider_name == "old_tenant" and isinstance(data, dict):
        tenant_rows = []
        for t in (data.get("tenants") or [])[:15]:
            if not isinstance(t, dict):
                continue
            tenant_rows.append(
                [
                    str(t.get("tenant_id") or "-"),
                    str(t.get("tenant_name") or "-"),
                    str(t.get("tenant_cnic") or "-"),
                    str(t.get("tenant_phone") or "-"),
                    str(t.get("house_no") or "-"),
                    str(t.get("street_mohalla") or "-"),
                    str(t.get("address") or "-"),
                ]
            )
        if tenant_rows:
            specs.append(("Tenants", ["Tenant ID", "Name", "CNIC", "Phone", "House No.", "Property Street / Mohalla", "Address"], tenant_rows, [16, 30, 28, 24, 20, 38, 34]))



    if provider_name == "watchlist":
        raw = result.get("raw")
        payload = raw.get("data", []) if isinstance(raw, dict) and isinstance(raw.get("data"), list) else []
        watchlist_rows = []
        for item in payload[:10]:
            if not isinstance(item, dict):
                continue
            watchlist_rows.append(
                [
                    str(item.get("id") or "-"),
                    str(item.get("court_district") or "-"),
                    str(item.get("police_station") or "-"),
                    str(item.get("arrest_type") or "-"),
                    str(item.get("description") or "").replace("\r", " ").replace("\n", " ").strip()[:300] or "-",
                ]
            )
        if watchlist_rows:
            specs.append(("Watchlist Matches", ["ID", "Court District", "PS", "Arrest Type", "Description"], watchlist_rows, [15, 25, 25, 30, 95]))

    if provider_name == "hotel_eye":
        raw = result.get("raw")
        payload = raw.get("records", []) if isinstance(raw, dict) and isinstance(raw.get("records"), list) else []
        hotel_rows = []
        for item in payload[:12]:
            if not isinstance(item, dict):
                continue
            hotel_rows.append(
                [
                    item.get("guest_name") or "-",
                    item.get("hotel_name") or "-",
                    item.get("district") or "-",
                    item.get("room_no") or "-",
                    item.get("check_in") or "-",
                    item.get("check_out") or "-",
                    item.get("visit_purpose") or "-",
                ]
            )
        if hotel_rows:
            specs.append(
                (
                    "Hotel Eye Records",
                    ["Guest Name", "Hotel Name", "District", "Room No", "Check In", "Check Out", "Visit Purpose"],
                    hotel_rows,
                    [28, 35, 20, 15, 24, 24, 34],
                )
            )

    if provider_name == "dls" and isinstance(data, dict):
        license_rows = []
        for item in (data.get("licenses") or [])[:12]:
            if not isinstance(item, dict):
                continue
            license_rows.append(
                [
                    item.get("license_no") or "-",
                    item.get("category") or "-",
                    item.get("license_type") or "-",
                    _format_compact_date(item.get("expiry_date")),
                    _format_compact_date(item.get("issued_date")),
                    item.get("issued_office") or "-",
                    item.get("status") or "-",
                ]
            )
        if license_rows:
            specs.append(
                (
                    "DLS Licenses",
                    ["License", "Category", "Type", "Expiry", "Issued Date", "Issued Office", "Status"],
                    license_rows,
                    [28, 35, 17, 19, 22, 42, 22],
                )
            )

    if provider_name == "sbvs" and isinstance(data, dict):
        sbvs_rows = []
        entries = data.get("entries", [])
        if isinstance(entries, list):
            for item in entries[:15]:
                if not isinstance(item, dict):
                    continue
                sbvs_rows.append(
                    [
                        str(item.get("id") or "-"),
                        str(item.get("organization_name") or "-"),
                        str(item.get("profession") or "-"),
                        str(item.get("purpose") or "-"),
                        str(item.get("district") or "-"),
                        str(item.get("ps") or "-"),
                        str(item.get("status") or "-"),
                    ]
                )
        if sbvs_rows:
            specs.append(("SBVS Entries", ["ID", "Organization Name", "Profession", "Purpose Name", "District", "PS", "Status"], sbvs_rows, [10, 35, 30, 35, 25, 30, 20]))

    return specs


def _format_compact_date(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    if "T" in text:
        text = text.split("T", 1)[0]
    if " " in text and len(text) > 10:
        text = text.split(" ", 1)[0]
    return text or "-"


def _nearest_ps_link(name: str | None, latitude: float | None, longitude: float | None) -> object:
    label = str(name or "").strip()
    if not label:
        return "-"
    if latitude is None or longitude is None:
        return label
    return (label, f"https://www.google.com/maps?q={latitude},{longitude}")


def _tracs_payload(result: dict[str, object]) -> list[dict[str, object]]:
    raw = result.get("raw")
    payload = raw.get("data", []) if isinstance(raw, dict) and isinstance(raw.get("data"), list) else raw if isinstance(raw, list) else []
    return [item for item in payload if isinstance(item, dict)]


def _latest_tracs_challan(result: dict[str, object]) -> dict[str, object] | None:
    payload = _tracs_payload(result)
    if not payload:
        return None

    def sort_key(item: dict[str, object]) -> tuple[str, str]:
        approved = str(item.get("approved_at") or "").strip()
        printed = str(item.get("print_at") or "").strip()
        return (approved, printed)

    ordered = sorted(payload, key=sort_key, reverse=True)
    return ordered[0] if ordered else None


def _tracs_challan_type(item: dict[str, object]) -> str | None:
    snapshots = item.get("vehicleChallanSnapshot")
    if not isinstance(snapshots, list) or not snapshots:
        return None
    first = snapshots[0]
    if not isinstance(first, dict):
        return None
    challan_type = first.get("challan_type")
    if isinstance(challan_type, dict):
        title = str(challan_type.get("title") or "").strip()
        return title or None
    title = str(first.get("challan_type") or "").strip()
    return title or None


def _render_text_attachments(pdf: ReportPdf, analysis: ReportAnalysis, placement_key: str | None) -> None:
    html_attachments = [
        artifact
        for artifact in analysis.attachments
        if artifact.media_type != "pdf"
        and (
            placement_key is None and not artifact.metadata.get("insert_after")
            or placement_key is not None and artifact.metadata.get("insert_after") == placement_key
        )
    ]
    html_attachments = sorted(html_attachments, key=_attachment_sort_key)
    for artifact in html_attachments:
        pdf.add_page()
        pdf.section_heading(artifact.label, min_height=60)
        metadata = artifact.metadata or {}
        _render_attachment_metadata(pdf, metadata)


def _queue_pdf_attachment_placeholders(
    pdf: ReportPdf,
    analysis: ReportAnalysis,
    placement_key: str,
    pdf_insertions: list[dict[str, object]],
) -> bool:
    artifacts = [
        artifact
        for artifact in analysis.attachments
        if artifact.media_type == "pdf"
        and artifact.bytes_content
        and artifact.metadata.get("insert_after") == placement_key
    ]
    artifacts = sorted(artifacts, key=_attachment_sort_key)
    if not artifacts:
        return False
    for artifact in artifacts:
        show_summary_page = bool(artifact.metadata.get("show_summary_page", True))
        if show_summary_page:
            _render_pdf_attachment_placeholder(pdf, artifact)
            insert_after_page = pdf.page_no()
        else:
            insert_after_page = pdf.page_no()
        pdf_insertions.append(
            {
                "insert_after_page": insert_after_page,
                "label": artifact.label,
                "bytes_content": artifact.bytes_content,
            }
        )
    return True


def _attachment_sort_key(artifact: AttachmentArtifact) -> tuple[int, str]:
    order = {
        "cro_pdf": 0,
        "fir_report": 1,
        "fir_pdf_original": 2,
    }
    kind = str(artifact.metadata.get("attachment_kind", "")).strip()
    if kind == "fir_report":
        year_text = str(artifact.metadata.get("fir_year") or "").strip()
        no_text = str(artifact.metadata.get("fir_no") or "").strip()
        try:
            year_value = int("".join(ch for ch in year_text if ch.isdigit()) or "0")
        except ValueError:
            year_value = 0
        try:
            no_value = int("".join(ch for ch in no_text if ch.isdigit()) or "0")
        except ValueError:
            no_value = 0
        return (order.get(kind, 50), f"{year_value:06d}-{no_value:08d}-{str(artifact.label or '')}")
    label = str(artifact.label or "")
    return (order.get(kind, 50), label)


def _render_pdf_attachment_placeholder(pdf: ReportPdf, artifact: AttachmentArtifact) -> None:
    pdf.add_page()
    pdf.section_heading(artifact.label, min_height=60)
    kind = str(artifact.metadata.get("attachment_kind", "")).strip()
    subject_label = str(artifact.metadata.get("subject_label", "")).strip()
    if kind == "cro_pdf":
        if artifact.metadata.get("cro_no"):
            pdf.key_value("CRO No.", artifact.metadata.get("cro_no"))
        title = artifact.metadata.get("title")
        if title:
            pdf.key_value("Report", title)
        pdf.note("نیچے اصل CRO پی ڈی ایف رپورٹ منسلک کی گئی ہے۔ اس کا اصل لے آؤٹ اور صفحات برقرار رکھے گئے ہیں۔", italic=False)
    elif kind == "fir_report":
        _render_attachment_metadata(pdf, artifact.metadata, title_override="FIR Summary")
        pdf.note("نیچے ایف آئی آر رپورٹ کو پی ڈی ایف میں تبدیل کر کے اصل لے آؤٹ کے قریب منسلک کیا گیا ہے۔", italic=False)
    elif kind == "fir_pdf_original":
        title = artifact.metadata.get("title")
        if title:
            pdf.key_value("Report", title)
        pdf.note("نیچے اصل ایف آئی آر پی ڈی ایف رپورٹ منسلک کی گئی ہے تاکہ اصل ماخذ کا لے آؤٹ بھی دیکھا جا سکے۔", italic=False)
    else:
        pdf.note("نیچے اصل بیرونی پی ڈی ایف رپورٹ منسلک کی گئی ہے۔", italic=False)


def _render_attachment_metadata(pdf: ReportPdf, metadata: dict[str, object], title_override: str | None = None) -> None:
    title = title_override or metadata.get("title")
    if title:
        pdf.sub_heading(str(title))

    # Consolidated FIR Summary Table
    header_fields = metadata.get("header_fields", []) or []
    def get_val(lbl):
        for f in header_fields:
            if isinstance(f, dict) and str(f.get("label")).strip() == lbl:
                return str(f.get("value") or "").strip()
        return "-"

    fir_no = get_val("FIR نمبر")
    if fir_no == "-":
        fir_no = f"{metadata.get('fir_no', '-')}/{metadata.get('fir_year', '-')}"
    
    summary_cols = ["Subject", "FIR No", "PS", "District", "Date", "Sections of Law / Lost Items "]
    summary_row = [
        str(metadata.get("subject_label") or "-"),
        fir_no,
        get_val("تھانہ"),
        get_val("ضلع"),
        get_val("تاریخ و وقت وقوعہ"),
        str(metadata.get("sections_of_law") or "-")
    ]
    pdf.table(summary_cols, [summary_row])
    pdf.ln(4)

    header_rows = []
    for field in header_fields:
        if not isinstance(field, dict):
            continue
        label = str(field.get("label") or "Field").strip()
        value = str(field.get("value") or "-").strip()
        if not value or value == "-" or label in ["FIR نمبر", "تھانہ", "ضلع", "تاریخ و وقت وقوعہ", "سیریل نمبر"]:
            continue
        header_rows.append([label, value])
    if header_rows:
        pdf.table(["Field", "Value"], header_rows, col_widths=[45, 145], align=["R", "L"])
        pdf.ln(2)

    fir_sections = metadata.get("fir_sections", []) or []
    if fir_sections:
        for section in fir_sections:
            if not isinstance(section, dict):
                continue
            section_title = str(section.get("title") or "").strip()
            if section_title:
                pdf.sub_heading(section_title)
            section_pairs = section.get("pairs", []) or []
            pair_rows = []
            for pair in section_pairs:
                if not isinstance(pair, dict):
                    continue
                label = str(pair.get("label") or "Field").strip()
                value = str(pair.get("value") or "").strip()
                if value:
                    pair_rows.append([label, value])
            if pair_rows:
                pdf.table(["Field", "Value"], pair_rows, col_widths=[50, 140], align=["R", "L"])
                pdf.ln(1)
            for line in (section.get("lines", []) or [])[:12]:
                pdf.note(str(line), italic=False, color=(33, 37, 41))
            if section_pairs or section.get("lines"):
                pdf.ln(1)

    main_narrative = str(metadata.get("main_narrative") or "").strip()
    if main_narrative:
        pdf.sub_heading("FIR")
        pdf.note(main_narrative, italic=False, color=(33, 37, 41))
        pdf.ln(1)

    footer_fields = metadata.get("footer_fields", []) or []
    footer_rows = []
    for field in footer_fields:
        if not isinstance(field, dict):
            continue
        label = str(field.get("label") or "Field").strip()
        value = str(field.get("value") or "").strip()
        if value:
            footer_rows.append([label, value])
    if footer_rows:
        pdf.sub_heading("Officer / Closing Details")
        pdf.table(["Field", "Value"], footer_rows, col_widths=[50, 140], align=["R", "L"])
        pdf.ln(2)

    def render_named_table(key: str, header_label: str) -> None:
        table_data = metadata.get(key, [])
        if not table_data or not isinstance(table_data, list):
            return
        # Limit to 8 headers max to avoid ultra-squashed tables
        headers = list(table_data[0].keys())[:8]
        rows: list[list[str]] = []
        for row_dict in table_data:
            if isinstance(row_dict, dict):
                # Ensure each row matches the length of the limited headers
                rows.append([str(row_dict.get(h, "")) for h in headers])
        
        if rows:
            pdf.sub_heading(header_label)
            try:
                pdf.table(headers, rows)
            except Exception as e:
                pdf.note(f"Table grid error: {str(e)}")
            pdf.ln(2)

    render_named_table("case_positions", "Case Positions (پوزیشن مقدمہ)")
    render_named_table("investigating_officers", "Investigating Officers (تفتیشی افسران)")
    render_named_table("nominated_suspects", "Nominated Suspects (نامزد ملزمان)")
    render_named_table("unknown_suspects", "Unknown Suspects (نامعلوم ملزمان)")
    render_named_table("witnesses", "Witnesses (گواہان)")
    render_named_table("stolen_property", "Stolen Property (مسروقہ مال)")
    
    investigation_result = str(metadata.get("investigation_result") or "").strip()
    if investigation_result:
        pdf.sub_heading("Investigation Result (نتیجہ تفتیش)")
        pdf.note(investigation_result, italic=False, color=(33, 37, 41))
        pdf.ln(2)

    detail_tables = metadata.get("detail_tables", []) or []
    if not fir_sections and not any(metadata.get(k) for k in [
        "case_positions", "investigating_officers", "nominated_suspects", 
        "unknown_suspects", "witnesses", "stolen_property"
    ]):
        for table in detail_tables:
            if not isinstance(table, dict):
                continue
            headers = [str(item) for item in (table.get("headers") or []) if str(item).strip()]
            rows = table.get("rows") or []
            normalized_rows = [[str(cell) for cell in row] for row in rows if isinstance(row, list)]
            max_cols = min(8, max([len(headers)] + [len(row) for row in normalized_rows]))
            if max_cols == 0:
                continue
            if not headers:
                headers = [f"Detail {index}" for index in range(1, max_cols + 1)]
            elif len(headers) < max_cols:
                headers = headers + [f"Detail {index}" for index in range(len(headers) + 1, max_cols + 1)]
            elif len(headers) > max_cols:
                headers = headers[:max_cols]
            normalized_rows = [row[:max_cols] + [""] * (max_cols - len(row[:max_cols])) for row in normalized_rows]
            table_title = str(table.get("title") or "").strip()
            if table_title:
                pdf.sub_heading(table_title)
            try:
                pdf.table(headers, normalized_rows)
            except Exception as e:
                pdf.note(f"Table grid error: {str(e)}")
            pdf.ln(2)

    paragraph_lines = metadata.get("paragraph_lines", []) or []
    if paragraph_lines:
        pdf.sub_heading("Progress")
        for line in paragraph_lines[:20]:
            if pdf.get_y() > 270:
                pdf.add_page()
                pdf.sub_heading("Progress")
            pdf.note(str(line), italic=False, color=(33, 37, 41))

    body_lines = metadata.get("body_lines", []) or []
    if body_lines and not detail_tables:
        pdf.sub_heading("Report Details")
        for line in body_lines[:80]:
            if pdf.get_y() > 270:
                pdf.add_page()
                pdf.sub_heading("Report Details")
            pdf.note(str(line), italic=False, color=(33, 37, 41))


def _splice_pdf_attachments(base_pdf: bytes, pdf_insertions: list[dict[str, object]]) -> bytes:
    if not pdf_insertions:
        return base_pdf

    final_pdf = base_pdf
    for item in sorted(pdf_insertions, key=lambda value: int(value["insert_after_page"]), reverse=True):
        base_buf = extra_buf = merged = None
        try:
            base_buf = io.BytesIO(final_pdf)
            extra_buf = io.BytesIO(item["bytes_content"])  # type: ignore[arg-type]
            base_reader = PdfReader(base_buf)
            extra_reader = PdfReader(extra_buf)
            writer = PdfWriter()
            insert_after_page = max(0, min(int(item["insert_after_page"]), len(base_reader.pages)))
            for page_index in range(insert_after_page):
                writer.add_page(base_reader.pages[page_index])
            for page in extra_reader.pages:
                writer.add_page(page)
            for page_index in range(insert_after_page, len(base_reader.pages)):
                writer.add_page(base_reader.pages[page_index])
            merged = io.BytesIO()
            writer.write(merged)
            final_pdf = merged.getvalue()
        except Exception:
            continue
        finally:
            # Free the large PDF bytes stored in this insertion entry immediately
            item["bytes_content"] = None
            for buf in (base_buf, extra_buf, merged):
                try:
                    if buf is not None:
                        buf.close()
                except Exception:
                    pass
    return final_pdf
