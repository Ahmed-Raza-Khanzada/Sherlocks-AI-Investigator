"""O1 Officer agent's chain: the model reads the question (no keywords decide), then the
Dossier agent, the case board and graph, the API agent at run time - and a plain "I did
not find an answer" with what was checked when none of them has it."""

from __future__ import annotations

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph import question_reader
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.linkgraph.sherlock_team import run_turn


def _graph():
    return {"nodes": [
        {"id": "p1", "kind": "person", "label": "Muhammad Danish Rafiq",
         "data": {"seed": True, "cnic": "9999900000777", "phones": ["03990000777"],
                  "firs": [{"label": "45/2023", "role": "Accused", "offence": "392 PPC", "system": "psrms"}]}},
        {"id": "p2", "kind": "person", "label": "Tariq Mehmood", "data": {"cnic": "4210100000001"}},
    ], "edges": []}


class _ReaderOnly:
    """A model that only reads the question; every other call fails, so the answer comes
    from the agents' own work."""
    model = "scripted"

    def __init__(self, reading):
        self.reading = reading

    def generate_structured(self, *, prompt, schema, **_):
        if schema.__name__ == "_Reading":
            return schema.model_validate(self.reading), None
        raise RuntimeError("not scripted")


class _Backend:
    def __init__(self, found=None):
        self.asked, self.found = [], found or {}

    def lookup(self, system, cnic, phone):
        self.asked.append(system)
        if system in self.found:
            return {"status": "found", "hit": True, "summary": f"{system} record", "data": self.found[system]}
        return {"status": "not_found", "hit": False, "summary": "no record", "data": {}}


def test_the_models_reading_decides_not_keywords():
    """Words that the rules would read as a count ("how many") - the model says the officer
    wants the story; its reading is the ask."""
    net, case = PersonNetwork(_graph()), CaseFile()
    llm = _ReaderOnly({"act": "question", "type": "fir_details", "people": ["Muhammad Danish Rafiq"],
                       "depth": "detailed", "role": "accused", "scope": "case", "about_new_case": False})
    ask = question_reader.read_message("how many things happened in Danish's matter, walk me through", net, case, [], llm)
    assert ask["by"] == "model" and ask["type"] == "fir_details" and ask["depth"] == "detailed"
    assert ask["people"] == ["p1"] and ask["role"] == "accused" and ask["scope"] == "case"
    # The model's act is taken too: information, not a question, is recorded.
    told = question_reader.read_message("Danish ran away on Monday", net, case, [], _ReaderOnly({"act": "statement"}))
    assert told["act"] == "statement" and "type" not in told


def test_a_question_about_a_person_asks_the_dossier_agent_first():
    case = CaseFile()
    events = list(run_turn(_graph(), "who is Muhammad Danish Rafiq?", case=case))
    agents = [e["agent"] for e in events if e.get("type") == "agent"]
    assert "Dossier agent" in agents and agents.index("Dossier agent") < agents.index("Knowledge base")
    assert "p1" in case.dossiers                      # the card is kept on the board
    assert any(e.get("key") == "dossier" for e in events if e.get("type") == "status")


def test_the_api_agent_is_called_when_the_board_has_no_answer():
    backend, case = _Backend({"evs": {"name": "Muhammad Danish Rafiq", "cnic": "9999900000777",
                                      "organisation": "Karachi Port Trust", "designation": "Clerk"}}), CaseFile()
    events = list(run_turn(_graph(), "Muhammad Danish Rafiq ka kaam kya hai?", case=case, backend=backend, live_calls=3))
    assert backend.asked and "evs" in backend.asked
    assert any(e.get("agent") == "API agent" for e in events if e.get("type") == "agent")
    assert "Karachi Port Trust" in events[-1]["answer"]


def test_nothing_found_anywhere_is_said_plainly_with_what_was_checked():
    backend = _Backend()
    answer = list(run_turn(_graph(), "which shoe size was found at the scene?", case=CaseFile(), backend=backend,
                           live_calls=3))[-1]["answer"]
    assert "I did not find an answer" in answer and "I checked:" in answer
    assert "the case board" in answer and "graph" in answer
    # Offline (no live calls): it says the systems were not reachable, never guesses.
    offline = list(run_turn(_graph(), "which shoe size was found at the scene?", case=CaseFile()))[-1]["answer"]
    assert "I did not find an answer" in offline and "not connected" in offline


def test_a_lookup_that_finds_nothing_is_listed_as_checked_not_given_as_the_answer():
    from sherlocks.linkgraph.researcher import facts_from

    done = [{"what": "EVS lookup", "source": "EVS", "lines": ["nothing found (not_found)"], "document": None}]
    assert facts_from(done, "Tariq")["empty"]


def _hotel_graph():
    return {"nodes": [
        {"id": "z", "kind": "person", "label": "Zameer Ahmad",
         "data": {"seed": True, "firs": [{"label": "8/22", "role": "Accused", "offence": "364 PPC", "system": "psrms",
                                          "date": "2022-03-12"},
                                         {"label": "19/23", "role": "Accused", "offence": "392 PPC", "system": "psrms"}],
                  "stays": [{"hotel": "Hotel Sample", "district": "Hyderabad", "room": "4",
                             "check_in": "2022-03-11 22:00", "check_out": "2022-03-13 10:00"}]}},
    ], "edges": []}


def test_a_question_naming_someone_sherlock_asked_about_is_still_a_question():
    """Sherlock asked "who is the main suspect?"; the officer asks about Zameer's hotel stays.
    The model called it an answer - but it asks something, so it is a question."""
    from sherlocks.linkgraph.network import PersonNetwork

    case = CaseFile()
    case.ask("suspect", "Who is the main suspect in this case?")
    net = PersonNetwork(_hotel_graph())
    msg = "during that time this fir incident time did zameer ahmed was in any hotel ?"
    llm = _ReaderOnly({"act": "answer", "asks_something": True, "type": "hotels", "people": ["Zameer Ahmad"]})
    assert question_reader.read_message(msg, net, case, [], llm)["act"] == "question"
    llm = _ReaderOnly({"act": "answer", "type": "hotels", "people": ["Zameer Ahmad"]})     # the "?" says it asks
    assert question_reader.read_message(msg, net, case, [], llm)["act"] == "question"


def test_hotel_stays_are_checked_against_each_firs_time():
    case = CaseFile()
    final = list(run_turn(_hotel_graph(), "during that time this fir incident time did zameer ahmed was in any hotel ?",
                          case=case))[-1]
    answer = final["answer"]
    assert final["intent"] == "question" and final["query"]["type"] == "hotels"
    assert "FIR 8/22" in answer and "yes" in answer.lower() and "Hotel Sample" in answer
    assert "FIR 19/23" in answer and "not on record" in answer          # no date: said, not guessed
    assert "On record:" not in answer
