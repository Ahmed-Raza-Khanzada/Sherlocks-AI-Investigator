"""A CDR read for whose it is: the target's own CDR (his contacts, places and profile; his top contacts searched like a
target) or someone else's he is in (only their contact history; the subscriber searched). Made-up numbers only."""

from __future__ import annotations

import io
from datetime import datetime, timedelta

import pytest
from openpyxl import Workbook

from sherlocks.evidence import cdr_followup, cdr_profile
from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.network import PersonNetwork

T, A, B, C, S = "03990000111", "03990000222", "03990000333", "03990000444", "8484"


def _xlsx(subject: str, rows: list[tuple[str, str, datetime, int, str]], path) -> str:
    wb = Workbook()
    ws = wb.active
    ws.append(["CallType", "Aparty", "BParty", "Datetime", "Duration", "cellid", "Imsi", "Imei", "SiteLocation"])
    for kind, other, when, dur, site in rows:
        out = kind.startswith("Out")
        ws.append([kind, subject if out else other, other if out else subject, when, dur, site, "410010000000001",
                   "350000000000001", f"{site} Karachi"])
    buf = io.BytesIO()
    wb.save(buf)
    path.write_bytes(buf.getvalue())
    return str(path)


def _rows() -> list[tuple[str, str, datetime, int, str]]:
    day = datetime(2026, 1, 5, 10, 0)
    rows = []
    for i in range(12):                                              # A: most, by day, at the work tower
        rows.append(("Outgoing", A, day + timedelta(days=i % 4, hours=i % 3), 60 + i, "WORK1"))
    for i in range(5):                                               # B: at night, from the home tower
        rows.append(("InComing", B, day + timedelta(days=i, hours=14), 30, "HOME1"))
    rows.append(("Outgoing SMS", C, day + timedelta(days=2, hours=1), 0, "HOME1"))
    rows.append(("InComing SMS", S, day + timedelta(days=3), 0, "WORK1"))      # a service number
    return rows


def _case(tmp_path, subject: str, target_phone: str):
    case = CaseFile()
    path = _xlsx(subject, _rows(), tmp_path / f"{subject}.xlsx")
    doc = case.add_document(kind="cdr", key=f"t:{subject}", title=f"CDR {subject}", source="test", text="CDR",
                            data={"path": path, "analysis": {"subject": subject}})
    graph = {"nodes": [{"id": "p1", "kind": "person", "label": "Target One",
                        "data": {"seed": True, "phones": [target_phone]}}], "edges": []}
    return case, doc, graph


def test_the_targets_own_cdr_lists_every_contact_and_place_and_his_profile(tmp_path):
    case, doc, graph = _case(tmp_path, T, T)
    got = cdr_profile.write(case, doc, PersonNetwork(graph))
    assert got["mode"] == "own" and got["contacts"] == [A, B, C]          # most first; the service number not searched
    board = case.documents[got["doc"]]
    data = board["data"]
    assert [c["number"] for c in data["contacts"]][:3] == [A, B, C] and data["contacts"][-1]["service"]
    a = data["contacts"][0]
    assert (a["records"], a["calls_out"], a["calls_in"]) == (12, 12, 0) and a["where"] == "WORK1 Karachi"
    assert {p["place"] for p in data["places"]} == {"WORK1 Karachi", "HOME1 Karachi"}
    pro = data["profile"]
    assert pro["home"]["place"] == "HOME1 Karachi" and pro["work"]["place"] == "WORK1 Karachi"
    assert "Every unique contact of 03990000111:" in board["text"] and "Every unique place 03990000111 used:" in board["text"]
    assert any("Likely home area" in case.facts[f]["statement"] for f in board["facts"])
    assert cdr_profile.write(case, doc, PersonNetwork(graph)) is None                        # once


