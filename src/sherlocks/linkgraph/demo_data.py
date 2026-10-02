"""A synthetic world for the demo backend.

Every person, CNIC and number here is invented. CNICs start ``99999`` and mobiles
``0399`` - neither prefix is issued - so nothing can be mistaken for, or collide with,
a real citizen.

:class:`DemoHttp` stands in for cdr_report_app's ``HttpClient`` and answers each
adapter in that system's real wire format (the SIMs database's numeric keys, the
subscriber ``Data.SubscriberList``, PSRMS form posts, the FIR report as HTML, DLS's
login-then-fetch...). The real adapters parse those answers, so the demo exercises
exactly the code path a live run does.

The network is built to show every link type once:

* SIM 0399-0000102 used by Kamran is registered to his brother Imran   -> strong
* Kamran rents from Tariq; Tariq's other tenant is Asif                  -> strong x2
* Kamran and Sajid are co-accused in FIR 45/2023; ASI Zahid investigated -> strong
* Kamran drove Farhan's car (challan); Rashid drives it too              -> strong
* Kamran and Bilal stayed in the same hotel on overlapping nights        -> weak
* Kamran and Imran share a father's name and (spelled differently) address -> weak
* Farhan, Rashid and Kamran all list Qureshi Motors as employer          -> weak
"""

from __future__ import annotations

import base64
import hashlib
import io
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sherlocks.linkgraph.normalize import cnic13, dashed_cnic, mobile11


@dataclass
class DemoPerson:
    key: str
    name: str
    father: str
    cnic: str
    phones: list[str]
    address: str
    district: str = "Karachi"
    extra: dict[str, Any] = field(default_factory=dict)


PEOPLE: dict[str, DemoPerson] = {p.key: p for p in [
    DemoPerson("kamran", "Kamran Ahmed", "Nadeem Ahmed", "9999900000011", ["03990000101", "03990000102"],
               "House 12, Street 4, Block 13-D, Gulshan-e-Iqbal, Karachi"),
    DemoPerson("sajid", "Sajid Mehmood", "Rafiq Mehmood", "9999900000029", ["03990000201"],
               "House 7, Street 9, Block 2, PECHS, Karachi"),
    DemoPerson("tariq", "Tariq Hussain", "Ghulam Hussain", "9999900000037", ["03990000301"],
               "Bungalow 45, Block 13-D, Gulshan-e-Iqbal, Karachi"),
    DemoPerson("farhan", "Farhan Qureshi", "Anwar Qureshi", "9999900000045", ["03990000401"],
               "Plot 3, Sector 16, Korangi Industrial Area, Karachi"),
    DemoPerson("bilal", "Bilal Khan", "Sher Khan", "9999900000053", ["03990000501"],
               "Unit 6, Latifabad, Hyderabad", district="Hyderabad"),
    DemoPerson("imran", "Imran Ahmed", "Nadeem Ahmed", "9999900000061", ["03990000601"],
               "H# 12 St. 4 Blk 13D Gulshan Iqbal Khi"),
    DemoPerson("asif", "Asif Ali", "Liaquat Ali", "9999900000079", ["03990000701"],
               "Flat 5, Bungalow 45, Block 13-D, Gulshan-e-Iqbal, Karachi"),
    DemoPerson("zahid", "Zahid Iqbal", "Muhammad Iqbal", "9999900000087", ["03990000801"],
               "Police Lines, Garden, Karachi"),
    DemoPerson("naveed", "Naveed Akhtar", "Akhtar Hussain", "9999900000095", ["03990000901"],
               "House 22, Block 6, PECHS, Karachi"),
    DemoPerson("rashid", "Rashid Memon", "Abdul Memon", "9999900000103", ["03990001001"],
               "Goth Ali Nawaz, Malir, Karachi"),
    DemoPerson("waqas", "Waqas Javed", "Javed Iqbal", "9999900000111", ["03990001101"],
               "Block 7, Gulshan-e-Iqbal, Karachi"),
]}

# Which CNIC each SIM is registered to. 0102 is used by Kamran but registered to Imran.
SIM_OWNER: dict[str, str] = {"03990000102": "imran"}
for _person in PEOPLE.values():
    for _phone in _person.phones:
        SIM_OWNER.setdefault(_phone, _person.key)

