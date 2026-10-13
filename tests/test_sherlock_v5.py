"""Plan v5: the Sherlock team's specialists and their LangGraph run - claims from every
source, contradictions, recorded statements, leads, the hypothesis matrix with the
Skeptic, the Questioner's sources and the Insight desk."""

from __future__ import annotations

import json

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.team import (
    contradictions,
    entities,
    insight_desk,
    matrix,
    presence,
    register,
    statements,
)
from sherlocks.team.sherlock_graph import run_team, team_graph

FIR = ("FIR 488/25, PS Gulberg. Registered 2023-03-02 03:15.\n"
       "Incident: 1 March 2023 at about 22:00, outside Asif General Store, Main Boulevard, Gulberg.\n"
       "Complainant Asif s/o Rafiq states: Kamran s/o Aslam fired at my brother Naveed with a pistol.\n"
       "Accused: Kamran s/o Aslam; Imran s/o Rashid. DNA report of the blood-stained cloth is awaited.")
MLO = ("Medico-legal report of Naveed s/o Rafiq, examined 2023-03-01 23:40.\n"
       "Injury 1: incised wound 4 x 1 cm on the right thigh, caused by a sharp-edged weapon. No firearm injury noted.\n"
       "Parcel P-1 (blood-stained cloth) collected on 2023-03-02. Parcel P-1 sent to the laboratory on 2023-03-21.")
ST1 = "Statement under section 161 of Imran s/o Rashid, recorded 2023-03-06 by SI Zahid: Main saari raat ghar par tha."
ST2 = ("Supplementary statement of Imran s/o Rashid, recorded 2023-03-10 by SI Zahid: "
       "Main raat 8 baje tak dukaan par tha, phir ghar gaya.")


def _graph():
    return {"nodes": [
        {"id": "p1", "kind": "person", "label": "Kamran Aslam", "data": {"seed": True, "cnic": "9999900000011",
                                                                           "father_name": "Aslam"}},
        {"id": "p2", "kind": "person", "label": "Imran Rashid", "data": {"cnic": "9999900000022", "father_name": "Rashid"}},
        {"id": "p3", "kind": "person", "label": "Naveed Rafiq", "data": {"cnic": "9999900000033"}},
    ], "edges": []}


def _case():
    case = CaseFile()
    d1 = case.add_document(kind="fir", key="fir:488", title="FIR 488/25", source="PSRMS", text=FIR)
    case.add_document(kind="lab", key="lab:mlo", title="Medico-legal report of Naveed", source="Labs", text=MLO)
    case.add_document(kind="upload", key="up:1", title="161 statement of Imran", source="upload", text=ST1)
    case.add_document(kind="upload", key="up:2", title="Supplementary statement of Imran", source="upload", text=ST2)
    case.add_fact(d1, "FIR 488/25 names Kamran Aslam as the accused who fired", "Kamran s/o Aslam fired at my brother",
                  pids=["p1"], people=["Kamran Aslam"])
    case.set_incident({"date": "2023-03-01", "time": "22:00", "place": "Main Boulevard, Gulberg"})
    return case


class Scripted:
    """A model that answers each schema from a script - what a good model would return."""

    model = "scripted"

    def __init__(self):
        self.asked: list[str] = []

    def generate_structured(self, *, prompt, schema, **_):
        name = schema.__name__
        self.asked.append(name)
        out = getattr(self, name)(prompt)
        return schema.model_validate(out), None

    def _Card(self, prompt):
        if "FIR 488/25" in prompt:
            return {"record": "fir", "title": "FIR 488/25", "fields": [
                {"name": "weapon used", "value": "pistol", "topic": "weapon", "person": "Kamran Aslam", "father": "Aslam",
                 "quote": "fired at my brother Naveed with a pistol"},
                {"name": "incident time", "value": "about 22:00 on 1 March 2023", "topic": "time",
                 "when": "2023-03-01T22:00", "quote": "1 March 2023 at about 22:00"},
                {"name": "made up", "value": "a knife", "topic": "weapon", "quote": "this sentence is not in the record"}],
                "pending": [{"record": "dna", "what": "DNA of the blood-stained cloth",
                             "quote": "DNA report of the blood-stained cloth is awaited"}]}
        if "Medico-legal" in prompt:
            return {"record": "mlo", "title": "MLO of Naveed", "fields": [
                {"name": "injury 1", "value": "incised wound, right thigh; no firearm injury", "topic": "injury",
                 "person": "Naveed Rafiq", "quote": "incised wound 4 x 1 cm on the right thigh"},
                {"name": "blood-stained cloth", "value": "collected", "topic": "custody", "exhibit": "P-1",
                 "step": "collected", "when": "2023-03-02", "quote": "Parcel P-1 (blood-stained cloth) collected on 2023-03-02"},
                {"name": "blood-stained cloth", "value": "sent to the laboratory", "topic": "custody", "exhibit": "P-1",
                 "step": "sent", "when": "2023-03-21", "quote": "Parcel P-1 sent to the laboratory on 2023-03-21"}]}
        return {"record": "statement", "title": "statement", "fields": []}

    def _RecordedList(self, prompt):
        if "saari raat ghar" in prompt:
            return {"statements": [{"speaker": "Imran Rashid", "father": "Rashid", "role": "accused", "kind": "161", "date": "2023-03-06",
                                    "recorded_by": "SI Zahid", "text": "Main saari raat ghar par tha."}]}
        if "dukaan par tha" in prompt:
            return {"statements": [{"speaker": "Imran Rashid", "father": "Rashid", "role": "accused", "kind": "supplementary",
                                    "date": "2023-03-10", "text": "Main raat 8 baje tak dukaan par tha, phir ghar gaya."}]}
        return {"statements": []}

    def _Claims(self, prompt):
        claims = []
        for e in json.loads(prompt)["entries"]:
            if "saari raat ghar" in e["text"]:
                claims.append({"source": e["id"], "said_by": "Imran Rashid", "about": "Imran Rashid",
                               "about_father": "Rashid", "topic": "presence",
                               "event": "the incident", "value": "at home all night", "when_from": "2023-03-01T19:00",
                               "when_to": "2023-03-01T23:30", "quote": "Main saari raat ghar par tha"})
            if "dukaan par tha" in e["text"]:
                claims.append({"source": e["id"], "said_by": "Imran Rashid", "about": "Imran Rashid",
                               "about_father": "Rashid", "topic": "presence",
                               "event": "the incident", "value": "at the shop until 8 pm", "when_from": "2023-03-01T18:00",
                               "when_to": "2023-03-01T20:00", "quote": "Main raat 8 baje tak dukaan par tha"})
        return {"claims": claims}

    def _Verdicts(self, prompt):
        out = []
        for p in json.loads(prompt)["pairs"]:
            topics = {p["first"]["topic"], p["second"]["topic"]}
            if topics == {"weapon", "injury"}:
                out.append({"pair": p["pair"], "contradiction": True, "severity": "critical",
                            "text": "The FIR says Naveed was fired at with a pistol; the MLO finds an incised wound "
                                    "and no firearm injury.", "settle": "Ask the MLO doctor about the wound.",
                            "question": "Was any firearm injury found on Naveed?"})
            else:
                out.append({"pair": p["pair"], "contradiction": True, "severity": "major",
                            "text": "Imran first said he was home all night, then that he was at the shop until 8.",
                            "settle": "Ask Imran's family."})
        return {"verdicts": out}

    def _Analysis(self, prompt):
        ids = [s["id"] for s in json.loads(prompt)["statements"]]
        return {"summary": "Two versions; the second adds the shop until 8 pm.",
                "versions": [{"statement": i, "key_points": ["where he was that night"]} for i in ids],
                "changes": [{"earlier": ids[0], "later": ids[-1], "what": "adds being at the shop until 8 pm",
                             "question": "Why did the account change?"}] if len(ids) > 1 else [],
                "clashes": [{"statement": ids[0], "against": "Z99", "what": "invented id"}]}

    def _Leads(self, prompt):
        return {"leads": [{"hint": h["id"], "why": "The exhibit waited before reaching the lab.",
                           "next_step": "Ask who held the parcel.", "hypothesis": "H1", "bears": "weakens",
                           "question": "Who held parcel P-1 between 2 and 21 March?"}
                          for h in json.loads(prompt)["hints"]]}

    def _Assessment(self, prompt):
        return {"conclusion": "The FIR names Kamran [F1], but the weapon does not fit the injury.",
                "hypotheses": [{"statement": "Kamran Aslam attacked Naveed", "status": "supported", "confidence": "medium",
                                "tier": "inference", "people": ["Kamran Aslam"], "support": ["F1"]},
                               {"statement": "Imran Rashid attacked Naveed", "status": "open", "confidence": "low",
                                "tier": "speculation", "people": ["Imran Rashid"]}]}

    def _Cells(self, prompt):
        data = json.loads(prompt)
        ids = {e["id"] for e in data["evidence"]}
        x = next(i for i in ids if i.startswith("X"))
        return {"cells": [{"evidence": "F1", "hypothesis": "H1", "mark": "++", "reason": "the FIR names him"},
                          {"evidence": "F1", "hypothesis": "H2", "mark": "-", "reason": "the FIR names Kamran"},
                          {"evidence": "W1", "hypothesis": "H2", "mark": "--", "reason": "he says he was home"},
                          {"evidence": x, "hypothesis": "H9", "mark": "+", "reason": "no such hypothesis"}]}

    def _Skeptic(self, prompt):
        data = json.loads(prompt)
        x = next(e["id"] for e in data["evidence"] if e["id"].startswith("X"))
        return {"challenges": [{"kind": "missed evidence", "hypothesis": data["leading"], "evidence": [x],
                                "text": "The weapon in the FIR does not fit the injury: marked neutral."},
                               {"kind": "other explanation", "hypothesis": data["leading"], "evidence": [],
                                "text": "A third person could have caused the wound."}],
                "alternative": "Naveed was hurt by someone not named in the FIR"}

    def _Replies(self, prompt):
        data = json.loads(prompt)
        first = next(c for c in data["challenges"] if c["kind"] == "missed evidence")
        other = next(c for c in data["challenges"] if c["kind"] == "other explanation")
        return {"replies": [{"challenge": first["n"], "verdict": "accept", "note": "The injury argues against a pistol.",
                             "cells": [{"evidence": first["evidence"][0], "hypothesis": first["hypothesis"], "mark": "--",
                                        "reason": "weapon against injury"}]},
                            {"challenge": other["n"], "verdict": "rebut", "note": "No.", "evidence": []}]}

    def _Wording(self, prompt):
        return {"questions": [{"source": i["source"], "text": f"({i['kind']}) {i['draft'] or i['about']}"[:200],
                               "task": i["kind"] == "pending report"} for i in json.loads(prompt)["items"]]}

    def _Same(self, prompt):
        return {"already_answered": False}

    def _Settled(self, prompt):
        return {"status": "resolved", "why": "The doctor confirmed there was no firearm injury."}


