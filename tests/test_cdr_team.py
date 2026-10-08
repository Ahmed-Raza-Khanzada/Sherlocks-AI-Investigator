"""The CDR team: findings cite their rows; patterns, places, cross-CDR links; re-filing and
re-running when the board changes. Synthetic workbooks."""

from __future__ import annotations

import datetime as dt
import io
from pathlib import Path

from sherlocks.evidence import cdr, cdr_team
from sherlocks.evidence.case_file import CaseFile

KAMRAN, SAJID, OTHER = "03990000101", "03990000201", "03990009999"


def _xlsx(subject: str, peer: str, *, start: dt.datetime, imei_switch: int | None = None, gap_after: int | None = None,
          site: str = "PECHS") -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Calls"
    ws.append(["MSISDN", "B Number", "Call Type", "Start Time", "Duration", "Site Address", "Latitude", "Longitude", "IMEI"])
    when = start
    for i in range(24):
        when += dt.timedelta(hours=1) if not (gap_after and i == gap_after) else dt.timedelta(hours=30)
        ws.append([subject, peer if i % 2 else OTHER, "Call - Outgoing", when, 30, site, 24.86, 67.06,
                   "356938035643809" if not imei_switch or i < imei_switch else "356938035643817"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_findings_cite_rows_and_the_team_finds_patterns():
    content = _xlsx(KAMRAN, SAJID, start=dt.datetime(2023, 2, 28, 20, 0), imei_switch=12, gap_after=8)
    analysis = cdr.analyse(content, phones_on_graph={SAJID: "Sajid Mehmood"}, incident={"date": "2023-03-01"})
    assert analysis["sheet"] == "Calls"
    top = next(f for f in analysis["findings"] if f["text"].startswith("Top contact 1"))
    assert top["rows"] and top["rows"][0].isdigit()
    texts = [f["text"] for f in analysis["findings"]]
    assert any(t.startswith("IMEI changed on") and "356938035643809 -> 356938035643817" in t for t in texts)
    assert any(t.startswith(("Silence:", "Silent around the incident")) for t in texts)
    home = next(f for f in analysis["findings"] if "likely home area" in f["text"])
    assert home["tier"] == "inference" and "tower" in home["text"]
    assert any("incident:date" in f["rests_on"] for f in analysis["findings"])


def test_fact_writer_files_under_the_number_until_the_owner_is_known():
    case = CaseFile()
    content = _xlsx(KAMRAN, SAJID, start=dt.datetime(2023, 2, 28, 20, 0))
    analysis = cdr.analyse(content, phones_on_graph={}, incident=None)
    doc = case.add_document(kind="cdr", key="upload:1", title="Upload: k.xlsx", source="Uploaded by the officer",
                            text="\n".join(analysis["lines"]))
    ids = cdr_team.write_facts(case, doc, analysis)
    fact = case.facts[ids[0]]
    assert fact["by"] == "cdr" and fact["rows"].startswith("Calls, rows") and fact["pids"] == []
    assert fact["trust"] == 1                                         # CDR rows: an operator's record
    case.documents[doc]["owners"] = ["a"]
    assert cdr_team.refile(case, doc, "a") == len(ids)
    assert all(case.facts[i]["pids"] == ["a"] for i in ids)


def test_two_cdrs_are_linked(tmp_path: Path):
    case = CaseFile()
    graph = {"nodes": [], "edges": []}
    start = dt.datetime(2023, 2, 28, 20, 0)
    for name, subject, peer in (("k.xlsx", KAMRAN, SAJID), ("s.xlsx", SAJID, KAMRAN)):
        content = _xlsx(subject, peer, start=start)
        path = tmp_path / name
        path.write_bytes(content)
        analysis = cdr.analyse(content, phones_on_graph={}, incident=None)
        case.add_document(kind="cdr", key=f"upload:{name}", title=f"Upload: {name}", source="Uploaded by the officer",
                          text="\n".join(analysis["lines"]),
                          data={"path": str(path), "analysis": {"subject": subject, "kind": "cdr"}})
    written = cdr_team.rerun(case, graph, "cdr:cross")
    statements = [case.facts[f]["statement"] for f in written]
    assert any(s.startswith(f"Calls between {KAMRAN} and {SAJID}") for s in statements)
    together = next(case.facts[f] for f in written if "may have been together" in case.facts[f]["statement"])
    assert together["tier"] == "inference"


def test_moving_the_pin_reruns_location_and_old_findings_go_stale(tmp_path: Path):
    case = CaseFile()
    content = _xlsx(KAMRAN, SAJID, start=dt.datetime(2023, 2, 28, 20, 0))
    path = tmp_path / "k.xlsx"
    path.write_bytes(content)
    case.set_incident({"date": "2023-03-01", "lat": 24.86, "lon": 67.06})
    analysis = cdr.analyse(content, phones_on_graph={}, incident=case.incident)
    doc = case.add_document(kind="cdr", key="upload:k", title="Upload: k.xlsx", source="Uploaded by the officer",
                            text="\n".join(analysis["lines"]), data={"path": str(path), "analysis": {"subject": KAMRAN}})
    cdr_team.write_facts(case, doc, analysis)
    near = [f for f in case.facts.values() if f["statement"].startswith("Near the incident")]
    assert near and "connected to a tower" in near[0]["statement"]
    case.set_incident({"lat": 25.40, "lon": 68.36})                   # far away: nothing near any more
    cdr_team.rerun(case, {"nodes": [], "edges": []}, "cdr:location")
    assert all(case.facts[f["id"]]["stale"] for f in near)
