"""The failing session, replayed: pronouns follow the person discussed, counts are exact,
news is told once, and the three languages are understood. No model (rule mode)."""

from __future__ import annotations

import pytest

from sherlocks.linkgraph.understanding import rule_type


@pytest.mark.parametrize(("message", "people", "kind"), [
    ("ye kitni firs ma involve ha ?", 1, "count_cases"),
    ("I mean which cases danish is involve ?", 1, "cases"),
    ("total no of criminals found in his connections ?", 1, "criminals_near"),
    ("total criminal in whole graphs ?", 1, "criminals_all"),
    ("is ke kitne mujrim saathi hain?", 1, "criminals_near"),
    ("پورے گراف میں کتنے ملزم ہیں؟", 0, "criminals_all"),
    ("اس کے کتنے مقدمات ہیں؟", 1, "count_cases"),
    ("us ke hotel stays?", 1, "hotels"),
    ("Kamran aur Sajid ka kya taluq hai?", 2, "connection"),
    ("kya naya mila?", 0, "news"),
    ("case ka khulasa batao", 0, "summary"),
    ("graph mein kitne log hain?", 0, "graph_stats"),
])
def test_questions_are_understood_in_three_languages(message, people, kind):
    assert rule_type(message, people) == kind


@pytest.fixture(scope="module")
def demo_case():
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    handle = memory_manager(s).start(GraphRunParams(cnic="9999900000011", depth=2, max_persons=15, backend="demo"),
                                     wait=True)
    return handle.graph(), handle.case


def test_the_failing_session_now_answers_each_question(demo_case):
    from sherlocks.linkgraph.sherlock_team import run_turn

    graph, case = demo_case

    def say(message):
        return list(run_turn(graph, message, case=case))[-1]

    first = say("Kamran Ahmed")
    assert "Kamran Ahmed" in first["answer"] and "appears in 2 FIR(s)" in first["answer"]

    count = say("ye kitni firs ma involve ha ?")                 # "ye" = Kamran, from the last turn
    assert count["query"]["type"] == "count_cases" and count["facts"]["count"] == 2
    assert count["answer"].lstrip("*").startswith("Kamran Ahmed ka naam 2 FIRs mein hai: 2 mein mulzim")
    assert "dakaiti" in count["answer"]                      # 395/34 PPC, said in words
    assert "pattern(s) found by rule" not in count["answer"]

    near = say("total no of criminals found in his connections ?")
    assert near["query"]["type"] == "criminals_near" and near["facts"]["subject"] == "Kamran Ahmed"
    assert near["answer"].lstrip("*").startswith("1 of the people linked to Kamran Ahmed have a criminal record")

    everyone = say("total criminal in whole graphs ?")
    assert everyone["query"]["type"] == "criminals_all" and everyone["facts"]["count"] == 2

    # Findings about the person discussed are told once, not in every reply.
    # (Document ids depend on the order the evidence threads finish: take them from the reply.)
    told = [f"[{ref}]" for ref in __import__("re").findall(r"\[([DL]\d+)\]", first["answer"])]
    assert told and not any(ref in everyone["answer"] for ref in told)


def test_widened_criminal_questions():
    assert rule_type("i ask the question how many toal criminals are there not just danish cionnections ?", 1) == "criminals_all"
    assert rule_type("sirf danish ke nahi, total kitne mujrim hain?", 1) == "criminals_all"
    assert rule_type("us ke saathiyon mein kitne mujrim?", 1) == "criminals_near"


def test_the_questioner_asks_throughout_and_remembers_answers():
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.linkgraph.sherlock_team import run_turn
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    handle = memory_manager(s).start(GraphRunParams(cnic="9999900000011", depth=2, max_persons=15, backend="demo"),
                                     wait=True)
    graph, case = handle.graph(), handle.case
    asked = []

    def say(message):
        final = list(run_turn(graph, message, case=case))[-1]
        asked.append(final["checks"].get("asked"))
        return final

    assert say("salam")["asking"]["key"] == "incident:place"
    assert say("Gulshan mein university road par")["intent"] == "answer"
    say("kamran ki kitni firs hain?")                                   # a question in between
    assert say("Robbery hui thi, mobile chheena gaya")["intent"] == "answer"   # still answers "what happened"
    say("Kamran")                                                       # a name is not a date
    assert say("07-08-2025 raat 11 baje")["intent"] == "answer"
    assert case.incident["place"] == "Gulshan mein university road par"
    assert case.incident["offence"].startswith("Robbery") and case.incident["date"] == "2025-08-07"
    # Answered questions never come back; nothing is asked twice in a row.
    answered = {q["key"] for q in case.questions if q["status"] == "answered"}
    assert {"incident:place", "incident:what", "incident:when"} <= answered
    later = asked[asked.index("incident:when") + 1:]
    assert not set(later) & {"incident:place", "incident:what", "incident:when"}
    assert all(a != b for a, b in zip(asked, asked[1:]) if a)
    # "Don't know" closes a question.
    open_before = {q["key"] for q in case.open_questions()}
    say("pata nahi")
    assert len({q["key"] for q in case.open_questions()} & open_before) < len(open_before)


