"""The OSINT footprint PDF.

Built on cdr_report_app's ``ReportPdf`` so Sherlocks output looks like the reports the
unit already reads, and so the Urdu shaping works without being solved twice.

The document is arranged around one editorial rule: **an analyst must never be able to
mistake an OSINT finding for a verified fact.** So the unverified banner is the first
thing on page one, every findings section repeats the status, and the tools that did
*not* run are listed with the reason. A report that quietly omits a failed scan reads
identically to one where the subject has no footprint, and that difference matters.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from fpdf.enums import XPos, YPos

from sherlocks.osint.models import FindingStatus, OsintReport
from sherlocks.render._reportlib import load_report_pdf
from sherlocks.settings import Settings, load_settings

logger = logging.getLogger(__name__)

CLR_PRIMARY = (41, 65, 122)
CLR_MUTED = (108, 117, 125)
CLR_WARN = (176, 106, 0)
CLR_ACCENT = (220, 53, 69)

_TITLE_EN = "Online Footprint Report"
_TITLE_UR = "آن لائن معلوماتی رپورٹ"

_BANNER_EN = (
    "UNVERIFIED - OPEN SOURCE INTELLIGENCE. Everything in this report was collected from "
    "public internet sources. It is investigative lead material only. No finding here may "
    "be cited as evidence, or used to establish a relationship between two people, until "
    "it has been corroborated against an authoritative record."
)
_BANNER_UR = "غیر تصدیق شدہ۔ یہ معلومات صرف تفتیشی رہنمائی کے لیے ہیں۔"

_STATUS_LABEL = {
    FindingStatus.OK: "Result found",
    FindingStatus.NO_RESULT: "Ran, nothing found",
    FindingStatus.SKIPPED: "Not run",
    FindingStatus.ERROR: "Failed",
    FindingStatus.NOT_CONFIGURED: "Unavailable",
}

# How much raw tool output to reproduce per finding. The full text is kept in the
# database; the PDF is for reading, not for archiving.
_RAW_EXCERPT_CHARS = 2500


def render_osint_report(
    report: OsintReport,
    output_path: str | Path | None = None,
    settings: Settings | None = None,
) -> Path:
    """Write the footprint PDF and return its path."""
    settings = settings or load_settings()
    report_pdf_cls = load_report_pdf(settings)

    if output_path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        safe = _slug(report.subject.label())
        output_path = settings.output_path / f"osint_{safe}_{stamp}.pdf"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    pdf = report_pdf_cls()
    try:
        pdf.add_page()
        _cover(pdf, report)
        _subject_block(pdf, report)
        _coverage_block(pdf, report)
        _narrative_block(pdf, report)
        _identifiers_block(pdf, report)
        _findings_block(pdf, report)
        pdf.output(str(output_path))
    finally:
        # ReportPdf tracks temporary chart files; nothing here creates any, but the
        # contract is to always call it.
        try:
            pdf.cleanup()
        except Exception:  # pragma: no cover - cleanup must never mask a render error
            logger.debug("ReportPdf.cleanup() failed", exc_info=True)

    logger.info("OSINT report written to %s", output_path)
    return output_path


def _cover(pdf, report: OsintReport) -> None:
    pdf.set_text_font(_TITLE_EN, "B", 16)
    pdf.set_text_color(*CLR_PRIMARY)
    pdf.cell(0, 10, pdf._safe_text(_TITLE_EN), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.set_text_font(_TITLE_UR, "B", 12)
    pdf.cell(0, 9, pdf._safe_text(_TITLE_UR), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.set_text_color(0, 0, 0)

    pdf.set_text_font(report.subject.label(), "B", 12)
    pdf.cell(0, 8, pdf._safe_text(report.subject.label()), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="C")
    pdf.ln(2)

    # The banner is drawn before any finding, deliberately.
    pdf.set_fill_color(255, 244, 224)
    pdf.set_draw_color(*CLR_WARN)
    pdf.set_text_color(*CLR_WARN)
    _para(pdf, _BANNER_EN, style="B", size=8, height=4.5, border=1, fill=True)
    _para(pdf, _BANNER_UR, size=8, height=6, align="R", border=1, fill=True)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(3)


def _subject_block(pdf, report: OsintReport) -> None:
    subject = report.subject
    _heading(pdf, "Subject", "موضوع")
    for label, value in (
        ("Name", subject.full_name),
        ("CNIC", _dashed_cnic(subject.cnic)),
        ("Phone", subject.phone),
        ("Email", subject.email),
        ("Username", subject.username),
        ("City", subject.city),
        ("Employer", subject.employer),
        ("Notes", subject.notes),
    ):
        if value:
            pdf.key_value(label, value)
    if subject.cnic:
        pdf.note(
            "No open-source tool can search by CNIC. The CNIC appears here only to tie "
            "this report to a person record."
        )
    pdf.ln(2)


def _coverage_block(pdf, report: OsintReport) -> None:
    """What ran, what did not, and why. The honesty section."""
    _heading(pdf, "Scan Coverage", "تفتیشی احاطہ")

    started = report.started_at.strftime("%Y-%m-%d %H:%M UTC")
    pdf.key_value("Started", started)
    if report.finished_at:
        elapsed = (report.finished_at - report.started_at).total_seconds()
        pdf.key_value("Duration", f"{elapsed:.1f} s")
    if report.plan.reasoning:
        pdf.key_value("Plan", report.plan.reasoning)
    pdf.ln(1)

    rows = []
    for result in report.results:
        rows.append(
            [
                result.tool,
                result.query,
                _STATUS_LABEL.get(result.status, result.status.value),
                (result.error or "-")[:90],
            ]
        )
    if rows:
        pdf.table(
            ["Tool", "Query", "Outcome", "Reason"],
            rows,
            col_widths=pdf.dynamic_widths(
                ["Tool", "Query", "Outcome", "Reason"], rows, min_widths=[28, 40, 26, 40]
            ),
        )
    else:
        pdf.note("No tool was run for this subject.")

    not_run = [r for r in report.results if r.status in (FindingStatus.SKIPPED, FindingStatus.NOT_CONFIGURED)]
    if not_run:
        pdf.note(
            f"{len(not_run)} of {len(report.results)} tools did not run. Absence of a "
            "finding below is not evidence of absence online."
        )
    pdf.ln(2)


def _narrative_block(pdf, report: OsintReport) -> None:
    if not report.narrative:
        return
    _heading(pdf, "Assessment", "تجزیہ")
    for paragraph in report.narrative.split("\n\n"):
        text = paragraph.strip()
        if not text:
            continue
        _para(pdf, text, size=9, height=5)
        pdf.ln(1)
    pdf.note(
        "This summary was drafted by a local language model from the structured findings "
        "above. It restates them; it does not add to them."
    )
    pdf.ln(2)


def _identifiers_block(pdf, report: OsintReport) -> None:
    _heading(pdf, "Candidate Identifiers", "ممکنہ شناختیں")
    if not report.discovered_identifiers:
        pdf.note("No identifiers were surfaced by this scan.")
        pdf.ln(2)
        return

    rows = [
        [item.get("kind", ""), item.get("value", ""), item.get("tool", ""), item.get("context", "")[:70]]
        for item in report.discovered_identifiers
    ]
    headers = ["Kind", "Value", "Found by", "Context"]
    pdf.table(headers, rows, col_widths=pdf.dynamic_widths(headers, rows, min_widths=[24, 55, 28, 40]))
    pdf.note(
        "These are leads extracted from tool output. Each must be verified against an "
        "authoritative source before it is attached to a person record."
    )
    pdf.ln(2)


def _findings_block(pdf, report: OsintReport) -> None:
    hits = [r for r in report.results if r.status == FindingStatus.OK]
    if not hits:
        return

    _heading(pdf, "Raw Findings", "اصل نتائج")
    for result in hits:
        pdf.sub_heading(f"{result.tool} - {result.query}")
        pdf.key_value("Confidence", f"{result.confidence} (unverified)")
        pdf.key_value("Collected", result.fetched_at.strftime("%Y-%m-%d %H:%M UTC"))

        raw = (result.data or {}).get("raw") or ""
        excerpt = raw[:_RAW_EXCERPT_CHARS]
        pdf.set_text_color(*CLR_MUTED)
        _para(pdf, excerpt, size=8, height=4)
        pdf.set_text_color(0, 0, 0)
        if len(raw) > _RAW_EXCERPT_CHARS:
            pdf.note(
                f"Output truncated at {_RAW_EXCERPT_CHARS} characters. "
                "The complete text is stored against this finding in the database."
            )
        pdf.ln(3)


def _heading(pdf, english: str, urdu: str) -> None:
    """Bilingual section heading, as two lines.

    One line holding both scripts comes out with the Urdu reversed - fpdf resolves bidi
    per cell, and a mixed-direction cell has no correct single answer. Two cells, one
    direction each, is the only version that reads correctly.
    """
    pdf.section_heading(english, align="L")
    pdf.set_text_font(urdu, "B", 9)
    pdf.set_text_color(*CLR_PRIMARY)
    pdf.set_x(pdf.l_margin)
    pdf.cell(0, 6, pdf._safe_text(urdu), new_x=XPos.LMARGIN, new_y=YPos.NEXT, align="R")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(1)


def _dashed_cnic(value: str | None) -> str | None:
    """13 digits render as 00000-0000000-0. Anything else is passed through."""
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) != 13:
        return value
    return f"{digits[:5]}-{digits[5:12]}-{digits[12]}"


def _para(
    pdf,
    text: str,
    *,
    style: str = "",
    size: float = 9,
    height: float = 5,
    align: str = "L",
    border: int = 0,
    fill: bool = False,
) -> None:
    """Full-width paragraph.

    ``multi_cell`` leaves the cursor at the right edge of the cell it drew, so a second
    zero-width call would have no room and fpdf raises "Not enough horizontal space to
    render a single character". Resetting x is the whole point of this helper.
    """
    pdf.set_x(pdf.l_margin)
    pdf.set_text_font(text, style, size)
    pdf.multi_cell(0, height, pdf._safe_text(text), align=align, border=border, fill=fill)
    pdf.set_x(pdf.l_margin)


def _slug(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")
    return (cleaned or "subject")[:60]
