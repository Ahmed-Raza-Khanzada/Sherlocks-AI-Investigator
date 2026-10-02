"""PRVS unified profile (/api/person/profile): searchable by CNIC, mobile and email; a
profile brings email/passport, cases, verification witnesses and CRO-linked FIRs.
Synthetic data in the real response shape."""

from __future__ import annotations

from typing import Any

from sherlocks.linkgraph.ems import EmsBackend
from sherlocks.linkgraph.extractors import extract_record
from sherlocks.linkgraph.images import MemoryImageStore
from sherlocks.linkgraph.models import PersonRef

CNIC = "9999900000011"


def profile(cnic: str = "99999-0000001-1", mobile: str = "03990000101", name: str = "KAMRAN AHMED") -> dict:
    return {"success": True, "data": {
        "search_criteria": {"cnic": cnic},
        "person_details": {
            "name": name, "father_name": "NADEEM AHMED", "cnic": cnic, "passport": "AB0000001",
            "email": "kamran.demo@example.pk", "mobile": mobile, "address": "House 12, Street 4, Gulshan-e-Iqbal, Karachi",
            "district_name": None, "zone_name": "Karachi East",
            "cases_summary": {"total_cases": 2, "as_applicant": 1, "as_witness": 1},
            "cases": [
                {"case_id": 101, "roles": ["applicant"], "request_date": "2025-01-05 10:00:00",
                 "status_name": "Completed", "purpose_name": "Visa Purpose", "district_name": None, "zone_name": "Karachi East"},
                {"case_id": 202, "roles": ["witness2"], "request_date": "2025-03-01 09:00:00",
                 "status_name": "In Process", "purpose_name": "Immigration", "district_name": None, "zone_name": "Karachi East"},
            ],
        },
        "witnesses": [
            {"case_id": 101, "witness_number": 1, "name": "TARIQ HUSSAIN", "father_name": "GHULAM HUSSAIN",
             "cnic": "99999-0000003-7", "mobile": "03990000301", "address": "Gulshan-e-Iqbal"},
            {"case_id": 101, "witness_number": 2, "name": "ASIF ALI", "father_name": "LIAQUAT ALI",
             "cnic": "99999-0000007-9", "mobile": "03990000701", "address": "Gulshan-e-Iqbal"},
        ],
        "criminal_links": [
            {"source_system": "CRO", "case_id": 101, "subject_role": "applicant", "verification_status": 2,
             "cro_no": 123456, "disposal": None,
             "records": [{"fir_no": "45/2023", "fir_offence": "395/34 PPC", "police_station": "گلشن", "status": "N/A"}]},
        ],
    }}


class _Http:
    """Answers the unified endpoint per identifier; records what was asked."""

    timeout = 5

    def __init__(self, answers: dict[str, tuple[int, Any]]) -> None:
        self.answers = answers
        self.asked: list[tuple[str, dict]] = []

    def send(self, req):
        self.asked.append((req.url.rsplit("/api/", 1)[-1], dict(req.params or {})))
        if "person/profile" in req.url:
            for key, value in (req.params or {}).items():
                if f"{key}={value}" in self.answers:
                    return self.answers[f"{key}={value}"]
        return 404, {"success": False, "message": "No record found"}


def test_profile_by_cnic_brings_email_passport_cases_witnesses_and_cro():
    http = _Http({"cnic=99999-0000001-1": (200, profile())})
    out = EmsBackend(http=http).lookup("prvs", CNIC, None)
    assert out["hit"] and "2 verification case(s), 2 witness(es), 1 CRO record(s)" in out["summary"]
    assert http.asked[0] == ("person/profile", {"cnic": "99999-0000001-1"})

    rec = extract_record("prvs", out, PersonRef(cnic=CNIC), MemoryImageStore())
    assert rec.subject.extra["email"] == "kamran.demo@example.pk" and rec.subject.extra["passport"] == "AB0000001"
    fields = {f.label: f.value for f in rec.fields}
    assert fields["Email"] == "kamran.demo@example.pk" and "1 as applicant" in fields["Cases"]
    assert "Applicant · Visa Purpose · Completed · 2025-01-05" in fields["Case 101"]
    assert "Witness · Immigration" in fields["Case 202"]
    assert {(r.ref.name, r.relation) for r in rec.related} == {
        ("TARIQ HUSSAIN", "Verification witness (PRVS)"), ("ASIF ALI", "Verification witness (PRVS)")}
    assert [(f.fir_no, f.fir_year, f.role) for f in rec.firs] == [("45", "2023", "Accused (CRO via PRVS)")]
    assert {"criminal_record", "fir_record"} <= set(rec.flags)


def test_a_profile_on_the_persons_number_but_another_cnic_is_a_relation_not_them():
    other = profile(cnic="99999-0000099-9", name="SOMEONE ELSE")
    http = _Http({"mobile=03990000101": (200, other)})
    out = EmsBackend(http=http).lookup("prvs", CNIC, "03990000101")
    rec = extract_record("prvs", out, PersonRef(cnic=CNIC, phones=["03990000101"]), MemoryImageStore())
    assert rec.subject.cnic in (None, CNIC)                    # nothing of the other person grafted on
    assert [(r.ref.cnic, r.relation) for r in rec.related] == [("9999900000999", "PRVS profile found by this number")]
    assert not rec.firs                                        # their CRO record stays theirs


