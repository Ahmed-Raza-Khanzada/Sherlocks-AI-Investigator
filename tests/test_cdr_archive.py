"""CDR archive, CDR tools, the sandbox and the CDR analyst. Made-up numbers only."""

import io
from datetime import datetime

import pytest
from openpyxl import Workbook

from sherlocks.evidence import cdr, cdr_archive, cdr_sandbox, cdr_tools
from sherlocks.evidence.case_file import CaseFile

A, B, C, D = "03990000101", "03990000202", "03990000303", "03990000404"


def _xlsx(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["CallType", "Aparty", "BParty", "Datetime", "Duration", "cellid", "Imsi", "Imei", "SiteLocation",
               "Longitude and Latitude"])
    for r in rows:
        ws.append(r)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


ROWS = [
    ["Outgoing", A, B, datetime(2026, 1, 1, 10, 0), 74, "S1", "410010000000001", "350000000000001",
     "Saddar, KARACHI (4G)|24.8543|67.0232", None],
    ["InComing", B, A, datetime(2026, 1, 1, 23, 30), "00:01:14", "S1", "410010000000001", "350000000000001",
     "Saddar, KARACHI (4G)|24.8543|67.0232", None],
    ["Outgoing SMS", A, C, datetime(2026, 1, 2, 2, 15), None, "S2", "410010000000001", "350000000000001",
     "Clifton, KARACHI", "67.0300, 24.8100"],
    ["Outgoing", A, C, datetime(2026, 1, 3, 12, 0), 30, "S2", "410010000000002", "350000000000002",
     "Clifton, KARACHI", "67.0300, 24.8100"],
]


def test_an_archive_cdr_is_read_with_duration_sim_direction_and_tower_coordinates():
    read = cdr.read_events(_xlsx(ROWS))
    ev = read["events"]
    assert read["kind"] == "cdr" and len(ev) == 4
    assert [(e["kind"], e["way"]) for e in ev] == [("call", "out"), ("call", "in"), ("sms", "out"), ("call", "out")]
    assert [e["duration"] for e in ev] == [74, 74, None, 30]
    assert ev[0]["imsi"] == "410010000000001"
    assert (ev[0]["lat"], ev[0]["lon"], ev[0]["addr"]) == (24.8543, 67.0232, "Saddar, KARACHI (4G)")   # after the address
    assert (ev[2]["lat"], ev[2]["lon"]) == (24.81, 67.03)                                            # "Longitude and Latitude"


def _calls(tmp_path, rows=ROWS, subject=A):
    from sherlocks.team import cdr_analyst

    path = tmp_path / f"{subject}.xlsx"
    path.write_bytes(_xlsx(rows))
    case = CaseFile()
    case.add_document(kind="cdr", key=f"t:{subject}", title=f"Upload: {subject}.xlsx", source="test", text="CDR",
                      data={"path": str(path), "analysis": {"subject": subject}})
    return case, cdr_analyst.records(case)


def test_the_fixed_analytics_compute_from_every_record_and_name_their_rows(tmp_path):
    _, calls = _calls(tmp_path)
    top = cdr_tools.top_contacts(calls, A)
    assert [(t["number"], t["calls"], t["sms"], t["talk_seconds"]) for t in top] == [(B, 2, 0, 148), (C, 1, 1, 30)]
    assert top[0]["rows"] == {"D1": [2, 3]}
    late = cdr_tools.night(calls, start=23, end=5)                       # across midnight
    assert late["count"] == 2 and {r["other"] for r in late["records"]} == {B, C}
    assert cdr_tools.devices(calls, A)[1]["imei"] == "350000000000002"   # a new handset on 3 January
    assert cdr_tools.silences(calls, A, hours=24)[0]["hours"] == 33.8
    assert [r["kind"] for r in cdr_tools.between(calls, A, "0399-0000202")] == ["call", "call"]
    assert {r["addr"] for r in cdr_tools.near(calls, 24.8543, 67.0232, 1.0)} == {"Saddar, KARACHI (4G)"}
    assert cdr_tools.norm("+92 399 0000101") == A


