"""The Officer team's chat turn as a LangGraph graph: the model reads the message, O3
records what the officer tells us as fields, O1 fills the answer shape and names the
lookups he still needs, the desks add one finding or one question, O2 checks, O5 presents.
No words of the officer's message are matched anywhere on this path."""

from __future__ import annotations

import json

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.sherlock_team import run_turn
from sherlocks.team import insight_desk, shapes
from sherlocks.team.officer_graph import officer_graph


def _graph():
    return {"nodes": [
        {"id": "p1", "kind": "person", "label": "Muhammad Danish Rafiq",
         "data": {"seed": True, "cnic": "9999900000777", "phones": ["03990000777"],
                  "firs": [{"label": "45/2023", "role": "Accused", "offence": "392 PPC", "system": "psrms"}]}},
        {"id": "p2", "kind": "person", "label": "Kamran Aslam", "data": {"cnic": "9999900000011"}},
    ], "edges": []}


class Model:
    """Answers each schema from a script (a value, or a function of the prompt)."""

    model = "scripted"

    def __init__(self, **answers):
        self.answers, self.asked, self.prompts = answers, [], {}

    def generate_structured(self, *, prompt, schema, **_):
        name = schema.__name__
        self.asked.append(name)
        self.prompts.setdefault(name, []).append(prompt)
        answer = self.answers.get(name)
        if answer is None:
            raise RuntimeError(f"no scripted answer for {name}")
        return schema.model_validate(answer(prompt) if callable(answer) else answer), None


def _reading(**over):
    return {"act": "question", "asks_something": True, "type": "open", "people": ["Muhammad Danish Rafiq"],
            "depth": "detailed", "scope": "person", "shape": "person", **over}


OK = {"consistent": True, "issues": []}


class _Backend:
    def __init__(self, found):
        self.asked, self.found = [], found

    def lookup(self, system, cnic, phone):
        self.asked.append(system)
        if system in self.found:
            return {"status": "found", "hit": True, "summary": f"{system} record", "data": self.found[system]}
        return {"status": "not_found", "hit": False, "summary": "no record", "data": {}}


