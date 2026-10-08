"""The Sherlock team in the background: what wakes it, what never does, one run at a time."""

from __future__ import annotations

import time

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.casework import Casework, assess, case_covered, summarize
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.settings import Settings


def _graph():
    return {"nodes": [{"id": "a", "kind": "person", "label": "Kamran Ahmed", "data": {"seed": True, "phones": ["03990000101"]}},
                      {"id": "b", "kind": "person", "label": "Sajid Mehmood", "data": {"seed": True, "phones": ["03990000201"]}}],
            "edges": [{"id": "e", "source": "a", "target": "b", "kind": "strong", "label": "Co-accused in FIR 45/2023"}]}


def _wait(pred, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _setup(llm=None):
    case, graph = CaseFile(), _graph()
    saves = []
    work = Casework(Settings(), lambda: llm, debounce=0.05)
    board = work.attach("r1", case, lambda: graph, lambda: saves.append(1))
    return case, graph, work, board, saves


def _runs(case):
    return int(case.dialog.get("sherlock_runs") or 0)


def test_a_batch_wakes_the_team_once_and_its_own_writes_never_do():
    case, _, _, _, saves = _setup()
    doc = case.add_document(kind="fir", key="fir:1", title="FIR 45/2023", source="PSRMS", text="x " * 40)
    with case.batch("A2 Document reader", "document"):
        for i in range(25):
            case.add_fact(doc, f"Finding {i}", "x", check_quote=False)
    assert _wait(lambda: _runs(case) == 1)
    assert case.summary and case.assessment and saves
    time.sleep(0.3)
    assert _runs(case) == 1                    # its summary, assessment and gaps did not wake it again


def test_the_officers_information_wakes_it_but_a_question_turn_does_not():
    case, *_ = _setup()
    case.add_turn("kamran kaun hai?", "...")
    time.sleep(0.3)
    assert _runs(case) == 0                    # nothing new from outside: skipped, not counted
    case.set_role("a", "main suspect")
    assert _wait(lambda: _runs(case) == 1)


def test_loose_facts_wake_it_at_fifteen():
    case, *_ = _setup()
    doc = case.add_document(kind="fir", key="fir:1", title="FIR", source="PSRMS", text="x")
    for i in range(14):
        case.add_fact(doc, f"Loose {i}", "x", check_quote=False)
    time.sleep(0.3)
    assert _runs(case) == 0
    case.add_fact(doc, "Loose 14", "x", check_quote=False)
    assert _wait(lambda: _runs(case) == 1)


def test_the_run_budget_per_case():
    case, _g, work, board, _s = _setup()
    work.settings.evidence.sherlock_runs_per_case = 1
    case.set_role("a", "suspect")
    assert _wait(lambda: _runs(case) == 1)
    case.set_role("b", "victim")
    time.sleep(0.4)
    assert _runs(case) == 1
    assert work.work(board, force=True) is not None      # the report can always ask for one more


def test_rule_assessment_from_the_graph_analyst_and_bringing_it_up_to_date():
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    mgr = memory_manager(s)
    handle = mgr.start(GraphRunParams(cnic="9999900000011", depth=2, max_persons=15, backend="demo"), wait=True)
    case = handle.case
    assert any(d["kind"] == "graph" for d in case.documents.values())          # A1 read the graph
    case.set_incident({"date": "2023-03-01", "place": "Saddar"})
    assert mgr.casework.bring_up_to_date(mgr.casework.boards[handle.id], timeout=10)
    assert case_covered(case)
    assert case.hypotheses and all(h["for"] for h in case.hypotheses.values())
    graph = handle.graph()
    summary = summarize(case, graph, PersonNetwork(graph))
    assert "Saddar" in summary["text"] and any(r["what"].startswith("Incident") for r in summary["timeline"])


class _Llm:
    model = "scripted"

    def __init__(self, answer):
        self.answer = answer

    def generate_structured(self, *, prompt, schema, **_):
        if schema.__name__ != "_Assessment":
            raise RuntimeError("not scripted")
        return schema.model_validate(self.answer), None


def test_model_assessment_keeps_only_real_ids_and_checks_quick_views():
    case, graph = CaseFile(), _graph()
    doc = case.add_document(kind="fir", key="fir:1", title="FIR 45/2023", source="PSRMS", text="co-accused robbery")
    fid = case.add_fact(doc, "Kamran and Sajid co-accused", "co-accused robbery")
    case.add_view("is he involved?", "Lagta hai haan", turn=1)
    llm = _Llm({"conclusion": f"They act together [{fid}].",
                "hypotheses": [{"statement": "Kamran and Sajid act together", "status": "supported", "confidence": "high",
                                "people": ["Kamran Ahmed", "Sajid Mehmood"], "support": [fid, "F99"]},
                               {"statement": "Invented", "support": ["F77"]}],
                "views": [{"view": "V1", "verdict": "confirmed", "note": "the FIR agrees"}],
                "questions": ["Kamran ka CDR hai?"]})
    out = assess(case, graph, PersonNetwork(graph), llm)
    assert list(case.hypotheses) == ["H1"]                  # the one resting on nothing real was dropped
    h = case.hypotheses["H1"]
    assert h["for"] == [fid] and h["people"] == ["a", "b"] and h["status"] == "supported"
    assert case.views[0]["status"] == "confirmed" and out["questions"] in (["Kamran ka CDR hai?"], [{"text": "Kamran ka CDR hai?", "priority": "orange"}])