def test_common_contacts_and_the_same_tower_link_two_cdrs(tmp_path):
    _, calls = _calls(tmp_path)
    other = [["Outgoing", D, C, datetime(2026, 1, 3, 12, 5), 20, "S2", "", "", "Clifton, KARACHI", None]]
    _, more = _calls(tmp_path, other, subject=D)
    calls = calls + [{**r, "file": "D2"} for r in more]
    common = cdr_tools.common_contacts(calls)
    assert [(c["number"], sorted(c["with"])) for c in common] == [(C, [A, D])]
    assert cdr_tools.together(calls, A, D, minutes=10)[0]["site"] == "S2"


def test_the_sandbox_runs_analysis_and_refuses_everything_else():
    calls = [{"file": "D1", "subject": A, "other": B, "a": A, "b": B, "row": 2, "when": "2026-01-01 10:00:00",
              "date": "2026-01-01", "hour": 10, "kind": "call", "way": "out", "duration": 60}]
    ok = cdr_sandbox.run("result = top_contacts(calls, '03990000101')[0]['talk_seconds']", calls, timeout=10)
    assert ok["ok"] and ok["result"] == 60
    for code in ("import os\nresult = os.listdir('/')", "result = open('/etc/hostname').read()",
                 "result = ().__class__.__bases__", "result = getattr(calls, 'copy')", "import subprocess",
                 "from pathlib import Path"):
        out = cdr_sandbox.run(code, calls, timeout=10)
        assert not out["ok"] and out["error"].startswith("refused"), code
    assert "ZeroDivisionError" in cdr_sandbox.run("result = 1 / 0", calls, timeout=10)["error"]


def test_the_analyst_writes_code_runs_it_and_fixes_a_failed_run(tmp_path):
    from sherlocks.team import cdr_analyst

    case, _ = _calls(tmp_path)

    class Model:
        def __init__(self):
            self.given: list[str] = []

        def generate_text(self, prompt, **_):
            self.given.append(prompt)
            if len(self.given) == 1:
                return "```python\nresult = night(calls, start=23, end=5)['contacts'][0]['nummber']\n```"
            return "```python\nresult = night(calls, start=23, end=5)['contacts']\n```"

    model = Model()
    out = cdr_analyst.answer(case, "Whom did he contact at night?", model)
    assert out["ok"] and out["tries"] == 2 and out["result"] == [{"number": B, "count": 1}, {"number": C, "count": 1}]
    assert "KeyError" in model.given[1]                       # the error went back with the code
    assert "03990000101" not in model.given[0].split("FUNCTIONS")[0].split("FILES")[0]    # no records in the prompt
    assert '"subject": "03990000101"' in model.given[0]          # only what the files are
    assert case.dialog["cdr_code"][-1]["ok"] is True
    empty = cdr_analyst.answer(CaseFile(), "Top contacts?", model)
    assert not empty["ok"] and "No CDR" in empty["error"]


class _Response:
    def __init__(self, body=None, content=b"", headers=None):
        self.body, self.headers = body, headers or {}
        self.raw = io.BytesIO(content)
        self.raw.read = lambda n=-1, decode_content=True, _r=self.raw: io.BytesIO.read(_r, n)

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class _Session:
    def __init__(self, found=True):
        self.found, self.posted, self.got = found, [], []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posted.append((url, json, headers))
        records = [{"analysis_type": "multi", "analysis_id": 6, "file_index": 0, "party_type": "B Party",
                    "in_a_party": False, "in_b_party": True, "a_party_number": "0399-0000101",
                    "b_party_number": "0399-0000202", "has_fir": False, "fir_no": "—",
                    "icop_user": {"name": "Officer One", "police_station": "PS Test", "district": "South"},
                    "download_url": "https://archive.test/api/cdr-database/download?type=multi&id=6&file_index=0"}]
        return _Response({"success": True, "data": {"CDR_DATABASE": {
            "status": self.found, "isError": False, "message": "Record Found" if self.found else "No Record Found",
            "records": records if self.found else [], "searched_mobile": "0399-0000202"}}})

    def get(self, url, headers=None, timeout=None, stream=None):
        self.got.append((url, headers))
        return _Response(content=_xlsx(ROWS), headers={"content-disposition": "attachment; filename=archived.xlsx"})