def test_details_said_in_passing_are_not_asked_again():
    """The reported session: "he is suspect on murder case" after "where did it happen?"
    must not become the place, and "what happened" must not be asked after it."""
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.linkgraph.sherlock_team import run_turn
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    handle = memory_manager(s).start(GraphRunParams(cnic="9999900000011", depth=2, max_persons=15, backend="demo"),
                                     wait=True)
    graph, case = handle.graph(), handle.case
    asked = []
    for message in ["what are the firs on kamran ?", "he is suspect on on m,murder case",
                    "it happened near University Road Gulshan", "07-08-2025 raat 11 baje", "KDE-1234 bike thi"]:
        asked.append(list(run_turn(graph, message, case=case))[-1]["checks"].get("asked"))
    inc = case.incident
    assert "murder" in inc["offence"] and "University Road" in inc["place"]
    assert inc["date"] == "2025-08-07" and inc["vehicle_or_weapon"].startswith("KDE-1234")
    assert "incident:what" not in asked                       # said in passing, never asked
    assert len([a for a in asked if a]) == len({a for a in asked if a})     # no question twice
    assert case.roles == {"p1": "suspect"}                    # "he" = Kamran, the person discussed


def test_fir_status_questions_and_reading_documents_of_an_older_graph():
    """A graph built without document reading: the first chat question starts reading its
    FIR files; then "har FIR ka status" / "acquitted or convicted" answer per FIR."""
    import time

    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    s.evidence.enabled = False                       # an older graph: nothing read while it was built
    mgr = memory_manager(s)
    handle = mgr.start(GraphRunParams(cnic="9999900000011", depth=2, max_persons=15, backend="demo"), wait=True)
    assert not [d for d in handle.case.documents.values() if d["kind"] != "graph"]   # nothing read (A1 aside)
    s.evidence.enabled = True                        # today's Sherlocks

    first = list(mgr.investigate(handle.graph(), "kamran ka fir status batao ?", run_id=handle.id))[-1]
    assert first["query"]["type"] == "fir_status" and first["reading_documents"] >= 2
    assert "FIR 45/2023" in first["answer"] and "adalat mein zer-e-samaat" in first["answer"]       # from the CRO status
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and sum(d["kind"] == "fir" for d in handle.case.documents.values()) < 2:
        time.sleep(0.1)
    assert sum(d["kind"] == "fir" for d in handle.case.documents.values()) >= 2

    status = list(mgr.investigate(handle.graph(), "kis fir ma aquited ha or kis fir ma convcited ha ?", run_id=handle.id))[-1]
    assert status["query"]["type"] == "fir_status" and "reading_documents" not in status
    assert "FIR file" in " ".join(i["source"] for i in status["facts"]["items"])    # the FIR file's position now counts
    vague = list(mgr.investigate(handle.graph(), "hmm theek", run_id=handle.id))[-1]
    assert "pattern(s) found by rule" not in vague["answer"]


def test_the_second_failing_session():
    """Ranks are not names, typing slips are understood, "ye" keeps the person discussed,
    and a question word's sound never names anyone ("ktne" ~ "خاتون")."""
    from sherlocks.linkgraph.conversation import detect_language, route
    from sherlocks.linkgraph.investigator import _mentioned
    from sherlocks.linkgraph.network import PersonNetwork
    from sherlocks.linkgraph.understanding import understand

    def person(pid, label):
        return {"id": pid, "kind": "person", "label": label, "data": {}}

    net = PersonNetwork({"nodes": [person("n", "SUB INSPECTOR-Fahim Baig"), person("m", "Muhammad Yasir SUB INSPECTOR"),
                                   person("k", "Qaiser Zaman Sub Inspector"), person("z", "ریحانہ خاتون"),
                                   person("a", "Muhammad Danish Rafiq"), person("t", "افضل خان"),
                                   person("l", "فضل آفریدی")], "edges": []})
    assert _mentioned(net, "fir k statuses batou sub inspector fahim ki ?") == ["n"]
    assert understand("fir k statuses batou sub inspector fahim ki ?", net, ["a"])["type"] == "fir_status"
    q = understand("kit ni fir in k uper ha", net, ["n"])
    assert (q["type"], q["people"]) == ("count_cases", ["n"])
    assert route("kit ni fir in k uper ha", [{"key": "incident:when"}]) == "question"
    q = understand("kitni fir ma ye convicted ha or kitni ma aquited ha or ktne abhi case jari ha ?", net, ["a"])
    assert (q["type"], q["people"]) == ("fir_status", ["a"])
    assert _mentioned(net, "afzal kaun hai?") == ["t"]
    assert _mentioned(net, "fazal kaun hai?") == ["l"]
    assert detect_language("fir k statuses batou sub inspector fahim ki ?") == "roman"