def test_the_turn_is_one_graph_with_a_rules_node_only_as_the_fallback():
    nodes = set(officer_graph().get_graph().nodes)
    assert {"read", "rules", "record", "brief", "draft", "lookup", "desk", "check", "present"} <= nodes
    # No model: the rules node answers. One start, one final - as the chat expects.
    events = list(run_turn(_graph(), "who is Muhammad Danish Rafiq?", case=CaseFile()))
    kinds = [e["type"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "final" and kinds.count("start") == 1 and kinds.count("final") == 1
    # A model that cannot read the message: the same fallback, and no second attempt.
    llm = Model()
    final = list(run_turn(_graph(), "who is Muhammad Danish Rafiq?", case=CaseFile(), llm=llm))[-1]
    assert final["answer"] and llm.asked == ["_Reading"]


def test_o1_names_the_lookups_he_needs_and_the_api_agent_fetches_them_at_run_time():
    backend, case = _Backend({"evs": {"name": "Muhammad Danish Rafiq", "cnic": "9999900000777",
                                      "organisation": "Karachi Port Trust", "designation": "Clerk"}}), CaseFile()

    def draft(prompt):
        data = json.loads(prompt)
        looked = data["given"]["looked_up_now"]
        if not looked:
            assert "api_router" in data["tools"] and "LIVE" in data["tools"]["api_router"]
            return {"first_line": "Records mein Danish ka kaam nahi hai.", "answered": False,
                    "lookups": [{"tool": "api_router", "args": {"who": "Muhammad Danish Rafiq", "topic": "work"},
                                 "why": "his employer"},
                                {"tool": "no_such_tool", "args": {}}]}
        assert "Karachi Port Trust" in json.dumps(looked) and data["tools"] == {}      # the last draft: no more tools
        return {"first_line": "Danish Karachi Port Trust mein Clerk hai.", "answered": True,
                "sections": [{"name": "Identity", "body": "• Karachi Port Trust, Clerk [EVS]"}]}

    llm = Model(_Reading=_reading(topics=["work"]), _Draft=draft, _Check=OK)
    events = list(run_turn(_graph(), "Danish kya kaam karta hai?", case=case, llm=llm, backend=backend, live_calls=3))
    final = events[-1]
    assert "evs" in backend.asked and llm.asked.count("_Draft") == 2
    assert "Karachi Port Trust" in final["answer"] and final["answer"].startswith("**Danish Karachi Port Trust")
    assert [d["tool"] for d in final["lookups"]] == ["api_router"]             # the unknown tool was never called
    assert any(e.get("agent") == "API agent" for e in events if e["type"] == "agent")
    assert any(e["type"] == "step" and e["tool"] == "api_router" and e["live"] for e in events)
    assert case.calls and case.calls[-1]["agent"] == "O1 Officer agent"         # audited, under the officer's team


def test_every_section_of_the_shape_is_filled_or_marked_never_dropped():
    case = CaseFile()
    llm = Model(_Reading=_reading(), _Check=OK,
                _Draft={"first_line": "Muhammad Danish Rafiq is the accused in FIR 45/2023.", "answered": True, "sections": [
                    {"name": "FIRs", "body": "• FIR 45/2023: robbery (392 PPC), accused [PSRMS]"},
                    {"name": "Hotel stays", "status": "not on record"}]})
    final = list(run_turn(_graph(), "who is Danish?", case=case, llm=llm))[-1]
    answer = final["answer"]
    assert answer.startswith("**Muhammad Danish Rafiq is the accused in FIR 45/2023.**")
    for name in shapes.sections("person"):
        assert f"## {name}" in answer                                        # every heading, in the shape's order
    assert answer.index("## FIRs") < answer.index("## Hotel stays") < answer.index("## Not on record")
    assert "Not on record." in answer and "Not checked yet." in answer
    assert final["checks"]["rules"]["complete"] is False and "Identity" in final["checks"]["restored_sections"]
    assert {s["name"]: s["status"] for s in final["sections"]}["Identity"] == "not checked yet"
    assert final["shape"] == "person"
    # The draft was given every source the shape needs, and the dossier of the person asked about.
    given = json.loads(llm.prompts["_Draft"][0])["given"]
    assert {"statements", "presence", "contradictions", "evidence_register"} <= set(given["sources"])
    assert given["dossiers"][0]["name"] == "Muhammad Danish Rafiq"
    # Asked for short: the first line and the points only - no headings.
    short = Model(_Reading=_reading(depth="brief"), _Check=OK, _Draft=llm.answers["_Draft"])
    brief = list(run_turn(_graph(), "Danish kaun hai, short mein", case=CaseFile(), llm=short))[-1]["answer"]
    assert "## " not in brief and "FIR 45/2023" in brief


def test_nothing_found_anywhere_is_said_plainly_with_every_place_checked():
    llm = Model(_Reading=_reading(people=[], scope="case", shape="other"), _Check=OK,
                _Draft={"first_line": "The records do not give a shoe size.", "answered": False, "sections": [], "lookups": []})
    final = list(run_turn(_graph(), "which shoe size was found at the scene?", case=CaseFile(), llm=llm))[-1]
    answer = final["answer"]
    assert "The records do not give a shoe size." in answer and "I checked:" in answer
    assert "the case board" in answer and "not connected" in answer and final["facts"]["type"] == "not_found"
    assert "_Check" not in llm.asked                                          # nothing to check against the records


def test_the_recorder_writes_who_said_it_how_it_is_known_and_when_it_happened():
    case = CaseFile()
    said = "Muddai ne bataya ke kal raat 10 baje ke qareeb Kamran ne pistol se fire kiya."
    llm = Model(_Reading={"act": "statement"}, _Record={
        "entries": [{"what": "Kamran fired with a pistol", "quote": "Kamran ne pistol se fire kiya", "topic": "event",
                     "about": "Kamran Aslam", "original_speaker": "the complainant", "speaker_role": "complainant",
                     "how_known": "heard", "certainty": "approximate", "when": "2026-10-11T22:00",
                     "when_words": "kal raat 10 baje ke qareeb", "tolerance_min": 30},
                    {"what": "The incident happened in Saddar", "quote": "invented words", "topic": "incident_place",
                     "value": "Saddar", "how_known": "record"}],
        "read_back": "Note kar liya: muddai ke mutabiq Kamran ne 11 Oct raat taqreeban 10 baje fire kiya."})
    events = list(run_turn(_graph(), said, case=case, llm=llm))
    final = events[-1]
    assert final["intent"] == "statement" and final["answer"].startswith("Note kar liya: muddai ke mutabiq")
    fact = next(f for f in case.facts.values() if "fired with a pistol" in f["statement"])
    assert fact["trust"] == 4 and fact["tier"] == "inference" and fact["pids"] == ["p2"]   # the complainant's word, relayed
    assert fact["said"]["original_speaker"] == "the complainant" and fact["said"]["when"] == "2026-10-11T22:00"
    assert fact["said"]["tolerance_min"] == 30 and fact["said"]["when_words"].startswith("kal raat")
    assert case.incident["place"] == "Saddar" and case.dialog["focus"] == ["p2"]
    assert llm.asked[:2] == ["_Reading", "_Record"] and "_Draft" not in llm.asked
    assert any(e.get("agent") == "Statement recorder" for e in events if e["type"] == "agent")


def test_a_correction_replaces_what_was_said_and_an_answer_closes_its_question():
    case = CaseFile()
    case.set_incident({"date": "2023-03-01"})
    q = case.ask("incident:what", "What happened?")
    q["times"] = 1
    llm = Model(_Reading={"act": "correction"}, _Record={
        "entries": [{"what": "The incident was on 2 March 2023", "quote": "2 March tha", "topic": "incident_date",
                     "value": "2023-03-02", "corrects": True}],
        "read_back": "Theek hai, waqia 2 March 2023 kar diya."})
    final = list(run_turn(_graph(), "nahi, 2 March tha", case=case, llm=llm))[-1]
    assert case.incident["date"] == "2023-03-02" and "2023-03-02" in final["answer"]
    assert any(f.get("replaces") or f["kind"] == "officer_correction" for f in case.facts.values())
    # "Don't know" is an answer: the question is closed and never asked again.
    llm = Model(_Reading={"act": "answer"}, _Record={"entries": [], "answers": q["id"], "dont_know": True,
                                                    "read_back": "Theek hai."})
    final = list(run_turn(_graph(), "pata nahi", case=case, llm=llm))[-1]
    assert final["intent"] == "answer" and case.questions[0]["status"] == "dont_know"
    assert (final["asking"] or {}).get("key") != "incident:what"


def test_one_finding_reaches_the_officer_after_the_answer_once_and_can_be_held():
    case = CaseFile()
    case.set_contradiction("C1|C2", {"topic": "weapon / injury", "person": None, "person_name": None, "severity": "critical",
                                     "text": "The FIR says pistol fire; the MLO finds an incised wound.",
                                     "settle": "Ask the doctor.", "a": {"trust": 2}, "b": {"trust": 2}})
    draft = {"first_line": "Muhammad Danish Rafiq is the accused in FIR 45/2023.", "answered": True,
             "sections": [{"name": "FIRs", "body": "• FIR 45/2023: robbery, accused [PSRMS]"}]}
    llm = Model(_Reading=_reading(), _Draft=draft, _Check=OK)
    first = list(run_turn(_graph(), "who is Danish?", case=case, llm=llm))[-1]
    answer = first["answer"]
    assert "FINDING: Sherlock's new point (not confirmed): The FIR says pistol fire" in answer
    assert answer.index("**Muhammad Danish") < answer.index("FINDING:")          # after the answer, never instead of it
    assert "ASK:" not in answer and first["asking"] is None                      # a critical finding: the question waits
    assert first["finding"]["ref"] == "X1" and first["checks"]["told"] == "X1"
    # The Officer agent was shown the open contradiction, so he never answers as if it were not there.
    assert "pistol fire" in json.dumps(json.loads(llm.prompts["_Draft"][0])["given"]["findings"])
    second = list(run_turn(_graph(), "who is Danish?", case=case, llm=llm))[-1]
    assert "FINDING:" not in second["answer"]                                    # told once
    # "Sherlock ki baatein baad mein": held until the officer asks; the report still has them.
    case.move("X1", "resolved", "the doctor confirmed")
    hold = Model(_Reading={"act": "statement"}, _Record={"entries": [], "wish": "pause_findings", "read_back": "Theek hai."})
    held = list(run_turn(_graph(), "Sherlock ki baatein baad mein", case=case, llm=hold))[-1]
    assert insight_desk.paused(case) and "FINDING:" not in held["answer"]
    assert insight_desk.waiting(case)                                            # still waiting, not lost


def test_a_question_that_needs_real_work_goes_to_sherlock_in_the_background():
    case = CaseFile()
    llm = Model(_Reading=_reading(opinion=True, wants_view=False, shape="other", people=[]), _Check=OK,
                _Draft={"first_line": "Yeh records se tay nahi hota.", "answered": False, "deeper": True, "sections": []})
    final = list(run_turn(_graph(), "asal mein yeh kis ne kiya?", case=case, llm=llm))[-1]
    assert any(a["task"] == "sherlock" for a in case.agenda)                     # never inside the reply
    assert case.dialog["asked_of_sherlock"][0]["question"].startswith("asal mein")
    assert "Sherlock is par kaam kar rahe hain" in final["answer"] and final["followup_pending"]


def test_the_gatekeeper_reads_the_chat_and_never_asks_what_was_already_said():
    case = CaseFile()
    q = case.ask("incident:what", "What happened?")
    case.add_turn("Kamran ne Naveed ko chaku mara tha, yeh bata do uski FIRs", "noted")
    draft = {"first_line": "Muhammad Danish Rafiq is the accused in FIR 45/2023.", "answered": True,
             "sections": [{"name": "FIRs", "body": "• FIR 45/2023: robbery, accused [PSRMS]"}]}
    llm = Model(_Reading=_reading(), _Draft=draft, _Check=OK,
                _Sweep={"answered": [{"id": q["id"], "turn": 1, "answer": "Kamran ne Naveed ko chaku mara tha"},
                                     {"id": "Q99", "turn": 1, "answer": "no such question"}]})
    final = list(run_turn(_graph(), "who is Danish?", case=case, llm=llm))[-1]
    assert case.questions[0]["status"] == "answered" and case.incident["offence"].startswith("Kamran ne Naveed")
    assert (final["asking"] or {}).get("key") != "incident:what"
    assert any(d["decision"] == "withdrawn" and "Already answered in turn 1" in d["reason"] for d in case.desk)
    # The Gatekeeper's reading did not come back: nothing new is asked this turn, rather than risk asking twice.
    case2 = CaseFile()
    case2.ask("incident:what", "What happened?")
    case2.add_turn("salam", "wa alaikum salam")
    silent = Model(_Reading=_reading(), _Draft=draft, _Check=OK)
    assert list(run_turn(_graph(), "who is Danish?", case=case2, llm=silent))[-1]["asking"] is None


def test_an_fir_question_returns_every_section_of_the_fir_shape():
    llm = Model(_Reading=_reading(type="fir_details", shape="fir", scope="case"), _Check=OK,
                _Draft={"first_line": "FIR 45/2023: robbery (392 PPC); Muhammad Danish Rafiq is the accused.", "answered": True,
                        "sections": [{"name": "What happened", "body": "• Robbery, as the record has it [PSRMS]"},
                                     {"name": "Who", "body": "• Accused: Muhammad Danish Rafiq [PSRMS]"}]})
    final = list(run_turn(_graph(), "FIR 45/2023 ke baare mein batao", case=CaseFile(), llm=llm))[-1]
    assert final["shape"] == "fir" and final["language"] == "roman"
    assert [s["name"] for s in final["sections"]] == shapes.sections("fir")
    for name in ("Kya hua", "Kab", "Kahan", "Kaun", "Kyun (jaisa bataya gaya)", "Hathiyar aur maal", "Bayanaat", "Saboot",
                 "Maujooda soorat-e-haal", "Tazadaat aur suraagh", "Record mein nahi"):
        assert f"## {name}" in final["answer"]                            # headings in the officer's language
    assert "Abhi check nahi hua." in final["answer"]


def test_a_finding_that_cites_nothing_real_is_held_back_and_new_tools_have_a_waiting_line():
    from sherlocks.linkgraph.conversation import status_text

    case = CaseFile()
    case.set_contradiction("C1|C2", {"topic": "weapon", "person": None, "severity": "critical", "a": {"trust": 2},
                                     "b": {"trust": 2}, "text": "The pistol in the FIR does not fit the wound [D77]."})
    draft = {"first_line": "Muhammad Danish Rafiq is the accused in FIR 45/2023.", "answered": True,
             "sections": [{"name": "FIRs", "body": "• FIR 45/2023: robbery, accused [PSRMS]"}]}
    events = list(run_turn(_graph(), "who is Danish?", case=case, llm=Model(_Reading=_reading(), _Draft=draft, _Check=OK)))
    assert "FINDING:" not in events[-1]["answer"] and events[-1]["finding"] is None
    assert any("held back" in e["text"] for e in events if e["type"] == "agent")
    for tool in ("statements", "presence", "evidence_register", "contradictions", "case_leads", "hypotheses"):
        assert len({status_text("think", lang, tool) for lang in ("en", "roman", "ur")}) == 3
        assert status_text("think", "roman", tool) != status_text("think", "roman")


def test_three_or_more_firs_are_laid_out_as_a_table():
    graph = _graph()
    graph["nodes"][0]["data"]["firs"] = [
        {"label": f"{n}/2023", "role": "Accused", "offence": "392 PPC", "system": "psrms", "police_station": "Saddar"}
        for n in (45, 46, 47)]
    llm = Model(_Reading=_reading(type="cases", shape="count"), _Check=OK,
                _Draft={"first_line": "3 FIRs name Muhammad Danish Rafiq.", "answered": True, "sections": [
                    {"name": "The items", "body": "• FIR 45/2023 [PSRMS]\n• FIR 46/2023 [PSRMS]\n• FIR 47/2023 [PSRMS]"}]})
    answer = list(run_turn(graph, "Danish ki FIRs?", case=CaseFile(), llm=llm))[-1]["answer"]
    assert answer.startswith("**3 FIRs name Muhammad Danish Rafiq.**")
    assert all(f"{n}/2023" in answer for n in (45, 46, 47))


def _recurring():
    """A target, and a stock witness who appears in the FIRs of two unrelated accused."""
    def person(pid, name, firs, seed=False):
        return {"id": pid, "kind": "person", "label": name, "data": {
            "name": name, "names": [name], "father_name": None, "cnic": None, "phones": [], "addresses": [], "flags": [],
            "firs": firs, "stays": [], "organisations": [], "vehicles": [], "police_stations": [], "records": [],
            "discovered_via": [], "osint": [], "seed": seed, "search_status": "searched", "searchable": True, "depth": 1}}

    def fir(label, role):
        no, year = label.split("/")
        return {"label": label, "fir_no": no, "fir_year": year, "police_station": "Saddar", "role": role,
                "offence": "392 PPC", "system": "psrms"}

    return {"nodes": [person("t", "Dad Muhammad .", [fir("9/2022", "Accused")], seed=True),
                      person("w", "Shahid Stock", [fir("1/2024", "Witness"), fir("2/2024", "Witness")]),
                      person("x", "Accused One", [fir("1/2024", "Accused")]),
                      person("y", "Accused Two", [fir("2/2024", "Accused")])], "edges": []}


def test_a_question_about_the_whole_case_is_answered_from_the_graphs_patterns():
    """"Who keeps reappearing across records?" - the Officer agent is shown what the link
    analysis found across the whole case, so he answers it; he is not left to look only
    at the target."""
    seen = {}

    def draft(prompt):
        given = json.loads(prompt)["given"]
        seen["patterns"] = given["graph_patterns"]
        hit = next(p for p in given["graph_patterns"]["patterns"] if p["pattern"] == "Recurring witness")
        return {"first_line": f"{hit['people'][0]} keeps reappearing as a witness.", "answered": True,
                "sections": [{"name": "Recurring witness", "body": f"• {hit['summary']} [PSRMS]"}]}

    llm = Model(_Reading=_reading(people=[], scope="case", shape="other"), _Draft=draft, _Check=OK)
    final = list(run_turn(_recurring(), "Who keeps reappearing across records and documents (witness, guarantor, "
                          "co-accused, hotel, SIM owner)?", case=CaseFile(), llm=llm))[-1]
    assert final["answer"].startswith("**Shahid Stock keeps reappearing as a witness.**")
    assert seen["patterns"]["count"] >= 1 and seen["patterns"]["key_people"] and llm.asked.count("_Draft") == 1
    assert "I checked" not in final["answer"]


def test_a_lookup_narrowed_to_the_target_is_widened_for_a_question_about_the_whole_case():
    def draft(prompt):
        looked = json.loads(prompt)["given"]["looked_up_now"]
        if not looked:                                     # the model narrows it to the target, as it did live
            return {"first_line": "Not known", "answered": False, "lookups": [
                {"tool": "findings", "args": {"scenario": "recurring_witness", "who": "Dad Muhammad ."}}]}
        found = looked[0]["result"]["findings"]
        assert looked[0]["args"] == {"scenario": "recurring_witness"} and found      # looked up for the whole case
        return {"first_line": f"{found[0]['people'][0]} recurs as a witness.", "answered": True,
                "sections": [{"name": "Recurring witness", "body": f"• {found[0]['summary']} [PSRMS]"}]}

    llm = Model(_Reading=_reading(people=[], scope="case", shape="other"), _Draft=draft, _Check=OK)
    final = list(run_turn(_recurring(), "who keeps reappearing?", case=CaseFile(), llm=llm))[-1]
    assert "Shahid Stock recurs as a witness." in final["answer"] and final["lookups"][0]["args"] == {"scenario": "recurring_witness"}
    # Asked about the person himself, the lookup stays on him.
    about = Model(_Reading=_reading(people=["Dad Muhammad ."], shape="other"), _Check=OK, _Draft=lambda p: (
        {"first_line": "Not known", "answered": False, "lookups": [
            {"tool": "findings", "args": {"scenario": "recurring_witness", "who": "Dad Muhammad ."}}]}
        if not json.loads(p)["given"]["looked_up_now"] else {"first_line": "Not known", "answered": False}))
    final = list(run_turn(_recurring(), "is Dad Muhammad a recurring witness?", case=CaseFile(), llm=about))[-1]
    assert final["lookups"][0]["args"]["who"] == "Dad Muhammad ."
    # Nothing found: said in plain words - what was checked, never the tool's own syntax.
    answer = final["answer"]
    assert answer.startswith("Not known. I checked:") and "linkage patterns (recurring witness, Dad Muhammad)" in answer
    assert "findings(" not in answer and "recurring_witness" not in answer


def test_what_the_model_is_loose_about_is_settled_in_code():
    """Found on the real model: it wrote the points but marked every section "not on
    record", and gave made-up ids to lines saying nothing is on record."""
    from sherlocks.linkgraph.question_reader import _Reading
    from sherlocks.team import officer_agent

    known = {"D1", "F2"}
    draft = officer_agent.normalise({"first_line": "He is the accused in FIR 45/2023 [D1, G7].", "answered": True, "sections": [
        {"name": "Identity", "body": "• CNIC on record [F2]\\n• Father: Aslam [K1]", "status": "not on record"},
        {"name": "Hotel stays", "body": "", "status": "filled"},
        {"name": "Statements", "body": "", "status": "not checked yet"}]}, known)
    assert draft["first_line"] == "He is the accused in FIR 45/2023 [D1]."
    assert draft["sections"][0] == {"name": "Identity", "status": "filled", "body": "• CNIC on record [F2]\\n• Father: Aslam"}
    assert [s["status"] for s in draft["sections"][1:]] == ["not on record", "not checked yet"]
    # Every field of a schema is asked of the model: one with a default is otherwise left out.
    assert set(_Reading.model_json_schema()["required"]) >= {"act", "type", "depth", "shape", "wants_view", "language"}
    assert set(officer_agent._Draft.model_json_schema()["required"]) == {"first_line", "sections", "answered", "lookups", "deeper"}
    assert _Reading.model_validate({"act": "question"}).shape == "other"       # the defaults still apply


def _hotel_target():
    """A target shown in Urdu script, with other spellings from other systems, a hotel stay -
    and another person whose name only looks similar."""
    def person(pid, label, **data):
        base = {"name": label, "names": [label], "father_name": None, "cnic": None, "phones": [], "addresses": [], "flags": [],
                "firs": [], "stays": [], "organisations": [], "vehicles": [], "records": [], "lookups": {}, "seed": False,
                "extra": {}}
        return {"id": pid, "kind": "person", "label": label, "data": {**base, **data}}

    stay = {"hotel": "Hotel Sample", "district": "Larkana", "room": "206", "check_in": "2024-12-25 18:03:00",
            "check_out": "2024-12-26 00:00:00"}
    return {"nodes": [
        person("p1", "محمد حسنین", seed=True, cnic="9999900000111", stays=[stay], flags=["police_officer"],
               names=["محمد حسنین", "MUHAMMAD HASNAIN AZAM", "Muhammad Hasnain"],
               records=[{"system": "hotel_eye", "node": "s:hotel_eye:p1", "summary": "1 Hotel Eye stay(s) in 1 profile(s)"}],
               lookups={"hotel_eye": {"status": "success"}, "cro": {"status": "no_record"}, "nadra": {"status": "error"}}),
        person("p10", "Hassan", cnic="9999900000222"),
        {"id": "s:hotel_eye:p1", "kind": "system", "label": "Hotel Eye", "data": {
            "system": "hotel_eye", "owner": "p1", "summary": "1 Hotel Eye stay(s)", "stays": [stay],
            "fields": [{"label": "Stay 1", "value": "Hotel Sample (Larkana, PS Civil Line) room 206 | 2024-12-25 18:03:00"}]}},
    ], "edges": []}


def test_a_name_means_the_person_some_record_calls_that_never_a_look_alike():
    from sherlocks.linkgraph.network import PersonNetwork

    net = PersonNetwork(_hotel_target())
    assert net.resolve("hasnain") == "p1"                 # a word of another spelling of the target's name
    assert net.resolve("Muhammad Hasnain") == "p1" and net.resolve("حسنین") == "p1"
    assert net.resolve("Hassan") == "p10" and net.resolve("hasan") == "p10"     # the look-alike still finds its own person
    assert net.resolve("Hassaan") == "p10"                # a misspelling: only when nobody is really called that


def test_the_dossier_holds_what_every_system_returned_and_o1_is_told_what_each_system_is():
    from sherlocks.evidence.dossiers import build_card
    from sherlocks.linkgraph.network import PersonNetwork
    from sherlocks.linkgraph.systems import MEANING, SYSTEMS

    graph = _hotel_target()
    card = build_card(CaseFile(), PersonNetwork(graph), "p1")
    assert card["hotel_stays"][0]["hotel"] == "Hotel Sample" and card["police_officer"]
    assert card["records"][0]["system"] == "Hotel Eye" and "room 206" in card["records"][0]["fields"][0]
    assert card["checked_nothing_found"] == ["cro"] and card["could_not_be_checked"] == ["nadra"]
    assert "MUHAMMAD HASNAIN AZAM" in card["also_recorded_as"]
    assert set(SYSTEMS) - {"fir_roster"} <= set(MEANING)                  # every police system is explained
    seen = {}

    def draft(prompt):
        seen.update(json.loads(prompt)["given"])
        return {"first_line": "Yes: one stay at Hotel Sample, Larkana, 25-26 December 2024 [Hotel Eye].", "answered": True,
                "sections": [{"name": "The basis", "body": "• Hotel Sample, room 206, check-in 2024-12-25 [Hotel Eye]"}]}

    llm = Model(_Reading=_reading(people=["hasnain"], shape="yes_no", yes_no=True, type="hotels"), _Draft=draft, _Check=OK)
    final = list(run_turn(graph, "is there any record of hasnain in hotel ?", case=CaseFile(), llm=llm))[-1]
    assert final["query"]["people"] == ["p1"]                              # the target, not "Hassan"
    assert seen["dossiers"][0]["hotel_stays"] and "Hotel Eye" in seen["what_each_system_tells"]
    assert "stayed with them" in seen["what_each_system_tells"]["Hotel Eye"]
    assert "Hotel Sample" in final["answer"] and "[Hotel Eye]" in final["answer"]


def test_the_new_case_is_what_the_officer_described_never_a_past_fir():
    from sherlocks.linkgraph.question_reader import _shape
    from sherlocks.linkgraph.relevance import the_new_case
    from sherlocks.team import officer_agent
    from sherlocks.team.runtime import NEW_CASE

    case = CaseFile()
    assert the_new_case(case)["described_by_the_officer"] is False
    assert the_new_case(case)["which_case"].startswith("not said yet") and "not known to be arrested" in the_new_case(case)["target_status"]
    case.set_incident({"offence": "fraud: he took money and never delivered"})
    told = the_new_case(case)
    assert told["described_by_the_officer"] and told["offence"].startswith("fraud") and "date" not in told
    seen = {}

    def draft(prompt):
        seen.update(json.loads(prompt)["given"])
        return {"first_line": "The new case is a fraud complaint; its date is not on record yet.", "answered": True, "sections": []}

    def check(prompt):
        seen["checker"] = json.loads(prompt)
        return OK

    llm = Model(_Reading=_reading(people=[], scope="case", shape="other", about_new_case=True), _Draft=draft, _Check=check)
    list(run_turn(_graph(), "is case ki tareekh kya hai?", case=case, llm=llm))
    assert seen["the_new_case"]["offence"].startswith("fraud") and "incident" not in seen["case"]
    assert seen["checker"]["the_new_case_as_the_officer_described_it"]["offence"].startswith("fraud")
    assert "PAST RECORDS" in NEW_CASE and NEW_CASE in officer_agent._SYSTEM
    # The answer's shape follows the model's own reading: a connection is a link; one thing asked is a focused answer.
    assert _shape({"shape": "person", "scope": "connection"}, ["a", "b"], "") == "link"
    assert _shape({"shape": "person", "scope": "person"}, ["a"], "registered owner") == "other"
    assert _shape({"shape": "person", "scope": "person"}, ["a"], "") == "person"


def test_sherlock_asks_which_case_this_is_and_takes_the_officers_word():
    """Three situations only the officer can tell apart: a target with no record, a target
    with earlier cases (both: a suspect in a new matter), and a target arrested in an FIR
    that IS the case being worked."""
    from sherlocks.evidence.questioner import gaps
    from sherlocks.linkgraph.relevance import the_new_case

    graph = _graph()                                       # the target is named in FIR 45/2023
    case = CaseFile()
    first = gaps(case, graph)[0]
    assert first["key"] == "case:which" and "45/2023" in first["text"]
    # (3) "it is that FIR, and we have arrested him": that FIR describes this case.
    llm = Model(_Reading={"act": "answer"}, _Record={
        "entries": [], "this_case": "an FIR already on record", "this_case_fir": "45/2023", "target_in_custody": "arrested",
        "read_back": "Theek hai: yeh case FIR 45/2023 hai aur mulzim giraftar hai."})
    list(run_turn(graph, "yehi FIR 45/2023 hai, usay giraftar kar liya hai", case=case, llm=llm))
    told = the_new_case(case)
    assert case.dialog["case_basis"] == "fir_on_record" and case.incident["fir"] == "45/2023"
    assert "IS FIR 45/2023" in told["which_case"] and told["target_status"] == "arrested"
    assert "case:which" not in [g["key"] for g in gaps(case, graph)]
    # (2) "no, he cheated me - a new matter": the FIR on file is his history.
    other = CaseFile()
    llm = Model(_Reading={"act": "statement"}, _Record={
        "entries": [{"what": "The target cheated the complainant of money", "quote": "usne mere saath fraud kiya",
                     "topic": "incident_offence", "value": "fraud"}],
        "this_case": "a new matter", "read_back": "Note kar liya: naya mamla, fraud."})
    list(run_turn(graph, "naya mamla hai, usne mere saath fraud kiya", case=other, llm=llm))
    told = the_new_case(other)
    assert other.dialog["case_basis"] == "new_matter" and told["offence"] == "fraud" and "fir" not in told
    assert "NEW matter" in told["which_case"] and "date" not in told       # the old FIR lends nothing to it
    # (1) a target with no FIR on file: nothing to tell apart, so it is not asked.
    clean = {"nodes": [{"id": "p1", "kind": "person", "label": "Sajid Ali", "data": {"seed": True}}], "edges": []}
    assert "case:which" not in [g["key"] for g in gaps(CaseFile(), clean)]


def test_a_message_that_tells_and_asks_is_recorded_and_answered():
    from sherlocks.linkgraph.question_reader import _shape

    case = CaseFile()
    seen = {}

    def draft(prompt):
        seen.update(json.loads(prompt))
        return {"first_line": "Naye mamle (fraud) ki tareekh abhi record mein nahi; FIR 45/2023 us ka purana case hai.",
                "answered": True, "sections": []}

    llm = Model(_Reading=_reading(states_something=True, people=["Muhammad Danish Rafiq"], shape="other", scope="case"),
                _Record={"entries": [{"what": "Danish cheated the complainant of 5 lakh", "quote": "fraud kiya, 5 lakh liye",
                                      "topic": "incident_offence", "value": "fraud (5 lakh)", "about": "Muhammad Danish Rafiq"}],
                         "this_case": "a new matter", "read_back": "Note kar liya: naya mamla - fraud, 5 lakh."},
                _Draft=draft, _Check=OK)
    events = list(run_turn(_graph(), "naya mamla hai: Danish ne mere saath fraud kiya, 5 lakh liye - is case ki tareekh kya hai?",
                           case=case, llm=llm))
    final = events[-1]
    assert llm.asked[:3] == ["_Reading", "_Record", "_Draft"] and final["intent"] == "question"
    assert case.incident["offence"] == "fraud (5 lakh)" and case.dialog["case_basis"] == "new_matter"
    # The Officer agent is told the new case first, and what is on file is labelled a past record.
    assert seen["the_new_case"]["offence"] == "fraud (5 lakh)" and "NEW matter" in seen["the_new_case"]["which_case"]
    assert "PAST_RECORDS_on_file_not_the_new_case" in seen["given"] and "records_on_file" not in seen["given"]
    assert final["answer"].startswith("**Naye mamle (fraud) ki tareekh") and "Note kar liya: naya mamla" in final["answer"]
    # One particular thing asked is a focused answer; the whole picture keeps the full shape.
    assert _shape({"shape": "person", "whole_picture": False}, ["a"], "") == "other"
    assert _shape({"shape": "fir", "whole_picture": True}, [], "") == "fir"
    # The script is a fact: a message in Latin letters is never answered in Urdu script because the model said so.
    roman = Model(_Reading=_reading(language="ur"), _Draft=draft, _Check=OK)
    assert list(run_turn(_graph(), "Danish ka number kis ke naam par hai?", case=CaseFile(), llm=roman))[-1]["language"] == "roman"


def test_every_question_leaves_a_trace_of_which_agent_did_what_and_who_handed_what_to_whom():
    from sherlocks.team.trace import overview

    backend, case = _Backend({"evs": {"name": "Muhammad Danish Rafiq", "cnic": "9999900000777",
                                      "organisation": "Karachi Port Trust", "designation": "Clerk"}}), CaseFile()

    def draft(prompt):
        if not json.loads(prompt)["given"]["looked_up_now"]:
            return {"first_line": "Records mein Danish ka kaam nahi hai.", "answered": False,
                    "lookups": [{"tool": "api_router", "args": {"who": "Muhammad Danish Rafiq", "topic": "work"},
                                 "why": "his employer"}]}
        return {"first_line": "Danish Karachi Port Trust mein Clerk hai.", "answered": True,
                "sections": [{"name": "Identity", "body": "• Karachi Port Trust, Clerk [EVS]"}]}

    llm = Model(_Reading=_reading(topics=["work"]), _Draft=draft, _Check=OK)
    final = list(run_turn(_graph(), "Danish kya kaam karta hai?", case=case, llm=llm, backend=backend, live_calls=3))[-1]

    trace = overview(case, 1)
    assert trace is overview(case, 0) and trace["turn"] == 1 and trace["question"] == "Danish kya kaam karta hai?"
    # The path through the graph: the draft asked for a lookup and was written again with what it brought.
    assert trace["path"] == ["read", "brief", "draft", "lookup", "draft", "desk", "check", "present"]
    steps = {(s["n"], s["node"]): s for s in trace["steps"]}
    assert steps[(1, "read")]["agent"] == "O0 Question reader" and steps[(1, "read")]["reading"]["shape"] == "person"
    assert steps[(3, "draft")]["answered"] is False and steps[(3, "draft")]["lookups_asked"][0]["tool"] == "api_router"
    assert steps[(4, "lookup")]["lookups"][0]["found"] is True
    assert steps[(5, "draft")]["answered"] is True and steps[(5, "draft")]["sections"][0]["name"] == "Identity"
    assert steps[(7, "check")]["ok"] is True and all(s["ms"] is not None for s in trace["steps"])
    assert any(n["agent"] == "API agent" for n in steps[(4, "lookup")]["notes"])        # the agents' own notes
    # Who handed what to whom.
    flow = [(f["from"], f["to"]) for f in trace["flow"]]
    assert flow[2] == ("O1 Officer agent", "Toolbox (board tools and the API agent)") and "api_router" in trace["flow"][2]["handed_over"]
    assert flow[3] == ("Toolbox (board tools and the API agent)", "O1 Officer agent") and "found" in trace["flow"][3]["handed_over"]
    assert flow[-1] == ("Presenter", "the officer")
    # Every model call: which agent, in which step, what it was given and what it answered.
    calls = trace["model_calls"]
    assert [c["kind"] for c in calls][:1] == ["question_reader"] and [c["agent"] for c in calls].count("O1 Officer agent") == 2
    first = next(c for c in calls if c["kind"] == "officer_draft")
    assert first["ok"] and first["step"] == "draft" and first["answer"]["lookups"][0]["tool"] == "api_router"
    assert "question" in first["given"] and "given" in first["given"] and first["input_chars"] > 100
    assert trace["answer"].startswith("**Danish Karachi Port Trust") and trace["answer"] == final["answer"][:3000]
    assert trace["checks"]["ok"] is True and trace["done"] is True

    # A statement takes another path, and a failed model call is in the trace as failed.
    llm = Model(_Reading=_reading(act="statement", asks_something=False, states_something=True, shape="other"),
                _Record={"entries": [], "this_case": "a new matter", "read_back": "Note kar liya: naya mamla."}, _Check=OK)
    list(run_turn(_graph(), "yeh naya mamla hai", case=case, llm=llm))
    told = overview(case, 2)
    assert told["path"][:2] == ["read", "record"] and "brief" not in told["path"]
    assert next(s for s in told["steps"] if s["node"] == "record")["recorded"]
    broken = Model(_Reading=_reading(), _Check=OK)                                   # no draft scripted: the call fails
    list(run_turn(_graph(), "Danish kaun hai?", case=case, llm=broken))
    failed = overview(case, 3)
    assert any(c["kind"] == "officer_draft" and not c["ok"] and "no scripted answer" in c["error"] for c in failed["model_calls"])

    # The list, and the board keeps the traces when saved and loaded.
    listing = overview(case)
    assert [t["turn"] for t in listing["turns"]] == [1, 2, 3] and listing["turns"][0]["model_calls"] == len(calls)
    assert "officer_draft" in listing["turns"][2]["failed_calls"]
    again = CaseFile.from_dict(json.loads(json.dumps(case.to_dict(), default=str)))
    assert overview(again, 1)["path"] == trace["path"]


def test_the_trace_is_read_over_the_api():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sherlocks.api.auth import SessionAuth
    from sherlocks.api.linkgraph import build_router
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    s.app.output_dir = str(__import__("tempfile").mkdtemp())
    s.api.api_key = "k"
    mgr = memory_manager(s)
    handle = mgr.start(GraphRunParams(cnic="9999900000011", depth=1, max_persons=5, backend="demo"), wait=True)
    llm = Model(_Reading=_reading(people=[]), _Check=OK,
                _Draft={"first_line": "Graph par log maujood hain.", "answered": True, "sections": []})
    list(run_turn(handle.graph(), "is case mein kaun kaun hai?", case=handle.case, llm=llm))

    app = FastAPI()
    app.include_router(build_router(s, lambda: mgr, SessionAuth(s)))
    client = TestClient(app)
    url, h = f"{s.api.prefix}/graph/runs/{handle.id}/trace", {"X-API-Key": "k"}
    assert client.get(url).status_code in (401, 403)                                # behind the same sign-in as the case
    listing = client.get(url, headers=h).json()
    assert listing["run_id"] == handle.id and listing["turns"][-1]["question"] == "is case mein kaun kaun hai?"
    assert "background_runs" in listing and listing["turns"][-1]["path"][0] == "read"
    one = client.get(f"{url}/latest", headers=h).json()
    assert one["turn"] == listing["turns"][-1]["turn"] and one["steps"] and one["model_calls"] and one["flow"]
    assert client.get(f"{url}/{one['turn']}", headers=h).json()["question"] == one["question"]
    assert client.get(f"{url}/999", headers=h).status_code == 404
    assert client.get(f"{url}/abc", headers=h).status_code == 422
    assert client.get(f"{s.api.prefix}/graph/runs/nope/trace", headers=h).status_code == 404


def test_a_new_suspicion_about_a_crime_already_on_file_is_that_case_not_a_new_matter():
    """The officer suspects the complainant of the murder his own FIR reports: the same case."""
    graph = {"nodes": [{"id": "p1", "kind": "person", "label": "Rahim Bakhsh",
                        "data": {"seed": True, "cnic": "9999900000555",
                                 "firs": [{"key": "k", "label": "121/25", "ps": "Karampur", "role": "Complainant",
                                           "offence": "302", "system": "psrms"}]}}], "edges": []}

    def board():
        case = CaseFile()
        fir = case.add_document(kind="fir", key="fir:121", title="FIR 121/25 · PS Karampur", source="PSRMS",
                                text="FIR 121/25. Complainant Rahim Bakhsh reports the murder of his daughter Sana.",
                                ref={"fir_no": "121", "fir_year": "25"},
                                data={"sections": "302 PPC", "occurred": "30-12-2025 09:00 PM", "place": "Village Mir Beg",
                                      "police_station": "Karampur", "complainant": {"name": "Rahim Bakhsh"}})
        case.add_fact(fir, "Rahim Bakhsh reports the murder of his daughter Sana", "the murder of his daughter Sana")
        case.ask("case:which", "Rahim Bakhsh is already on record: FIR 121/25 ... a new matter?", priority="red")
        case.ask("free:old", "What is the new case you are investigating?")        # written before he said
        case.set_hypothesis("The fraud allegation is a separate, unverified matter.")
        return case

    seen = {}

    def record(prompt):
        seen.update(json.loads(prompt))
        return {"entries": [{"what": "Rahim killed his daughter himself", "quote": "Rahim killed his daughter by himself",
                             "topic": "incident_fir", "value": "Rahim killed his daughter by himself", "about": "Rahim"}],
                "answers": "Q1", "this_case": "an FIR already on record", "this_case_fir": "121/25",
                "suspected": ["Rahim Bakhsh"],
                "read_back": "Noted: you suspect Rahim Bakhsh killed his daughter Sana himself - the murder of FIR 121/25, "
                             "where he is the complainant. The officer is sure. Nothing else was mentioned."}

    reading = _reading(act="statement", asks_something=False, states_something=True, shape="other", scope="case",
                       crimes=["murder"], about_new_case=True, people=["Rahim Bakhsh"])
    case = board()
    final = list(run_turn(graph, "I have suspect that Rahim killed his daughter by himself !", case=case,
                          llm=Model(_Reading=reading, _Record=record, _Check=OK, _Sweep={"answered": []})))[-1]
    # The recorder is told what each FIR on file is about - that is how the statement is matched to its case.
    on_file = seen["firs_on_file"][0]
    assert on_file["fir"] == "121/25" and on_file["complainant"] == "Rahim Bakhsh" and "302" in on_file["sections"]
    assert any("murder of his daughter" in line for line in on_file["what_it_says"])
    # The same case: its FIR, the offence in the officer's word with the FIR's sections, date and place from the file.
    inc = case.incident
    assert case.dialog["case_basis"] == "fir_on_record" and inc["fir"] == "121/25"       # never the sentence itself
    assert inc["offence"].startswith("murder") and "302" in inc["offence"] and inc["date"] == "2025-12-30"
    assert case.roles == {"p1": "main suspect"}
    open_keys = {q["key"] for q in case.open_questions()}
    assert not {"case:which", "incident:what", "incident:when", "incident:place", "roles:main", "free:old"} & open_keys
    assert (final.get("asking") or {}).get("key") not in ("incident:what", "free:old", "case:which")
    assert all(h.get("stale") for h in case.hypotheses.values())       # formed before: to be thought through again
    assert any(a["task"] == "sherlock" for a in case.agenda)
    assert "Noted: you suspect Rahim Bakhsh" in final["answer"] and "Nothing else was mentioned" not in final["answer"]

    # When the recorder cannot tell which case it is, nothing is decided for it - the question stays open.
    case = board()
    vague = Model(_Reading=reading, _Check=OK, _Sweep={"answered": []},
                  _Record={"entries": [], "answers": "Q1", "this_case": "not said", "read_back": "Noted."})
    list(run_turn(graph, "he is involved", case=case, llm=vague))
    assert not case.dialog.get("case_basis") and "case:which" in {q["key"] for q in case.open_questions()}