def test_the_archive_is_searched_and_only_its_own_host_is_downloaded_from():
    session = _Session()
    archive = cdr_archive.Archive("https://archive.test/api/cdr-database", "k", session)
    found = archive.search("03990000202")
    assert found["found"] and session.posted[0][1] == {"mobile": "03990000202"}
    assert session.posted[0][2]["X-API-KEY"] == "k" and "Mozilla" in session.posted[0][2]["User-Agent"]
    name, content = archive.download(found["records"][0]["download_url"])
    assert name == "archived.xlsx" and content[:2] == b"PK"
    with pytest.raises(ValueError):
        archive.download("https://elsewhere.test/file.xlsx")
    assert not cdr_archive.Archive("https://archive.test/api/cdr-database", "k", _Session(False)).search(B)["found"]
    line = cdr_archive.describe(found["records"][0], B)
    assert "CDR of 0399-0000101" in line and "a contact (B party)" in line and "no FIR" in line and "Officer One" in line


def _person(pid, name, seed=False, **data):
    return {"id": pid, "kind": "person", "label": name, "data": {"seed": seed, **data}}


def _tie(eid, a, b, label, system, sentence):
    return {"id": eid, "source": a, "target": b, "kind": "strong", "label": label, "system": system,
            "relation": {"sentence": sentence}}


def _archive_case(monkeypatch, tmp_path):
    from sherlocks.evidence import uploads
    from sherlocks.linkgraph.network import PersonNetwork

    case = CaseFile()
    session = _Session()
    real = cdr_archive.Archive
    monkeypatch.setattr(cdr_archive, "Archive", lambda *a, **k: real("https://archive.test/api/cdr-database", "k", session))
    read = []

    def fake_upload(agents, *, name, content, note, upload_dir, emit=None, owner=None):
        read.append((name, note))
        doc_id = case.add_document(kind="cdr", key=f"upload:{len(read)}", title=f"Upload: {name}", source="x", text="CDR")
        case.upload_status(name, "done", doc=doc_id, findings=3)
        return doc_id

    monkeypatch.setattr(uploads, "process_upload", fake_upload)
    graph = {"nodes": [
        _person("t1", "Target One", True, phones=[A, B], firs=[{"label": "5/26", "role": "Accused / suspect"}]),
        _person("k1", "Kin One", phones=[C]),                         # the target's brother: searched
        _person("w1", "Witness One", phones=[D]),                     # a witness of the target's FIR: never
        _person("o1", "SIP Officer One", phones=["03990000505"]),     # an officer on duty: never
    ], "edges": [
        _tie("e1", "t1", "k1", "Family member (brother)", "nadra", "Kin One is brother of Target One"),
        _tie("e2", "w1", "t1", "Witness in FIR 5/26", "psrms", "Witness One is a witness in FIR 5/26 against Target One"),
        _tie("e3", "o1", "t1", "Investigating officer of FIR 5/26", "psrms", "SIP Officer One investigates FIR 5/26"),
    ]}

    class Tools:
        def __init__(self):
            self.case, self.spent, self.live_left, self.net = case, [], 1, PersonNetwork(graph)
            self.agents = type("Agents", (), {"settings": type("S", (), {"output_path": tmp_path})()})()

        def _spend(self, system, identifier):
            if self.live_left <= 0:
                raise ValueError("No live calls left")
            self.live_left -= 1
            self.spent.append((system, identifier))

    return case, graph, Tools(), read


