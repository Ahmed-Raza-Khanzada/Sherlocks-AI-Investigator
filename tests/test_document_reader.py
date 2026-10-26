"""R7 Document reader, P11 Case papers and the report fixes of 2026-10-23 - synthetic data only."""

from __future__ import annotations

import json

from sherlocks.evidence.case_file import CaseFile

INQUIRY = (
    "[page 1] Commission of Inquiry into the death of Kamran Aslam\n"
    "[page 2] Chronology. 12.03.2026 9:10 p.m. Kamran Aslam leaves the bakery on foot.\n"
    "13.03.2026 7:45 a.m. A watchman finds the body near the canal bridge. "
    "The empty casing found near the body did not match the guard's licensed pistol.\n"
    "[page 3] Findings. The Commission finds that the post-mortem of 13.03.2026 did not record the exit wound. "
    "The occupants of the white van seen at the bridge should be traced. The investigation budget should be raised.\n"
)


class Lines:
    """A model that writes the labelled lines it is given for each part, and the whole-document lines."""

    def __init__(self, parts: dict[str, str], whole: str) -> None:
        self.parts, self.whole, self.prompts = parts, whole, []

    def generate_text(self, *, prompt, system="", **_):
        given = json.loads(prompt)
        self.prompts.append(given)
        if "part" in given:
            return self.parts.get(given["pages"], "")
        return self.whole


def _case_with(text: str) -> tuple[CaseFile, str]:
    case = CaseFile()
    doc = case.add_document(kind="upload", key="upload:x", title="Upload: inquiry.pdf", source="Uploaded by the officer",
                            text="Uploaded file: inquiry.pdf (pdf, 10 KB)\n" + text, max_text=400_000)
    return case, doc


def test_a_long_document_is_read_in_parts_of_whole_pages_to_the_end():
    from sherlocks.team.document_reader import parts

    text = "".join(f"[page {i}] " + "word " * 900 for i in range(1, 13))
    got = parts(text, size=7000)
    assert len(got) > 1 and got[0][0].startswith("1") and got[-1][0].endswith("12")
    assert "".join(t for _, t in got) == text                       # nothing left out
    assert all(len(t) <= 7000 * 1.5 for _, t in got)
    # A document kept whole on the board: an upload is not cut at 60,000 characters.
    case, doc = _case_with("x" * 200_000)
    assert len(case.documents[doc]["text"]) > 200_000


def test_the_reader_keeps_only_what_it_read_in_the_text_and_puts_leads_on_the_board():
    from sherlocks.team import document_reader

    case, doc = _case_with(INQUIRY)
    llm = Lines({"1-3": (
        "EVENT: 13.03.2026 | 7:45 a.m. | A watchman finds the body near the canal bridge | A watchman finds the body near the canal bridge\n"
        "EVENT: 14.03.2026 | | The suspect confessed at the station | The suspect confessed at the station\n"   # not in the text
        "FINDING: The casing did not match the guard's licensed pistol | did not match the guard's licensed pistol\n"
        "PERSON: Kamran Aslam | the deceased | left the bakery on foot at 9:10 p.m.\n"
        "EVIDENCE: empty casing | found near the body; did not match | The empty casing found near the body\n"
        "LEAD: Trace the occupants of the white van seen at the bridge | occupants of the white van seen at the bridge\n"
        "LEAD: Trace the white van occupants seen at the bridge | occupants of the white van seen at the bridge\n")},
        "DOCUMENT: Commission of inquiry report | The Commission | 2026 | death of Kamran Aslam\n"
        "SUMMARY: The Commission finds the post-mortem incomplete.\n"
        "TOP:\n- The casing does not match the guard's pistol.\n- The white van is untraced.\n")
    reading = document_reader.read(case, doc, llm)
    assert reading["kind"] == "Commission of inquiry report" and reading["top"] == [
        "The casing does not match the guard's pistol.", "The white van is untraced."]
    assert [e["what"] for e in reading["events"]] == ["A watchman finds the body near the canal bridge"]  # invented one dropped
    assert reading["findings"] and reading["evidence"] and reading["people"][0]["name"] == "Kamran Aslam"
    assert len(reading["leads"]) == 1                               # the same lead said twice is kept once
    lead = case.leads[reading["leads"][0]["lead"]]
    assert lead["found_in"] == "document" and lead["rests_on"] == [reading["leads"][0]["fact"]]
    assert case.documents[doc]["reading"] is reading and case.documents[doc]["ai_summary"].startswith("The Commission")
    # The chat reads it before answering, with its ids.
    from sherlocks.team.case_reader import uploaded

    got = uploaded(case)[0]
    assert got["what_it_is"] == "Commission of inquiry report" and got["dated_steps"][0].startswith(reading["events"][0]["fact"])
    # Without a model that writes plain text there is no reading (the short reader takes over).
    assert document_reader.read(case, doc, object()) is None


