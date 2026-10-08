"""The knowledge base: any question about a person is answered from what is known about
them - and a topic nothing records (his job) is said to be missing, never filled with
lines about someone else."""

from __future__ import annotations

from sherlocks.linkgraph.knowledge import KnowledgeBase


def _graph():
    def person(pid, label, **data):
        return {"id": pid, "kind": "person", "label": label, "data": data}

    def record(nid, owner, system, fields):
        return {"id": nid, "kind": "system", "label": system,
                "data": {"owner": owner, "system": system, "fields": [{"label": k, "value": v} for k, v in fields]}}

    return {"nodes": [
        person("p1", "Muhammad Danish Rafiq", cnic="9999900000777", father_name="Rafiq Anwar", seed=True,
               addresses=["Flat 4 Block 2 Sample Terrace"], phones=["03990000777"]),
        person("p2", "Tariq Mehmood", cnic="4210100000001", addresses=["House 5 Gulshan"]),
        person("p3", "محمد نبی ولد", cnic="4210100000002"),
        record("s1", "p1", "dls", [("Name", "MUHAMMAD AQIB SHAHID"), ("Address", "Fl No 23 Block 7 Sample Terrace")]),
        record("s2", "p2", "prvs", [("Employer", "Karachi Port Trust"), ("Address", "House 5 Gulshan")]),
        record("s3", "p2", "cro", [("Current posting", "PS Saddar")]),
    ], "edges": []}


def _kb():
    from sherlocks.linkgraph.network import PersonNetwork

    graph = _graph()
    return KnowledgeBase.build(graph, None, PersonNetwork(graph))


def test_a_persons_address_comes_from_their_records():
    hits = _kb().search("danish kahan rehta hai?", people=["p1"])
    assert hits and all("p1" in e.people for e in hits)
    assert any("Sample Terrace" in e.text for e in hits)


def test_a_topic_no_record_holds_returns_nothing_not_someone_else():
    kb = _kb()
    # Tariq's employer is known; Danish's is not - his answer must not borrow Tariq's.
    assert kb.search("danish ka kaam kya hai?", people=["p1"]) == []
    assert kb.topics("danish ka kaam kya hai?") == ["work"]
    hits = kb.search("masood ka kaam kya hai?", people=["p2"])
    assert hits and "Karachi Port Trust" in hits[0].text


def test_the_father_field_beats_a_name_that_contains_walad():
    hits = _kb().search("danish ke walid ka naam?", people=["p1"])
    assert hits and all("Rafiq Anwar" in e.text for e in hits)


def test_an_unknown_job_is_said_to_be_missing():
    from sherlocks.evidence.case_file import CaseFile
    from sherlocks.linkgraph.sherlock_team import run_turn

    graph = _graph()
    case = CaseFile()
    case.dialog["focus"] = ["p1"]
    final = list(run_turn(graph, "danish ka kaam kya hai?", case=case))[-1]
    assert "Muhammad Danish Rafiq" in final["answer"]
    assert "Karachi Port Trust" not in final["answer"]
    assert "nahi" in final["answer"]


def test_the_research_agent_calls_the_systems_when_nothing_is_known():
    """"danish ka kaam kya hai?" with no job on record: the Research agent looks him up in the
    employment systems (within the budget) and answers from what comes back."""
    from sherlocks.evidence.case_file import CaseFile
    from sherlocks.linkgraph.sherlock_team import run_turn

    class Backend:
        def __init__(self):
            self.asked = []

        def lookup(self, system, cnic, phone):
            self.asked.append(system)
            if system == "evs":
                return {"status": "found", "hit": True, "summary": "EVS record",
                        "data": {"name": "Muhammad Danish Rafiq", "cnic": "9999900000777",
                                 "organisation": "Karachi Port Trust", "designation": "Clerk"}}
            return {"status": "not_found", "hit": False, "summary": "no record", "data": {}}

    backend, case = Backend(), CaseFile()
    case.dialog["focus"] = ["p1"]
    final = list(run_turn(_graph(), "danish ka kaam kya hai?", case=case, backend=backend, live_calls=2))[-1]
    assert backend.asked == ["evs", "hope"]                       # work systems, at most the budget
    assert "Karachi Port Trust" in final["answer"] and "Clerk" in final["answer"]
    assert "maine abhi check kiya" in final["answer"]
    # Without a budget nothing is called and the gap is said plainly.
    quiet = Backend()
    final = list(run_turn(_graph(), "danish ka kaam kya hai?", case=case, backend=quiet, live_calls=0))[-1]
    assert quiet.asked == [] and "kuch nahi mila" in final["answer"]