def test_a_number_found_in_the_archive_is_read_onto_the_board_once(monkeypatch, tmp_path):
    case, _, tools, read = _archive_case(monkeypatch, tmp_path)
    out = cdr_archive.fetch(tools, "0399-0000202")
    assert tools.spent == [("cdr_archive", B)] and out["found"]
    assert [f["status"] for f in out["files"]] == ["read"] and read[0][0] == "CDR 03990000101 - iCOP archive multi-6-0.xlsx"
    assert "the searched number 03990000202 is a contact (B party)" in read[0][1]
    doc = case.documents[out["files"][0]["doc"]]
    assert doc["source"] == "iCOP CDR archive" and doc["data"]["archive"]["party_type"] == "B Party"
    assert doc["owners"] == ["t1"]                                      # filed under the person whose number it is
    line = case.dialog["uploads"][read[0][0]]                          # the chat's line for the file
    assert line["archive"] and line["who"] == "Target One" and line["role"] == "target" and line["state"] == "done"
    tools.live_left = 1
    again = cdr_archive.fetch(tools, B)
    assert [f["status"] for f in again["files"]] == ["already on the board"] and len(read) == 1


def test_the_sweep_searches_the_targets_and_the_people_tied_to_them_never_officers_or_witnesses(monkeypatch, tmp_path):
    case, graph, tools, read = _archive_case(monkeypatch, tmp_path)
    assert [(x["number"], x["role"]) for x in cdr_archive.sweep_numbers(graph)] == [
        (A, "target"), (B, "target"), (C, "contact")]
    tools.live_left = 0                                                 # the sweep has an allowance of its own
    status = cdr_archive.sweep(case, graph, tools)
    assert [n for _, n in tools.spent] == [A, B, C]
    assert status["state"] == "done" and status["searched"] == 3 and (status["targets"], status["contacts"]) == (1, 1)
    assert [f["name"] for f in status["found"]] == ["Target One", "Target One", "Kin One"]
    assert len(read) == 1 and status["read"] == 1                       # one archived file, read once
    assert case.dialog["cdr_archive"]["state"] == "done"                # the chat's line
    assert (status["all_numbers"], status["all_targets"], status["all_contacts"]) == (3, 1, 1)   # every sweep together
    assert [f["name"] for f in status["all_found"]] == ["Target One", "Target One", "Kin One"]
    board = case.documents[status["doc"]]
    assert board["title"] == "Search of the iCOP CDR archive"
    assert "Kin One's number 03990000303 (tied to Target One)" in board["text"]
    assert any(f["kind"] == "cdr_archive" and f["pids"] == ["k1"] for f in case.facts.values())
    assert cdr_archive.sweep(case, graph, tools) is None               # every number searched already
    graph["nodes"][1]["data"]["phones"].append("03990000606")         # a new number joins the case
    assert [x["number"] for x in cdr_archive.to_search(case, graph)] == ["03990000606"]


def test_in_the_targets_own_fir_the_complainant_and_witnesses_are_searched():
    # The target was the victim / complainant (a person found dead): the family who filed and witnessed are his side.
    graph = {"nodes": [_person("t1", "Target One", True, phones=[A], firs=[{"label": "7/21", "role": "complainant"}]),
                       _person("f1", "Father One", phones=[B]), _person("o1", "ASI Officer Two", phones=[C])],
             "edges": [_tie("e1", "f1", "t1", "Witness in FIR 7/21", "psrms", "Father One is a witness in FIR 7/21"),
                       _tie("e2", "o1", "t1", "Witness in FIR 7/21", "psrms", "ASI Officer Two is a witness in FIR 7/21")]}
    assert [(x["number"], x["role"]) for x in cdr_archive.sweep_numbers(graph)] == [(A, "target"), (B, "contact")]


