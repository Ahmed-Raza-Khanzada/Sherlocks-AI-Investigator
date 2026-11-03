"""P12 Group writer: several targets searched together - how the records tie them. Made-up people."""

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.team import case_scope, group_view


def _person(pid, name, seed=False, **data):
    return {"id": pid, "kind": "person", "label": name, "data": {"seed": seed, **data}}


def _tie(eid, a, b, label, system, sentence, kind="strong"):
    return {"id": eid, "source": a, "target": b, "kind": kind, "label": label, "system": system,
            "relation": {"sentence": sentence}}


def _gang():
    graph = {"nodes": [
        _person("t1", "Kamran Aslam", True, cnic="9999900000011", phones=["03990000101"], addresses=["House 4, Lyari"]),
        _person("t2", "Imran Rashid", True, cnic="9999900000022", phones=["03990000201"], addresses=["Malir"]),
        _person("t3", "Naveed Rafiq", True, cnic="9999900000033", phones=["03990000301"],
                addresses=["Chak Number 214 JB, Jhang"]),
        _person("t4", "Asif Rafiq", True, cnic="9999900000044", phones=["03990000302"],
                addresses=["Chak No 214 Tehsil Jhang"]),
        _person("t5", "Sajid Mehmood", True, cnic="9999900000055", phones=["03990000501"], addresses=["Okara"]),
        _person("t6", "Tariq Hussain", True, cnic="9999900000066", phones=["03990000601"], addresses=["Sahiwal"]),
        # FIR 1/25 names "ساجد محمود" with no CNIC - it sounds like the target Sajid Mehmood.
        _person("n1", "ساجد محمود"),
        _person("x1", "Zahid Ali", cnic="9999900000077", phones=["03990000701"], addresses=["Korangi"]),
        _person("c1", "Bilal Khan", cnic="9999900000088", phones=["03990000801"]),        # complainant of FIR 1/25
        _person("c2", "Waqas Khan", cnic="9999900000099"),                                 # the complainant's brother
    ], "edges": [
        _tie("e1", "t1", "t2", "Co-accused in FIR 1/25", "psrms", "Imran Rashid is co-accused with Kamran Aslam in FIR 1/25"),
        _tie("e2", "t1", "n1", "Co-accused in FIR 1/25", "psrms", "ساجد محمود is co-accused with Kamran Aslam in FIR 1/25"),
        _tie("e3", "x1", "t1", "Phone contact", "cdr", "Zahid Ali called Kamran Aslam"),
        _tie("e4", "x1", "t3", "Phone contact", "cdr", "Zahid Ali called Naveed Rafiq"),
        _tie("e5", "c1", "t1", "Complainant against the subject in FIR 1/25", "psrms", "Bilal Khan filed FIR 1/25 against Kamran Aslam"),
        _tie("e6", "c1", "t2", "Both on FIR Roster", "fir_roster", None),
        _tie("e7", "c2", "c1", "Family member (brother)", "nadra", "Waqas Khan is brother of Bilal Khan"),
        _tie("e8", "c2", "t3", "Phone contact", "cdr", "Waqas Khan called Naveed Rafiq"),
    ]}
    net = PersonNetwork(graph)
    case = CaseFile()
    return case, net, case_scope.build(case, net)


def test_the_targets_are_tied_by_crime_numbers_in_sequence_and_the_same_village():
    case, net, scope = _gang()
    group = group_view.build(case, net, scope)
    ties = {frozenset((r["a"], r["b"])): r for r in group["ties"]}
    assert "Co-accused in FIR 1/25" in ties[frozenset(("Imran Rashid", "Kamran Aslam"))]["how"]
    near = ties[frozenset(("Asif Rafiq", "Naveed Rafiq"))]
    assert any("in sequence: 03990000302 and 03990000301" in h or "in sequence: 03990000301 and 03990000302" in h
               for h in near["how"])
    assert any("same village" in h and "chak 214" in h for h in near["how"])
    # The common contact ties two groups' members; one target has no tie at all.
    common = {c["name"]: c for c in group["common"]}
    assert {x["target"] for x in common["Zahid Ali"]["tied_to"]} == {"Kamran Aslam", "Naveed Rafiq"}
    assert common["Zahid Ali"]["phones"] == ["03990000701"] and common["Zahid Ali"]["address"] == "Korangi"
    assert "Tariq Hussain" in group["alone"]


def test_a_name_with_no_cnic_that_sounds_like_a_target_is_a_question_not_a_tie():
    case, net, scope = _gang()
    group = group_view.build(case, net, scope)
    same = group["same_person"]
    assert [(s["target"], s["name_in_record"], s["tied_to"]) for s in same] == [("Sajid Mehmood", "ساجد محمود", ["Kamran Aslam"])]
    assert same[0]["target_cnic"] == "99999-0000005-5" and "(CNIC 99999-0000005-5)" in " ".join(group_view.lines(group))
    # It joins no group by itself - it says what it would join.
    assert "Sajid Mehmood" in group["alone"]
    assert any("Sajid Mehmood" in b and "group" in b for b in group["bridges"])


def test_the_complainants_family_is_no_common_connection_and_one_target_is_no_group():
    case, net, scope = _gang()
    group = group_view.build(case, net, scope)
    names = {c["name"] for c in group["common"]}
    # The complainant is tied to two targets (he named them both); his brother is not reached through him.
    assert "Bilal Khan" in names
    assert not any(x["how"].startswith("through Bilal Khan") for c in group["common"] for x in c["tied_to"])
    contacts = {c["target"]: c for c in group["contacts"]}
    first = contacts["Kamran Aslam"]["rows"][0]
    assert first["also_tied_to"] and first["cnic"] != "-"
    lines = group_view.lines(group)
    assert lines[0].startswith("From the police") and lines[1].startswith("Group 1:") and any("Tariq Hussain" in x for x in lines)

    one = {"nodes": [_person("t1", "Kamran Aslam", True, cnic="9999900000011")], "edges": []}
    single = PersonNetwork(one)
    assert group_view.build(CaseFile(), single, case_scope.build(CaseFile(), single)) is None
