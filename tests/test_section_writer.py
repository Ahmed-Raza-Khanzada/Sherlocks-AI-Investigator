"""P13 Section writer: headings and tables chosen for the case, kept only where the records hold them. Made-up data."""

from sherlocks.team import section_writer

MATERIAL = {"earlier_cases": [
    {"target": "Kamran Aslam", "fir": "45/23", "police_station": "PS Gulberg", "offence": "392 robbery",
     "registered": "06-06-2023 10:00", "doc": "D1"},
    {"target": "Imran Rashid", "fir": "12/21", "police_station": "PS Saddar", "offence": "381-A vehicle theft",
     "registered": "2021-02-03", "doc": "D2"}],
    "targets": [{"name": "Kamran Aslam", "phones": ["03990000101"], "vehicles": ["Corolla LEA-1234"]}]}

TEXT = """Here are the sections.
SECTION: Offences of the targets in date order | the pattern across their FIRs
PLACE: after record
TABLE: Date | FIR | Police station | Offence | Target
ROW: 03-02-2021 | 12/21 | PS Saddar | 381-A vehicle theft | Imran Rashid [D2]
ROW: 06-06-2023 | 45/23 | PS Gulberg | 392 robbery | Kamran Aslam [D1]
ROW: 01-01-2024 | 77/24 | PS Lyari | 395 dacoity | Kamran Aslam [D9]
SECTION: Vehicles on record | a car used in snatching may be among them
PLACE: targets
TABLE: Vehicle | Holder
ROW: Corolla LEA-1234 | Kamran Aslam | extra note
POINT: Kamran Aslam's phone 03990000101 is the number on the vehicle record.
POINT: Phone 03990009999 was used in the snatching.
SECTION: Hotel stays | already in the report
POINT: Kamran Aslam stayed at a hotel.
"""


def test_the_model_s_sections_are_read_and_only_what_the_records_hold_is_kept():
    got = section_writer.read_sections(TEXT, known={"D1", "D2"})
    assert [s["heading"] for s in got] == ["Offences of the targets in date order", "Vehicles on record", "Hotel stays"]
    assert got[0]["place"] == "record" and got[1]["place"] == "targets" and got[2]["place"] == "end"
    assert got[0]["columns"] == ["Date", "FIR", "Police station", "Offence", "Target"]
    assert got[1]["rows"] == [["Corolla LEA-1234", "Kamran Aslam | extra note"]]       # extra cells go to the last column
    kept, dropped = section_writer.keep(got, MATERIAL, ["The case at a glance", "Hotel stays"])
    assert [s["heading"] for s in kept] == ["Offences of the targets in date order", "Vehicles on record"]   # no repeat
    # A date in the other order is the same date; an FIR the records do not hold (77/24) is dropped.
    assert [r[1] for r in kept[0]["rows"]] == ["12/21", "45/23"]
    # A phone number the records do not hold is dropped.
    assert kept[1]["points"] == ["Kamran Aslam's phone 03990000101 is the number on the vehicle record."]
    assert dropped == 2


def test_without_a_model_or_with_nothing_to_add_there_are_no_sections():
    assert section_writer.write(MATERIAL, [], set(), None) == {"sections": [], "dropped": 0}

    class Model:
        def generate_text(self, **_):
            return "NONE"

    assert section_writer.write(MATERIAL, [], set(), Model()) == {"sections": [], "dropped": 0}


def test_an_added_section_is_drawn_where_the_writer_placed_it():
    import io

    from pypdf import PdfReader

    from sherlocks.evidence.case_file import CaseFile
    from sherlocks.evidence.report_pdf import _added_sections, _Pdf, _Sources

    pdf = _Pdf("Kamran Aslam")
    pdf.add_page()
    kept, _ = section_writer.keep(section_writer.read_sections(TEXT, set()), MATERIAL, [])
    n = _added_sections(pdf, 4, [s for s in kept if s["place"] == "record"], _Sources(CaseFile()))
    assert n == 5
    text = " ".join(" ".join(p.extract_text().split()) for p in PdfReader(io.BytesIO(bytes(pdf.output()))).pages)
    assert "Offences of the targets in date order" in text and "Added by Sherlock for this case" in text
    assert "45/23" in text and "77/24" not in text


def test_record_ids_written_in_another_cell_go_to_the_source_column():
    text = ("SECTION: Vehicles on record | why\nTABLE: Vehicle | Context | Source\n"
            "ROW: Corolla LEA-1234 | seen with Kamran Aslam [D1, D2] | -\n")
    kept, _ = section_writer.keep(section_writer.read_sections(text, {"D1", "D2"}), MATERIAL, [])
    assert kept[0]["rows"] == [["Corolla LEA-1234", "seen with Kamran Aslam", "[D1, D2]"]]