def test_no_profile_falls_back_to_the_case_endpoints():
    http = _Http({})
    EmsBackend(http=http).lookup("prvs", CNIC, "03990000101")
    asked = [path for path, _ in http.asked]
    assert asked[:2] == ["person/profile", "person/profile"]
    assert {"cases/by-cnic", "cases/by-mobile"} <= set(asked)


def test_email_search_asks_prvs_by_email():
    http = _Http({"email=kamran.demo@example.pk": (200, profile())})
    out = EmsBackend(http=http).lookup_email("kamran.demo@example.pk")
    assert out["hit"] and http.asked == [("person/profile", {"email": "kamran.demo@example.pk"})]


def test_email_only_run_starts_from_the_prvs_profile():
    """An email-only search: PRVS returns the CNIC, and the run searches with it."""
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    http = _Http({"email=kamran.demo@example.pk": (200, profile()), "cnic=99999-0000001-1": (200, profile())})
    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = True
    s.osint.free_web_search = False
    s.osint.allowed_tools = []
    s.osint.name_match = False
    mgr = memory_manager(s, backends={"ems": EmsBackend(http=http)})
    run = mgr.get(mgr.start(GraphRunParams(email="kamran.demo@example.pk", depth=1, max_persons=1, backend="ems",
                                           systems=["prvs"]), wait=True).id)
    seed = next(n for n in run["graph"]["nodes"] if n["kind"] == "person" and n["data"]["seed"])
    assert seed["data"]["cnic"] == CNIC and seed["data"]["search_status"] == "searched"
    assert any("is on the PRVS profile of" in e["message"] for e in run["events"])
    names = {n["label"] for n in run["graph"]["nodes"] if n["kind"] == "person"}
    assert {"Tariq Hussain", "Asif Ali"} <= names


def test_verification_witness_reads_as_vouching_and_recurring_guarantor_is_found():
    from sherlocks.linkgraph.relations import describe
    from sherlocks.linkgraph.scenarios import find_scenarios

    rel = describe("Verification witness (PRVS)", named="w", owner="a").sentence({"w": "Tariq", "a": "Kamran"})
    assert rel == "Tariq vouched as a PRVS witness for Kamran"

    def person(pid, name):
        return {"id": pid, "kind": "person", "label": name, "data": {"name": name, "flags": [], "firs": [], "stays": [],
                "organisations": [], "vehicles": [], "police_stations": [], "names": [name], "records": []}}

    nodes = [person("w", "Tariq Hussain"), person("a", "Kamran Ahmed"), person("b", "Sajid Mehmood")]
    edges = []
    for owner in ("a", "b"):
        nodes.append({"id": f"s:prvs:{owner}", "kind": "system", "label": "PRVS", "data": {"system": "prvs", "owner": owner}})
        edges.append({"id": f"f{owner}", "source": owner, "target": f"s:prvs:{owner}", "kind": "found_in", "label": ""})
        edges.append({"id": f"s{owner}", "source": f"s:prvs:{owner}", "target": "w", "kind": "strong",
                      "label": "Verification witness (PRVS)", "system": "prvs"})
    found = [f for f in find_scenarios({"nodes": nodes, "edges": edges}) if f["scenario"] == "recurring_guarantor"]
    assert found and found[0]["people"][0] == "w"


def test_a_witness_found_by_the_searched_number_is_explained():
    """The real shape: searched by one number, PRVS returns a person whose own number is
    different and who is only ever a witness - a relation, with the reason spelled out."""
    body = profile(cnic="99999-0000055-5", mobile="03990005555", name="ABDUL WITNESS")
    pd = body["data"]["person_details"]
    pd["cases_summary"] = {"total_cases": 2, "as_applicant": 0, "as_witness": 2}
    pd["cases"] = [dict(pd["cases"][0], case_id=1, roles=["witness1"], request_date="2021-10-28 12:00:00"),
                   dict(pd["cases"][0], case_id=2, roles=["witness1"], request_date="2025-02-12 00:00:00")]
    body["data"]["witnesses"], body["data"]["criminal_links"] = [], []
    out = EmsBackend(http=_Http({"mobile=03990000101": (200, body)})).lookup("prvs", None, "03990000101")
    rec = extract_record("prvs", out, PersonRef(phones=["03990000101"]), MemoryImageStore())
    (rel,) = rec.related
    assert rel.ref.cnic == "9999900000555" and rel.relation == "PRVS profile found by this number"
    assert "witness in 2 PRVS verification case(s) (2021-2025)" in rel.detail
    assert "their own number is 03990005555" in rel.detail
    assert rel.ref.extra["email"] == "kamran.demo@example.pk"          # the email stays with its owner
    assert any(f.label == "PRVS profile of someone else" and "found by mobile 03990000101" in f.value for f in rec.fields)
    assert not rec.subject.cnic and not rec.firs
