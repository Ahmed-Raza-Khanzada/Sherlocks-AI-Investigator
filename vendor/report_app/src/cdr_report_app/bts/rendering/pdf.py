"""PDF report renderer for BTS analysis."""

from __future__ import annotations

import logging
from pathlib import Path

from cdr_report_app.bts.models import BtsAnalysis, BtsAnalysisRequest, BtsWindowResult
from cdr_report_app.rendering.pdf.builder import ReportPdf

logger = logging.getLogger(__name__)

_NA = "-"
_TOP_N = 10


# ---------------------------------------------------------------------------
# Urdu labels / notes (non-heading text)
# ---------------------------------------------------------------------------

_LBL = {
    "date":            "تاریخ",
    "dates":           "تاریخیں",
    "window":          "وقت کی رینج",
    "window_n":        "وقت رینج {}",
    "total_calls_w":   "ونڈو میں کل کالز",
    "total_calls_wi":  "    ونڈو میں کل کالز",
    "msisdn":          "MSISDN",
    "tower_count":     "ٹاورز کی تعداد",
    "towers":          "ٹاورز",
    "calls_window":    "ونڈو میں کالز",
    "rank":            "#",
}

_NOTE_CROSS_BTS = (
    "نمبرز جو اپنی اپنی ونڈوز میں 2 یا زیادہ ٹاورز پر "
    "اے-پارٹی کے طور پر نظر آئے۔"
)
_NOTE_B_AS_A = (
    "وہ اے-پارٹی نمبرز جن کے بی-پارٹی کانٹیکٹس بھی اُسی ٹاور پر خود اے-پارٹی کے طور پر "
    "موجود تھے۔"
)
_NOTE_WINDOW_ONLY = (
    "اے-پارٹی نمبرز جو صرف اس وقت کی رینج میں ایکٹو تھے، "
    "باقی وقت فائل میں موجود نہیں۔"
)
_NOTE_NO_COMMON = (
    "کوئی کامن نمبر نہیں ملا دونوں ٹاورز کی دی گئی ٹائم رینجز میں۔  |  "
    "No common numbers found across the given tower windows."
)
_NOTE_LEGEND = (
    "مکمل معلومات جنریٹ کی گئی ایکسل فائل میں موجود ہے۔  |  "
    "Complete information is available in the generated Excel file."
)


def _top_n_label(items, max_n: int = _TOP_N) -> str:
    n = min(len(items), max_n)
    return f"Top {n}"



def render_bts_pdf(
    analysis: BtsAnalysis,
    request: BtsAnalysisRequest,
    file_info: list[dict] | None = None,
    output_path: Path | None = None,
    spec_tower_map: dict[str, tuple[str, str]] | None = None,
) -> bytes:
    logger.info("BTS PDF rendering started | windows=%d", len(analysis.window_results))
    pdf = ReportPdf()
    pdf.add_page()

    _render_title(pdf)
    _render_legend(pdf)
    _render_executive_summary(pdf, analysis, spec_tower_map or {})

    multi_window = len(analysis.window_results) > 1
    for idx, wr in enumerate(analysis.window_results, start=1):
        _render_window_section(pdf, wr, request, window_number=idx, multi=multi_window)

    distinct_specs = {wr.key.spec_id for wr in analysis.window_results}
    if request.include_cross_bts_common and len(distinct_specs) >= 2:
        _render_cross_bts(pdf, analysis)

    content = bytes(pdf.output())
    pdf.cleanup()

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(content)

    logger.info("BTS PDF rendering completed | size=%d bytes", len(content))
    return content


# ---------------------------------------------------------------------------
#  Title, legend, executive summary
# ---------------------------------------------------------------------------

def _render_title(pdf: ReportPdf) -> None:
    pdf.set_fill_color(41, 65, 122)
    pdf.rect(10, 10, 190, 22, style="F")
    pdf.set_xy(10, 12)
    pdf.set_font(pdf.dfont, "B", 18)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(190, 9, "BTS Analysis Report", align="C", ln=True)
    pdf.set_font(pdf.dfont, "", 9)
    pdf.set_text_color(220, 220, 240)
    pdf.cell(190, 5, "Base Transceiver Station — Tower-Centric CDR Analysis", align="C", ln=True)
    pdf.set_text_color(33, 37, 41)
    pdf.ln(8)


def _render_legend(pdf: ReportPdf) -> None:
    pdf.set_fill_color(255, 250, 220)
    pdf.set_draw_color(200, 170, 50)
    y = pdf.get_y()
    pdf.rect(pdf.l_margin, y, 190, 8, style="FD")
    pdf.set_xy(pdf.l_margin + 2, y + 1)
    pdf.set_text_font(_NOTE_LEGEND, "I", 6)
    pdf.set_text_color(100, 70, 0)
    pdf.cell(186, 6, pdf._safe_text(_NOTE_LEGEND), align="C")
    pdf.set_text_color(33, 37, 41)
    pdf.ln(10)


