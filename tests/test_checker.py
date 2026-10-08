"""The Answer checker and the order of trust."""

from __future__ import annotations

from sherlocks.evidence.case_file import CaseFile
from sherlocks.evidence.trust import conflict_lines, conflicts, leader
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.linkgraph.sherlock_team import drop_quotes, unverified_quotes, validate


def _graph():
    return {"nodes": [{"id": "a", "kind": "person", "label": "Kamran Ahmed",
                       "data": {"seed": True, "cnic": "4210112345671", "phones": ["03990000101"]}}], "edges": []}


def _case():
    case = CaseFile()
    doc = case.add_document(kind="fir", key="fir:45/23/7", title="FIR 45/2023 · PS Gulberg", source="PSRMS FIR file",
                            text="ملزم نے پستول سے فائر کیا۔ The accused fired with a pistol.",
                            ref={"fir_no": "45", "fir_year": "2023", "ps_id": "7"},
                            people=[{"role": "nominated_suspects", "name": "Kamran", "cnic": "4210112345671"}],
                            data={"occurred": "02-03-2023 22:00", "place": "Gulberg"})
    fact = case.add_fact(doc, "The accused fired with a pistol", "The accused fired with a pistol")
    return case, doc, fact


def test_real_quotes_pass_invented_quotes_are_dropped():
    case, doc, fact = _case()
    good = f'The FIR says "The accused fired with a pistol" [{doc}].'
    assert unverified_quotes(good, case) == []
    bad = f'The FIR says "he confessed to the murder at once" [{fact}].'
    assert unverified_quotes(bad, case) == ["he confessed to the murder at once"]
    assert "quote removed" in drop_quotes(bad, unverified_quotes(bad, case))


def test_checker_rules():
    case, _doc, fact = _case()
    net = PersonNetwork(_graph())
    # Answered what was asked: a count question must open with the count.
    final = {"answer": "Kamran has a long history. He is in 3 FIRs.", "intent": "question",
             "query": {"type": "count_cases"}, "facts": {"count": 3, "empty": False}}
    check = validate(case, net, final)
    assert not check["ok"] and check["rules"]["answered"] is False
    assert validate(case, net, {**final, "answer": "Kamran is in 3 FIRs [F1]."})["ok"]
    # Honest about gaps: nothing found, and the reply must say so.
    empty = {"answer": "He works at a bank.", "intent": "question", "query": {"type": "knowledge"},
             "facts": {"count": 0, "empty": True}}
    assert validate(case, net, empty)["rules"]["honest"] is False
    assert validate(case, net, {**empty, "answer": "No record of his employer was found."})["ok"]
    # A source replaced by a correction no longer counts.
    case.mark_stale([fact], "corrected")
    assert validate(case, net, {"answer": f"He fired [{fact}]."})["rules"]["sourced"] is False


def test_order_of_trust_and_officer_leads_on_the_incident():
    values = [{"value": "2023-03-02", "trust": 2, "source": "FIR"}, {"value": "2023-03-01", "trust": 3, "source": "officer"}]
    assert leader(values, "incident:date")[0]["source"] == "officer"            # exception: officer leads
    assert leader(values, "vehicle")[0]["source"] == "FIR"                      # otherwise the document leads
    sys_vs_doc = [{"value": "x", "trust": 2, "source": "doc"}, {"value": "y", "trust": 1, "source": "NADRA"}]
    assert leader(sys_vs_doc, "identity")[0]["source"] == "NADRA"


def test_conflicts_are_found_and_shown_not_hidden():
    case, doc, _ = _case()
    net = PersonNetwork(_graph())
    case.set_incident({"date": "2023-03-01", "fir": "45/2023"})
    case.set_role("a", "victim")
    found = conflicts(case, net)
    topics = {c["topic"] for c in found}
    assert {"incident:date", "role:a"} <= topics
    date = next(c for c in found if c["topic"] == "incident:date")
    assert date["values"][0]["source"] == "officer" and date["values"][1]["ref"] == doc
    line = conflict_lines([date], "roman")[0]
    assert "aap" in line and "2023-03-02" in line and doc in line