def test_case_papers_says_what_is_on_the_board_named_elsewhere_or_missing():
    from sherlocks.linkgraph.network import PersonNetwork
    from sherlocks.team import case_papers

    case, doc = _case_with(INQUIRY)
    fir = case.add_document(kind="fir", key="fir:1", title="FIR 12/26 · PS Canal", source="PSRMS",
                            text="First information: a man was shot dead near the canal.", data={"sections": "302 PPC"})
    scope = {"documents": [doc, fir]}
    got = case_papers.check(case, PersonNetwork({"nodes": [], "edges": []}), scope, {})
    status = {r["paper"]: r["status"] for r in got["papers"]}
    assert {"death", "firearm"} <= set(got["kinds"])
    assert status["First information report (FIR)"] == "on the board"
    assert status["Post-mortem report"] == "mentioned, not on the board"     # the inquiry speaks of it
    assert status["Inquest report (174 Cr.P.C.)"] == "not on the board"
    assert "Identification parade" not in status                               # not a robbery
    assert got["papers"][0]["status"] == "not on the board"                     # what is missing first


def test_a_report_filed_twice_is_read_once_and_the_card_date_stands():
    from sherlocks.team import case_analyst

    case = CaseFile()
    body = "[page 1] " + "Swab from the plant pot matches the deceased. " * 30
    a = case.add_document(kind="lab", key="lab:1", title="DNA report LAB-1", source="Lab", text="DNA report LAB-1 " + body,
                          summary="DNA report · LAB-1 · case received 06-08-2026 09:55:36")
    b = case.add_document(kind="lab", key="lab:2", title="DNA report LAB-2", source="Lab", text="DNA report LAB-2 " + body,
                          summary="DNA report · LAB-2 · case received 10-08-2026 11:51:33")
    reads = case_analyst.forensics(case, [a, b], llm=None)
    assert [r["doc"] for r in reads] == [a] and reads[0]["copies"] == [b]
    assert reads[0]["received"] == "06-08-2026 09:55:36" and "LAB-2" in reads[0]["also_filed_as"][0]


def test_a_claim_of_the_analysis_that_misstates_a_record_is_taken_out():
    from sherlocks.team import case_analyst

    class Checker:
        def generate_text(self, *, prompt, **_):
            lines = json.loads(prompt)["analysis_lines"]
            wrong = [n for n, x in lines.items() if "no sedative in the blood" in x or "never happened" in x]
            quibble = [n for n, x in lines.items() if "GSR on both hands" in x]
            return "\n".join([f"WRONG: {n} | the record does not say that: the blood held a sedative" for n in wrong]
                             + [f"WRONG: {n} | the record says residue was found, consistent with this" for n in quibble])

    got = {"explanations": [{"name": "suicide", "for": ["GSR on both hands", "no sedative in the blood"], "against": [],
                             "settle": []}],
           "clashes": [{"one": "FIR place 12A", "other": "narrative 21A", "why": "which plot"}],
           "view": "It fits murder best. The shot never happened from the front."}
    removed = case_analyst.check(got, {"reports_of_this_case": []}, Checker())
    assert got["explanations"][0]["for"] == ["GSR on both hands"] and got["clashes"]
    # Sherlock's view is his labelled opinion and stays whole; its claims are checked as the points above.
    assert got["view"] == "It fits murder best. The shot never happened from the front." and len(removed) == 1


def test_officers_by_rank_vehicle_owners_and_a_check_out_with_no_time():
    from sherlocks.linkgraph.dossier import criminal_flags
    from sherlocks.team.case_scope import _RANK
    from sherlocks.team.record_writer import _duration

    assert all(_RANK.match(n) for n in ("P/ASI-Karim Ali", "SIP Adeel", "HC Babar", "PI/IO Mr. Hamid", "DSP Tariq"))
    assert not any(_RANK.match(n) for n in ("Sajid Ali", "Sikandar", "Spinbel Khan"))
    # The AVLC record's subject is the owner of a stolen vehicle - a victim, not an offender.
    assert criminal_flags({"flags": ["stolen_vehicle"]}) == []
    assert _duration("2022-10-02 22:56:00", "2022-10-08 00:00:00") == "about 6 day(s) (check-out time not recorded)"
    assert _duration("2024-08-06 09:22:00", "2024-08-08 11:52:00") == "2 day(s) 2 h"


