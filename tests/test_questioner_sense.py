"""The Questioner never asks what the records or the officer already said. Made-up people."""

from sherlocks.evidence.case_file import CaseFile
from sherlocks.evidence.questioner import gaps
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.team import recorder


def _person(pid, name, seed=False, **data):
    return {"id": pid, "kind": "person", "label": name, "data": {"seed": seed, **data}}


def _edge(eid, a, b, label):
    return {"id": eid, "source": a, "target": b, "kind": "strong", "label": label, "system": "psrms"}


def _gang():
    return {"nodes": [
        _person("t1", "Kamran Aslam", True), _person("t2", "Imran Rashid", True), _person("t3", "Naveed Rafiq", True),
        _person("c1", "Bilal Khan"),
        {"id": "s:fir:1", "kind": "system", "label": "FIR 5/26", "data": {"system": "fir_roster"}},
    ], "edges": [
        _edge("e1", "s:fir:1", "t1", "Accused in FIR 5/26"), _edge("e2", "s:fir:1", "t2", "Accused in FIR 5/26"),
        _edge("e3", "c1", "t1", "Complainant against the subject in FIR 5/26"),
    ]}


def _keys(case, graph):
    return {g["key"]: g for g in gaps(case, graph)}


def test_a_pair_the_records_tie_is_never_asked_about_and_the_targets_are_never_offered_as_the_victim():
    case, graph = CaseFile(), _gang()
    asked = _keys(case, graph)
    # t1 and t2 are co-accused in one FIR: the question is about a pair the records do not tie.
    assert "Kamran Aslam and Naveed Rafiq" in asked["link:why"]["text"]
    assert "records show no tie" in asked["link:why"]["text"]
    victim = [o["id"] for o in asked["roles:victim"]["options"]]
    assert victim == ["c1", "other"]                     # the complainant on record; never a target
    graph["edges"].append(_edge("e4", "t3", "t1", "Brother"))
    graph["edges"].append(_edge("e5", "t3", "t2", "Same phone 03990000101"))
    assert "link:why" not in _keys(case, graph)          # every pair tied by the records


def test_the_victim_is_not_asked_when_the_incidents_fir_file_names_the_complainant():
    case, graph = CaseFile(), _gang()
    case.set_incident({"fir": "5/26"})
    case.add_document(kind="fir", key="fir:5/26", title="FIR 5/26", source="PSRMS FIR file", text="FIR",
                      ref={"fir_no": "5", "fir_year": "2026"}, data={"complainant": {"name": "Bilal Khan"}})
    assert "roles:victim" not in _keys(case, graph)


def test_an_officer_who_says_the_targets_are_one_gang_is_not_asked_who_the_main_suspect_is():
    case, graph = CaseFile(), _gang()
    assert "roles:main" in _keys(case, graph)
    said = recorder._Record(read_back="Noted.", all_targets="yes")
    out = recorder.write(case, PersonNetwork(graph), "these targets are one gang, all of them did it", said,
                         language="en")
    assert {p: case.roles.get(p) for p in ("t1", "t2", "t3")} == {"t1": "suspect", "t2": "suspect", "t3": "suspect"}
    assert any("acting together" in line for line in out["recorded"])
    asked = _keys(case, graph)
    assert "roles:main" not in asked and "link:why" not in asked
    assert all(o["id"] not in ("t1", "t2", "t3") for o in asked["roles:victim"]["options"])


def test_the_targets_together_check_reads_the_models_last_answer_line():
    class Model:
        def __init__(self, text):
            self.text = text

        def generate_text(self, **_):
            return self.text

    targets = ["Kamran Aslam", "Imran Rashid"]
    assert recorder.targets_together("they are one gang", targets, Model("They act as one.\nANSWER: yes"))
    assert not recorder.targets_together("Kamran is the main one", targets, Model("ANSWER: yes?\nANSWER: no"))
    assert not recorder.targets_together("x", targets, Model("no answer line"))