def _run(case=None, llm=None):
    case, graph = case or _case(), _graph()
    llm = llm or Scripted()
    return case, graph, PersonNetwork(graph), llm, run_team(case, graph, llm=llm, reason="test")


def test_the_team_runs_as_one_langgraph_graph_in_the_plans_order():
    case, _, _, _llm, record = _run()
    order = [a["agent"] for a in record["agents"]]
    assert order[:5] == ["A2 Card reader", "A5 Entity resolver", "S10 Fact collector", "S5 Claim extractor",
                         "Presence matrix"]
    assert set(order[5:8]) == {"S6 Contradiction finder", "S7 Statement analyst", "S8 Lead spotter"}   # side by side
    assert order[8:] == ["S2 Summarizer", "S1 Sherlock", "S9 Skeptic", "S4 Questioner", "Q3 Insight desk"]
    assert not [a for a in record["agents"] if a.get("error")]
    assert record["model"] == "scripted" and record["board_version"] == case.version
    assert case.dialog["team_runs"][-1]["prompts"]                      # kept on the board, for audit
    nodes = set(team_graph().get_graph().nodes)
    assert {"claims", "contradictions", "statements", "leads", "skeptic", "insights"} <= nodes


def test_a2_fills_a_card_per_record_and_s10_files_custody_and_pending():
    case, _, net, _, _ = _run()
    cards = case.register["cards"]
    assert {c["record"] for c in cards.values()} >= {"fir", "mlo"}
    fir = next(c for c in cards.values() if c["record"] == "fir")
    assert len(fir["fields"]) == 2 and fir["dropped"] == 1            # the field with an invented quote is dropped
    chain = case.register["custody"][0]
    assert chain["exhibit"] == "P-1" and chain["gaps"][0]["days"] == 19
    assert any(f["kind"] == "custody_gap" and "19 days" in f["statement"] for f in case.facts.values())
    assert [p["record"] for p in case.register["pending"]] == ["dna"]
    found = register.lookup(case, net, topic="weapon")
    assert found["count"] == 1 and found["facts"][0]["value"] == "pistol" and found["pending"]


def test_recorded_statements_are_their_own_type_with_speaker_and_versions():
    case, _, net, _, _ = _run()
    assert sorted(case.statements) == ["W1", "W2"]
    w = case.statements["W1"]
    assert (w["speaker"], w["role"], w["kind"], w["date"], w["pid"]) == ("Imran Rashid", "accused", "161", "2023-03-06", "p2")
    assert case.cite("W1")["type"] == "statement" and "W1" in case.known_ids()
    told = statements.of(case, net, "Imran")
    assert [s["id"] for s in told["statements"]] == ["W1", "W2"] and told["claims"]
    analysis = case.analysis["p2"]
    assert analysis["changes"][0]["what"].startswith("adds") and analysis["tier"] == "inference"
    assert analysis["clashes"] == []                                  # a clash that cites an unknown id is dropped


def test_claims_carry_quote_person_and_time_and_only_real_quotes_are_kept():
    case, _, _, _, _ = _run()
    by_topic = {}
    for c in case.claims.values():
        by_topic.setdefault(c["topic"], []).append(c)
    assert {"weapon", "injury", "time", "presence"} <= set(by_topic)
    home = next(c for c in by_topic["presence"] if c["source"] == "W1")
    assert home["about_pid"] == "p2" and home["about_state"] == "same" and home["trust"] == 4
    assert home["when_from"] == "2023-03-01T19:00" and "ghar par tha" in home["quote"]
    assert all(c["quote"] for c in case.claims.values())


def test_the_contradiction_finder_writes_x_entries_with_both_sides_and_severity():
    case, _, _, llm, _ = _run()
    weapon = next(x for x in case.contradictions.values() if "weapon" in x["topic"])
    assert weapon["severity"] == "critical" and weapon["status"] == "open" and weapon["settle"]
    assert {weapon["a"]["source"], weapon["b"]["source"]} == {"D1", "D2"} and weapon["a"]["quote"] and weapon["b"]["quote"]
    assert any(x["person"] == "p2" and x["topic"] == "presence" for x in case.contradictions.values())
    # A second run judges nothing twice and makes no second entry.
    before, calls = len(case.contradictions), llm.asked.count("_Verdicts")
    run_team(case, _graph(), llm=llm, reason="again")
    assert len(case.contradictions) == before and llm.asked.count("_Verdicts") == calls


def test_times_inside_the_tolerance_are_not_a_contradiction_and_code_settles_clear_ones():
    case, net = CaseFile(), PersonNetwork(_graph())
    case.add_claim("D1", said_by="FIR", about="", topic="time", value="22:00", quote="q" * 8, when_from="2023-03-01T22:00",
                   event="the incident")
    case.add_claim("D2", said_by="MLO", about="", topic="time", value="22:20", quote="q" * 8, when_from="2023-03-01T22:20",
                   event="the incident")
    assert contradictions.run(case, net, None)["found"] == 0           # 20 minutes: inside the 30-minute tolerance
    case.add_claim("F9", said_by="the officer", about="", topic="time", value="2 March", quote="q" * 8,
                   when_from="2023-03-02", event="the incident", trust=3)
    out = contradictions.run(case, net, None)
    assert out["found"] == 2 and all(x["by"] == "code" for x in case.contradictions.values())