def test_the_same_file_uploaded_again_is_read_again_when_an_older_reader_read_it(tmp_path):
    """A file read before the whole-document reader (30 pages, ten facts) is read again, whole, when the officer
    uploads it again - into the same document; one already read whole is kept as it is."""
    from types import SimpleNamespace

    from fpdf import FPDF

    from sherlocks.evidence.uploads import process_upload

    pdf = FPDF()
    for line in ("Commission of Inquiry into the death of Kamran Aslam.",
                 "13.03.2026 7:45 a.m. A watchman finds the body near the canal bridge."):
        pdf.add_page()
        pdf.set_font("Helvetica", size=11)
        pdf.cell(0, 10, line)
    content = bytes(pdf.output())
    case = CaseFile()
    llm = Lines({}, "DOCUMENT: Commission of inquiry report | The Commission | 2026 | Kamran Aslam\nSUMMARY: A short reading.")
    llm.parts = {"1-2": "EVENT: 13.03.2026 | 7:45 a.m. | A watchman finds the body | A watchman finds the body near the canal bridge"}
    agents = SimpleNamespace(case=case, cfg=SimpleNamespace(max_upload_mb=25), graph=lambda: {"nodes": [], "edges": []},
                             vision=lambda: None, images=None, llm=lambda: llm, builder=None, source=None)
    import hashlib

    key = f"upload:{hashlib.sha256(content).hexdigest()[:16]}"
    old = case.add_document(kind="upload", key=key, title="Upload: inquiry.pdf", source="Uploaded by the officer",
                            text="Uploaded file: inquiry.pdf\n[page 1] Commission of Inquiry")      # an older, cut reading
    got = process_upload(agents, name="inquiry.pdf", content=content, upload_dir=tmp_path)
    doc = case.documents[got]
    assert got == old and doc["reading"]["events"][0]["what"] == "A watchman finds the body"
    assert "canal bridge" in doc["text"] and doc["reading_status"]["state"] == "done"
    # The officer's chat follows each file: its last state, and what was found in it, are in the case index.
    up = case.index()["uploads"]["inquiry.pdf"]
    assert up["state"] == "done" and up["doc"] == old and up["events"] == 1 and "reading" not in case.index()["documents"][0]
    calls = len(llm.prompts)
    assert process_upload(agents, name="inquiry.pdf", content=content, upload_dir=tmp_path) == old
    assert len(llm.prompts) == calls                         # read whole already: not read a third time


def test_a_lead_the_new_document_answers_is_marked_answered_with_its_fact():
    from sherlocks.team import document_reader

    case, doc = _case_with(INQUIRY)
    fir = case.add_document(kind="fir", key="fir:1", title="FIR 12/26", source="PSRMS", text="He did not come home.")
    fact = case.add_fact(fir, "He did not come home", "He did not come home", kind="event")
    asked = case.set_lead("chat:who found", {"hint": "Who found the body is not recorded", "why": "", "next_step": "",
                                             "source": fact, "rests_on": [fact], "found_in": "chat"})
    other = case.set_lead("chat:van", {"hint": "Owner of the red car", "why": "", "next_step": "", "source": fact})

    class Reader(Lines):
        def generate_text(self, *, prompt, system="", **_):
            given = json.loads(prompt)
            if "open_leads" in given:
                found = given["dated_steps"][0].split(":")[0]
                return f"ANSWERED: {asked} | A watchman found it near the canal bridge | {found}\nANSWERED: K99 | x | F1"
            return super().generate_text(prompt=prompt, system=system)

    llm = Reader({"1-3": "EVENT: 13.03.2026 | 7:45 a.m. | A watchman finds the body near the canal bridge | "
                         "A watchman finds the body near the canal bridge"}, "SUMMARY: s")
    reading = document_reader.read(case, doc, llm)
    assert reading["answered"] == [asked] and case.leads[asked]["status"] == "answered"
    assert reading["events"][0]["fact"] in case.leads[asked]["reason"] and case.leads[other]["status"] == "open"


