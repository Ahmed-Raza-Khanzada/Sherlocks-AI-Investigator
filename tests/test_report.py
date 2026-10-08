"""The downloaded case report is Sherlock's full, current assessment, kept per board version."""

from __future__ import annotations


def _demo():
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    s.evidence.report_wait_s = 15
    mgr = memory_manager(s)
    handle = mgr.start(GraphRunParams(cnic="9999900000011", depth=2, max_persons=15, backend="demo"), wait=True)
    return mgr, handle


def test_the_report_lays_out_sherlocks_board_and_is_kept_per_version():
    mgr, handle = _demo()
    case = handle.case
    report = case.report
    assert report and report["status"] == "current" and report["hypotheses"]
    assert report["assessments"] and all(a["basis"] for a in report["assessments"])
    assert report["checked"] == [] and report["audit"]["total"] >= 0
    assert "nothing_found" in report and "qa" in report and "cdrs" in report
    pdf = mgr.report_pdf(handle.graph(), handle.id)
    assert pdf.startswith(b"%PDF")
    saved = case.report["saved_at_version"]
    mgr.report_pdf(handle.graph(), handle.id)
    assert case.report["saved_at_version"] == saved                 # nothing changed: the same report
    # The officer adds something: the next download is a new report, Sherlock first.
    from sherlocks.linkgraph.sherlock_team import run_turn

    list(run_turn(handle.graph(), "Kamran main suspect hai", case=case))
    mgr.report_pdf(handle.graph(), handle.id)
    assert case.report["saved_at_version"] > saved and case.report["status"] == "current"
    assert any("main suspect" in st["statement"] for st in case.report["statements"])