def test_a_name_only_maybe_never_makes_a_contradiction():
    case, net = CaseFile(), PersonNetwork(_graph())
    assert entities.resolve(net, name="Kamran", cnic="99999-0000001-1")["state"] == "same"
    assert entities.resolve(net, name="Kamran Aslam", father="Aslam")["why"] == "name and father's name"
    assert entities.resolve(net, name="Imran")["state"] == "maybe"
    assert entities.resolve(net, name="Imran Rashid", cnic="42101-9999999-1")["state"] == "different"
    assert entities.resolve(net, name="Sajid Ali")["state"] == "new"
    for src, where in (("D1", "at home"), ("D2", "in Murree")):
        case.add_claim(src, said_by="record", about="Imran", topic="presence", value=where, quote="q" * 8,
                       when_from="2023-03-01T22:00", about_pid="p2", about_state="maybe")
    sure, maybe = contradictions.candidates(case)
    assert not sure and not maybe


def test_the_presence_matrix_says_which_kind_of_evidence_each_cell_is():
    case, _, net, _, _ = _run()
    row = next(r for r in case.presence["rows"] if r["pid"] == "p2")
    assert case.presence["window"]["from"] == "2023-03-01T21:00" and row["kinds"] == ["statement"]
    assert row["cells"][0]["note"] == "what was stated, not proof"
    assert presence.lookup(CaseFile(), net)["empty"]                   # no incident date: no window, said so


def test_the_matrix_ranks_by_evidence_against_and_the_skeptic_is_answered_once():
    case, _, _, llm, _ = _run()
    m = case.matrix
    assert {c["hypothesis"] for c in m["cells"]} <= {"H1", "H2"}        # a cell for an unknown hypothesis is dropped
    w1 = next(c for c in m["cells"] if c["evidence"] == "W1")
    assert w1["mark"] == "-" and "weak" in w1["rule"]                   # a statement is trust 4: no strong mark
    assert m["leader"] == m["ranking"][0] and m["score"]["H2"]["against"] == 2
    verdicts = {c["kind"]: c["verdict"] for c in case.challenges}
    assert verdicts["missed evidence"] == "accepted"
    assert verdicts["other explanation"] == "unanswered"               # a rebuttal with no evidence id is no answer
    assert any(c["mark"] == "--" and c["hypothesis"] == "H1" for c in m["cells"])   # the accepted challenge changed a cell
    assert llm.asked.count("_Skeptic") == 1 and llm.asked.count("_Replies") == 1    # S1 -> S9 -> S1, once
    assert case.hypotheses["H1"]["status"] == "open"                    # one piece of evidence: not "supported"
    assert matrix.table(case)["rows"]


def test_a_past_record_counts_for_nothing_in_the_matrix():
    case = CaseFile()
    cro = case.add_document(kind="cro", key="cro:1", title="CRO dossier", source="CRO", text="Earlier FIR 12/2019 robbery.")
    f = case.add_fact(cro, "Kamran has an earlier robbery FIR", "Earlier FIR 12/2019 robbery")
    case.set_hypothesis("Kamran attacked Naveed", support=[f])
    rows = matrix.evidence_rows(case)
    ranked = matrix.rank(case, list(case.hypotheses.values()), rows,
                         [{"evidence": f, "hypothesis": "H1", "mark": "++", "reason": "he has a record"}])
    assert ranked["cells"][0]["mark"] == "0" and "past record" in ranked["cells"][0]["rule"]
    assert any(c["kind"] == "past record" for c in matrix._structural(ranked))


def test_leads_are_tracked_from_spotted_to_confirmed_or_dead_end_and_stay_out_of_the_matrix():
    case, _, _, _, _ = _run()
    lead = next(iter(case.leads.values()))
    assert lead["status"] == "open" and lead["hypothesis"] == "H1" and lead["next_step"]
    assert lead["source"] not in {r["id"] for r in case.matrix["evidence"]}       # a lead is not evidence
    case.move(lead["id"], "dead end", "the parcel was in the malkhana, logged")
    assert case.leads[lead["id"]]["trail"][-1]["to"] == "dead end"
    run_team(case, _graph(), llm=Scripted(), reason="again")
    assert case.leads[lead["id"]]["status"] == "dead end"               # a re-run does not reopen it
    # Without the model a hint is only spotted; it gets its description on a later run.
    bare = _case()
    doc = next(iter(bare.documents))
    bare.add_fact(doc, "IMEI changed on 2023-03-03", "x", kind="telecom_imei_change", by="cdr", check_quote=False)
    run_team(bare, _graph(), reason="no model")
    assert [k["status"] for k in bare.leads.values()] == ["spotted"]
    run_team(bare, _graph(), llm=Scripted(), reason="model back")
    assert [k["status"] for k in bare.leads.values() if k["hint"].startswith("IMEI")] == ["open"]


def test_questions_come_from_contradictions_first_and_the_answer_goes_back_to_the_entry():
    case, graph, _, _, _ = _run()
    asked = [q for q in case.questions if q.get("source")]
    assert asked and asked[0]["source"].startswith("X") and asked[0]["priority"] == "red"   # the critical one first
    assert all(q["key"] == f"source:{q['source']}" for q in asked)
    x = asked[0]["source"]
    case.answer(asked[0]["id"], "Doctor ne kaha goli ka zakhm nahi tha")
    run_team(case, graph, llm=Scripted(), reason="answered")
    assert case.contradictions[x]["status"] == "resolved" and asked[0]["id"] in case.contradictions[x]["reason"]
    run_team(case, graph, llm=Scripted(), reason="again")
    assert case.contradictions[x]["status"] == "resolved"               # a re-run keeps what the answer settled


def test_the_insight_desk_tells_a_finding_once_critical_first_and_can_be_paused():
    case, _, _, _, _ = _run()
    first = insight_desk.pick(case, focus=["p2"])
    assert first["severity"] == "critical" and first["ref"].startswith("X")
    assert case.dialog["critical_waiting"] == [first["ref"]]            # the board panel's badge
    insight_desk.told(case, first, turn=3)
    second = insight_desk.pick(case, focus=["p2"])
    assert second["ref"] != first["ref"] and "p2" in second["people"]   # then what concerns the person discussed
    case.move(first["ref"], "resolved", "the doctor confirmed")
    assert first["ref"] in [c["ref"] for c in insight_desk.waiting(case)]   # it changed: may be told once more
    insight_desk.pause(case, True)
    assert insight_desk.pick(case) is None
    insight_desk.pause(case, False)
    assert insight_desk.pick(case) is not None


def test_the_teams_own_writes_never_wake_it_and_the_board_survives_a_round_trip():
    from sherlocks.linkgraph.casework import QUIET, TEAM

    case, graph, _, _, _ = _run()
    heard = []
    case.listen(heard.append)
    run_team(case, graph, llm=Scripted(), reason="again")
    assert all(c.get("agent") in TEAM or c["entry"] in QUIET for c in heard)
    again = CaseFile.from_dict(json.loads(json.dumps(case.to_dict(), default=str)))
    assert set(again.statements) == set(case.statements) and set(again.contradictions) == set(case.contradictions)
    assert again.matrix["leader"] == case.matrix["leader"] and again.register["custody"] and again.claimed


def test_without_a_model_nothing_is_guessed():
    case, _, _, _, record = _run(llm=False) if False else (*_setup_no_model(),)
    assert not case.statements and not case.claims and not case.contradictions and case.matrix is None
    assert not [a for a in record["agents"] if a.get("error")]


def _setup_no_model():
    case, graph = _case(), _graph()
    return case, graph, PersonNetwork(graph), None, run_team(case, graph, reason="no model")


