"""A FIR read after the officer described the case is matched again; a failed laboratory fetch is asked again.
Made-up people."""

from datetime import UTC, datetime, timedelta

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.team import case_rematch

GRAPH = {"nodes": [{"id": "t1", "kind": "person", "label": "Kamran Aslam", "data": {"seed": True}}], "edges": []}


class Matcher:
    def __init__(self):
        self.calls = 0

    def generate_text(self, **_):
        self.calls += 1
        return "He was reported kidnapped in FIR 45/26 and is now found dead: the same victim.\nANSWER: same 45/26"


def _fir(case, no="45", year="26"):
    case.add_document(kind="fir", key=f"fir:{no}/{year}/9", title=f"FIR {no}/{year} · PS Saddar", source="PSRMS FIR file",
                      text="FIR", ref={"fir_no": no, "fir_year": year, "ps_id": "9"}, summary="Sections 365 · kidnapping",
                      data={"occurred": "01-07-2026", "offence": "365"})


def test_a_fir_read_after_the_officer_spoke_is_matched_again_and_the_officer_is_told():
    case, net, llm = CaseFile(), PersonNetwork(GRAPH), Matcher()
    case.add_turn("We found the dead body of the target - is it suicide or murder?", "noted")
    case.dialog["case_basis"] = "new_matter"
    assert case_rematch.rematch(case, net, llm) is None and llm.calls == 0      # no FIR file yet
    _fir(case)
    assert case_rematch.rematch(case, net, llm) == "45/26"
    assert case.dialog["case_basis"] == "fir_on_record" and case.incident["fir"] == "45/26"
    assert "FIR 45/26 has now been read" in case.followups[-1]["text"]
    assert case_rematch.rematch(case, net, llm) is None and llm.calls == 1      # tied now: not asked again


def test_a_laboratory_error_is_asked_again_a_few_times_minutes_apart():
    case = CaseFile()
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat(timespec="seconds")
    case.attempt("lab", "labs:45/26/9", "error", "HTTPError: HTTP 500")
    assert case_rematch.lab_retries(case) == [] and case_rematch.lab_retries(case, due_only=False)   # too soon
    case.attempts[-1]["at"] = old
    assert case_rematch.lab_retries(case) == [("45", "26", "9")]

    class Agents:
        def fetch_lab_reports(self, no, year, ps_id):
            case.attempt("lab", f"labs:{no}/{year}/{ps_id}", "hit", "2 lab case(s)")
            return {"documents": ["D7", "D8"]}

    assert case_rematch.retry_labs(case, Agents()) == 2
    assert "2 report(s) of FIR 45/26 are now on the case board" in case.followups[-1]["text"]
    assert case_rematch.lab_retries(case, due_only=False) == []                  # answered: never again
    for _ in range(3):
        case.attempt("lab", "labs:9/26/9", "error", "timeout")
    assert case_rematch.lab_retries(case, due_only=False) == []                  # three tries: given up


class NotYet:
    """A matcher that cannot tell from questions alone - until an upload about the FIR is given."""

    def __init__(self):
        self.prompts = []

    def generate_text(self, *, prompt, **_):
        import json

        self.prompts.append(json.loads(prompt))
        if any("FIR No. 45/2026" in str(d.get("about")) for d in self.prompts[-1]["documents_uploaded"]):
            return "The inquiry the colleague uploaded is about FIR 45/26.\nANSWER: same 45/26"
        return "Only questions so far.\nANSWER: no event"


def test_the_case_is_matched_again_when_the_officer_says_more_or_uploads_a_document_about_the_fir():
    case, net, llm = CaseFile(), PersonNetwork(GRAPH), NotYet()
    _fir(case)
    case.add_turn("where was the body found?", "...")
    assert case_rematch.rematch(case, net, llm) is None and len(llm.prompts) == 1
    assert not case_rematch.unmatched(case)                                     # nothing new since
    case.add_turn("who saw him last?", "...")
    assert not case_rematch.unmatched(case)
    case.add_turn("what did the mother say?", "...")
    assert case_rematch.unmatched(case)                                         # two more messages
    assert case_rematch.rematch(case, net, llm) is None and len(llm.prompts) == 2
    doc = case.add_document(kind="upload", key="upload:1", title="Upload: inquiry.pdf", source="Uploaded by the officer",
                            text="...")
    case.documents[doc]["reading"] = {"kind": "Inquiry report", "about": "Death of Kamran Aslam, FIR No. 45/2026, PS Saddar",
                                      "summary": "He was murdered."}
    assert case_rematch.unmatched(case)                                         # an upload read in full
    assert case_rematch.rematch(case, net, llm) == "45/26" and case.incident["fir"] == "45/26"


def test_matching_stops_after_its_tries():
    case, net, llm = CaseFile(), PersonNetwork(GRAPH), NotYet()
    _fir(case)
    for i in range(30):
        case.add_turn(f"question {i}?", "...")
        case_rematch.rematch(case, net, llm)
    assert len(llm.prompts) == case_rematch.MATCH_TRIES


def test_a_whole_message_kept_as_the_place_is_dropped_and_the_fir_gives_it():
    from sherlocks.team.recorder import adopt_fir, repair_incident

    case, net = CaseFile(), PersonNetwork(GRAPH)
    asked = "check in the fir report what IO says where we found dead body of kamran ?"
    case.add_turn(asked, "...")
    q = case.ask("incident:place", "Where did the incident happen?")
    case.answer(q["id"], asked.rstrip(" ?"))
    case.set_incident({"place": asked.rstrip(" ?"), "offence": "Murder"})
    assert repair_incident(case) == ["place"]
    assert "place" not in case.incident and case.incident["offence"] == "Murder" and q["status"] == "open"
    _fir(case)
    case.documents[next(iter(case.documents))]["data"]["place"] = "outside plot 12, Block 2"
    adopt_fir(case, net, "45/26")
    assert case.incident["place"] == "outside plot 12, Block 2"