def test_a_name_the_model_respelt_is_put_back_as_the_records_spell_it():
    from sherlocks.team.story_writer import respelt

    case, doc = _case_with(INQUIRY + "The watchmen Tariq and Qasim found it.")
    case.documents[doc]["reading"] = {"people": [{"name": "Qasim", "part": "watchman"}, {"name": "Tariq", "part": "watchman"}]}
    assert respelt(case, {"documents": [doc]}, ["Kasim", "Tariq", "Kamran Aslam"]) == {"Kasim": "Qasim"}


def test_points_go_on_their_side_and_a_measured_finding_is_given_with_its_numbers():
    from sherlocks.team import case_analyst

    got = {"explanations": [{"name": "murder by a third party", "for": [],
                             "against": ["The pistol was never recovered and the casing matched no known gun [D17].",
                                         "The FIR was registered at 15:15 [D3]."]},
                            {"name": "suicide", "for": [], "against": [("The residue was heavier on the back of the shirt, "
                                                                        "so the bullet entered the back [D11].")]}],
           "view": "Murder fits best."}

    class Sides:
        def generate_text(self, *, prompt, **_):
            lines = json.loads(prompt)
            return "\n".join(f"{n}: " + ("MORE" if "pistol" in x["point"] else "NEITHER" if "15:15" in x["point"] else "LESS")
                             for n, x in lines.items())

    moved = case_analyst.sides(got, Sides())
    murder = got["explanations"][0]
    assert murder["for"] == ["The pistol was never recovered and the casing matched no known gun [D17]."]
    assert murder["against"] == [] and len(moved) == 2               # moved to its side; one that bears on nothing, out
    material = {"reports_of_this_case": [{"id": "D11", "findings": [
        "The shirt has a small hole on the back (posterior) measuring approximately 6.99 mm.",
        "The shirt has a larger hole on the front (anterior) measuring approximately 35.37 mm.", "Acid was found."]}]}
    assert case_analyst.measured(got, material)
    point = got["explanations"][1]["against"][0]
    assert "6.99 mm" in point and "35.37 mm" in point and point.count("[D11]") == 1
    assert case_analyst.measured(got, material) == []               # already given: not added twice


def test_the_same_team_serves_a_fraud_or_a_theft_not_only_a_death():
    """Nothing in the papers, the measurements or the sides is about one kind of crime."""
    from sherlocks.linkgraph.network import PersonNetwork
    from sherlocks.team import case_analyst, case_papers

    case = CaseFile()
    fir = case.add_document(kind="fir", key="fir:9", title="FIR 41/26 · PS Saddar", source="PSRMS",
                            text="First information: the cheque given by Naveed Rafiq was dishonoured.",
                            data={"sections": "489-F PPC, 420 PPC"})
    bank = case.add_document(kind="upload", key="upload:bank", title="Upload: bank.pdf", source="Uploaded by the officer",
                             text="Bank statement of account 0101. Agreement between the parties.")
    got = case_papers.check(case, PersonNetwork({"nodes": [], "edges": []}), {"documents": [fir, bank]}, {})
    status = {r["paper"]: r["status"] for r in got["papers"]}
    assert {"cheque", "fraud"} <= set(got["kinds"]) and "death" not in got["kinds"]
    assert status["The cheque and the bank's return memo"] == "not on the board"
    assert status["Bank statements and account records"] == "mentioned, not on the board"
    assert "Post-mortem report" not in status and "Identification parade" not in status

    theft = CaseFile()
    fir = theft.add_document(kind="fir", key="fir:10", title="FIR 7/26", source="PSRMS", text="A motorcycle was stolen.",
                             data={"sections": "381-A PPC"})
    assert "vehicle" in case_papers.check(theft, PersonNetwork({"nodes": [], "edges": []}), {"documents": [fir]}, {})["kinds"]

    # A measured amount is put into the point that rests on the same record, as for any other measurement.
    weighed = {"explanations": [{"name": "the manager took the money", "against": [],
                                 "for": ["Money moved from the company account to his own account [D5]."]}], "view": ""}
    case_analyst.measured(weighed, {"reports_of_this_case": [{"id": "D5", "findings": [
        "Rs 4,500,000 moved from the company account to the manager's account on 3 March."]}]})
    assert "Rs 4,500,000" in weighed["explanations"][0]["for"][0]