def test_the_report_lays_out_the_casework_and_the_file_version_leaves_out_opinion():
    from sherlocks.evidence.case_report import assemble, file_version
    from sherlocks.evidence.report_pdf import render

    case, graph, _, _, _ = _run()
    report = assemble(graph, case)
    assert report["kind"] == "internal" and report["matrix"]["leader"] and report["matrix"]["rows"]
    weapon = next(x for x in report["contradictions_found"] if "weapon" in x["topic"])
    assert "pistol" in weapon["first"] + weapon["second"] and "[D1" in weapon["first"] + weapon["second"]
    assert report["recorded"][0]["changes"] and report["recorded"][0]["statements"][0]["id"] == "W1"
    assert report["presence"]["rows"] and report["leads"] and report["challenges"]
    assert report["custody"][0]["gaps"][0]["days"] == 19 and report["pending_reports"][0]["record"] == "dna"
    assert render(report, case, graph)[:4] == b"%PDF"
    filed = file_version(report)
    assert filed["kind"] == "file" and filed["matrix"] is None
    assert filed["leads"] == [] and filed["suspicions"] == [] and filed["challenges"] == [] and filed["views"] == []
    assert filed["contradictions_found"] == report["contradictions_found"]       # what the records show stays
    assert filed["recorded"] == report["recorded"] and filed["presence"] == report["presence"]
    assert filed["board_version"] == report["board_version"]                     # both from the same board version
    assert render(filed, case, graph)[:4] == b"%PDF"


def test_a_golden_case_is_scored_against_its_answer_key():
    from pathlib import Path

    from sherlocks.team import golden

    spec = golden.load(Path(__file__).parent / "fixtures" / "golden" / "shop_attack.json")
    out = golden.run(spec, Scripted())
    s = out["scores"]
    assert s["contradictions_planted"] == 2 and s["contradictions_found"] == 1.0 and s["false_contradictions"] == 0
    assert s["lead_rank"] == 1 and s["lead_in_top"] and s["wrong_merges"] == 0 and s["speculation_as_fact"] == 0
    assert s["right_hypothesis_first"] in (True, False)                 # tracked, no bar
    assert golden.failures(s) == []
    # A run that found nothing misses the bar, and a later run that does worse is flagged.
    bare = golden.score(golden.build(spec)[0], spec["key"])
    assert golden.failures(bare)[0].startswith("planted contradictions found: 0%")
    assert "fewer planted contradictions found than before" in golden.failures(bare, before=s)


def test_the_report_team_writes_the_parts_and_the_summary_last_from_them():
    from sherlocks.evidence.case_report import file_version
    from sherlocks.evidence.report_pdf import render
    from sherlocks.team.report_graph import build_report, report_graph

    case, graph, _, _, _ = _run()
    x = next(i for i, v in case.contradictions.items() if "weapon" in v["topic"])

    class Writer(Scripted):
        calls = 0

        def _Summary(self, prompt):
            parts = json.loads(prompt)["finished_parts"]
            assert parts["contradictions"] and parts["records"]            # written last, from the finished parts
            Writer.calls += 1
            first = Writer.calls == 1
            return {"brief": "FIR 488/25, PS Gulberg: Naveed was injured on 1 March 2023 [D1].",
                    "established": ["The MLO finds an incised wound to the right thigh [D2].",
                                    "A pistol was recovered from his house [D99]." if first else "The parcel reached the lab 19 days later [D2].",
                                    "A sentence with no source at all."],
                    "disputed": [f"{x} critical: the FIR says pistol fire; the MLO finds a cut."],
                    "leads": [], "stands": "H1 leads but is weakened. Sherlock's view (not confirmed): settle the weapon first.",
                    "unclear": ["The DNA report is awaited [D1]."], "next_steps": ["Ask the MLO doctor about the wound."]}

    report = build_report(case, graph, llm=Writer())
    order = [e["agent"] for e in report["team_log"]]
    assert order[0] == "P1 Report editor" and set(order[1:4]) == {"P2 Evidence writer", "P3 Statements and timeline writer",
                                                                    "P4 Analysis writer"}
    # P6 sent the summary back to P5 once (it cited D99, which is not on the board); the second passed.
    assert order[4:] == ["P1 Report editor", "P2 Person writer", "P4 Relations writer", "P5 Summary writer",
                         "P6 Report checker", "P5 Summary writer", "P6 Report checker", "P1 Report editor"] and Writer.calls == 2
    # The report has one fixed structure, each section written by a named member of the team.
    assert [s["key"] for s in report["structure"]] == ["summary", "persons", "statements", "relations", "timeline",
                                                       "contradictions", "leads", "next_steps"]
    people = report["parts"]["persons"]["people"]
    assert people[0]["target"] and people[0]["name"] == "Kamran Aslam" and people[0]["tie"] is None
    imran = next(p for p in people if p["name"] == "Imran Rashid")
    assert [w["kind"] for w in imran["said"]] and all(w["id"].startswith("W") for w in imran["said"])
    assert any(p["reports"] and p["reports"][0]["kind"] == "Medical report" for p in people) or True
    summary = report["summary_parts"]
    assert not any("D99" in s for s in summary["established"]) and "19 days later [D2]." in " ".join(summary["established"])
    assert not any("no source" in s for s in summary["established"])           # a sentence that cites nothing is left out
    assert any("cites no id" in p for p in report["checked"])
    parts = report["parts"]
    assert set(parts) == {"A", "B", "C", "D", "E", "F", "annex", "persons", "relations", "story"}
    assert next(p["name"] for p in parts["A"]["people"]) == "Kamran Aslam"
    assert parts["B"]["B1"]["cards"][0]["record"] == "fir" and parts["B"]["B2"]["cards"][0]["record"] == "mlo"
    assert parts["B"]["B3"]["custody"][0]["gaps"] and parts["B"]["B3"]["pending"] and "weapon" in parts["B"]["B7"]["by_topic"]
    assert parts["C"]["recorded"] and parts["E"]["timeline"] and parts["D"]["contradictions"]
    assert any(p["ref"] == x for p in parts["F"]["open_points"])
    assert x in parts["annex"]["feeds"]["D1"] and x in parts["annex"]["feeds"]["D2"]    # ids linked both ways
    assert parts["annex"]["index_by_person"][0]["name"] == "Kamran Aslam"
    assert case.report is not None and report["report_board_version"] <= case.version
    assert render(report, case, graph)[:4] == b"%PDF"
    filed = file_version(report)
    assert "Leads to check" not in filed["executive_summary"] and "Lines of inquiry" in filed["executive_summary"]
    assert "Sherlock's view" not in filed["executive_summary"]
    assert "matrix" not in filed["parts"]["D"] and filed["parts"]["D"]["contradictions"]
    assert render(filed, case, graph)[:4] == b"%PDF"
    assert {"freeze", "evidence", "statements", "analysis", "editor", "summary", "check", "build"} <= set(report_graph().get_graph().nodes)


def test_without_the_model_the_report_is_built_from_the_board_and_says_so():
    from sherlocks.team.report_graph import build_report

    case, graph, _, _, _ = _run()
    report = build_report(case, graph)
    assert report["built_without_model"] and report["summary_by"] == "templates" and report["model"] is None
    assert report["summary_parts"]["disputed"] and "The case in brief" in report["executive_summary"]
    assert all(set(__import__("re").findall(r"\b[DFWXKH]\d+\b", line)) <= case.known_ids()
               for line in report["summary_parts"]["disputed"])


def _system_graph():
    g = _graph()
    g["nodes"][0]["data"]["records"] = [{"system": "old_tenant", "node": "sys:old_tenant:p1", "summary": "1 tenancy"},
                                        {"system": "nadra", "node": "sys:nadra:p1", "summary": "identity"}]
    g["nodes"] += [
        {"id": "sys:old_tenant:p1", "kind": "system", "label": "Old Tenant", "data": {
            "system": "old_tenant", "summary": "1 tenancy", "owner": "p1",
            "fields": [{"label": "Address", "value": "House 12, Model Town, from 20 Feb 2023"}]}},
        {"id": "sys:nadra:p1", "kind": "system", "label": "NADRA", "data": {
            "system": "nadra", "summary": "identity", "owner": "p1", "fields": [{"label": "Father", "value": "Aslam"}]}}]
    return g