def test_archive_searches_have_limits_of_their_own():
    from types import SimpleNamespace

    from sherlocks.evidence.guard import GuardError, check_limits

    case = CaseFile()
    for _ in range(5):
        case.log_call({"system": "cdr_archive", "identifier": A, "status": "called"})
    cfg = SimpleNamespace(evidence=SimpleNamespace(case_live_calls=5, case_live_calls_per_hour=5,
                                                   archive_searches_per_case=6, archive_searches_per_hour=6))
    check_limits(case, cfg)                                            # NADRA and the rest: none used
    check_limits(case, cfg, "cdr_archive")
    case.log_call({"system": "cdr_archive", "identifier": A, "status": "called"})
    with pytest.raises(GuardError, match="cdr archive searches"):
        check_limits(case, cfg, "cdr_archive")


def test_a_number_in_no_record_is_caught_by_the_answer_checker():
    from sherlocks.linkgraph.network import PersonNetwork
    from sherlocks.linkgraph.sherlock_team import numbers_not_on_record

    case = CaseFile()
    case.add_document(kind="cdr", key="t", title="CDR", source="x", text=f"Top contact 1: {B} - 4 calls.")
    net = PersonNetwork({"nodes": [{"id": "p1", "kind": "person", "label": "T", "data": {"phones": [A]}}], "edges": []})
    given = {"sources": {"cdr_analysis": {"result": [{"number": C, "calls": 3}]}}}
    answer = f"He called 0399-0000202 most, then {C} and {A}; also 0300-7878787."
    assert numbers_not_on_record(answer, case, net, given) == ["03007878787"]


def test_a_number_the_officer_wrote_is_searched_and_an_old_search_is_done_again_when_asked(monkeypatch, tmp_path):
    case, graph, tools, _ = _archive_case(monkeypatch, tmp_path)
    case.officer_note("His friend Friend One uses 0399-0000707")
    numbers = [(x["number"], x["role"]) for x in cdr_archive.sweep_numbers(graph, case)]
    assert numbers[-1] == ("03990000707", "given") and ("03990000505", "given") not in numbers
    status = cdr_archive.sweep(case, graph, tools)
    assert "03990000707" in [n for _, n in tools.spent] and status["all_given"] == 1
    board = case.documents[status["doc"]]["text"]
    assert "the number 03990000707 (given by the officer)" in board
    assert cdr_archive.to_search(case, graph) == []
    assert cdr_archive.to_search(case, graph, again_after_h=6) == []             # searched just now
    for a in case.attempts:                                                      # ...and a week ago
        a["at"] = "2020-01-01T00:00:00+00:00"
    assert [x["number"] for x in cdr_archive.to_search(case, graph, again_after_h=6)] == [A, B, C, "03990000707"]


def test_a_search_asked_for_in_the_chat_is_written_on_the_board(monkeypatch, tmp_path):
    case, graph, tools, _ = _archive_case(monkeypatch, tmp_path)
    tools.graph = graph
    real = type(cdr_archive.Archive())               # the archive's client, here with an answer of "no record"
    monkeypatch.setattr(cdr_archive, "Archive", lambda *a, **k: real("https://archive.test/api/cdr-database", "k",
                                                                     _Session(False)))
    out = cdr_archive.fetch(tools, C)
    assert not out["found"]
    doc = case.doc_for(cdr_archive.BOARD_KEY)
    assert "No CDR in the iCOP CDR archive for Kin One's number(s) 03990000303" in doc["text"]
    assert any(f["kind"] == "cdr_archive" and f["pids"] == ["k1"] for f in case.facts.values())


def test_an_investigating_officer_of_the_targets_fir_is_never_searched():
    graph = {"nodes": [_person("t1", "Target One", True, phones=[A]), _person("i1", "Plain Name", phones=[B]),
                       _person("f1", "Father One", phones=[C])],
             "edges": [_tie("e1", "t1", "i1", "Investigating officer in FIR 9/21", "fir_roster", "Plain Name investigated"),
                       _tie("e2", "t1", "f1", "Witness in FIR 9/21", "fir_roster", "Father One is a witness"),
                       _tie("e3", "f1", "i1", "Investigating officer in FIR 9/21", "fir_roster", "Plain Name investigated")]}
    assert [(x["number"], x["role"]) for x in cdr_archive.sweep_numbers(graph)] == [(A, "target"), (C, "contact")]
