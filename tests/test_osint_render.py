"""Renderer tests.

These check what the document *says*, not how it looks. The one property that must hold
on every page is that a reader cannot mistake an OSINT finding for a verified fact.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sherlocks.osint.models import (
    FindingStatus,
    OsintPlan,
    OsintReport,
    OsintSubject,
    PlannedTool,
    ToolResult,
)
from sherlocks.render._reportlib import report_app_available
from sherlocks.render.osint_report import _dashed_cnic, render_osint_report
from sherlocks.settings import Settings

pytestmark = pytest.mark.skipif(
    not report_app_available(), reason="cdr_report_app PDF engine is not on this machine"
)


def _report() -> OsintReport:
    subject = OsintSubject(full_name="Ali Raza Memon", city="Karachi", cnic="42101-1234567-1")
    return OsintReport(
        subject=subject,
        plan=OsintPlan(
            subject_label=subject.label(),
            tools=[PlannedTool(tool="generate_dorks", query="Ali Raza Memon Karachi")],
            reasoning="Query composed by rule.",
        ),
        results=[
            ToolResult(
                tool="generate_dorks",
                query="Ali Raza Memon Karachi",
                status=FindingStatus.OK,
                data={"raw": "Google dork URLs for 'Ali Raza Memon Karachi'"},
                summary="Google dork URLs",
            ),
            ToolResult(
                tool="search_footprint",
                query="Ali Raza Memon Karachi",
                status=FindingStatus.NOT_CONFIGURED,
                summary="Skipped.",
                error="no Bright Data API key and SERP zone are configured",
            ),
        ],
        narrative="One tool returned results.",
        discovered_identifiers=[
            {"kind": "url", "value": "https://facebook.com/aliraza", "tool": "search_footprint", "context": ""}
        ],
        finished_at=datetime.now(UTC),
    )


def _text(path) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() for page in PdfReader(str(path)).pages)


def test_the_unverified_banner_is_on_the_first_page(tmp_path) -> None:
    path = render_osint_report(_report(), tmp_path / "r.pdf", Settings())
    from pypdf import PdfReader

    assert "UNVERIFIED" in PdfReader(str(path)).pages[0].extract_text()


def test_tools_that_did_not_run_are_named_with_their_reason(tmp_path) -> None:
    text = _text(render_osint_report(_report(), tmp_path / "r.pdf", Settings()))
    assert "search_footprint" in text
    assert "Bright Data" in text


def test_the_cnic_carries_its_own_disclaimer(tmp_path) -> None:
    text = _text(render_osint_report(_report(), tmp_path / "r.pdf", Settings()))
    assert "No open-source tool can search by CNIC" in text


def test_discovered_identifiers_are_labelled_as_leads(tmp_path) -> None:
    text = _text(render_osint_report(_report(), tmp_path / "r.pdf", Settings()))
    assert "facebook.com/aliraza" in text
    assert "verified against an" in text


def test_a_report_with_no_findings_still_renders(tmp_path) -> None:
    report = _report()
    report.results = []
    report.discovered_identifiers = []
    path = render_osint_report(report, tmp_path / "empty.pdf", Settings())
    assert path.exists()
    assert "No tool was run" in _text(path)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("4210112345671", "42101-1234567-1"),
        ("42101-1234567-1", "42101-1234567-1"),
        ("12345", "12345"),
        (None, None),
    ],
)
def test_cnic_formatting(raw, expected) -> None:
    assert _dashed_cnic(raw) == expected