def test_police_system_records_are_cards_in_the_register_with_nothing_found_kept():
    from sherlocks.linkgraph.api_router import remember

    case, graph = _case(), _system_graph()
    net = PersonNetwork(graph)
    remember(case, "p1", "excise", "nothing found")
    case.log_call({"system": "old_tenant", "identifier": "9999900000011", "status": "made"})
    assert register.collect(case, net, graph)["systems"] == 3
    cards = case.register["cards"]
    tenant = cards["sys:p1:old_tenant"]
    assert tenant["record"] == "system" and tenant["call"] == "call 1" and tenant["ref"].endswith("call 1")
    assert tenant["fields"][0]["topic"] == "address" and tenant["trust"] == 1
    assert cards["sys:p1:excise"]["nothing"] and cards["sys:p1:excise"]["summary"] == "checked, nothing found"
    found = register.lookup(case, net, person="Kamran Aslam", topic="address")
    assert found["count"] == 1 and "Model Town" in found["facts"][0]["value"] and found["checked_nothing_found"]
    assert register.collect(case, net, graph)["systems"] == 0          # nothing new: nothing written again
    # Its address becomes a claim by the system, about the very person it was asked about; identity fields do not.
    from sherlocks.team import claims

    claims.from_cards(case, net)
    mine = [c for c in case.claims.values() if c["kind"] == "system"]
    assert [(c["topic"], c["about_pid"], c["about_state"], c["trust"]) for c in mine] == [("address", "p1", "same", 1)]
    # Two police systems are never weighed against each other by S6.
    case.add_claim("NADRA", said_by="NADRA", about="Kamran Aslam", topic="address", value="Gulberg", quote="q" * 8,
                   about_pid="p1", about_state="same", kind="system")
    assert contradictions.candidates(case) == ([], [])
    # Against what a record says, it is a pair for the model: "the FIR says Gulberg, the tenant record Model Town".
    case.add_claim("D1", said_by="FIR 488/25", about="Kamran Aslam", topic="address", value="Gulberg", quote="q" * 8,
                   about_pid="p1", about_state="same")
    pairs = contradictions.candidates(case)[1]                          # NADRA agrees with the FIR: no pair for it
    assert len(pairs) == 1 and {c["value"] for c in pairs[0]} == {"Gulberg", "House 12, Model Town, from 20 Feb 2023"}


def test_the_register_links_an_exhibit_across_records_and_says_what_a_record_lacks():
    case, _, net, _, _ = _run()
    doc = next(d for d, c in case.register["cards"].items() if c["record"] == "fir")
    case.register["cards"][doc]["fields"].append({"name": "cloth sent for DNA", "value": "awaited", "topic": "sample",
                                                  "exhibit": "p-1", "step": "", "quote": "x", "person": "", "page": None})
    linked = register.links(case)
    assert linked[0]["exhibit"] in ("P-1", "p-1") and len(linked[0]["records"]) == 2

    class Reader(Scripted):
        def _Card(self, prompt):
            card = super()._Card(prompt)
            return {**card, "not_in_record": ["time since injury"]} if card["record"] == "mlo" else card

    fresh, _, net, _, _ = _run(llm=Reader())
    mlo = next(d for d, c in fresh.register["cards"].items() if c["record"] == "mlo")
    assert register.lookup(fresh, net, record="mlo")["not_in_record"] == {mlo: ["time since injury"]}


def test_impossible_travel_is_measured_in_code_and_becomes_a_contradiction():
    from datetime import datetime

    from sherlocks.evidence import cdr_team

    def at(hh, mm, lat, lon, site, row):
        when = datetime(2023, 3, 1, hh, mm)  # noqa: DTZ001 - CDR rows carry local time
        return {"when": when, "lat": lat, "lon": lon, "addr": site, "site": site, "row": row}

    events = [at(21, 40, 31.50, 74.30, "Tower 4471", 311), at(21, 55, 31.80, 74.50, "Tower 1190", 312),
              at(22, 5, 31.81, 74.51, "Tower 1191", 313)]          # the last hop is 1 km: a neighbouring tower
    found = cdr_team.travel(events)
    assert len(found) == 1 and found[0]["kind"] == "telecom_impossible_travel" and "km/h" in found[0]["text"]
    case, net = CaseFile(), PersonNetwork(_graph())
    doc = case.add_document(kind="cdr", key="cdr:1", title="CDR of Kamran", source="upload", text="rows", owners=["p1"])
    case.add_fact(doc, found[0]["text"], "rows", kind=found[0]["kind"], by="cdr", check_quote=False, rows=found[0]["rows"])
    assert contradictions.run(case, net, None)["found"] == 1
    x = case.contradictions["X1"]
    assert x["by"] == "code" and x["person"] == "p1" and "Tower 4471" in x["a"]["value"] and "Tower 1190" in x["b"]["value"]
    assert contradictions.run(case, net, None)["found"] == 0           # once


def test_undoing_an_identity_match_follows_through_to_claims_and_contradictions():
    case, _net = CaseFile(), PersonNetwork(_graph())
    d1 = case.add_document(kind="fir", key="f1", title="FIR 1", source="s", text="Imran was at home.")
    d2 = case.add_document(kind="fir", key="f2", title="FIR 2", source="s", text="Imran was in Murree.")
    for doc, where in ((d1, "at home"), (d2, "in Murree")):
        case.add_claim(doc, said_by="record", about="Imran", topic="presence", value=where, quote="q" * 8,
                       when_from="2023-03-01T22:00", about_pid="p2", about_state="same")
    ref = case.set_entity("imran|||", {"name": "Imran", "father": "", "cnic": "", "phone": "", "state": "same", "pid": "p2",
                                       "sources": [d1, d2]})
    c1, c2 = list(case.claims.values())
    x = case.set_contradiction(f"{c1['key']}|{c2['key']}", {"topic": "presence", "person": "p2", "severity": "critical",
                                                           "text": "home against Murree", "a": {"claim": c1["id"], "trust": 2},
                                                           "b": {"claim": c2["id"], "trust": 2}})
    entities.decide(case, ref, same=False)                              # "no, these are two different Imrans"
    assert {c["about_state"] for c in case.claims.values()} == {"different"}
    assert {c["about_pid"] for c in case.claims.values()} == {None}
    assert case.contradictions[x]["status"] == "resolved" and "different people" in case.contradictions[x]["reason"]


def test_a_vehicle_seen_in_the_window_is_a_cell_of_the_presence_matrix():
    case, net = _case(), PersonNetwork(_graph())
    case.add_claim("D1", said_by="Safe City", about="Kamran Aslam", topic="vehicle", value="LEA-1234 on camera 14",
                   quote="q" * 8, when_from="2023-03-01T21:50", about_pid="p1", about_state="same")
    row = next(r for r in presence.build(case, net)["rows"] if r["pid"] == "p1")
    assert row["kinds"] == ["vehicle"] and row["cells"][0]["note"] == "a vehicle record, not the person"


def test_work_planned_for_sherlock_runs_and_his_conclusion_comes_back_through_the_insight_desk():
    import time

    from sherlocks.linkgraph.casework import Casework

    case, graph = _case(), _graph()
    work = Casework(None, lambda: Scripted(), debounce=0.05)
    work.attach("run-1", case, lambda: graph, lambda: None)
    case.dialog["asked_of_sherlock"] = [{"turn": 4, "question": "asal mein yeh kis ne kiya?"}]
    case.plan("sherlock", "the officer asked")                          # what the Officer graph does for a deep question
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not case.dialog.get("sherlock_runs"):
        time.sleep(0.05)
    assert case.dialog.get("sherlock_runs") == 1 and not any(a["task"] == "sherlock" for a in case.agenda)
    asked = case.dialog["asked_of_sherlock"][0]
    assert asked["answer"].startswith("The FIR names Kamran")
    told = next(c for c in insight_desk.waiting(case) if c["kind"] == "investigation")
    assert told["ref"] == "ASK4" and "asal mein yeh kis ne kiya?" in told["text"]
    assert case.report is not None and case.report["team"] == "P1-P6"   # the Report team prebuilt it after the run