def test_firs_are_told_in_words_with_the_persons_role():
    """"koi khatarnak FIR?", "har FIR mein kya ilzaam hai?", "qatl ka case?": crimes in words,
    the most serious first, and an FIR where he is the complainant is never counted against him."""
    from sherlocks.linkgraph import case_queries
    from sherlocks.linkgraph.narrate import narrate
    from sherlocks.linkgraph.network import PersonNetwork
    from sherlocks.linkgraph.offences import asked_crimes, classify
    from sherlocks.linkgraph.understanding import rule_type

    assert classify("365/377/337A(i)/34") == ["sexual_assault", "kidnapping", "hurt"]
    assert classify("376, 511, 337A") == ["attempted_rape", "hurt"]
    assert asked_crimes("gaari chori ka case?") == {"vehicle_theft"}
    assert rule_type("koi khatarnak fir ?", 1) == "serious_cases"
    assert rule_type("har fir ma kya ilzaam ha ?", 1) == "fir_details"
    assert rule_type("kya us par qatl ka case hai?", 1) == "serious_cases"

    net = PersonNetwork({"nodes": [{"id": "a", "kind": "person", "label": "Muhammad Danish Rafiq", "data": {"firs": [
        {"label": "391/26", "ps": "Bin Qasim", "role": "Accused / suspect", "offence": "376, 511, 337A", "system": "psrms"},
        {"label": "345/26", "ps": "Preedy", "role": "AFFP", "offence": "324, 365, 375", "system": "psrms"},
        {"label": "884/26", "ps": "Ferozabad", "role": "Accused / suspect", "offence": "489F", "system": "psrms"},
        {"label": "1124/25", "ps": "Ferozabad", "role": "Accused", "offence": "406", "system": "psrms"},
        {"label": "1124/2025", "ps": "Ferozabad", "role": "Accused (ARMS)", "offence": "406", "system": "arms"}]}}],
        "edges": []})
    facts = case_queries.serious_cases(net, "a")
    assert [it["fir"] for it in facts["items"]] == ["391/26", "345/26"]
    reply = narrate(facts, "roman")
    assert reply.startswith("Haan - Muhammad Danish Rafiq 1 sangeen FIR(s) mein mulzim hai: FIR 391/26 (zina bil jabr ki koshish")
    assert "FIR 345/26" in reply and "muddai" not in reply.split("FIR 345/26")[0]   # victim there, said apart
    assert "mutasira fareeq hai, mulzim nahi" in reply
    assert case_queries.cases(net, "a")["count"] == 4                             # 1124/25 = 1124/2025
    qatl = narrate(case_queries.serious_cases(net, "a", asked_crimes("kya us par qatl ka case hai?")), "en")
    assert qatl.startswith("No - Muhammad Danish Rafiq is not accused of")
    assert "FIR 345/26" in qatl and "not the accused" in qatl


def test_the_io_report_is_read_from_the_fir_file():
    """"what was the IO report about him, guilty or not?": the IO's own conclusion, quoted -
    challan (sent to court), C class (closed, not held guilty) - not a general status list."""
    from sherlocks.evidence.case_file import CaseFile
    from sherlocks.linkgraph import case_queries
    from sherlocks.linkgraph.narrate import narrate
    from sherlocks.linkgraph.network import PersonNetwork
    from sherlocks.linkgraph.understanding import rule_type

    assert rule_type("what was the investigation officer report about him he declare him guilty or not", 1) == "io_report"
    assert rule_type("IO ki report kya hai?", 1) == "io_report"
    case = CaseFile()

    def fir_file(no, result, position):
        case.documents[f"D{no}"] = {"id": f"D{no}", "kind": "fir", "title": f"FIR {no}/22", "source": "PSRMS",
                                    "ref": {"fir_no": str(no), "fir_year": "22", "ps_id": "1"},
                                    "data": {"investigation_result": result, "case_positions": [{"position": position}],
                                             "investigating_officers": [{"rank": "SUB INSPECTOR", "name": "Test Officer"}],
                                             "complainant": {"name": "Sample Complainant", "cnic": "9999900000101"},
                                             "nominated_suspects": [{"name": "Danish", "cnic": "9999900000777"}]}}

    fir_file(10, "تفتیش مکمل کی گئی۔ ملزم کے خلاف قابل چالان شہادتیں میسر ہونے پر چالان قطع کرنے کی استدعا کی جاتی ہے۔",
             "چالان")
    fir_file(11, "فریقین کا لین دین کا تنازعہ ہے۔ مقدمہ بالا کا ڈسپوزل فائنل رپورٹ(C) کلاس قطع کرنے کی استدعا کی جاتی ہے۔",
             "اخراج رپورٹ")
    net = PersonNetwork({"nodes": [{"id": "d", "kind": "person", "label": "Muhammad Danish Rafiq", "data": {
        "cnic": "9999900000777", "firs": [{"label": "10/22", "ps": "PS One", "role": "Accused", "offence": "392"},
                                          {"label": "11/22", "ps": "PS Two", "role": "Accused", "offence": "406"}]}}],
        "edges": []})
    facts = case_queries.io_report(net, "d", case)
    assert [i["detail"]["verdict"] for i in facts["items"]] == ["challan", "c_class"]
    reply = narrate(facts, "en")
    assert reply.startswith("According to the IO reports on Muhammad Danish Rafiq: in 1 FIR(s) (10/22) the IO found him liable")
    assert "in 1 (11/22) the IO did not hold him guilty" in reply
    assert "SUB INSPECTOR Test Officer".title().split()[0] in reply and "قابل چالان شہادتیں" in reply
    assert case_queries.outcome("اخراج رپورٹ") == "disposed / closed"