FIRS = {
    ("45", "2023", "501"): {
        "ps": "PS Gulshan-e-Iqbal", "offence": "395/34 PPC", "status": "Under trial",
        "accused": ["kamran", "sajid"], "witnesses": [("Shahid", "Khalid Mehmood")],
        "io": ["zahid"], "complainant": "waqas",
    },
    ("112", "2024", "502"): {
        "ps": "PS Ferozabad", "offence": "489-F PPC", "status": "Challan submitted",
        "accused": ["kamran"], "witnesses": ["bilal"], "io": ["zahid"], "complainant": None,
    },
}


def _avatar_png(seed: str) -> str:
    """A generated head-and-shoulders silhouette, standing in for an ID photo."""
    from PIL import Image, ImageDraw

    digest = hashlib.sha256(seed.encode()).digest()
    background = (90 + digest[0] % 100, 100 + digest[1] % 90, 120 + digest[2] % 90)
    image = Image.new("RGB", (160, 200), background)
    draw = ImageDraw.Draw(image)
    tone = (225, 190 - digest[3] % 40, 160 - digest[4] % 50)
    draw.ellipse((50, 30, 110, 100), fill=tone)
    draw.pieslice((15, 105, 145, 260), 180, 360, fill=(40 + digest[5] % 60, 50, 70 + digest[6] % 60))
    draw.rectangle((0, 180, 160, 200), fill=(20, 20, 30))
    draw.text((52, 184), "DEMO", fill=(255, 255, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _p(key: str) -> DemoPerson:
    return PEOPLE[key]


class DemoWorld:
    """Answers each upstream endpoint from :data:`PEOPLE`."""

    def by_cnic(self, cnic: str | None) -> DemoPerson | None:
        return next((p for p in PEOPLE.values() if p.cnic == cnic), None) if cnic else None

    def by_phone(self, phone: str | None) -> DemoPerson | None:
        return next((p for p in PEOPLE.values() if phone in p.phones), None) if phone else None

    def find(self, ids: dict[str, list[str]]) -> DemoPerson | None:
        for cnic in ids["cnic"]:
            if person := self.by_cnic(cnic):
                return person
        for phone in ids["phone"]:
            if person := self.by_phone(phone):
                return person
        return None

    # -- identity -------------------------------------------------------------------

    def h_simsdb(self, rest, ids, kw):
        phone = ids["phone"][0] if ids["phone"] else None
        owner = PEOPLE.get(SIM_OWNER.get(phone or "", ""))
        body: dict[str, Any] = {"allNumbers": {}, "notification": {"msg1": "demo"}}
        if not owner:
            return body
        sims = [s for s, k in SIM_OWNER.items() if k == owner.key]
        for index, sim in enumerate(sims):
            body["allNumbers"][f"number{index + 1}"] = sim[1:]
            body[str(index * 2)] = {"number": sim[1:], "name": owner.name.upper(), "cnic": owner.cnic, "address": owner.address.upper()}
            body[str(index * 2 + 1)] = {"number": sim[1:], "name": "DATA NOT RECIEVED FROM NADRA", "cnic": owner.cnic, "address": "no"}
        return body

    def h_subscriber(self, rest, ids, kw):
        rows = []
        if rest and rest[0] == "mobile" and ids["phone"]:
            owner = PEOPLE.get(SIM_OWNER.get(ids["phone"][0], ""))
            sims = [ids["phone"][0]] if owner else []
        else:
            owner = self.by_cnic(ids["cnic"][0] if ids["cnic"] else None)
            sims = [s for s, k in SIM_OWNER.items() if owner and k == owner.key]
        for sim in sims:
            rows.append({"Name": owner.name.upper(), "Cnic": owner.cnic, "Phone": sim,
                         "ActivationDate": "2018-06-14", "Address": owner.address})
        return {"Data": {"SubscriberList": rows}}

    # -- criminal -------------------------------------------------------------------

    def h_cro(self, rest, ids, kw):
        person = self.find(ids)
        if not person or person.key not in {"kamran", "sajid"}:
            return {"status": False, "message": "No CRO record found against CNIC"}
        firs = [
            {"fir_no": no, "fir_year": year, "ps_desc": fir["ps"], "fir_offence": fir["offence"], "status_desc": fir["status"]}
            for (no, year, _), fir in FIRS.items() if person.key in fir["accused"]
        ]
        return {"data": [{"cro_no": f"D-{1000 + list(PEOPLE).index(person.key)}", "cro_full_name": person.name.upper(),
                          "cro_father_name": person.father.upper(), "cro_age": 31, "category_desc": "Accused",
                          "record_district": "Karachi East", "FIRList": firs}]}

    def h_psrms(self, rest, ids, kw):
        if rest and rest[0] == "fir":
            return self._fir_html(kw.get("data") or {})
        person = self.find(ids)
        rows = []
        for (no, year, ps_id), fir in FIRS.items():
            roles = []
            if person and person.key in fir["accused"]:
                roles.append("WIT")  # cdr_report_app reads WIT as accused/suspect
            if person and person.key in fir["witnesses"]:
                roles.append("SUS")
            if person and person.key == fir["complainant"]:
                roles.append("FIR")
            for role in roles:
                rows.append({"fir_id": f"{no}{year}", "fir_no": no, "fir_year": year, "ps_tbl_id": ps_id,
                             "fir_status": fir["status"], "person_id": person.key, "person_name": person.name,
                             "person_father": person.father, "person_cnic": dashed_cnic(person.cnic),
                             "person_phone": person.phones[0], "person_address": person.address, "person_type": role})
        return {"status": True, "data": rows, "pagination": {"current_page": 1, "total_records": len(rows)}}

    def _fir_html(self, form: dict) -> dict:
        fir = FIRS.get((str(form.get("fir_no")), str(form.get("fir_year")), str(form.get("ps_id"))))
        if not fir:
            return {"status_code": 200, "content_type": "text/html", "text_response": "<html>FIR not found</html>"}

        def row(entry) -> str:
            if isinstance(entry, tuple):
                return f"<tr><td>{entry[0]}</td><td>{entry[1]}</td><td></td><td></td><td></td></tr>"
            p = _p(entry)
            return (f"<tr><td>{p.name}</td><td>{p.father}</td><td>{dashed_cnic(p.cnic)}</td>"
                    f"<td>{p.phones[0]}</td><td>{p.address}</td></tr>")

        def table(heading: str, entries) -> str:
            header = "<tr class='innerTr'><th>نام</th><th>ولدیت</th><th>شناختی کارڈ</th><th>موبائل</th><th>پتہ</th></tr>"
            return (f"<table><tr class='trHeading'><td>{heading}</td></tr></table>"
                    f"<table>{header}{''.join(row(e) for e in entries)}</table>")

        complainant = ""
        if fir["complainant"]:
            c = _p(fir["complainant"])
            complainant = (f"<table id='PrintableFirTbl'><tr><td>مستغیث:</td><td>{c.name} s/o {c.father} "
                           f"CNIC {dashed_cnic(c.cnic)}</td></tr></table>")
        html = (f"<html><head></head><body><div id='inboxContent'><h3>FIR {form.get('fir_no')}/{form.get('fir_year')} "
                f"{fir['ps']}</h3>{complainant}{table('نامزد ملزمان', fir['accused'])}"
                f"{table('گواہان', fir['witnesses'])}{table('تفتیشی افسران', fir['io'])}</div></body></html>")
        return {"status_code": 200, "content_type": "text/html; charset=utf-8", "text_response": html}

    def h_watchlist(self, rest, ids, kw):
        person = self.find(ids)
        if person and person.key == "sajid":
            return {"status": True, "message": "Record found", "data": [
                {"name": person.name, "cnic": person.cnic, "mobile": person.phones[0], "category": "Street crime",
                 "added_by": "CIA Karachi East", "remarks": "Active member of a snatching group"}]}
        return {"status": True, "message": "No record found in watchlist", "data": []}

    # -- property -------------------------------------------------------------------

    def h_prvs(self, rest, ids, kw):
        person = self.find(ids)
        if person and person.key == "sajid":
            landlord = _p("naveed")
            return {"data": [{"id": "PRVS-88121", "name": person.name, "police_station": "PS Ferozabad",
                              "remarks": "Tenant verified", "owner_name": landlord.name, "owner_cnic": landlord.cnic,
                              "owner_mobile": landlord.phones[0], "tenant_address": person.address}]}
        return {"data": []}

    def h_old_tenant(self, rest, ids, kw):
        tenancies = [("kamran", "tariq", "12", "Street 4", "Block 13-D, Gulshan-e-Iqbal, Karachi"),
                     ("asif", "tariq", "Flat 5, Bungalow 45", "Block 13-D", "Gulshan-e-Iqbal, Karachi")]
        person = self.find(ids)
        rows = []
        for index, (tenant_key, owner_key, house, street, address) in enumerate(tenancies):
            if not person or person.key not in (tenant_key, owner_key):
                continue
            tenant, owner = _p(tenant_key), _p(owner_key)
            rows.append({"tenant_id": 7000 + index, "tenant_name": tenant.name, "tenant_cnic": dashed_cnic(tenant.cnic),
                         "tenant_mobile": tenant.phones[0], "owner_mobile_number": owner.phones[0],
                         "owner_cnic": dashed_cnic(owner.cnic),
                         "raw": {"owner_name": owner.name, "owner_mobile_number": owner.phones[0],
                                 "owner_cnic": dashed_cnic(owner.cnic), "tenant_mobile_number": tenant.phones[0],
                                 "property_house_no": house, "property_street_mohalla": street,
                                 "property_address": address}})
        return {"success": True, "message": "Records fetched" if rows else "No records", "data": rows}

    def h_trust(self, rest, ids, kw):
        person = self.find(ids)
        if person and person.key == "naveed":
            tenant = _p("sajid")
            return {"data": {"user_details": {"name": person.name, "cnic": dashed_cnic(person.cnic), "mobile": person.phones[0]},
                             "properties_owned": [{"property_address": tenant.address, "tenant_name": tenant.name,
                                                   "tenant_cnic": dashed_cnic(tenant.cnic), "tenant_mobile": tenant.phones[0],
                                                   "agreement_date": "2024-01-01"}],
                             "tenacies": []}}
        return {"data": {}}

    # -- travel, employment, police, traffic ----------------------------------------

    def h_hotel_eye(self, rest, ids, kw):
        stays = {"kamran": ("4", "2025-03-10 22:10:00", "2025-03-12 09:00:00", "Business"),
                 "bilal": ("5", "2025-03-11 20:30:00", "2025-03-12 10:15:00", "Ziarat")}
        person = self.find(ids)
        if not person or person.key not in stays:
            return {"status": "false", "data": "No record found"}
        room, check_in, check_out, purpose = stays[person.key]
        return {"data": [{"guest_cell": person.phones[0], "guest_name": person.name, "guest_cnic": dashed_cnic(person.cnic),
                          "room_no": room, "check_in": check_in, "check_out": check_out, "visit_purpose": purpose,
                          "hotel_name": "Demo Rest House", "hotel_district": "Hyderabad", "ps_name": "PS Market",
                          "guest_permanent_district": person.district, "guest_temporary_district": "Hyderabad"}]}

    def h_sbvs(self, rest, ids, kw):
        person = self.find(ids)
        if not person or person.key != "rashid":
            return {"success": False, "message": "No record found"}
        employer = _p("farhan")
        return {"success": True, "data": {
            "details": {"id": 5511, "full_name": person.name, "father_name": person.father, "cnic": dashed_cnic(person.cnic),
                        "mobile": person.phones[0], "address": person.address, "org_name": "Qureshi Motors",
                        "profession": "Driver", "purpose_name": "Employee verification", "district_name": "Korangi",
                        "police_station_name": "PS Korangi Industrial Area", "status_name": "Verified",
                        "employer_name": employer.name, "employer_cnic": dashed_cnic(employer.cnic),
                        "employer_mobile": employer.phones[0]},
            "image": {"photo": _avatar_png(person.key)}}}

    def _employment(self, ids, registry: dict[str, tuple[str, str]]):
        person = self.find(ids)
        if not person or person.key not in registry:
            return {"status": False, "message": "No record found", "data": []}
        company, designation = registry[person.key]
        # Kamran gives his second number - the SIM registered to his brother - as an
        # alternate contact, which is how that SIM enters the graph.
        other = person.phones[1] if len(person.phones) > 1 else ""
        return {"status": True, "message": "Record found", "data": [{
            "name": person.name, "father_name": person.father, "cnic": person.cnic, "contact": person.phones[0],
            "other_contact": other, "perm_address": person.address, "designation": designation,
            "company_name": company, "last_verified_at": "2025-01-20"}]}

    def h_evs(self, rest, ids, kw):
        return self._employment(ids, {"kamran": ("Qureshi Motors", "Sales Officer"), "farhan": ("Qureshi Motors", "Proprietor")})

    def h_hope(self, rest, ids, kw):
        return self._employment(ids, {"asif": ("Gulshan Traders", "Accountant"), "imran": ("Gulshan Traders", "Clerk")})

    def h_hrmis(self, rest, ids, kw):
        person = self.find(ids)
        if not person or person.key != "zahid":
            return {"status": False, "message": "No Data", "output": None}
        return {"output": [{"ofc_name": person.name, "ofc_mobile": person.phones[0], "ofc_cnic": dashed_cnic(person.cnic),
                            "ofc_belt_no": "KE-4471", "ofc_dateofbirth": "1984-02-11", "current_posting_new": "Investigation",
                            "ofc_address": person.address, "rnk_name": "ASI", "ps_name_eng": "PS Gulshan-e-Iqbal",
                            "dst_name": "Karachi East", "police_station_arrival_date": "2022-07-01"}]}

    def h_dls(self, rest, ids, kw):
        if rest and rest[0] == "login":
            return {"success": True, "data": {"accessToken": "demo-token"}}
        person = self.find(ids)
        if not person or person.key not in {"kamran", "farhan", "rashid"}:
            return {"success": True, "data": {"message": "No record found"}, "meta": {"includesImage": True}}
        first, _, last = person.name.partition(" ")
        return {"success": True, "data": [{
            "firstname": first, "lastname": last, "fathername": person.father, "mobile": person.phones[0],
            "cnic": dashed_cnic(person.cnic), "address": person.address, "license_no": f"KHI-{person.cnic[-6:]}",
            "license_category": "LTV" if person.key != "rashid" else "HTV", "license_type": "Permanent",
            "issued_date": "2019-05-02", "expiry_date": "2029-05-01", "issued_office": "DLS Karachi",
            "status": "Valid", "applicant_image": _avatar_png(person.key)}]}

    def h_tracs(self, rest, ids, kw):
        person = self.find(ids)
        owner, driver_for_owner = _p("farhan"), _p("rashid")
        challans = []
        if person and person.key == "kamran":
            challans.append({"challanNumber": "TR-2025-00931", "vehicleNumPlate": "BKX-419", "ownerName": owner.name,
                             "ownerCNIC": owner.cnic, "violation": "Signal violation", "approved_at": "2025-02-02"})
        if person and person.key == "farhan":
            challans.append({"challanNumber": "TR-2025-01470", "vehicleNumPlate": "BKX-419", "ownerName": owner.name,
                             "ownerCNIC": owner.cnic, "driver_name": driver_for_owner.name,
                             "driver_cnic": driver_for_owner.cnic, "violation": "Over speeding", "approved_at": "2025-04-18"})
        return {"status": "success", "message": "Challans fetched", "data": {"challans": challans, "oldChallans": []}}

    def h_milap(self, rest, ids, kw):
        person = self.find(ids)
        if person and person.key == "asif":
            return {"data": [{"report_no": "MLP-3321", "reporting_name": person.name, "reporting_cnic": dashed_cnic(person.cnic),
                              "reporting_contact": person.phones[0], "lost_item": "CNIC card", "report_date": "2024-11-03"}]}
        return {"data": []}

    def h_cfms(self, rest, ids, kw):
        return {"status": False, "data": []}

    def h_pfc(self, rest, ids, kw):
        return {"data": []}

    def h_igp_cms(self, rest, ids, kw):
        return {"data": []}

    def h_callerid(self, rest, ids, kw):
        tags = {"kamran": ["Kamran Gulshan", "Kami Bhai"], "farhan": ["Farhan Qureshi Motors"]}
        person = self.by_phone(ids["phone"][0] if ids["phone"] else None)
        accounts = [{"name": t, "suspicious_spam": False, "type": "person"} for t in tags.get(person.key if person else "", [])]
        return {"result": {"accounts": accounts, "facebook": {"FB_ID": "NOT FOUND", "FB_Link": "NOT FOUND"}}}


def _collect_identifiers(parts: list[str], kwargs: dict[str, Any]) -> dict[str, list[str]]:
    values: list[str] = list(parts)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item)
        elif value is not None:
            values.append(str(value))

    for key in ("params", "data", "json"):
        walk(kwargs.get(key))
    out: dict[str, list[str]] = {"cnic": [], "phone": []}
    for value in values:
        if (cnic := cnic13(value)) and cnic not in out["cnic"]:
            out["cnic"].append(cnic)
        elif (phone := mobile11(value)) and phone not in out["phone"]:
            out["phone"].append(phone)
    return out


class DemoHttp:
    """Drop-in for ``cdr_report_app.integrations.http.HttpClient``."""

    timeout = 5

    def __init__(self, world: DemoWorld | None = None) -> None:
        self.world = world or DemoWorld()
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str | None, **kwargs: Any) -> dict[str, Any] | list[Any]:
        if not url:
            return {"error": "URL not configured"}
        parts = urlsplit(url).path.strip("/").split("/")
        system, rest = parts[0], parts[1:]
        if system == "api":  # caller id: /api/v1/key/<key>/search/single
            system = "callerid"
        self.calls.append((method, url))
        handler = getattr(self.world, f"h_{system}", None)
        if handler is None:
            return {"error": f"Unknown demo endpoint {url}", "status_code": 404}
        return handler(rest, _collect_identifiers(rest, kwargs), kwargs)

    def request_bytes(self, method: str, url: str | None, **kwargs: Any) -> tuple[dict[str, Any], bytes | None]:
        return {"error": "Not available in demo"}, None

    def close(self) -> None:
        pass


