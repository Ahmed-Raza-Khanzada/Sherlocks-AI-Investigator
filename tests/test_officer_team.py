"""The Officer team: O4's context pack, time budgets with follow-ups, Sherlock's quick view."""

from __future__ import annotations

import time

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph import briefer
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.linkgraph.sherlock_team import run_turn
from sherlocks.settings import Settings


def _graph():
    return {"nodes": [{"id": "a", "kind": "person", "label": "Kamran Ahmed", "data": {"seed": True, "phones": ["03990000101"]}},
                      {"id": "b", "kind": "person", "label": "Sajid Mehmood", "data": {"seed": True, "phones": ["03990000201"]}}],
            "edges": [{"id": "e", "source": "a", "target": "b", "kind": "strong", "label": "Co-accused in FIR 45/2023"}]}


class _Llm:
    model = "scripted"

    def __init__(self, answers, delays=None):
        self.answers, self.delays = answers, delays or {}

    def generate_structured(self, *, prompt, schema, system=None, cache_kind=None, prompt_version="v1", **_):
        time.sleep(self.delays.get(schema.__name__, 0))
        answer = self.answers.get(schema.__name__)
        if answer is None:
            raise RuntimeError("no scripted answer")
        return schema.model_validate(answer(prompt) if callable(answer) else answer), None


def test_the_context_pack_has_both_parts_ids_and_whats_missing():
    case, graph = CaseFile(), _graph()
    case.set_role("a", "main suspect")
    case.add_turn("Kamran ki bike KDE-1234 thi", "noted")
    pack = briefer.build(case, graph, PersonNetwork(graph), "Kamran ka employer kaun hai?", people=["a"])
    assert pack["board_version"] == case.version
    assert pack["chat_recent"][0]["officer"].startswith("Kamran ki bike")
    assert pack["focus"][0]["role"] == "main suspect"
    assert any("work" in m for m in pack["missing"])                     # no employer on record
    assert pack["targets"]                                              # the case memory parts stay


def test_a_slow_draft_goes_out_as_a_follow_up():
    s = Settings()
    s.chat.draft_budget_s = 0.2
    case, graph = CaseFile(), _graph()
    saved = []
    llm = _Llm({"_Statements": {"statements": []},
                "_KnowledgeAnswer": {"answer": "Kamran and Sajid are co-accused in FIR 45/2023.",
                                     "needs_deeper_investigation": False},
                "_Reply": {"reply": "unused"}, "_Check": {"consistent": True, "issues": []}},
               delays={"_KnowledgeAnswer": 0.8})
    turn = list(run_turn(graph, "How are Kamran and Sajid connected?", llm=llm, case=case, settings=s,
                         on_late=lambda: saved.append(1)))
    final = turn[-1]
    assert "draft" in final["late"] and final["followup_pending"]
    assert final["answer"]                                             # the board's answer went out now
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not case.followups:
        time.sleep(0.05)
    assert case.followups and "co-accused in FIR 45/2023" in case.followups[0]["text"]
    assert case.followups[0]["turn"] == 1 and saved


def test_sherlocks_view_is_given_labelled_and_kept():
    s = Settings()
    case, graph = CaseFile(), _graph()
    llm = _Llm({"_Statements": {"statements": []},
                "_KnowledgeAnswer": {"answer": "Kamran and Sajid are co-accused in FIR 45/2023.",
                                     "needs_deeper_investigation": False},
                "_View": {"view": "Lagta hai dono saath kaam karte hain.", "people": ["Kamran Ahmed"]},
                "_Check": {"consistent": True, "issues": []}})
    final = list(run_turn(graph, "kya Kamran is mein shamil hai?", llm=llm, case=case, settings=s))[-1]
    assert "VIEW: Sherlock ka andaza (tasdeeq nahi): Lagta hai dono saath kaam karte hain." in final["answer"]
    assert case.views and case.views[0]["turn"] == 1
    assert final["timing"]["total"] >= 0 and "brief" in final["timing"]
    assert 1 <= final["model_calls"] <= 4