def test_target_charges_explain_and_a_full_summary(demo_case):
    """A real session: "the target" was not resolved, "charges" got a count, "explain
    that" lost the subject, and the summary said nothing of the documents."""
    from sherlocks.linkgraph.sherlock_team import run_turn

    graph, case = demo_case

    def say(message):
        return list(run_turn(graph, message, case=case))[-1]

    summary = say("Summarise this case: who are the targets, what connects them, and what do the documents establish?")
    assert summary["query"]["type"] == "summary"
    text = summary["answer"]
    assert "FIR 45/2023" in text and "dacoity" in text                     # the target's FIRs, crimes in words
    assert "is linked to" in text and "Sajid Mehmood" in text                # what connects him
    assert "FIR 45/2023 · PS Gulshan-e-Iqbal:" in text                      # what the documents establish
    charges = say("what are the charges on target ?")
    assert charges["query"]["type"] == "fir_details" and charges["query"]["people"] == ["p1"]
    assert "dacoity" in charges["answer"] and "Complainant's account" in charges["answer"]
    explain = say("what si that explain")                                    # the same subject, deeper
    assert explain["query"]["type"] == "documents" and explain["query"]["people"] == ["p1"]
    assert explain["query"]["by"] == "follow-up"


def test_the_role_a_question_asks_about(demo_case):
    """"Is there any FIR on X?" asks where X is the accused. A complainant is told "no",
    then his FIRs in other roles with their accused - for any role, in any language."""
    from sherlocks.linkgraph.sherlock_team import run_turn, validate
    from sherlocks.linkgraph.understanding import asked_role

    graph, case = demo_case

    def say(message):
        return list(run_turn(graph, message, case=case))[-1]

    on_complainant = say("is there any fir on Waqas Javed ?")
    assert on_complainant["facts"]["asked_role"] == "accused" and on_complainant["facts"]["count"] == 0
    text = on_complainant["answer"].replace("**", "")
    assert text.startswith("No - no FIR names Waqas Javed as accused.")
    assert "as the complainant - the accused: Kamran Ahmed and Sajid Mehmood" in text
    roman = say("Waqas Javed par koi fir hai?")["answer"].replace("**", "")
    assert roman.startswith("Nahi - kisi FIR mein Waqas Javed mulzim nahi.") and "muddai" in roman
    on_accused = say("is there any fir on Kamran Ahmed ?")["answer"].replace("**", "")
    assert on_accused.startswith("Yes - Kamran Ahmed is accused in 2 FIR(s)")
    filed = say("did Waqas Javed file any case?")["answer"].replace("**", "")
    assert filed.startswith("Yes - Waqas Javed is the complainant in 1 FIR(s): FIR 45/2023 (accused:")
    # Neutral questions keep every role; the map's "par" is not a role.
    assert asked_role("kamran kitni firs ma involve ha") is None and asked_role("map par pin karo") is None
    assert asked_role("ڈاڈ پر کوئی ایف آئی آر ہے؟") == "accused"
    # The checker stops a "yes" when no FIR names him in the role asked.
    final = {**on_complainant, "answer": "Yes, there is one FIR on Waqas Javed: FIR 45/2023, where he is the complainant."}
    from sherlocks.linkgraph.network import PersonNetwork

    assert validate(case, PersonNetwork(graph), final)["rules"]["answered"] is False