def _render_executive_summary(
    pdf: ReportPdf,
    analysis: BtsAnalysis,
    spec_tower_map: dict[str, tuple[str, str]],
) -> None:
    pdf.section_heading("Executive Summary")

    total_window_calls = sum(wr.total_rows for wr in analysis.window_results)

    x_base = pdf.l_margin
    y_base = pdf.get_y()
    box_w = 60
    boxes = [
        ("Windows Analyzed", str(len(analysis.window_results))),
        ("کل کالز ونڈوز میں", f"{total_window_calls:,}"),
        ("کل بی ٹی ایس ریکارڈز", f"{analysis.total_bts_records:,}"),
    ]
    for i, (lbl, val) in enumerate(boxes):
        pdf.stat_box(lbl, val, x_base + i * (box_w + 2), y_base, w=box_w, h=20)

    pdf.set_xy(pdf.l_margin, y_base + 24)

    dates = sorted({wr.key.window_start.strftime("%Y-%m-%d") for wr in analysis.window_results})
    if dates:
        pdf.key_value(_LBL["dates"] if len(dates) > 1 else _LBL["date"], ", ".join(dates))

    multi = len(analysis.window_results) > 1
    for idx, wr in enumerate(analysis.window_results, start=1):
        time_range = (
            f"{wr.key.window_start.strftime('%H:%M:%S')} - "
            f"{wr.key.window_end.strftime('%H:%M:%S')}"
        )
        if multi:
            pdf.key_value(_LBL["window_n"].format(idx), time_range)
            pdf.key_value(_LBL["total_calls_wi"], f"{wr.total_rows:,}")
        else:
            pdf.key_value(_LBL["window"], time_range)
            pdf.key_value(_LBL["total_calls_w"], f"{wr.total_rows:,}")

    # Tower → filename mapping
    seen_specs: list[str] = []
    for wr in analysis.window_results:
        sid = wr.key.spec_id
        if sid not in seen_specs:
            seen_specs.append(sid)
    for tower_n, spec_id in enumerate(seen_specs, start=1):
        bts_label, filenames = spec_tower_map.get(spec_id, (spec_id, spec_id))
        pdf.key_value(f"Tower {tower_n}", f"{bts_label}  |  {filenames}")

    pdf.ln(2)


# ---------------------------------------------------------------------------
#  Per-window section
# ---------------------------------------------------------------------------

def _render_window_section(
    pdf: ReportPdf,
    wr: BtsWindowResult,
    request: BtsAnalysisRequest,
    *,
    window_number: int,
    multi: bool,
) -> None:
    k = wr.key
    if multi:
        time_range = (
            f"{k.window_start.strftime('%H:%M:%S')} - "
            f"{k.window_end.strftime('%H:%M:%S')}"
        )
        bts_label = k.bts_id or k.spec_id
        pdf.section_heading(f"Window {window_number}: {time_range}  |  BTS: {bts_label}")

    # 1. B-as-A
    if request.include_b_as_a and wr.b_as_a:
        n = _top_n_label(wr.b_as_a)
        pdf.sub_heading(f"{n} A-Party Contacts Whose B-Parties Are Also Present at Tower")
        pdf.note(_NOTE_B_AS_A)
        headers = [_LBL["rank"], "A-Party", "B-Parties Also At Tower", "Count"]
        rows = [
            [
                str(i),
                ba.a_party,
                ", ".join(ba.b_parties_at_tower),
                str(ba.contact_count),
            ]
            for i, ba in enumerate(wr.b_as_a[:_TOP_N], 1)
        ]
        pdf.table(headers, rows)
        pdf.ln(3)

    # 2. Window-Only Presence
    if request.include_window_only and wr.window_only:
        n = _top_n_label(wr.window_only)
        pdf.sub_heading(f"{n} Window-Only Numbers")
        pdf.note(_NOTE_WINDOW_ONLY)
        headers = [_LBL["rank"], _LBL["msisdn"], _LBL["calls_window"], "B-Party Contacts"]
        rows = [
            [
                str(i),
                wo.msisdn,
                str(wo.call_count),
                ", ".join(wo.b_parties) if wo.b_parties else "-",
            ]
            for i, wo in enumerate(wr.window_only[:_TOP_N], 1)
        ]
        pdf.table(headers, rows)
        pdf.ln(3)


# ---------------------------------------------------------------------------
#  Cross-BTS section
# ---------------------------------------------------------------------------

def _render_cross_bts(pdf: ReportPdf, analysis: BtsAnalysis) -> None:
    common = analysis.cross_bts_common
    n_label = _top_n_label(common) if common else "Cross-BTS"
    pdf.section_heading(f"{n_label} Cross-BTS Common Numbers")
    pdf.note(_NOTE_CROSS_BTS)

    if not common:
        pdf.note(_NOTE_NO_COMMON)
        pdf.ln(4)
        return

    all_towers = sorted({t for cc in common[:_TOP_N] for t in cc.towers})

    headers = [_LBL["rank"], _LBL["msisdn"], _LBL["tower_count"]]
    for t in all_towers:
        headers.append(f"Calls ({t})")

    rows = []
    for i, cc in enumerate(common[:_TOP_N], 1):
        row = [str(i), cc.msisdn, str(cc.tower_count)]
        for t in all_towers:
            row.append(str(cc.tower_call_counts.get(t, 0)))
        rows.append(row)

    pdf.table(headers, rows)
    pdf.ln(4)