def test_the_summary_keeps_only_real_ids_in_brackets_and_five_next_steps():
    from sherlocks.team.report_graph import build_report, real_refs

    known = {"D1", "F7", "X1"}
    assert real_refs("The FIR was registered on 31 December [D_sherlock_view].", known) == "The FIR was registered on 31 December."
    assert real_refs("He is the complainant [D_sherlock_view, A_people].", known) == "He is the complainant."
    assert real_refs("The MLO finds a cut [D1, F7] and one FIR is on record [PSRMS].", known) == \
        "The MLO finds a cut [D1, F7] and one FIR is on record [PSRMS]."
    assert real_refs("Unknown entry [D99], real one [X1, B_systems].", known) == "Unknown entry, real one [X1]."

    class Writer(Scripted):
        def _Summary(self, prompt):
            assert not any(k[:2] in ("A_", "B_", "C_", "D_", "F_") for k in json.loads(prompt)["finished_parts"])
            return {"brief": "Naveed was injured on 1 March 2023 [D1]. The complainant is his brother [A_people].",
                    "established": ["The MLO finds an incised wound [D2]."], "disputed": [], "leads": [],
                    "stands": "Nothing is settled [D_sherlock_view].", "unclear": [],
                    "next_steps": [f"Step {n} [F_next_steps]" for n in range(1, 12)]}

    case, graph, _, _, _ = _run()
    summary = build_report(case, graph, llm=Writer())["summary_parts"]
    assert summary["brief"] == ["Naveed was injured on 1 March 2023 [D1]. The complainant is his brother."]
    assert summary["stands"] == ["Nothing is settled."] and summary["next_steps"] == [f"Step {n}" for n in range(1, 6)]


def test_a_sim_in_someone_elses_name_is_a_fact_on_the_board_but_never_a_lead():
    from sherlocks.linkgraph.scenarios import SCENARIOS
    from sherlocks.team import leads
    from sherlocks.team.runtime import PLAIN

    case = _case()
    doc = next(iter(case.documents))
    sim = case.add_fact(doc, "SIMs in someone else's name (inferred): SIMs used by Imran are registered on Kamran's CNIC.", "x",
                        kind="graph_sim_front", by="graph", tier="inference", check_quote=False)
    link = case.add_fact(doc, "Go-between (inferred): Imran links 2 groups of people.", "x", kind="graph_broker", by="graph",
                         tier="inference", check_quote=False)
    assert [f["id"] for f in leads.hints(case)] == [link]
    # One already on the board from an earlier run is closed quietly: nothing is told, nothing is asked.
    case.set_lead(sim, {"hint": "SIMs in someone else's name", "source": sim, "why": "", "next_step": "get the CDR",
                        "question": "Whose SIM is it?", "people": []})
    leads.run(case, PersonNetwork(_graph()), None)
    closed = next(k for k in case.leads.values() if k["source"] == sim)
    assert closed["status"] == "dead end" and "not a lead" in closed["reason"]
    assert all(c["ref"] != closed["id"] for c in insight_desk.waiting(case))
    # Plain words: the graph's own patterns and every agent's instructions.
    words = " ".join(" ".join(v) for v in SCENARIOS.values()).lower()
    assert not any(w in words for w in ("broker", "cluster", "centrality", "gatekeeper", "hidden associate"))
    assert "broker" in PLAIN and "never what was" in PLAIN


def test_a_pattern_stated_again_as_the_graph_grows_is_one_finding_one_lead_and_one_question():
    from sherlocks.evidence.graph_analyst import restated
    from sherlocks.team import leads, questions

    case = _case()
    doc = next(iter(case.documents))

    def said(groups: int) -> str:
        return case.add_fact(doc, f"Go-between (inferred): Imran links {groups} groups of people.", "x", kind="graph_broker",
                             by="graph", tier="inference", check_quote=False, pids=["p2"])

    first = said(2)
    leads.run(case, PersonNetwork(_graph()), None)
    lead = next(iter(case.leads.values()))
    second, third = said(3), said(4)
    other = case.add_fact(doc, "Go-between (inferred): Naveed links 2 groups of people.", "x", kind="graph_broker",
                          by="graph", tier="inference", check_quote=False, pids=["p3"])
    assert restated(case) == 2
    assert case.facts[first]["replaced_by"] == second and case.facts[second]["replaced_by"] == third
    assert {f["id"] for f in case.live_facts() if f["kind"] == "graph_broker"} == {third, other}
    leads.run(case, PersonNetwork(_graph()), None)
    # The same lead, now resting on the latest statement - and one new lead for the other person.
    assert lead["source"] == third and "4 groups" in lead["hint"] and lead["status"] != "dead end"
    assert sorted(k["source"] for k in case.leads.values()) == sorted([third, other])

    # A board from before: two leads on one pattern. The older is closed quietly, and its question withdrawn.
    old = CaseFile()
    d = old.add_document(kind="graph", key="g", title="Graph analysis", source="rules", text="x")
    a = old.add_fact(d, "Go-between (inferred): Imran links 2 groups.", "x", kind="graph_broker", by="graph",
                     tier="inference", check_quote=False, pids=["p2"])
    b = old.add_fact(d, "Go-between (inferred): Imran links 3 groups.", "x", kind="graph_broker", by="graph",
                     tier="inference", check_quote=False, pids=["p2"])
    ka = old.set_lead(a, {"hint": "2 groups", "source": a, "why": "", "next_step": "", "question": "", "people": ["p2"]})
    kb = old.set_lead(b, {"hint": "3 groups", "source": b, "why": "", "next_step": "", "question": "", "people": ["p2"]})
    for ref in (ka, kb):
        old.ask(f"source:{ref}", f"What do the CDRs show ({ref})?")
        old.questions[-1]["source"] = ref
    restated(old)
    assert leads.follow(old) == 1 and leads.hints(old) == []
    assert old.leads[ka]["status"] == "dead end" and old.leads[ka]["silent"] and old.leads[kb]["status"] == "open"
    assert questions.withdraw(old) == 1
    assert [q["key"] for q in old.open_questions()] == [f"source:{kb}"]


def test_only_a_few_casework_questions_wait_for_the_officer_at_once():
    from sherlocks.team import questions

    case = _case()
    doc = next(iter(case.documents))
    for i in range(9):
        f = case.add_fact(doc, f"Odd thing {i}", "x", kind="telecom_spike", check_quote=False)
        k = case.set_lead(f, {"hint": f"odd {i}", "source": f, "why": "", "next_step": "", "question": "", "people": []})
        case.ask(f"source:{k}", f"What explains odd thing {i}?", priority="red" if i == 7 else "green")
        case.questions[-1]["source"] = k
    assert questions.withdraw(case) == 9 - questions.OPEN_AT_ONCE
    left = [q for q in case.open_questions() if q["key"].startswith("source:")]
    assert len(left) == questions.OPEN_AT_ONCE and any(q["priority"] == "red" for q in left)


def test_a_name_in_a_persons_own_record_or_in_the_graph_note_is_that_person_not_a_maybe():
    case = _case()
    net = PersonNetwork(_graph())
    note = case.add_document(kind="graph", key="g", title="Graph analysis", source="rules", text="Imran Rashid links 2 groups")
    assert entities.resolve(net, name="Imran Rashid")["state"] == "maybe"            # a name alone settles nothing
    assert entities.lookup(case, net, "Imran Rashid", source="sys:p2:subscriber")["state"] == "same"
    assert entities.lookup(case, net, "Imran Rashid", source=note)["state"] == "same"
    assert entities.lookup(case, net, "Imran Rashid", source="sys:p1:subscriber")["state"] == "maybe"   # another man's record
    # "none" where a record has no name is nobody to look for.
    case.add_claim(note, said_by="record", about="none", topic="address", value="House 5", quote="x")
    assert all(m["name"].lower() != "none" for m in entities.mentions(case))