def test_a_heading_for_the_same_kind_of_section_is_a_repeat_and_an_empty_column_goes():
    assert section_writer.same_heading("Forensic and chemical findings on the deceased", "Forensic and medical findings")
    assert section_writer.same_heading("Phone numbers and holders", "Phones of the targets")
    assert not section_writer.same_heading("Items recovered and their forensic status", "Forensic and medical findings")
    text = ("SECTION: Vehicles seen | why\nTABLE: Vehicle | Registered to | Holder\n"
            "ROW: Corolla LEA-1234 | Not recorded | Kamran Aslam\nROW: Corolla LEA-1234 | - | Imran Rashid\n"
            "SECTION: Lab submissions | why\nTABLE: Date | Submitted by\nROW: 06-06-2023 | PI (implied from the report)\n")
    kept, dropped = section_writer.keep(section_writer.read_sections(text, set()), MATERIAL, [])
    assert kept[0]["columns"] == ["Vehicle", "Holder"]                 # "not recorded" in every row: no column
    assert [s["heading"] for s in kept] == ["Vehicles seen"] and dropped == 1      # a guessed value is no row


def test_an_open_question_the_records_answer_is_not_open():
    from sherlocks.team.case_analyst import still_open

    material = {"the_firs": [{"fir": "45/23", "first_information": "At 9 pm Asif Rafiq saw Kamran Aslam fire at his shop."}]}

    class Model:
        def generate_text(self, **_):
            return ("Q1: ANSWERED | Asif Rafiq saw Kamran Aslam fire at his shop at 9 pm [D1]\n"
                    "Q2: OPEN\n"
                    "Q3: PARTLY | Kamran Aslam fired at the shop at 9 pm [D1] | the exact place of the shooter\n"
                    "Q4: ANSWERED | Bilal Khan saw it at 10 pm [D9]")

    gaps = ["Who saw the shooting?", "Where is the pistol now?", "When and from where was the shot fired?",
            "Who else was there?"]
    still, answered = still_open(gaps, material, Model())
    assert answered == [{"question": "Who saw the shooting?", "answer": "Asif Rafiq saw Kamran Aslam fire at his shop at 9 pm [D1]"}]
    assert still[0] == "Where is the pistol now?"
    assert still[1].startswith("When and from where was the shot fired? (the records give: Kamran Aslam fired at the shop at 9 pm")
    assert still[2] == "Who else was there?"         # an answer the material does not hold is not taken
    assert still_open(gaps, material, None) == (gaps, [])


def test_story_dates_read_as_dates():
    from sherlocks.evidence.report_pdf import _day

    assert _day("2026-07-28T04:00:00") == "28-07-2026 04:00" and _day("2026-08-10") == "10-08-2026"
    assert _day("date not recorded") == "date not recorded"


def test_the_same_facts_under_a_new_heading_are_a_repeat():
    shown = {"Forensic and medical findings": ["LAB-000101 blood: traces of a sedative",
                                               "LAB-000101 shirt posterior defect 5.00 mm"],
             "The targets": ["Kamran Aslam 03990000101"]}
    material = {**MATERIAL, "reports": shown["Forensic and medical findings"]}
    text = ("SECTION: Samples and laboratory results | why\nTABLE: Sample | Result | Report\n"
            "ROW: Blood | traces of a sedative | LAB-000101\nROW: Shirt | posterior defect 5.00 mm | LAB-000101\n"
            "SECTION: Vehicles on record | why\nTABLE: Vehicle | Holder\nROW: Corolla LEA-1234 | Kamran Aslam\n"
            "ROW: Kamran Aslam's phone | 03990000101\n"
            "SECTION: Cars again | why\nTABLE: Car | Holder\nROW: Corolla LEA-1234 | Kamran Aslam\n")
    kept, dropped = section_writer.keep(section_writer.read_sections(text, set()), material, [], shown)
    # The lab table only repeats the findings section; the phone row repeats the targets; the second car table
    # repeats the first added one.
    assert [(s["heading"], s["rows"]) for s in kept] == [("Vehicles on record", [["Corolla LEA-1234", "Kamran Aslam"]])]
    assert dropped == 4


def test_the_writer_passes_what_the_report_shows_to_the_repeat_check():
    class Model:
        def generate_text(self, **_):
            return ("SECTION: Samples and laboratory results | why\nTABLE: Sample | Result | Report\n"
                    "ROW: Blood | traces of a sedative | LAB-000101\n")

    shown = {"Forensic and medical findings": ["LAB-000101 blood: traces of a sedative"]}
    out = section_writer.write({"reports": shown["Forensic and medical findings"]}, ["Forensic and medical findings"],
                               set(), Model(), holds={}, shown=shown)
    assert out == {"sections": [], "dropped": 1}
