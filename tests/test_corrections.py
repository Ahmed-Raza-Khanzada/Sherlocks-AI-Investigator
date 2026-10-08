"""Corrections: the new statement replaces the old (kept), the ledger updates, entries
built on the old value go stale and their redo is planned, and the officer is told."""

from __future__ import annotations

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.sherlock_team import run_turn


def _graph():
    return {"nodes": [{"id": "a", "kind": "person", "label": "Kamran Ahmed", "data": {"seed": True, "phones": ["03990000101"]}},
                      {"id": "b", "kind": "person", "label": "Sajid Mehmood", "data": {"seed": True, "phones": ["03990000201"]}}],
            "edges": []}


def _last(case, graph, message):
    return list(run_turn(graph, message, case=case))[-1]


def test_a_marked_correction_replaces_marks_stale_and_plans_the_redo():
    case, graph = CaseFile(), _graph()
    _last(case, graph, "Waqia 01-03-2023 ko hua")
    assert case.incident["date"] == "2023-03-01"
    old = case.statement_for("incident:date")
    # A CDR finding computed for the old date.
    doc = case.add_document(kind="cdr", key="upload:x", title="Upload: kamran.xlsx", source="Uploaded by the officer",
                            text="Incident day 2023-03-01: 12 record(s).")
    near = case.add_fact(doc, "Incident day 2023-03-01: 12 record(s).", "Incident day 2023-03-01: 12 record(s).",
                         by="cdr", rests_on=["incident:date"])
    reply = _last(case, graph, "nahi, waqia 02-03-2023 ko hua tha")
    assert case.incident["date"] == "2023-03-02"
    new = case.statement_for("incident:date")
    assert new["id"] != old["id"] and case.facts[old["id"]]["replaced_by"] == new["id"]
    assert case.facts[near]["stale"] and near not in {f["id"] for f in case.live_facts()}
    assert {"cdr:location", "sherlock"} <= {a["task"] for a in case.agenda}
    assert "Durust kar diya" in reply["answer"] and "2023-03-02" in reply["answer"]
    assert new["agent"] == "O3 Statement recorder" and new["event"]          # one event for the whole correction


def test_an_unmarked_change_is_confirmed_first():
    case, graph = CaseFile(), _graph()
    _last(case, graph, "Waqia 01-03-2023 ko hua")
    ask = _last(case, graph, "waqia 05-03-2023 ko hua")
    assert case.incident["date"] == "2023-03-01"                      # not changed yet
    assert "Pehle aap ne 2023-03-01 bataya tha. 2023-03-05 kar dun?" in ask["answer"]
    _last(case, graph, "haan")
    assert case.incident["date"] == "2023-03-05" and "pending_correction" not in case.dialog


def test_no_keeps_the_earlier_value():
    case, graph = CaseFile(), _graph()
    _last(case, graph, "Waqia 01-03-2023 ko hua")
    _last(case, graph, "waqia 05-03-2023 ko hua")
    _last(case, graph, "nahi")
    assert case.incident["date"] == "2023-03-01" and "pending_correction" not in case.dialog


def test_a_role_moves_from_one_person_to_another():
    case, graph = CaseFile(), _graph()
    _last(case, graph, "Kamran main suspect hai")
    assert case.roles == {"a": "main suspect"}
    reply = _last(case, graph, "Kamran nahi, Sajid main suspect hai")
    assert case.roles == {"b": "main suspect"}
    assert "dossiers" in {a["task"] for a in case.agenda}
    assert "Sajid Mehmood = main suspect" in reply["answer"]
