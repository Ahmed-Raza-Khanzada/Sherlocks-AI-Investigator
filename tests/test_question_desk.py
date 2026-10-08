"""The Question desk: the Gatekeeper stops repeats - answered in passing, "don't know",
muted, asked twice - and tells the Sherlock team why, with the answer on file."""

from __future__ import annotations

from sherlocks.evidence.case_file import CaseFile
from sherlocks.evidence.question_desk import Gatekeeper, free_key, known_answers
from sherlocks.linkgraph.sherlock_team import run_turn


def _graph():
    return {"nodes": [{"id": "a", "kind": "person", "label": "Kamran Ahmed", "data": {"seed": True, "phones": ["03990000101"]}},
                      {"id": "b", "kind": "person", "label": "Sajid Mehmood", "data": {"seed": True, "phones": ["03990000201"]}}],
            "edges": [{"id": "e", "source": "a", "target": "b", "kind": "strong", "label": "Co-accused in FIR 45/2023"}]}


def _asked(turns):
    return [t[-1]["checks"].get("asked") for t in turns]


def test_an_answer_given_in_passing_closes_the_question():
    case, graph = CaseFile(), _graph()
    # The officer mentions the vehicle while telling something else.
    list(run_turn(graph, "Kamran ne KDE-1234 bike use ki thi", case=case))
    desk = Gatekeeper(case, graph)
    decision = desk.check({"key": "incident:vehicle", "text": "Was a vehicle used?"})
    assert not decision["ok"]
    assert case.incident.get("vehicle_or_weapon", "").startswith("Kamran ne KDE-1234")


def test_free_form_question_is_rejected_with_the_answer_on_file():
    case, graph = CaseFile(), _graph()
    case.add_turn("Waqia 1 March ko raat 10 baje hua", "Noted")
    desk = Gatekeeper(case, graph)
    q = desk.admit({"text": "Waqia kis waqt hua tha?", "key": "incident:when"})
    assert q is None                         # the board / the chat already answers it
    entry = case.desk[-1]
    assert entry["decision"] == "rejected" and entry["reason"]


def test_dont_know_is_an_answer_and_is_never_asked_again():
    case, graph = CaseFile(), _graph()
    first = list(run_turn(graph, "Kamran main suspect hai", case=case))
    key = first[-1]["checks"]["asked"]
    assert key
    list(run_turn(graph, "pata nahi", case=case))
    q = next(q for q in case.questions if q["key"] == key)
    assert q["status"] == "dont_know"
    later = [list(run_turn(graph, msg, case=case)) for msg in ("theek hai", "aur kuch?", "Sajid bhi tha")]
    assert key not in _asked(later)
    assert any(k["key"] == key for k in known_answers(case))


def test_muted_topic_is_never_asked_again():
    case, graph = CaseFile(), _graph()
    first = list(run_turn(graph, "Kamran main suspect hai", case=case))
    key = first[-1]["checks"]["asked"]
    list(run_turn(graph, "ye sawal mat poocho", case=case))
    assert key in case.dialog["muted"]
    assert any(d["decision"] == "muted" and d["key"] == key for d in case.desk)
    later = [list(run_turn(graph, msg, case=case)) for msg in ("theek", "Sajid bhi shamil tha", "ok")]
    assert key not in _asked(later)


def test_one_question_per_reply_never_twice_in_a_row_max_two():
    case, graph = CaseFile(), _graph()
    turns = [list(run_turn(graph, msg, case=case)) for msg in
             ("Kamran main suspect hai", "ok", "theek hai", "acha", "hmm", "ok ji", "chalo")]
    asked = [a for a in _asked(turns) if a]
    assert all(a != b for a, b in __import__("itertools").pairwise(asked))          # never twice in a row
    assert all(asked.count(k) <= 2 for k in set(asked))                         # at most twice
    assert all(q.get("times", 0) <= 2 for q in case.questions)
    assert all(q.get("asked_turns") for q in case.questions if q.get("times"))


def test_free_key_is_stable_for_the_same_words():
    assert free_key("Kamran ka CDR hai?") == free_key("kamran ka cdr hai")