def test_a_cdr_the_target_is_only_a_contact_in_gives_only_their_contact_history(tmp_path):
    case, doc, graph = _case(tmp_path, A, B)                              # B is the target, in A's CDR
    rows = [(k, o, w, d, s) for k, o, w, d, s in _rows()]
    _xlsx(A, [(k, (T if o == A else o), w, d, s) for k, o, w, d, s in rows], tmp_path / f"{A}.xlsx")
    got = cdr_profile.write(case, doc, PersonNetwork(graph))
    assert got["mode"] == "contact" and got["contacts"] == [A]            # only the subscriber is searched
    data = case.documents[got["doc"]]["data"]
    h = data["histories"][0]
    assert h["other"] == B and h["records"] == 5 and h["to_subject"] == 5 and len(h["events"]) == 5
    assert "contacts" not in data                                        # nothing about A's other contacts


def test_the_follow_up_searches_the_top_contacts_once_and_the_first_few_in_the_archive(tmp_path, monkeypatch):
    from types import SimpleNamespace

    case, _, graph = _case(tmp_path, T, T)
    searched, fetched = [], []
    monkeypatch.setattr("sherlocks.evidence.cdr_archive.fetch",
                        lambda tools, number, **_: fetched.append(number) or {"files": []})

    def contacts(people, after=None):
        searched.extend(people)
        for i in range(len(people)):
            after(i + 1, len(people))
        return len(people)

    settings = SimpleNamespace(evidence=SimpleNamespace(cdr_contacts_searched=2, cdr_target_cdrs_per_case=1,
                                                        cdr_contacts_in_archive=1))
    tools = lambda: SimpleNamespace(live_left=0)
    done = cdr_followup.follow(case, graph, contacts=contacts, tools=tools, settings=settings)
    assert done and _wait(lambda: case.dialog.get("cdr_contacts", {}).get("state") == "done")
    assert [p["number"] for p in searched] == [A, B] and searched[0]["of"] == "p1"
    assert searched[0]["label"].startswith("In phone contact 12 times")
    assert fetched == [A]
    assert any("searching 2 number(s) like a target" in f["text"] for f in case.followups)
    # A second CDR of the target over the limit: its contacts are listed, not searched.
    path = _xlsx(T, _rows(), tmp_path / "second.xlsx")
    case.add_document(kind="cdr", key="t:2", title="CDR 2", source="test", text="CDR", data={"path": path, "analysis": {"subject": T}})
    searched.clear()
    done = cdr_followup.follow(case, graph, contacts=contacts, tools=tools, settings=settings)
    assert any("already searched" in d for d in done) and searched == []


def _wait(cond, seconds: float = 5.0) -> bool:
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_a_saved_graph_is_restored_to_search_more_people_into_it():
    from sherlocks.linkgraph.graph import GraphBuilder
    from sherlocks.linkgraph.models import PersonRef

    b = GraphBuilder()
    p, _ = b.upsert_person(PersonRef(name="Target One", cnic="9999900000011", phones=[T]), depth=0, seed=True)
    q, _ = b.upsert_person(PersonRef(name="Friend One", phones=[A]), depth=1)
    b.add_direct_strong(p, q, label="Friend", reasons=["test"], system="test")
    again = GraphBuilder.restore(b.snapshot())
    assert again.snapshot()["nodes"] == b.snapshot()["nodes"] and len(again.edges) == 1
    assert again.find_person(PersonRef(cnic="9999900000011")) == p and again.find_person(PersonRef(phones=[A])) == q
    new, created = again.upsert_person(PersonRef(phones=[B]), depth=1)
    assert created and new not in (p, q)


def test_numbers_a_file_came_with_are_not_taken_as_the_officers():
    from sherlocks.evidence.cdr_archive import officer_numbers

    case = CaseFile()
    case.officer_note(f"About CDR {A}.xlsx: From the iCOP CDR archive: CDR of {A}; the searched number {T}", source="upload")
    case.officer_note(f"his friend uses {B}")
    assert officer_numbers(case) == [B]


