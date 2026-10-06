"""Hotel Eye person API (/api/person): one call by phone and CNIC; profiles with stays,
companions and CRO links. Synthetic data in the real response shape."""

from __future__ import annotations

from sherlocks.linkgraph import ems
from sherlocks.linkgraph.ems import EmsBackend
from sherlocks.linkgraph.extractors import extract_record
from sherlocks.linkgraph.images import MemoryImageStore
from sherlocks.linkgraph.models import PersonRef
from sherlocks.linkgraph.relations import describe

ME, PHONE = "9999900000011", "03990000101"


def _profile(cnic, phone, name, stays, with_=(), links=()):
    return {"person": {"name": name, "father_name": "Test Father", "gender": "Male", "date_of_birth": "1994-08-31",
                       "cnic": cnic, "passport": "", "phone": phone, "email": "", "nationality": "",
                       "permanent_address": "Village X, Khairpur", "temporary_address": "House B-140, Malir, Karachi"},
            "hotel_eye_stays": stays, "stay_with_persons": list(with_), "criminal_links": list(links)}


STAY = {"source_system": "Hotel Eye", "stay_id": 1, "role": "primary_guest", "guest_name": "Kamran", "hotel": "Embassy Inn",
        "district": "EAST", "police_station": "Tipu Sultan", "room_no": "12", "check_in": "2023-07-28 12:31:00",
        "check_out": "2023-08-03 09:16:00", "visit_purpose": "business"}


class _Http:
    timeout = 5

    def __init__(self, body):
        self.body, self.asked = body, []

    def send(self, req):
        self.asked.append((req.url, req.json, dict(req.headers)))
        return 200, self.body


def test_person_api_reads_stays_companions_and_other_profiles(monkeypatch):
    monkeypatch.setitem(ems.CONF, "hotel_person_url", "https://hotel.test/api/person")
    monkeypatch.setitem(ems.CONF, "hotel_person_key", "k")
    body = {"status": "success", "found": True, "profiles": [
        _profile("99999-0000001-1", PHONE, "Kamran Ahmed", [STAY],
                 with_=[{"name": "Sajid Mehmood", "cnic": "99999-0000002-9", "phone": "03990000201",
                         "hotel": "Embassy Inn", "room_no": "12", "check_in": "2023-07-28 12:31:00"}],
                 links=[{"cro_no": 777, "records": [{"fir_no": "45/2023", "fir_offence": "395 PPC", "police_station": "Gulshan"}]}]),
        _profile("99999-0000099-9", PHONE, "Other Person", [dict(STAY, hotel="SK Inn", check_in="2023-01-24 14:10:00")])]}
    http = _Http(body)
    out = EmsBackend(http=http).lookup("hotel_eye", ME, PHONE)
    assert out["hit"] and "1 person(s) stayed with them" in out["summary"]
    url, sent, headers = http.asked[0]
    assert sent == {"phone": PHONE, "cnic": "99999-0000001-1", "passport": "", "email": ""} and headers["X-API-KEY"] == "k"

    rec = extract_record("hotel_eye", out, PersonRef(cnic=ME, phones=[PHONE]), MemoryImageStore())
    assert [s.hotel for s in rec.stays] == ["Embassy Inn"]
    assert {(r.ref.name, r.relation) for r in rec.related} == {
        ("Sajid Mehmood", "Shared hotel stay"), ("Other Person", "Used same phone/CNIC at hotel check-in")}
    assert [(f.fir_no, f.fir_year) for f in rec.firs] == [("45", "2023")] and "criminal_record" in rec.flags
    assert any(f.label == "CRO No. (via Hotel Eye)" for f in rec.fields)


def test_edges_read_shared_hotel_with():
    names = {"a": "Sajid", "b": "Kamran"}
    for label in ("Shared hotel stay", "Room-mate at hotel"):
        assert describe(label, named="a", owner="b").sentence(names) == "Sajid shared hotel with Kamran"


def test_falls_back_to_the_older_endpoints_when_the_person_api_fails(monkeypatch):
    monkeypatch.setitem(ems.CONF, "hotel_person_url", "https://hotel.test/api/person")

    class _Mixed(_Http):
        def send(self, req):
            self.asked.append((req.url, req.json, {}))
            if req.url.endswith("/api/person"):
                return 502, {"text_response": "Bad gateway"}
            return 200, {"status": "success", "data": [{"guest_name": "Kamran Ahmed", "guest_cnic": "99999-0000001-1",
                                                         "hotel_name": "Old Inn", "check_in": "2023-01-01 10:00:00"}]}

    http = _Mixed(None)
    out = EmsBackend(http=http).lookup("hotel_eye", ME, None)
    assert out["hit"] and len(http.asked) == 2 and out["raw"]["records"][0]["hotel_name"] == "Old Inn"
