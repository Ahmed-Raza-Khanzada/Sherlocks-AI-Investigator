"""A1 Graph analyst: co-accused links between people accused in the same FIR."""

from __future__ import annotations

from sherlocks.evidence.case_file import CaseFile
from sherlocks.evidence.graph_analyst import link_co_accused


class _Builder:
    def __init__(self, nodes, edges=()):
        self.nodes, self.edges, self.added = nodes, list(edges), []

    def snapshot(self):
        return {"nodes": self.nodes, "edges": self.edges}

    def add_direct_strong(self, a, b, *, label, reasons, system):
        self.added.append((a, b, label, reasons))
        self.edges.append({"source": a, "target": b, "label": label})


def _person(pid, name, firs, cnic=None):
    return {"id": pid, "kind": "person", "label": name, "data": {"firs": firs, "cnic": cnic, "phones": []}}


def test_two_people_accused_in_one_fir_are_linked_once():
    nodes = [_person("a", "Kamran", [{"label": "45/2023", "role": "Accused", "system": "cro"}]),
             _person("b", "Sajid", [{"label": "45/23", "role": "accused", "system": "psrms"}]),
             _person("c", "Waqas", [{"label": "45/2023", "role": "Complainant", "system": "psrms"}])]
    builder = _Builder(nodes)
    assert link_co_accused(builder) == [("a", "b", "45/2023")]
    assert builder.added[0][2] == "Co-accused in FIR 45/2023" and "CRO" in builder.added[0][3][0]
    assert link_co_accused(builder) == []                               # already linked: not again


def test_a_roster_link_for_the_fir_is_kept_and_the_fir_file_counts():
    nodes = [_person("a", "Kamran", [], cnic="4210112345671"), _person("b", "Sajid", [], cnic="4210176543211")]
    case = CaseFile()
    case.add_document(kind="fir", key="fir:45/23/501", title="FIR 45/2023", source="PSRMS", text="x",
                      ref={"fir_no": "45", "fir_year": "2023", "ps_id": "501"},
                      data={"nominated_suspects": [{"name": "Kamran", "cnic": "42101-1234567-1"},
                                                   {"name": "Sajid", "cnic": "42101-7654321-1"}]})
    assert link_co_accused(_Builder(nodes), case) == [("a", "b", "45/2023")]
    roster = [{"source": "a", "target": "b", "label": "Co-accused (nominated) in FIR 45/2023"}]
    assert link_co_accused(_Builder(nodes, roster), case) == []