def test_a_number_tied_to_the_target_only_by_a_cdr_is_not_swept_into_the_archive():
    from sherlocks.evidence.cdr_archive import sweep_numbers

    graph = {"nodes": [{"id": "p1", "kind": "person", "label": "Target One", "data": {"seed": True, "phones": [T]}},
                       {"id": "p2", "kind": "person", "label": "Brother One", "data": {"phones": [A]}},
                       {"id": "p3", "kind": "person", "label": "Caller One", "data": {"phones": [B]}}],
             "edges": [{"id": "e1", "source": "p1", "target": "p2", "kind": "strong", "label": "Family member (brother)",
                        "system": "nadra"},
                       {"id": "e2", "source": "p1", "target": "p3", "kind": "strong",
                        "label": "In phone contact 9 times (CDR of 03990000111)", "system": "cdr_contact"}]}
    assert [x["number"] for x in sweep_numbers(graph)] == [T, A]


@pytest.fixture
def manager():
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings
    from tests.test_linkgraph_pipeline import _ensure_on_path

    s = load_settings()
    _ensure_on_path(s.app.report_app_src)
    pytest.importorskip("cdr_report_app.integrations.providers")
    s.ollama.enabled = s.llm.enabled = s.osint.enabled = False
    s.api.api_key = None
    s.linkgraph.backend = "demo"
    return memory_manager(s)


def test_a_contact_is_searched_like_a_target_into_a_finished_runs_graph(manager):
    from sherlocks.linkgraph.demo_data import PEOPLE
    from sherlocks.linkgraph.models import GraphRunParams

    handle = manager.start(GraphRunParams(backend="demo", cnic=PEOPLE["kamran"].cnic, depth=1), wait=True)
    run = manager.get(handle.id)
    seed = next(n["id"] for n in run["graph"]["nodes"] if n["kind"] == "person" and n["data"].get("seed"))
    asif = PEOPLE["asif"]                                                 # two hops away: not on the graph
    assert asif.cnic not in {n["data"].get("cnic") for n in run["graph"]["nodes"]}
    manager._runs.pop(handle.id)                                          # finished and out of memory: restored
    n = manager.expand_contacts(handle.id, handle.case, [{"number": asif.phones[0], "of": seed, "rank": 1,
                                                          "label": "In phone contact 7 times (CDR of 03990000101)"}])
    graph = manager.get(handle.id)["graph"]
    found = next(x for x in graph["nodes"] if x["kind"] == "person" and x["data"].get("cnic") == asif.cnic)
    assert n == 1 and found["data"]["search_status"] == "searched"
    assert any(e["system"] == "cdr_contact" and {e["source"], e["target"]} == {seed, found["id"]} for e in graph["edges"])
    assert any(c["system"] == "cdr_contact_search" for c in handle.case.calls)           # in the audit log


def test_a_finished_run_tells_a_viewer_cheaply_when_its_board_or_graph_changed(manager):
    from sherlocks.linkgraph.demo_data import PEOPLE
    from sherlocks.linkgraph.models import GraphRunParams

    handle = manager.start(GraphRunParams(backend="demo", cnic=PEOPLE["kamran"].cnic, depth=1), wait=True)
    run = manager.get(handle.id)
    seed = next(n["id"] for n in run["graph"]["nodes"] if n["kind"] == "person" and n["data"].get("seed"))
    manager._runs.pop(handle.id)                                          # finished and out of memory
    before = manager.changes(handle.id)
    assert before["graph"] == run["graph"]["version"] and before["busy"] is False
    case = manager.board(handle.id)
    case.plan("sherlock", "a new upload")                                 # the team at work: poll often
    assert manager.changes(handle.id)["busy"] is True and manager.changes(handle.id)["case"] > before["case"]
    case.done("sherlock")
    manager.expand_contacts(handle.id, case, [{"number": PEOPLE["asif"].phones[0], "of": seed, "rank": 1,
                                               "label": "In phone contact 7 times (CDR of 03990000101)"}])
    after = manager.changes(handle.id)
    assert after["graph"] > before["graph"] and after["graph"] == manager.get(handle.id)["graph"]["version"]
    assert manager.changes("no-such-run") is None
