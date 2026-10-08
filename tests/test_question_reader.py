"""O0 Question reader and the Officer agent's plan: the ask decides what is fetched."""

from __future__ import annotations

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph import question_reader
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.linkgraph.sherlock_team import run_turn


def _graph():
    return {"nodes": [{"id": "a", "kind": "person", "label": "Kamran Ahmed",
                       "data": {"seed": True, "phones": ["03990000101"],
                                "firs": [{"label": "45/2023", "role": "Complainant", "offence": "302 PPC", "system": "psrms"}]}},
                      {"id": "b", "kind": "person", "label": "Sajid Mehmood",
                       "data": {"firs": [{"label": "45/2023", "role": "Accused", "offence": "302 PPC", "system": "psrms"}]}}],
            "edges": []}


class _Llm:
    model = "scripted"

    def __init__(self, reading):
        self.reading = reading

    def generate_structured(self, *, prompt, schema, **_):
        if schema.__name__ == "_Reading":
            return schema.model_validate(self.reading), None
        raise RuntimeError("not scripted")


def test_a_request_is_a_question_not_a_statement():
    case = CaseFile()
    assert question_reader.act_of("explain me the fir filed by Kamran !", []) == "question"
    assert question_reader.act_of("tafseel batao Kamran ke case ki", []) == "question"
    assert question_reader.act_of("Kamran main suspect hai", []) == "statement"
    final = list(run_turn(_graph(), "explain me the fir filed by Kamran Ahmed !", case=case))[-1]
    assert final["intent"] == "question" and final["query"]["type"] == "fir_details"
    assert final["query"]["role"] == "complainant" and "not the accused" not in final["answer"]


def test_how_much_is_wanted_comes_from_the_meaning():
    case, net = CaseFile(), PersonNetwork(_graph())
    detailed = question_reader.read("what is the case filed by Kamran Ahmed ?", net, case, [])
    assert detailed["type"] == "fir_details" and detailed["depth"] == "detailed"
    brief = question_reader.read("how many firs is Kamran Ahmed in?", net, case, [])
    assert brief["type"] == "count_cases" and brief["depth"] == "brief"


def test_the_plan_fetches_only_what_the_ask_needs():
    case, net = CaseFile(), PersonNetwork(_graph())
    fir = question_reader.plan(question_reader.read("explain the fir filed by Kamran Ahmed", net, case, []), case, net)
    assert fir["fir_files"] and not fir["systems"]                       # the FIR file, not Hotel Eye or CRO
    job = question_reader.plan(question_reader.read("Kamran Ahmed ka kaam kya hai?", net, case, []), case, net)
    assert job["systems"] and not job["fir_files"]                       # a person's job: the systems that hold it
    summary = question_reader.plan(question_reader.read("case ka khulasa batao", net, case, []), case, net)
    assert summary["sources"] == ["board", "facts"]


def test_the_model_reads_any_wording():
    """No keyword in the message says "complainant" or "details": the model's reading does."""
    case = CaseFile()
    llm = _Llm({"act": "question", "type": "fir_details", "people": ["Kamran Ahmed"], "role": "complainant",
                "depth": "detailed", "yes_no": False})
    ask = question_reader.read_message("the matter Kamran took to the police - walk me through it", PersonNetwork(_graph()),
                                       case, [], llm)
    assert ask["act"] == "question" and ask["type"] == "fir_details" and ask["by"] == "model"
    assert ask["people"] == ["a"] and ask["role"] == "complainant" and ask["depth"] == "detailed"


def test_one_specific_fact_is_answered_not_the_whole_case():
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    s.evidence.auto_report = False
    h = memory_manager(s).start(GraphRunParams(cnic="9999900000011", depth=2, max_persons=15, backend="demo"), wait=True)

    def say(message):
        return list(run_turn(h.graph(), message, case=h.case))[-1]

    say("what is the case filed by Waqas Javed ?")                       # "this case" = his FIR from here on
    for question in ("who is the investigator of this case ?", "who is investigating officer of this case ?",
                     "is case ka IO kaun hai?"):
        out = say(question)
        assert out["query"]["asks_for"] == "investigating officer", question
        lines = out["answer"].replace("**", "").splitlines()
        assert "Investigating officer" in lines[0] and "Zahid Iqbal" in lines[0], question    # the answer first
        assert "Evidence:" in out["answer"] or "Saboot:" in out["answer"]                    # with its evidence
        assert "Complainant's account" not in out["answer"]
    when = say("when did it happen?")
    assert when["query"]["asks_for"] == "date of occurrence" and "01-03-2023" in when["answer"].splitlines()[0]
    witnesses = say("who are the witnesses in this case?")
    assert "Shahid" in witnesses["answer"].splitlines()[0]
    # A person is not a "fact"; the whole picture is not one either.
    from sherlocks.linkgraph.network import PersonNetwork

    net = PersonNetwork(h.graph())
    assert question_reader.field_of("who is Kamran Ahmed?", net) == ""
    assert question_reader.field_of("who are the targets of this case?", net) == ""


def test_a_model_reading_never_narrows_a_whole_picture_to_one_fact():
    """A real session: the model put the whole summary request into asks_for, and narrowed
    "what is fir says" to one detail - both answered far too little."""
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    s.evidence.auto_report = False
    h = memory_manager(s).start(GraphRunParams(cnic="9999900000011", depth=2, max_persons=15, backend="demo"), wait=True)
    net = PersonNetwork(h.graph())
    summary = question_reader.read("Summarise this case: who are the targets, what connects them, and what do the "
                                   "documents establish?", net, h.case, [],
                                   reading={"type": "summary", "asks_for": "targets, connections and what the documents "
                                            "establish", "depth": "brief", "role": "any", "people": []})
    assert summary["type"] == "summary" and summary["asks_for"] == ""
    # The model's reading is the ask: what it says is followed, no keyword overrides it.
    fir = question_reader.read("what is fir says !", net, h.case, ["p8"],
                               reading={"type": "fir_details", "asks_for": "", "depth": "detailed",
                                        "role": "any", "people": ["Waqas Javed"]})
    assert fir["depth"] == "detailed" and fir["asks_for"] == "" and fir["by"] == "model"
    # Without the model, the rules read it: what the FIR says is the whole of it.
    fallback = question_reader.read("what is fir says !", net, h.case, ["p8"])
    assert fallback["depth"] == "detailed" and fallback["asks_for"] == ""
    # And when one fact finds nothing, the full answer is given instead of "records do not give".
    out = list(run_turn(h.graph(), "what is the shoe size of Kamran Ahmed in this case?", case=h.case))[-1]
    assert "do not give" not in out["answer"]


def test_this_case_is_the_new_case_once_it_is_described():
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    s.evidence.auto_report = False
    h = memory_manager(s).start(GraphRunParams(cnic="9999900000011", depth=2, max_persons=15, backend="demo"), wait=True)

    def say(message):
        return list(run_turn(h.graph(), message, case=h.case))[-1]

    say("Kamran pe shak hai. Gulshan-e-Iqbal mein dakaiti hui 05-06-2024 ko")
    assert "Gulshan-E-Iqbal" in (h.case.incident or {}).get("place", "") or "Gulshan" in h.case.incident.get("place", "")
    out = say("who is the investigator of this case ?")
    assert out["answer"].replace("**", "").startswith("The new case's FIR is not on the board yet")
    assert "Zahid Iqbal" in out["answer"]                                      # the old FIRs, as background
    assert "ASK:" in out["answer"] or "VIEW:" in out["answer"]                 # Sherlock always adds something
