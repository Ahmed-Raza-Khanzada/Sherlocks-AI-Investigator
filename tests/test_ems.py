"""EMS backend adapters + extractors, over the documented sample responses. No network."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from sherlocks.linkgraph.ems import EmsBackend, Req
from sherlocks.linkgraph.extractors import extract_record
from sherlocks.linkgraph.images import MemoryImageStore
from sherlocks.linkgraph.models import GraphRunParams, PersonRef

CNIC = "4210112345671"
CNIC_D = "42101-1234567-1"


class FakeEmsHttp:
    """Routes by URL/params to a canned (status, body). Records calls."""

    timeout = 5

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def send(self, req: Req) -> tuple[int, Any]:
        self.calls.append(f"{req.method} {req.url}")
        for needle, resp in self.routes.items():
            if needle in req.url:
                # DB=4w/2w for excise: allow a callable to see params
                return resp(req) if callable(resp) else resp
        return 404, {"message": "not found"}


def _rec(backend: EmsBackend, system: str, cnic=None, phone=None):
    result = backend.lookup(system, cnic, phone)
    rec = extract_record(system, result, PersonRef(cnic=cnic, phones=[phone] if phone else []), MemoryImageStore())
    return result, rec


def test_cro_maps_firs_and_subject():
    body = {"success": True, "data": [{
        "cro_no": "12345", "cnic": CNIC, "cro_full_name": "AHMED ALI", "cro_father_name": "MUHAMMAD ALI",
        "cro_age": "35", "category_desc": "Proclaimed Offender", "record_district": "Karachi East",
        "FIRList": [{"fir_no": "12", "fir_year": "2020", "ps_name": "Gulshan", "fir_offence": "302 PPC"}]}]}
    b = EmsBackend(http=FakeEmsHttp({"crodashboard": (200, body)}))
    result, rec = _rec(b, "cro", cnic=CNIC)
    assert result["hit"] and rec.subject.name == "AHMED ALI" and rec.subject.father_name == "MUHAMMAD ALI"
    assert "criminal_record" in rec.flags
    assert rec.firs and rec.firs[0].fir_no == "12" and rec.firs[0].offence == "302 PPC"


def test_arms_nested_profile():
    body = {"status": True, "data": [{
        "main": {"full_name": "AHMED ALI", "cnic": CNIC_D, "father_name": "MUHAMMAD ALI",
                 "contact_number_1": "03001234567", "district_name": "Karachi"},
        "bioData": {"crime_category_name": "Murder"},
        "addresses": [{"detailed_address": "House 12, Gulshan", "city_name": "Karachi"}],
        "firs": [{"fir_number": 12, "fir_year": 2020, "fir_offence": "302 PPC", "ps_name": "Gulshan"}]}]}
    b = EmsBackend(http=FakeEmsHttp({"get-profile-by-cnic": (200, body)}))
    result, rec = _rec(b, "arms", cnic=CNIC)
    assert result["hit"] and rec.subject.name == "AHMED ALI"
    assert "03001234567" in rec.subject.phones
    assert "arms_record" in rec.flags and rec.firs[0].fir_no == "12"


def test_nadra_identity_and_photo():
    body = {"status": True, "message": "CNIC verified", "data": {
        "citizen_number": CNIC, "name": "AHMED ALI", "father_husband_name": "MUHAMMAD ALI",
        "gender": "Male", "date_of_birth": "1990-01-15", "present_address": "Present address",
        "photograph": "/9j/4AAQSkZJRg" + "A" * 300}}
    b = EmsBackend(http=FakeEmsHttp({"/api/admin/": (200, body)}))
    result, rec = _rec(b, "nadra", cnic=CNIC)
    assert result["hit"]
    labels = {f.label for f in rec.fields}
    assert any("Name" in x for x in labels)


def test_watchlist_flag():
    body = {"status": True, "data": [{"name": "AHMED ALI", "cnic": CNIC, "police_station": "Gulshan",
                                      "is_proclaimed_offender": True}]}
    b = EmsBackend(http=FakeEmsHttp({"suspect-info": (200, body)}))
    result, rec = _rec(b, "watchlist", cnic=CNIC)
    assert result["hit"] and "watchlist" in rec.flags


def test_hotel_eye_mobile_hit():
    body = {"status": "success", "count": 1, "data": [{
        "guest_name": "AHMED ALI", "guest_cnic": CNIC_D, "guest_cell": "03001234567", "room_no": "201",
        "check_in": "2024-01-10", "check_out": "2024-01-12", "hotel_name": "Pearl Continental",
        "hotel_district": "Karachi South", "visit_purpose": "Business"}]}
    b = EmsBackend(http=FakeEmsHttp({"findGuestSimple": (200, body)}))
    result, rec = _rec(b, "hotel_eye", phone="03001234567")
    assert result["hit"] and rec.stays and rec.stays[0].hotel == "Pearl Continental"


def test_sbvs_details():
    body = {"success": True, "data": {"details": {
        "full_name": "AHMED ALI", "father_name": "MUHAMMAD", "cnic": CNIC_D, "mobile": "03001234567",
        "permanent_address": "Perm", "district_name": "Karachi"}, "image": {"url": "https://x/p.jpg"}}}
    b = EmsBackend(http=FakeEmsHttp({"single-request/": (200, body)}))
    result, rec = _rec(b, "sbvs", cnic=CNIC)
    assert result["hit"] and rec.subject.name == "AHMED ALI" and "03001234567" in rec.subject.phones


def test_hrmis_officer_flag():
    body = {"status": True, "output": [{"ofc_name": "ASI AHMED", "ofc_cnic": CNIC_D, "ofc_mobile": "0300-1234567",
                                        "rnk_name": "ASI", "current_posting": "PS Gulshan"}]}
    b = EmsBackend(http=FakeEmsHttp({"officer_data": (200, body)}))
    result, rec = _rec(b, "hrmis", cnic=CNIC)
    assert result["hit"] and "police_officer" in rec.flags and rec.subject.name == "ASI AHMED"


def test_dls_login_then_data():
    routes = {
        "auth/login": (200, {"token": "T"}),
        "licenseDataWithImage/": (200, {"success": True, "data": [{
            "cnic": CNIC_D, "firstname": "AHMED", "lastname": "ALI", "fathername": "MUHAMMAD",
            "license_no": "KHI-123456", "license_category": "A", "mobile": "03001234567"}]}),
    }
    b = EmsBackend(http=FakeEmsHttp(routes))
    result, rec = _rec(b, "dls", cnic=CNIC)
    assert result["hit"] and rec.subject.name == "AHMED ALI" and rec.subject.father_name == "MUHAMMAD"


def test_excise_owner_is_subject_and_vehicle_kept():
    body = {"statusCode": 0, "data": [{
        "registration_number": "KHI-1234", "manufacturer": "Toyota", "model": "Corolla",
        "owner_name": "AHMED ALI", "owner_father_name": "MUHAMMAD", "owner_cnic": CNIC,
        "owner_mobile": "03001234567", "owner_address": "Karachi"}]}
    b = EmsBackend(http=FakeEmsHttp({"getVehicleInfo": (200, body)}))
    result, rec = _rec(b, "excise", cnic=CNIC)
    assert result["hit"] and rec.subject.name == "AHMED ALI"
    assert "KHI-1234" in rec.vehicles


def test_avlc_stolen_vehicle_and_complainant():
    body = {"status": True, "data": [{
        "CompCode": "AV-001", "RegNo": "KHI-1234", "Make": "Honda", "Crime": "Vehicle Snatching",
        "PoliceStation": "Gulshan", "FirNo": "12/2023", "CNIC": CNIC, "MobileNo": "0300-1234567",
        "ComplainantName": "COMPLAINANT"}]}
    b = EmsBackend(http=FakeEmsHttp({"/api/icop": (200, body)}))
    result, rec = _rec(b, "avlc", cnic=CNIC)
    assert result["hit"] and "stolen_vehicle" in rec.flags and "KHI-1234" in rec.vehicles
    assert any(r.relation.startswith("Complainant") for r in rec.related)


def test_subscriber_numeric_rows():
    body = {"allNumbers": ["03001234567", "03331234567"], "notification": "OK",
            "0": {"number": "03001234567", "name": "AHMED ALI", "cnic": CNIC, "address": "Karachi"},
            "1": {"number": "03331234567", "name": "AHMED ALI", "cnic": CNIC, "address": "Karachi"}}
    b = EmsBackend(http=FakeEmsHttp({"number_check.php": (200, body)}))
    result, rec = _rec(b, "subscriber", cnic=CNIC)
    assert result["hit"] and rec.subject.name == "AHMED ALI"


def test_empty_and_error_are_distinct():
    b = EmsBackend(http=FakeEmsHttp({"crodashboard": (200, {"success": False, "data": []})}))
    assert b.lookup("cro", CNIC, None)["status"] == "no_record"

    class Boom:
        timeout = 5

        def send(self, req):
            raise RuntimeError("connection refused")

    b2 = EmsBackend(http=Boom())
    assert b2.lookup("cro", CNIC, None)["status"] == "error"


def test_charges_as_a_list_do_not_crash_the_run():
    """A CRO/PSRMS record whose offence comes back as ['395', '34'] (a list) must not
    fail FirKey validation and abort the whole run - it is coerced to text."""
    from sherlocks.linkgraph.models import FirKey

    key = FirKey(fir_no=395, fir_year=2023, police_station="PS Gulshan", offence=["395", "34"])
    assert key.offence == "395, 34" and key.fir_no == "395"

    body = {"success": True, "data": [{
        "cro_no": "1", "cnic": CNIC, "cro_full_name": "AHMED ALI",
        "FIRList": [{"fir_no": "45", "fir_year": "2023", "ps_name": "Gulshan", "fir_offence": ["395", "34"]}]}]}
    b = EmsBackend(http=FakeEmsHttp({"crodashboard": (200, body)}))
    _, rec = _rec(b, "cro", cnic=CNIC)
    assert rec.firs and rec.firs[0].offence == "395, 34"


def test_gateway_error_page_is_error_not_no_record():
    """A 502/503 or an HTML error page from a broken upstream (TRACS returning nginx's
    "502 Bad Gateway") must read as `error` and show in Failed systems - never as a
    silent no_record. A real 404 stays "no record"."""
    from sherlocks.linkgraph.ems import EmsHttp, Req

    class FakeResp:
        def __init__(self, status, text):
            self.status_code, self.text = status, text

    class FakeSession:
        def __init__(self, resp):
            self._resp = resp

        def request(self, *a, **k):
            return self._resp

    http = EmsHttp()
    http.session = FakeSession(FakeResp(502, "<html><head><title>502 Bad Gateway</title></head></html>"))
    with pytest.raises(requests.HTTPError, match="502"):
        http.send(Req("GET", "https://tracs.example/challans"))

    # a 404 with a JSON "no record" body is not an error
    http.session = FakeSession(FakeResp(404, '{"success": false, "message": "No record found"}'))
    code, body = http.send(Req("GET", "https://prvs.example/by-cnic"))
    assert code == 404 and body["success"] is False

    # 200 with valid JSON parses normally
    http.session = FakeSession(FakeResp(200, '{"success": true, "data": [1]}'))
    code, body = http.send(Req("GET", "https://ok.example"))
    assert code == 200 and body["data"] == [1]


def test_old_tenant_links_landlord():
    body = {"success": True, "data": [{"tenant_id": 55, "raw": {
        "tenant_name": "AHMED ALI", "tenant_cnic": CNIC_D, "tenant_mobile_number": "03001234567",
        "owner_name": "OWNER NAME", "owner_cnic": "42101-9999999-9", "owner_mobile_number": "03331234567",
        "property_address": "House 12, Block A"}}]}
    b = EmsBackend(http=FakeEmsHttp({"old-db/search": (200, body)}))
    result, rec = _rec(b, "old_tenant", cnic=CNIC)
    assert result["hit"]
    assert any(r.relation == "Landlord" for r in rec.related)


def test_full_ems_run_expands_and_links(monkeypatch):
    """A depth-2 run over EMS with a fake transport: owner→subject, tenancy→landlord,
    every found person searched, links drawn."""
    from sherlocks.settings import load_settings

    tenant_row = {"success": True, "data": [{"tenant_id": 1, "raw": {
        "tenant_name": "AHMED ALI", "tenant_cnic": CNIC_D, "tenant_mobile_number": "03001234567",
        "owner_name": "TARIQ OWNER", "owner_cnic": "42101-2222222-2", "owner_mobile_number": "03002222222",
        "property_address": "House 5, Block 2, Gulshan-e-Iqbal, Karachi"}}]}
    subscriber = {"allNumbers": ["03001234567"], "0": {"number": "03001234567", "name": "AHMED ALI", "cnic": CNIC, "address": "Karachi"}}
    routes = {"old-db/search": (200, tenant_row), "number_check.php": (200, subscriber)}
    backend = EmsBackend(http=FakeEmsHttp(routes))  # everything else → 404 empty

    s = load_settings()
    s.ollama.enabled = False
    s.llm.enabled = False
    from sherlocks.linkgraph.runs import memory_manager

    mgr = memory_manager(s, backends={"ems": backend})
    handle = mgr.start(GraphRunParams(cnic=CNIC, depth=2, backend="ems"), wait=True)
    run = mgr.get(handle.id)
    assert run["status"] == "completed", run["events"][-3:]
    people = {n["data"].get("cnic"): n for n in run["graph"]["nodes"] if n["kind"] == "person"}
    assert CNIC in people                      # subject
    assert "4210122222222" in people           # landlord, discovered + searched
    strong = [e for e in run["graph"]["edges"] if e["kind"] == "strong"]
    assert any("Landlord" in e["label"] for e in strong)


def test_failed_system_is_reported_with_error(monkeypatch):
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    def cro_boom(req):
        return 500, {"message": "upstream 500"}

    backend = EmsBackend(http=FakeEmsHttp({"crodashboard": cro_boom}))
    # make lookup surface an error status for cro (HTTP 500 → not hit, not 404)

    def cro(cnic, phone):  # force an error result deterministically
        return {"provider": "cro", "hit": False, "status": "error", "summary": "CRO 500: upstream down"}

    backend._adapters["cro"] = cro
    s = load_settings()
    s.ollama.enabled = False
    s.llm.enabled = False
    mgr = memory_manager(s, backends={"ems": backend})
    run = mgr.get(mgr.start(GraphRunParams(cnic=CNIC, depth=1, backend="ems"), wait=True).id)
    assert run["status"] == "completed"
    assert "cro" in (run["stats"].get("failed_systems") or {})
    assert "upstream down" in run["stats"]["failed_systems"]["cro"]
    assert any("FAILED" in e["message"] and e["level"] == "error" for e in run["events"])


def test_telecom_expands_every_number_and_linked_cnic():
    """Subject's SIMs are all searched; a number registered to a different CNIC becomes a
    linked person, who is then searched too."""
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    A, B = "4210100000011", "4220200000029"
    N1, N2, N3 = "3001111111", "3002222222", "3003333333"  # 10-digit as simsdatabases returns

    def subscriber(req):
        number = (req.data or {}).get("number", "")
        if number == A:  # subject's CNIC → SIMs N1, N2 (both on A)
            return 200, {"0": {"number": N1, "name": "AHMED ALI", "cnic": A, "address": "Karachi"},
                         "1": {"number": N2, "name": "AHMED ALI", "cnic": A, "address": "Karachi"}}
        if number == N2:  # this SIM is registered to a DIFFERENT person, B
            return 200, {"0": {"number": N2, "name": "BILAL KHAN", "cnic": B, "address": "Hyderabad"},
                         "1": {"number": N3, "name": "BILAL KHAN", "cnic": B, "address": "Hyderabad"}}
        if number == B:  # B's own CNIC → B's SIMs
            return 200, {"0": {"number": N2, "name": "BILAL KHAN", "cnic": B, "address": "Hyderabad"},
                         "1": {"number": N3, "name": "BILAL KHAN", "cnic": B, "address": "Hyderabad"}}
        return 200, {"notification": "No record found"}

    backend = EmsBackend(http=FakeEmsHttp({"number_check.php": subscriber}))
    s = load_settings()
    s.ollama.enabled = False
    s.llm.enabled = False
    mgr = memory_manager(s, backends={"ems": backend})
    run = mgr.get(mgr.start(GraphRunParams(cnic=A, depth=2, backend="ems"), wait=True).id)
    assert run["status"] == "completed", run["events"][-3:]
    people = {n["data"].get("cnic"): n for n in run["graph"]["nodes"] if n["kind"] == "person"}
    # Subject A carries both of its SIMs.
    assert set(people[A]["data"]["phones"]) >= {"0" + N1, "0" + N2}
    # The number N2 is owned by B → B is a linked, discovered, searched person.
    assert B in people
    via = " ".join(v["relation"] for v in people[B]["data"]["discovered_via"])
    assert "Registered owner of SIM" in via
    # B was searched → B's other SIM N3 is now on B.
    assert "0" + N3 in people[B]["data"]["phones"]


def test_cnic_found_elsewhere_is_fed_back_to_nadra():
    """A phone-only search whose SIMs carry no CNIC: once PRVS returns the CNIC, the
    CNIC-only systems (NADRA, CRO…) are re-run with it to confirm identity."""
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    nadra_cnics: list[str] = []

    def subscriber(req):  # SIMs, but no CNIC on them
        return 200, {"0": {"number": "3330000991", "name": "MUHAMMAD TESTER", "cnic": "", "address": "KHI"}}

    def prvs(req):  # PRVS by mobile returns the person's CNIC
        if "by-mobile" in req.url:
            return 200, {"success": True, "data": [{"case_id": 1, "name": "MUHAMMAD TESTER",
                                                    "cnic": "42101-0000099-1", "mobile": "03330000991"}]}
        return 404, {"success": False, "message": "No record found"}

    def nadra(req):
        nadra_cnics.append((req.data or {}).get("cnic", ""))
        return 200, {"status": True, "message": "CNIC verified",
                     "data": {"citizen_number": "4210100000991", "name": "MUHAMMAD TESTER",
                              "father_husband_name": "X", "present_address": "KHI"}}

    backend = EmsBackend(http=FakeEmsHttp({"number_check.php": subscriber, "cases/": prvs, "admin/": nadra}))
    s = load_settings()
    s.ollama.enabled = False
    s.llm.enabled = False
    mgr = memory_manager(s, backends={"ems": backend})
    run = mgr.get(mgr.start(GraphRunParams(phone="0333-0000991", depth=1, max_persons=1, backend="ems"), wait=True).id)
    assert run["status"] == "completed"
    assert "4210100000991" in nadra_cnics                       # NADRA queried with the found CNIC
    seed = next(n for n in run["graph"]["nodes"] if n["kind"] == "person" and n["data"].get("seed"))
    assert seed["data"]["cnic"] == "4210100000991"
    assert seed["data"]["lookups"]["nadra"]["status"] == "success"


@pytest.mark.parametrize("body", [
    {"status": True, "data": {"name": "AHMED ALI", "citizen_number": CNIC, "father_husband_name": "X"}},
    {"status": "true", "data": {"name": "AHMED ALI", "cnic": CNIC}},          # string flag
    {"success": True, "result": {"fullName": "AHMED ALI", "nic": CNIC}},        # success + result key
    {"status": 1, "data": [{"name": "AHMED ALI", "citizen_number": CNIC}]},     # list payload
    {"name": "AHMED ALI", "citizen_number": CNIC, "father_name": "X"},          # top-level fields
])
def test_nadra_reads_any_response_shape(body):
    """NADRA deployments disagree on the flag and where the record sits; a real hit in any
    of these shapes must be read as success, not misread as no_record (and then cached)."""
    b = EmsBackend(http=FakeEmsHttp({"/api/admin/": (200, body)}))
    out = b.lookup("nadra", CNIC, None)
    assert out["hit"] and out["data"]["details"]["name"] == "AHMED ALI"
    assert out["data"]["details"]["cnic"] == CNIC


def test_nadra_ip_refusal_is_error_not_no_record():
    """NADRA whitelists source IPs. When the server IP is not whitelisted it answers HTTP
    200 {"status":false,"messaage":"Invalid IP Address"} - an ACCESS failure, not a real
    "no record". It must be reported as error so the operator fixes access."""
    for body in ({"status": False, "messaage": "Invalid IP Address"},
                 {"status": False, "message": "Unauthorized: IP not allowed"}):
        b = EmsBackend(http=FakeEmsHttp({"/api/admin/": (200, body)}))
        out = b.lookup("nadra", CNIC, None)
        assert out["status"] == "error" and "access" in out["summary"].lower()


def test_nadra_genuine_empty_is_no_record():
    for body in ({"status": False, "message": "No record found", "data": None},
                 {"status": True, "data": {}}):
        b = EmsBackend(http=FakeEmsHttp({"/api/admin/": (200, body)}))
        assert b.lookup("nadra", CNIC, None)["status"] == "no_record"


def test_nadra_tries_archive_before_live():
    calls: list[dict] = []

    def nadra(req):
        calls.append(dict(req.data or {}))
        if (req.data or {}).get("archive") == "true":  # archive empty
            return 200, {"status": False, "message": "No archive record found", "data": None}
        return 200, {"status": True, "message": "CNIC verified", "data": {
            "citizen_number": CNIC, "name": "AHMED ALI", "father_husband_name": "MUHAMMAD ALI",
            "present_address": "Present"}}

    b = EmsBackend(http=FakeEmsHttp({"/api/admin/": nadra}))
    out = b.lookup("nadra", CNIC, None)
    assert out["hit"] and out["data"]["details"]["source"] == "live"
    # archive was tried first, then live
    assert calls[0].get("archive") == "true" and "archive" not in calls[1]


def test_psrms_phone_tried_in_every_format_until_hit():
    """DB stores the number as +92…; a plain-format query misses it. The variant sweep
    must find it and stop once a format hits."""
    stored = "+923001234567"
    tried: list[str] = []

    def psrms(req):
        val = (req.data or {}).get("phone") or (req.data or {}).get("cnic")
        if "phone" in (req.data or {}):
            tried.append(val)
        if (req.data or {}).get("phone") == stored:
            return 200, {"status": True, "data": [{"person_name": "AHMED ALI", "person_cnic": "42101-7654321-1",
                                                   "person_phone": stored, "fir_no": "45", "fir_year": "2023",
                                                   "ps_name": "Gulshan", "person_type": "Accused"}]}
        return 200, {"status": False, "data": []}  # every other format misses

    b = EmsBackend(http=FakeEmsHttp({"personsearch": psrms}))
    out = b.lookup("psrms", None, "0300-1234567")
    assert out["hit"] and out["data"]["record_count"] == 1
    # the sweep stopped at the format that hit, having tried the earlier ones first
    assert stored in tried and tried.index(stored) == len(tried) - 1
    assert tried == ["03001234567", "0300-1234567", "3001234567", "923001234567", stored]


def test_either_systems_ask_by_cnic_and_by_mobile():
    """A record can be filed under the person's number carrying a DIFFERENT CNIC - a PRVS
    tenancy case registered by the landlord, a SIM in a relative's name. Asking only by
    CNIC (the old `if cnic ... elif phone`) reported "no record" on a record that exists."""
    seen: list[str] = []

    def prvs(req):
        seen.append(req.url.rsplit("/", 1)[-1])
        if "by-mobile" in req.url:
            return 200, {"success": True, "data": [{"case_id": 663562, "name": "TENANT ON THE NUMBER",
                                                    "cnic": "42101-7654321-1", "mobile": "03001234567",
                                                    "address": "Flat 24, Kharadar Karachi"}]}
        return 404, {"success": False, "message": "No record found for the given CNIC."}

    b = EmsBackend(http=FakeEmsHttp({"/api/cases/": prvs}))
    # the CNIC the SIM resolved to is not the CNIC on the case
    out = b.lookup("prvs", CNIC, "03001234567")
    assert seen == ["by-cnic", "by-mobile"]           # both asked, not just the first
    assert out["hit"] and out["data"]["name"] == "TENANT ON THE NUMBER"


def test_psrms_no_record_only_after_every_format_was_tried():
    """"No record" is only honest once all 8 spellings missed."""
    tried: list[str] = []

    def psrms(req):
        if "phone" in (req.data or {}):
            tried.append(req.data["phone"])
        return 200, {"status": False, "data": []}

    b = EmsBackend(http=FakeEmsHttp({"personsearch": psrms}))
    assert b.lookup("psrms", None, "03001234567")["status"] == "no_record"
    assert tried == ["03001234567", "0300-1234567", "3001234567", "923001234567",
                     "+923001234567", "92300-1234567", "+92300-1234567", "300-1234567"]


def test_phone_variants_cover_every_stored_format():
    """Every spelling the number might be stored as, from any spelling of the input."""
    from sherlocks.linkgraph.normalize import phone_variants

    expected = ["03001234567", "0300-1234567", "3001234567", "923001234567", "+923001234567",
                "92300-1234567", "+92300-1234567", "300-1234567"]
    for written_as in ("+923001234567", "923001234567", "0300-1234567", "3001234567",
                       "03001234567", "0092-300-1234567", "+92 300 1234567"):
        assert phone_variants(written_as) == expected, written_as


def test_hope_employer_shape_and_employees():
    routes = {
        "hope-employee-cnic-search": (200, {"message": "No records found", "data": []}),
        "hope_employer_cnic_search": (200, {"message": "Request Successful", "data": {
            "employer": {"name": "AHMED ALI", "cnic": CNIC, "org_name": "ABC Co", "org_address": "Karachi",
                         "contact": "03001234567"},
            "employees": [{"name": "SARA", "father_name": "ALI", "cnic": "4210198765432",
                           "contact": "03331234567", "designation": "Clerk"}]}}),
    }
    b = EmsBackend(http=FakeEmsHttp(routes))
    result, rec = _rec(b, "hope", cnic=CNIC)
    assert result["hit"] and rec.subject.name == "AHMED ALI"
    assert "ABC Co" in rec.organisations
    assert any(r.ref.name == "SARA" and r.relation.lower().startswith("employee") for r in rec.related), \
        [(r.relation, r.ref.name) for r in rec.related]


def test_old_tenant_witness_linked():
    body = {"success": True, "data": [{"tenant_id": 55, "raw": {
        "tenant_name": "AHMED ALI", "tenant_cnic": CNIC_D, "owner_name": "OWNER", "owner_cnic": "42101-9999999-9",
        "property_address": "House 12", "tenant_witness_name_1": "WITNESS ONE", "tenant_witness_cnic_1": "4210188888888"}}]}
    b = EmsBackend(http=FakeEmsHttp({"old-db/search": (200, body)}))
    _, rec = _rec(b, "old_tenant", cnic=CNIC)
    assert any(r.relation == "Tenancy witness" and r.ref.name == "WITNESS ONE" for r in rec.related)


def test_psrms_names_the_role_a_person_plays_against_the_subject():
    """"Same identifier in FIR 45/2023 (Accused)" hides the fact worth knowing: the other
    person is a CO-ACCUSED of the subject in that FIR."""
    body = {"status": True, "data": [
        {"person_name": "AHMED ALI", "person_cnic": CNIC_D, "person_type": "Accused",
         "fir_no": "45", "fir_year": "2023", "ps_name": "Gulshan"},
        {"person_name": "CO ACCUSED", "person_cnic": "42101-7654321-1", "person_type": "Accused",
         "fir_no": "45", "fir_year": "2023", "ps_name": "Gulshan"},
        {"person_name": "COMPLAINANT", "person_cnic": "42101-9999999-9", "person_type": "Complainant",
         "fir_no": "45", "fir_year": "2023", "ps_name": "Gulshan"},
        {"person_name": "STRANGER", "person_cnic": "42101-8888888-8", "person_type": "Accused",
         "fir_no": "99", "fir_year": "2021", "ps_name": "Saddar"}]}
    b = EmsBackend(http=FakeEmsHttp({"personsearch": (200, body)}))
    _, rec = _rec(b, "psrms", cnic=CNIC)
    by_name = {r.ref.name: r.relation for r in rec.related}
    assert by_name["CO ACCUSED"] == "Co-accused in FIR 45/2023"
    assert by_name["COMPLAINANT"] == "Complainant against the subject in FIR 45/2023"
    # a FIR the subject is not in stays what it is: a shared-identifier hit
    assert by_name["STRANGER"].startswith("Same identifier in FIR 99/2021")


def test_hotel_eye_names_room_mates_and_co_guests():
    body = {"status": "success", "data": [
        {"guest_name": "AHMED ALI", "guest_cnic": CNIC_D, "guest_cell": "03001234567", "room_no": "201",
         "check_in": "2024-01-10", "check_out": "2024-01-12", "hotel_name": "Pearl Continental"},
        {"guest_name": "ROOM MATE", "guest_cnic": "42101-5151515-1", "room_no": "201",
         "check_in": "2024-01-10", "check_out": "2024-01-12", "hotel_name": "Pearl Continental"},
        {"guest_name": "SAME DAY GUEST", "guest_cnic": "42101-6161616-1", "room_no": "305",
         "check_in": "2024-01-10", "check_out": "2024-01-11", "hotel_name": "Pearl Continental"}]}
    b = EmsBackend(http=FakeEmsHttp({"findGuestSimple": (200, body)}))
    _, rec = _rec(b, "hotel_eye", phone="03001234567")
    by_name = {r.ref.name: r.relation for r in rec.related}
    assert by_name["ROOM MATE"] == "Room-mate at hotel"
    assert by_name["SAME DAY GUEST"] == "Co-guest (same hotel, same day)"


def test_trust_names_landlord_family_and_co_tenants():
    """The tenant register carries a household, not just a person: the landlord, the
    family living there and the other tenants each get their own named relation."""
    body = {"success": True, "data": {
        "person_details": {"name": "AHMED ALI", "cnic": CNIC_D, "mobile": "03001234567",
                           "present_address": "Flat 2, Block 13-D, Gulshan-e-Iqbal, Karachi"},
        "owner_details": {"name": "LANDLORD", "cnic": "42101-1111111-1", "mobile": "03001111111"},
        "family_members": [{"name": "WIFE", "cnic": "42101-2222222-2", "relation": "Wife"},
                           {"name": "SON", "cnic": "42101-3333333-3", "relation": "Son"}],
        "tenants": [{"name": "CO TENANT", "cnic": "42101-4444444-4"}]}}
    b = EmsBackend(http=FakeEmsHttp({"comprehensive-info": (200, body)}))
    _, rec = _rec(b, "trust", cnic=CNIC)
    by_name = {r.ref.name: r.relation for r in rec.related}
    assert by_name["LANDLORD"] == "Landlord"
    assert by_name["WIFE"] == "Family member (Wife)" and by_name["SON"] == "Family member (Son)"
    assert by_name["CO TENANT"] == "Co-tenant"


def test_milap_links_the_missing_person_to_the_reporter():
    body = {"status": "success", "data": [{
        "record_no": "M-1", "record_type": "Missing person", "status": "Open", "police_station": "Gulshan",
        "reporting_name": "AHMED ALI", "reporting_cnic": CNIC_D, "reporting_contact": "03001234567",
        "lost_person_name": "MISSING CHILD", "lost_person_cnic": "42101-8888888-8"}]}
    b = EmsBackend(http=FakeEmsHttp({"lost-records": (200, body)}))
    _, rec = _rec(b, "milap", cnic=CNIC)
    assert rec.subject.name == "AHMED ALI"
    assert any(r.relation == "Missing person" and r.ref.name == "MISSING CHILD" for r in rec.related)


def test_dls_no_records_dict_is_no_record():
    routes = {"auth/login": (200, {"token": "T"}),
              "licenseDataWithImage": (200, {"success": True, "data": {"message": "No records found"}})}
    b = EmsBackend(http=FakeEmsHttp(routes))
    assert b.lookup("dls", CNIC, None)["status"] == "no_record"


def test_empty_envelope_is_no_record_no_node():
    # HOPE employee empty, then employer envelope with only empty employees/null picture.
    routes = {
        "hope-employee-cnic-search": (200, {"message": "No records found", "data": []}),
        "hope_employer_cnic_search": (200, {"data": [{"employees": [], "picture_new": None}]}),
    }
    b = EmsBackend(http=FakeEmsHttp(routes))
    out = b.lookup("hope", CNIC, None)
    assert out["status"] == "no_record" and not out["hit"]


def test_empty_hit_creates_no_node_even_if_backend_says_hit():
    """Guard: a backend that wrongly reports hit on an empty envelope still makes no node."""
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    sub = {"0": {"number": "3001234567", "name": "AHMED ALI", "cnic": CNIC, "address": "Karachi"}}
    backend = EmsBackend(http=FakeEmsHttp({"number_check.php": (200, sub)}))
    backend._adapters["hope"] = lambda c, p: {
        "provider": "hope", "hit": True, "status": "success", "summary": "Employee record found",
        "data": {}, "raw": {"data": [{"employees": [], "picture_new": None}]}}
    s = load_settings()
    s.ollama.enabled = False
    s.llm.enabled = False
    mgr = memory_manager(s, backends={"ems": backend})
    run = mgr.get(mgr.start(GraphRunParams(cnic=CNIC, depth=1, backend="ems"), wait=True).id)
    systems = [n["data"]["system"] for n in run["graph"]["nodes"] if n["kind"] == "system"]
    assert "hope" not in systems


@pytest.mark.parametrize("system", ["cro", "arms", "psrms", "watchlist", "cfms", "nadra", "excise", "avlc"])
def test_cnic_only_systems_reject_missing_cnic(system):
    b = EmsBackend(http=FakeEmsHttp({}))
    # phone-only, no cnic: these are cnic-keyed, must not crash; status invalid/no_record/error
    out = b.lookup(system, None, "03001234567")
    assert out["status"] in ("invalid_input", "no_record", "error")


def test_dls_searched_by_cnic_too(monkeypatch):
    """DLS accepts CNIC as well as mobile; a CNIC-only subject must still be searched
    there. The adapter sends both branches when both identifiers are known."""
    seen = []

    def dls(req):
        seen.append(req.url.rsplit("/", 1)[-1])
        return 200, {"success": True, "data": {"message": "No records found"}}

    b = EmsBackend(http=FakeEmsHttp({"auth/login": (200, {"data": {"accessToken": "T"}}),
                                     "licenseData": dls}))
    b.lookup("dls", CNIC, "03001234567")
    assert any("42101" in s for s in seen) and any("0300" in s for s in seen)  # cnic and mobile both tried


def test_failures_log_records_input_and_masks_secrets(tmp_path):
    """A failed call is written to the failures file with the system, the input, the URL
    and the error - and secret keys are masked so the log can be shared."""
    from sherlocks.linkgraph.ems import EmsHttp, Req

    class Resp:
        def __init__(self, code, text):
            self.status_code, self.text = code, text

    fail = tmp_path / "fail.txt"
    http = EmsHttp(log_path=str(tmp_path / "all.txt"), failures_path=str(fail))
    http.session.request = lambda *a, **k: Resp(502, "<html>502 Bad Gateway</html>")
    http.set_context("tracs", "4210112345671")
    with pytest.raises(requests.HTTPError):
        http.send(Req("GET", "https://tracs.example/challans",
                      headers={"Authorization": "Basic verysecrettoken"}, params={"cnic": "4210112345671"}))
    text = fail.read_text()
    assert "[tracs] input=4210112345671" in text        # system + input
    assert "cnic': '4210112345671'" in text              # the input is visible
    assert "502" in text                                  # the error
    assert "verysecrettoken" not in text                 # the secret is masked


def test_failures_log_skips_successful_calls(tmp_path):
    from sherlocks.linkgraph.ems import EmsHttp, Req

    class Resp:
        status_code = 200
        text = '{"success": true, "data": [1]}'

    fail = tmp_path / "fail.txt"
    http = EmsHttp(log_path=str(tmp_path / "all.txt"), failures_path=str(fail))
    http.session.request = lambda *a, **k: Resp()
    http.set_context("cro", CNIC)
    http.send(Req("GET", "https://cro.example/x", params={"cnic": CNIC}))
    assert not fail.exists() or fail.read_text() == ""     # a success is not a failure


def _complaint(**over):
    row = {"id": 1, "tracking_id": "130226-00000001", "created_at": "2026-02-13T10:58:11.000000Z", "subject": "others",
           "other_subject": "Request for protection", "complainant_name": "AHMED ALI", "complainant_fathername": "ALI KHAN",
           "complainant_address": "House 1, Malir", "complainant_cnic": CNIC_D, "complainant_phone": "0300-1234567",
           "complainant_cell": "0300-1234567", "district_name": "Malir", "complaint_category": "safety life threats",
           "status": "In Process", "complaint_against": [], "cnic_role": "complainant"}
    return {**row, **over}


def test_igp_cms_reads_complaints_filed_and_against_by_cnic_and_phone():
    seen = []
    filed = _complaint(complaint_against=[{"name": "RASHID KHAN", "cnic": "42101-7777777-7", "phone": "0311-2223334"}])
    against = _complaint(id=2, tracking_id="070725-00000002", complainant_name="WAQAR AHMED", complainant_fathername="X",
                         complainant_cnic="42101-5555555-5", complainant_phone="0322-1112223", complainant_cell=None,
                         status="Resolved", cnic_role="complain_against",
                         complaint_against=[{"name": "AHMED ALI", "cnic": CNIC_D}])

    def igp(req):
        seen.append(req.params)
        body = {"success": True, "summary": {"total_records": 2},
                "data": {"all_complaints": [filed, against], "as_complainant": [filed], "as_complain_against": [against]}}
        return 200, body

    b = EmsBackend(http=FakeEmsHttp({"search-complaints-by-cnic": igp}))
    result, rec = _rec(b, "igp_cms", cnic=CNIC, phone="03001234567")
    assert seen == [{"cnic": CNIC}, {"phone": "03001234567"}]                 # same endpoint, the parameter changes
    assert result["hit"] and result["summary"] == "2 IGP CMS complaint(s): 1 filed, 1 against"
    assert rec.subject.name == "AHMED ALI" and rec.subject.father_name == "ALI KHAN"
    labels = {f.label for f in rec.fields}
    assert {"IGP complaint filed", "IGP complaint against the subject"} <= labels
    relations = {(r.ref.name, r.relation) for r in rec.related}
    assert ("RASHID KHAN", "Complained against by the subject") in relations
    assert ("WAQAR AHMED", "Filed a complaint against the subject") in relations


def test_igp_cms_older_endpoint_still_read(monkeypatch):
    from sherlocks.linkgraph import ems

    monkeypatch.setitem(ems.CONF, "igp_url", "https://ems.test/api/complaint-details")
    b = EmsBackend(http=FakeEmsHttp({"complaint-details": (200, {"success": True, "complaints": [_complaint()]})}))
    result, rec = _rec(b, "igp_cms", cnic=CNIC)
    assert result["hit"] and rec.subject.name == "AHMED ALI"


def test_igp_cms_no_record_and_failure_are_told_apart():
    none = {"success": False, "message": "No records found for the provided cnic", "search_type": "cnic",
            "data": {"total_records": 0, "as_complainant": [], "as_complain_against": [], "all_complaints": []}}
    b = EmsBackend(http=FakeEmsHttp({"search-complaints-by-cnic": (200, none)}))
    assert b.lookup("igp_cms", CNIC, None)["status"] == "no_record"
    bad = EmsBackend(http=FakeEmsHttp({"search-complaints-by-cnic": (401, {"success": False, "message": "Invalid API key"})}))
    out = bad.lookup("igp_cms", CNIC, None)
    assert out["status"] == "error" and "Invalid API key" in out["summary"]
