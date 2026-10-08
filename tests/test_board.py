"""The case board foundation: entries know who wrote them and what they rest on, a batch
wakes listeners once, corrections replace without deleting, the board is stored with a
version guard and shared by every caller."""

from __future__ import annotations

from sherlocks.evidence.board_store import MemoryBoardStore, merge_posted
from sherlocks.evidence.case_file import TRUST_OFFICER, TRUST_SYSTEM, CaseFile


def _case_with_doc(kind: str = "fir") -> tuple[CaseFile, str]:
    case = CaseFile()
    doc = case.add_document(kind=kind, key=f"{kind}:1", title="FIR 45/2023", source="PSRMS",
                            text="The accused fired with a pistol on 01-03-2023 near Rashid Minhas Road.")
    return case, doc


def test_entries_record_agent_event_tier_and_trust():
    case, doc = _case_with_doc("cdr")
    with case.batch("R6 Fact writer", "cdr") as event:
        fid = case.add_fact(doc, "Fired with a pistol", "fired with a pistol", by="cdr", tier="fact",
                            rests_on=["incident:date"], rows="12-14")
    fact = case.facts[fid]
    assert fact["agent"] == "R6 Fact writer" and fact["event"] == event
    assert fact["trust"] == TRUST_SYSTEM and fact["tier"] == "fact" and fact["rows"] == "12-14"
    notes, line = case.officer_note("Waqia 1 March ko hua")
    officer = case.add_fact(notes, "Stated by the officer: 1 March", line, by="officer", topic="incident:date")
    assert case.facts[officer]["trust"] == TRUST_OFFICER
    assert case.statement_for("incident:date")["id"] == officer


def test_a_batch_wakes_listeners_once_and_names_its_agent():
    case, doc = _case_with_doc()
    heard = []
    case.listen(heard.append)
    with case.batch("A2 Document reader", "document"):
        for i in range(30):
            case.add_fact(doc, f"Finding {i}", "fired with a pistol", check_quote=False)
    assert len(heard) == 1 and heard[0]["entry"] == "batch" and heard[0]["count"] == 30
    assert heard[0]["agent"] == "A2 Document reader" and heard[0]["kind"] == "document"
    case.set_role("p1", "suspect")                     # outside a batch: heard at once
    assert heard[-1]["entry"] == "role" and len(heard) == 2


def test_corrections_replace_and_mark_what_rests_on_them_stale():
    case, doc = _case_with_doc()
    notes, line = case.officer_note("Waqia 1 March ko hua")
    old = case.add_fact(notes, "Incident on 1 March", line, by="officer", topic="incident:date")
    near = case.add_fact(doc, "Phone near the scene on 1 March", "fired with a pistol", rests_on=[old, "incident:date"])
    hyp = case.set_hypothesis("Kamran was near the scene", support=[near])
    notes, line2 = case.officer_note("Nahi, 2 March tha")
    new = case.add_fact(notes, "Incident on 2 March", line2, by="officer", topic="incident:date", replaces=old)
    assert case.facts[old]["replaced_by"] == new and old in case.facts       # kept, not deleted
    stale = case.mark_stale(case.dependents([old, "incident:date"]), "incident date corrected")
    assert near in stale and hyp in stale
    assert near not in {f["id"] for f in case.live_facts()} and not case.usable(near)
    # Redone: the same finding written again comes back to life.
    assert case.add_fact(doc, "Phone near the scene on 1 March", "fired with a pistol") == near
    assert case.usable(near)


def test_hypothesis_history_keeps_what_changed_it():
    case = CaseFile()
    hid = case.set_hypothesis("Kamran and Sajid acted together", status="open", confidence="low")
    case.set_hypothesis("Kamran and Sajid acted together", status="supported", confidence="medium", cause="CDR D9")
    assert [h["change"] for h in case.history] == ["new", "updated"]
    assert case.history[-1]["before"] == "open (low)" and case.history[-1]["cause"] == "CDR D9"
    assert hid in case.known_ids() and case.cite(hid)["type"] == "hypothesis"


def test_round_trip_keeps_the_sherlock_team_work():
    case = CaseFile()
    case.run_id = "run-1"
    case.set_hypothesis("H statement")
    case.set_summary({"text": "So far"})
    case.add_view("is he involved?", "Records mein nahi; andaza: haan", turn=2)
    case.add_followup("Sherlock has more", turn=2)
    case.log_call({"system": "NADRA", "identifier": "4210..", "officer": "admin"})
    case.plan("sherlock", "new CDR")
    again = CaseFile.from_dict(case.to_dict())
    assert again.run_id == "run-1" and list(again.hypotheses) == ["H1"] and again.summary["text"] == "So far"
    assert again.views[0]["turn"] == 2 and again.followups and again.calls and again.agenda[0]["task"] == "sherlock"