DEMO_BASE = "http://demo.sherlocks.local"


def demo_cdr_settings() -> Any:
    from cdr_report_app.settings import CallerIdSettings, ProviderConfig, ProviderSettings
    from cdr_report_app.settings import Settings as CdrSettings

    b, k = DEMO_BASE, "demo"
    providers = ProviderSettings(
        simsdb=ProviderConfig(base_url=f"{b}/simsdb", api_key=k),
        subscriber=ProviderConfig(api_key=k, extra={"cookie": k, "cnic_url": f"{b}/subscriber/cnic", "mobile_url": f"{b}/subscriber/mobile"}),
        prvs=ProviderConfig(api_key=k, extra={"cnic_url": f"{b}/prvs/cnic", "mobile_url": f"{b}/prvs/mobile"}),
        cro=ProviderConfig(base_url=f"{b}/cro", api_key=k),
        psrms=ProviderConfig(base_url=f"{b}/psrms/personsearch", api_key=k,
                             extra={"personsearch_cookie": k, "fir_report_url": f"{b}/psrms/fir", "fir_cookie": k}),
        watchlist=ProviderConfig(base_url=f"{b}/watchlist", api_key=k),
        nearest_ps=ProviderConfig(enabled=False),
        nadra=ProviderConfig(enabled=False),
        cfms=ProviderConfig(base_url=f"{b}/cfms"),
        hotel_eye=ProviderConfig(api_key=k, extra={"guest_url": f"{b}/hotel_eye/guest", "simple_url": f"{b}/hotel_eye/simple"}),
        sbvs=ProviderConfig(api_key=k, extra={"cnic_url": f"{b}/sbvs/cnic", "mobile_url": f"{b}/sbvs/mobile"}),
        pfc=ProviderConfig(base_url=f"{b}/pfc", api_key=k),
        hrmis=ProviderConfig(base_url=f"{b}/hrmis", api_key=k, extra={"auth_token": k}),
        igp_cms=ProviderConfig(base_url=f"{b}/igp_cms", api_key=k),
        imei=ProviderConfig(enabled=False),
        evs=ProviderConfig(base_url=f"{b}/evs", extra={"i_key": k, "j_key": k, "api_token": k}),
        hope=ProviderConfig(extra={"employee_url": f"{b}/hope", "i_key": k, "j_key": k, "api_token": k}),
        dls=ProviderConfig(base_url=f"{b}/dls/data", extra={"login_url": f"{b}/dls/login", "username": k, "password": k, "seed_token": k}),
        tracs=ProviderConfig(base_url=f"{b}/tracs", extra={"auth_token": k}),
        old_tenant=ProviderConfig(base_url=f"{b}/old_tenant", api_key=k),
        trust=ProviderConfig(base_url=f"{b}/trust", api_key=k),
        milap=ProviderConfig(base_url=f"{b}/milap", api_key=k),
    )
    return CdrSettings(
        providers=providers,
        caller_id=CallerIdSettings(enabled=True, base_url=b, api_keys=["demo"], delay_seconds=0),
    )


def build_demo_backend() -> Any:
    from sherlocks.linkgraph.backends import ReportAppBackend

    return ReportAppBackend(demo_cdr_settings(), name="demo", http=DemoHttp())


DEMO_SEEDS = [
    {"label": "Kamran Ahmed (accused, 2 FIRs)", "cnic": "99999-0000001-1", "phone": "0399-0000101"},
    {"label": "Tariq Hussain (landlord)", "cnic": "99999-0000003-7", "phone": None},
    {"label": "Farhan Qureshi (vehicle owner)", "cnic": None, "phone": "0399-0000401"},
]
