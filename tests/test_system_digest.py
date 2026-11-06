"""What a police system holds for a person, written out - never just "record found". Made-up people."""

from sherlocks.team.register import digest

PERSON = {"name": "Kamran Aslam", "cnic": "9999900000011", "father_name": "Aslam Khan", "names": []}


def test_a_table_the_system_returned_is_written_row_by_row_with_what_the_rows_share_said_once():
    rows = [{"firno": "12", "firyear": "2025", "ps_name": "Saddar", "status": "Absconder", "accused_name": "Kamran Aslam",
             "accused_cnic": "9999900000011", "court_name": "Sessions Court South", "caseflow_name": "Pending",
             "current_caseflowdate": "2026-02-01 00:00:00"},
            {"firno": "12", "firyear": "2025", "ps_name": "Saddar", "status": "Absconder", "accused_name": "Kamran Aslam",
             "accused_cnic": "9999900000011", "court_name": "Judicial Magistrate South", "caseflow_name": "Disposed",
             "current_caseflowdate": "2025-11-20 00:00:00"}]
    lines = digest({"summary": "Record found in CFMS (2)", "raw": {"data": rows}, "fields": []}, PERSON, "Kamran Aslam")
    assert lines[0] == "2 entries, all: Firno: 12 · Firyear: 2025 · Ps name: Saddar · Status: Absconder"
    assert lines[1].startswith("• Current caseflowdate: 2026-02-01 · ")           # when first, no midnight
    assert "Sessions Court South" in lines[1] and "Judicial Magistrate South" in lines[2]
    assert not any("9999900000011" in x or "Kamran" in x for x in lines)           # who he is is said elsewhere


def test_one_record_gives_its_fields_its_firs_stays_and_vehicles():
    data = {"fields": [{"label": "Name", "value": "Kamran Aslam"}, {"label": "Address", "value": "House 4, Lyari"},
                       {"label": "SIMs", "value": "03990000101, 03990000102"}],
            "firs": [{"label": "45/24", "police_station": "PS Saddar", "offence": "392 PPC", "role": "accused",
                      "status": "challan"}],
            "stays": [{"hotel": "Hotel Test", "district": "South", "check_in": "2026-01-02 22:10", "check_out": "2026-01-03"}],
            "vehicles": ["ABC-123 Corolla"]}
    lines = digest(data, PERSON, "Kamran Aslam")
    assert lines[0] == "Address: House 4, Lyari · SIMs: 03990000101, 03990000102"
    assert "FIR 45/24 · PS Saddar · 392 PPC · as accused · challan" in lines
    assert "Stayed at Hotel Test · South · 2026-01-02 22:10 to 2026-01-03" in lines
    assert "Vehicle: ABC-123 Corolla" in lines