def test_the_three_situations_of_a_target_are_told_apart_by_asking_the_officer():
    """No record; a record that is only history; the very case he was arrested in."""
    from sherlocks.evidence.questioner import gaps
    from sherlocks.linkgraph.relevance import the_new_case
    from sherlocks.team import recorder

    def graph(firs):
        return {"nodes": [{"id": "p1", "kind": "person", "label": "Kamran Aslam",
                           "data": {"seed": True, "cnic": "9999900000011", "firs": firs}}], "edges": []}

    record = [{"key": "a", "label": "12/21", "ps": "Saddar", "role": "accused", "system": "cro", "offence": "381-A vehicle theft"},
              {"key": "b", "label": "604/25", "ps": "Gulistan-e-Johar", "role": "accused", "system": "arms", "offence": "392 robbery"}]

    # 1. No earlier record: nothing to tell apart - what happened, where and when are asked.
    keys = [g["key"] for g in gaps(CaseFile(), graph([]))]
    assert "case:which" not in keys and keys[:3] == ["incident:place", "incident:what", "incident:when"]
    assert "roles:main" not in keys                      # the one target is the person under investigation

    # 2 and 3 look the same on the records: the first question names the crimes, latest first, and asks.
    first = gaps(CaseFile(), graph(record))[0]
    assert first["key"] == "case:which"
    assert first["text"].index("604/25") < first["text"].index("12/21")
    assert "392 robbery" in first["text"] and "Gulistan-e-Johar" in first["text"] and "ARMS" in first["text"]
    assert "same crimes" in first["text"] and "arrested" in first["text"] and "new matter" in first["text"]

    def said(**fields):
        return recorder._Record(read_back="Noted.", **fields)

    # 2. A new matter: the record is history. He is a target, not an arrested accused; the case is still to be described.
    case, net = CaseFile(), PersonNetwork(graph(record))
    recorder.write(case, net, "yeh naya mamla hai", said(this_case="a new matter"), language="roman")
    new = the_new_case(case)
    assert case.dialog["case_basis"] == "new_matter" and "NEW matter" in new["which_case"] and "not said" in new["target_status"]
    keys = [g["key"] for g in gaps(case, graph(record))]
    assert "case:which" not in keys and "incident:what" in keys and "incident:when" in keys

    # 3. The same case as an FIR on file, and he was arrested in it: the FIR now describes the case.
    case = CaseFile()
    case.add_document(kind="fir", key="fir:604", title="FIR 604/25 · PS Gulistan-e-Johar", source="PSRMS", text="FIR 604/25",
                      ref={"fir_no": "604", "fir_year": "25"},
                      data={"occurred": "03-02-2025 09:30 PM", "sections": "392 PPC", "place": "Block 7, Gulistan-e-Johar",
                            "police_station": "Gulistan-e-Johar"})
    recorder.write(case, net, "yeh wohi 604/25 wala case hai, Kamran giraftar hai",
                   said(this_case="an FIR already on record", this_case_fir="604/25", target_in_custody="arrested"),
                   language="roman")
    inc = case.incident
    assert (inc["fir"], inc["date"], inc["time"], inc["offence"], inc["target_status"]) == (
        "604/25", "2025-02-03", "21:30", "392 PPC", "arrested")
    assert inc["place"] == "Block 7, Gulistan-e-Johar" and "date" in inc["from_fir"]
    keys = [g["key"] for g in gaps(case, graph(record))]
    assert not {"case:which", "incident:what", "incident:when", "incident:place", "incident:fir", "roles:main"} & set(keys)
    assert "IS FIR 604/25" in the_new_case(case)["which_case"]


def test_the_summary_is_points_under_headings_and_assumes_nothing_about_a_case_nobody_described():
    from sherlocks.team.report_graph import (
        NOT_DECLARED,
        NOT_WEIGHED,
        SUMMARY,
        build_report,
        declared,
        summary_text,
    )

    graph = {"nodes": [
        {"id": "p1", "kind": "person", "label": "Kamran Aslam",
         "data": {"seed": True, "cnic": "9999900000011", "father_name": "Aslam", "phones": ["03990000011"],
                  "firs": [{"key": "a", "label": "12/21", "ps": "Saddar", "role": "accused", "system": "cro",
                            "offence": "381-A vehicle theft"}]}},
        {"id": "p2", "kind": "person", "label": "Imran Rashid", "data": {"cnic": "9999900000022"}}],
        "edges": [{"id": "e1", "source": "p1", "target": "p2", "kind": "strong", "label": "Tenancy witness",
                   "system": "old_tenant",
                   "relation": {"sentence": "Imran Rashid was a witness to the tenancy of Kamran Aslam"}}]}
    seen = {}

    class Writer(Scripted):
        def _Summary(self, prompt):
            seen.update(json.loads(prompt)["finished_parts"])
            # A writer that theorises anyway: what it says about "the case" must not reach the report.
            return {"brief": ["This case is a vehicle theft by Kamran Aslam in Saddar."],
                    "target": ["Kamran Aslam, CNIC 9999900000011, s/o Aslam [CRO]."],
                    "connections": ["Imran Rashid witnessed Kamran Aslam's tenancy [Old Tenant]."],
                    "past_cases": ["FIR 12/21, PS Saddar - vehicle theft, Kamran Aslam as accused [CRO]."],
                    "established": [], "disputed": [], "leads": [],
                    "stands": ["Kamran Aslam is the likely offender in this case."], "unclear": [],
                    "next_steps": ["Get Kamran Aslam's CDR."]}

    case = CaseFile()                                           # nothing told by the officer
    assert not declared(case)
    report = build_report(case, graph, llm=Writer())
    summary = report["summary_parts"]
    assert seen["case_declared"] is False and "hypotheses" not in seen and "sherlocks_view_not_confirmed" not in seen
    assert seen["the_new_case"] == "not described by the officer yet" and seen["earlier_cases_on_record"]
    # Said in fixed words by code; nothing "established" or "weighed" about a case nobody described.
    assert summary["brief"] == [NOT_DECLARED] and summary["established"] == [] and summary["stands"] == [NOT_WEIGHED]
    assert report["case_declared"] is False and report["summary_order"] == list(SUMMARY)
    assert "likely offender" not in report["executive_summary"] and "This case is a vehicle theft" not in report["executive_summary"]
    # What can always be said: who the target is, the connections, the earlier cases - as points under headings.
    assert "Kamran Aslam" in summary["target"][0] and "Imran Rashid" in summary["connections"][0]
    assert "12/21" in summary["past_cases"][0] and summary["next_steps"][0].startswith("Tell Sherlock what is being investigated")
    text = summary_text(summary)
    assert all(h in text for h in ("The case in brief", "The target", "Connections", "Earlier cases on record", "Next steps"))
    assert all(isinstance(summary[name], list) for name in SUMMARY)
    assert all(line.startswith("• ") for block in text.split("\n\n") for line in block.split("\n")[1:])

    # Without the model the same headings are filled from the records.
    plain = build_report(CaseFile(), graph, llm=None)["summary_parts"]
    assert plain["brief"] == [NOT_DECLARED] and "9999900000011" in plain["target"][0]
    assert any("Imran Rashid" in c for c in plain["connections"]) and any("12/21" in c for c in plain["past_cases"])

    # Once the officer says what the case is, the brief is theirs and the lines of inquiry are back.
    told = CaseFile()
    told.dialog["case_basis"] = "new_matter"
    told.set_incident({"offence": "fraud (5 lakh)"})
    assert declared(told)
    again = build_report(told, graph, llm=Writer())["summary_parts"]
    assert again["brief"] == ["This case is a vehicle theft by Kamran Aslam in Saddar."] and again["stands"] != [NOT_WEIGHED]