def test_store_refuses_an_older_board_and_merges_officer_edits():
    store = MemoryBoardStore()
    newer = CaseFile()
    newer.set_incident({"place": "Saddar"})
    newer.set_incident({"date": "2023-03-01"})
    assert store.save("r", newer.to_dict())
    older = CaseFile()
    older.set_role("p1", "main suspect")
    assert older.version < newer.version and not store.save("r", older.to_dict())
    merged = merge_posted(newer, {**older.to_dict(), "incident": {"place": "Elsewhere", "fir": "45/2023"}})
    assert newer.roles == {"p1": "main suspect"} and newer.incident["place"] == "Saddar"
    assert newer.incident["fir"] == "45/2023" and "incident fir" in merged


def test_every_caller_shares_one_board_after_the_run_leaves_memory():
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    mgr = memory_manager(s)
    handle = mgr.start(GraphRunParams(cnic="9999900000011", depth=1, max_persons=5, backend="demo"), wait=True)
    run_id = handle.id
    mgr._runs.pop(run_id)                                       # evicted
    a = mgr.board(run_id)
    b = mgr.case_of(mgr.graph_for(run_id), run_id)
    assert a is b
    a.set_role("p1", "victim")
    mgr.save_board(run_id, a)
    assert mgr.boards.load(run_id)["roles"] == {"p1": "victim"}
    # A stale copy posted back by the portal does not replace it.
    stale = {**mgr.boards.load(run_id), "version": 1, "roles": {}}
    assert mgr.case_of({"nodes": [], "edges": [], "case": stale}) is a


# -- safety and audit -------------------------------------------------------------------


def _tools(case, officer=None):
    from sherlocks.linkgraph.investigator import _Tools

    class _Backend:
        name = "stub"

        def lookup(self, system, cnic, phone):
            return {"status": "hit", "summary": f"{system} answered"}

    graph = {"nodes": [{"id": "a", "kind": "person", "label": "Kamran Ahmed",
                        "data": {"seed": True, "cnic": "4210112345671", "phones": ["03990000101"]}}], "edges": []}
    return _Tools(graph, case, None, _Backend(), live_calls=5, officer=officer, team="O1 Officer agent", reason="test")


def test_lookups_only_for_people_on_the_case_and_every_call_logged():
    import pytest

    from sherlocks.evidence.guard import GuardError, NeedsConfirmation

    case = CaseFile()
    case.run_id = "r1"
    tools = _tools(case, {"user": "si.fahim", "systems": None})
    assert tools.lookup({"system": "nadra", "who": "Kamran Ahmed"})["status"] == "hit"
    # A number that turns up only in a document waits for the officer.
    with pytest.raises(NeedsConfirmation):
        tools.lookup({"system": "simsdb", "phone": "03001234567"})
    assert case.dialog["unconfirmed_ids"] == ["03001234567"]
    # The officer wrote it in the chat: now it may be looked up.
    case.add_turn("is 0300-1234567 ka record nikalo", "ok")
    assert tools.lookup({"system": "simsdb", "phone": "03001234567"})["status"] == "hit"
    assert [c["status"] for c in case.calls] == ["called", "refused", "called"]
    assert {c["officer"] for c in case.calls} == {"si.fahim"} and case.calls[0]["agent"] == "O1 Officer agent"
    # The officer's account may not use a system: refused, logged.
    limited = _tools(case, {"user": "asi.x", "systems": ["simsdb"]})
    with pytest.raises(GuardError):
        limited.lookup({"system": "nadra", "who": "Kamran Ahmed"})
    assert case.calls[-1]["status"] == "refused"


def test_case_limit_stops_calls(settings):
    import pytest

    from sherlocks.evidence.guard import GuardError, check_limits

    case = CaseFile()
    settings.evidence.case_live_calls = 2
    for _ in range(2):
        case.log_call({"system": "nadra", "status": "called"})
    with pytest.raises(GuardError):
        check_limits(case, settings)


def test_token_carries_the_officers_systems(settings):
    from sherlocks.api.auth import SessionAuth

    settings.auth.secret = "s3cret"
    settings.auth.password = "pw"
    auth = SessionAuth(settings)
    token = auth.issue("si.fahim", systems=["NADRA", "simsdb"])["token"]
    claims = auth.claims(token)
    assert claims["u"] == "si.fahim" and claims["sys"] == ["nadra", "simsdb"]
    assert auth.verify(auth.issue("x")["token"]) == "x" and "sys" not in auth.claims(auth.issue("x")["token"])