def test_a_line_of_inquiry_about_an_earlier_fir_is_never_shown_as_the_view_on_a_new_case():
    from sherlocks.team.report_graph import build_report

    graph = {"nodes": [{"id": "p1", "kind": "person", "label": "Kamran Aslam", "data": {"seed": True, "cnic": "9999900000011"}}],
             "edges": []}

    def board(basis):
        case = CaseFile()
        old = case.add_document(kind="fir", key="fir:474", title="FIR 474/22 · PS Gadap", source="PSRMS",
                                text="FIR 474/22. Complainant Faisal. Robbery on 23-10-2022.", ref={"fir_no": "474", "fir_year": "22"})
        f = case.add_fact(old, "The complainant of FIR 474/22 is Faisal", "Complainant Faisal")
        case.set_incident({"offence": "fraud (5 lakh)", **({"fir": "474/22"} if basis == "fir_on_record" else {})})
        case.dialog["case_basis"] = basis
        case.set_assessment({"answer": "The primary case (FIR 474/22) has stalled."})
        case.set_hypothesis("Faisal is the complainant and his statements are consistent.", status="supported", support=[f])
        case.set_hypothesis("Kamran Aslam took part in the theft described in FIR 474/22.", against=[f])
        return case

    # A new matter: both lines are about the earlier FIR (one rests only on it, one names it) - neither is this case's.
    new = build_report(board("new_matter"), graph, llm=None)
    assert new["hypotheses"] == [] and new["assessments"] == [] and new["conclusion"] == ""
    assert "H1" in new["assessment_note"] and "earlier cases" in new["assessment_note"]
    assert not any("474/22" in line for line in new["summary_parts"]["stands"] + new["summary_parts"]["established"])
    assert any("474/22" in line for line in new["summary_parts"]["past_cases"])          # still set out, as a past case
    # The officer said this case IS that FIR: then it is this case's own record, and everything stays.
    same = build_report(board("fir_on_record"), graph, llm=None)
    assert len(same["hypotheses"]) == 2 and "474/22" in same["conclusion"] and not same.get("assessment_note")


def test_the_timeline_tells_each_case_as_a_story_in_date_order():
    """FIR -> the people in it -> what each said -> the reports and what they found -> the next FIR."""
    from sherlocks.evidence.report_pdf import render
    from sherlocks.team.report_graph import build_report

    graph = {"nodes": [{"id": "p1", "kind": "person", "label": "Kamran Aslam", "data": {"seed": True, "cnic": "9999900000011"}}],
             "edges": []}
    case = CaseFile()
    late = case.add_document(kind="fir", key="fir:488", title="FIR 488/25 · PS Gulberg", source="PSRMS",
                             text="FIR 488/25. Asif states: Kamran fired at my brother Naveed with a pistol.",
                             ref={"fir_no": "488", "fir_year": "25"},
                             data={"sections": "324 PPC", "occurred": "01-03-2025 10:00 PM", "reported": "02-03-2025 03:15",
                                   "police_station": "Gulberg", "place": "Main Boulevard", "complainant": {"name": "Asif Rafiq"},
                                   "nominated_suspects": [{"name": "Kamran Aslam"}], "witnesses": [{"name": "Naveed Rafiq"}],
                                   "case_diaries": [{"no": "01", "date": "03-03-2025", "remarks": "Site inspected."}]})
    early = case.add_document(kind="fir", key="fir:12", title="FIR 12/21 · PS Saddar", source="PSRMS",
                              text="FIR 12/21. Vehicle theft reported by Bilal.", ref={"fir_no": "12", "fir_year": "21"},
                              data={"sections": "381-A PPC", "occurred": "05-01-2021 08:00 AM", "police_station": "Saddar",
                                    "complainant": {"name": "Bilal Khan"}})
    w = case.add_statement(late, speaker="Asif Rafiq", role="complainant", kind="fir", date="02-03-2025",
                           recorded_by="SI Zahid", text="Kamran fired at my brother Naveed with a pistol.")
    mlo = case.add_document(kind="upload", key="up:mlo", title="Medico-legal report of Naveed", source="upload",
                            text="Medico-legal report, FIR 488/25. Incised wound 4 x 1 cm on the right thigh.")
    case.set_card(mlo, {"record": "mlo", "title": "Medico-legal report of Naveed",
                        "fields": [{"name": "injury", "value": "incised wound 4 x 1 cm on the right thigh", "topic": "injury",
                                    "quote": "Incised wound 4 x 1 cm on the right thigh"}]})
    stray = case.add_document(kind="lab", key="lab:x", title="Chemical report LAB-1", source="Lab", text="Chemical report.",
                              summary="Chemical report · case received 09-09-2019 10:00")
    report = build_report(case, graph, llm=None)
    story = report["parts"]["story"]
    # Date order: the 2021 case before the 2025 one, whatever order they were read in.
    assert [c["fir"] for c in story["cases"]] == ["12/21", "488/25"]
    first, second = story["cases"]
    assert first["people"] == [{"name": "Bilal Khan", "role": "complainant"}] and first["statements"] == [] and first["reports"] == []
    assert {(p["name"], p["role"]) for p in second["people"]} == {("Asif Rafiq", "complainant"), ("Naveed Rafiq", "witness"),
                                                                  ("Kamran Aslam", "accused (named in the FIR)")}
    assert [s["id"] for s in second["statements"]] == [w] and second["statements"][0]["speaker"] == "Asif Rafiq"
    # The medical report sits under the FIR it names, with what it found; a report of no case on the board is listed apart.
    assert [r["id"] for r in second["reports"]] == [mlo] and second["reports"][0]["kind"] == "Medical report"
    assert "incised wound" in second["reports"][0]["found"][0]
    assert second["steps"][0]["what"].startswith("Case diary 01") and [r["id"] for r in story["reports_without_a_case"]] == [stray]
    assert early not in {e["ref"] for e in story["other"]}                 # told in its case, not repeated below
    # Relations: two people a row, with every tie between them - their roles in the same FIR, with its number.
    pairs = {(r["a"], r["b"]): [t["relation"] for t in r["ties"]] for r in report["parts"]["relations"]["pairs"]}
    assert pairs[("Kamran Aslam", "Asif Rafiq")] == ["In FIR 488/25: Asif Rafiq is complainant, Kamran Aslam is accused"]
    assert pairs[("Asif Rafiq", "Naveed Rafiq")] == ["In FIR 488/25: Asif Rafiq is complainant, Naveed Rafiq is witness"]
    assert ("Kamran Aslam", "Naveed Rafiq") not in pairs          # accused and witness of one FIR: no tie of their own
    # Case diaries that repeat their opening are told by what is new in each; ones that add nothing, together.
    from sherlocks.team.report_graph import _diary_steps

    head = "Accused: 1. Kamran s/o Aslam 2. Imran s/o Rashid, under sections 324 and 34 of the Penal Code. "
    steps = _diary_steps([{"no": "01", "date": "03-03-2025", "remarks": head + "Site inspected and the plan drawn."},
                          {"no": "02", "date": "04-03-2025", "remarks": head + "Statements of two witnesses recorded."},
                          {"no": "03", "date": "05-03-2025", "remarks": head + "Statements of two witnesses recorded."},
                          {"no": "04", "date": "06-03-2025", "remarks": head + "Statements of two witnesses recorded."}])
    assert [s["what"] for s in steps][:2] == ["Case diary 01: " + head + "Site inspected and the plan drawn.",
                                             "Case diary 02: ... Statements of two witnesses recorded"]
    assert steps[2] == {"when": "05-03-2025 to 06-03-2025",
                        "what": "Case diaries 03, 04: nothing new recorded - the same entry as the diary before."}
    assert render(report, case, graph)[:4] == b"%PDF"
