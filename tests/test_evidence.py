"""Case evidence: FIR file reports, lab reports and CRO dossiers read alongside a run;
quoted facts; people found in documents; the chat's document tools; the case report.

All synthetic: the FIR page is tests/fixtures/psrms_fir_report.html (real markup,
invented people), PDFs are generated here."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest

from sherlocks.evidence.case_file import CaseFile
from sherlocks.evidence.demo import DemoEvidence, _avatar_png, _pdf
from sherlocks.evidence.fir_document import parse_fir_html, roster
from sherlocks.evidence.readers import ai_read, fir_facts, link_people
from sherlocks.linkgraph.ems import EmsBackend

FIXTURE = (Path(__file__).parent / "fixtures" / "psrms_fir_report.html").read_text(encoding="utf-8")
SUBJECT = "9999900002022"          # فرضی ساجد, nominated accused in the fixture FIR


def _lab_pdf() -> bytes:
    return _pdf(["DNA ANALYSIS REPORT - Lab No. LAB-1 (TEST)", "Reference: FIR 321/2025",
                 "Result: The DNA profile from Item 2 matches the reference sample of the accused (CNIC 99999-0000202-2)."])


def _cro_pdf() -> bytes:
    return _pdf(["CRO DOSSIER - CRO No. 777 (TEST)", "Name: test accused"], [base64.b64decode(_avatar_png("x"))])


class _Http:
    """The EMS endpoints this flow touches; everything else has no record."""

    timeout = 5

    def __init__(self) -> None:
        self.asked: list[tuple[str, Any]] = []
        self.downloads: list[str] = []

    def send(self, req):
        path = req.url.split("ems.test", 1)[-1]
        self.asked.append((path, req.data or req.json or req.params))
        if path.endswith("/personsearch") and (req.data or {}).get("cnic") in ("99999-0000202-2", SUBJECT):
            return 200, {"status": True, "data": [{
                "fir_no": "321", "fir_year": "25", "ps_tbl_id": "544", "ps_name": "Gulshan-e-Iqbal",
                "fir_status": "Challan", "person_type": "WIT", "person_name": "Test Accused",
                "person_cnic": "99999-0000202-2", "person_phone": "03990000202", "person_address": "Lyari"}]}
        if path.endswith("/firfilereport"):
            return 200, {"text_response": FIXTURE}
        if path.endswith("/show-reports"):
            return 200, {"success": True, "data": {"DNA": [{
                "ps_id": 544, "fir_no": 321, "fir_year": 25, "lab_token": "LAB-1", "unit_name": "DNA Lab Test",
                "case_received_at": "10-07-2025 11:00:00", "report_links": ["https://labs.test/r1.pdf"]}],
                "CHEMICAL": [], "FORENSIC": [], "MEDICOLEGAL": []}}
        if path.endswith("/GetDataByCnic"):
            return 200, {"data": [{"cro_no": "777", "cro_full_name": "TEST ACCUSED", "FIRList": []}]}
        if path.endswith("/cro-report-pdf"):
            return 200, {"status": True, "data": base64.b64encode(_cro_pdf()).decode()}
        return 404, {"status": False, "message": "No record found"}

    def fetch_bytes(self, url: str) -> bytes:
        self.downloads.append(url)
        return _lab_pdf()


# -- the FIR file report -----------------------------------------------------------------


def test_fir_file_report_is_read_by_label():
    doc = parse_fir_html(FIXTURE, ps_id="544")
    assert (doc["fir_no"], doc["fir_year"], doc["serial"]) == ("321", "25", "7001")
    assert doc["sections"] == "392 ت پ, 34 ت پ"
    c = doc["complainant"]
    assert (c["name"], c["father"], c["cnic"], c["phone"], c["occupation"]) == (
        "فرضی احمد", "فرضی رشید", "9999900001011", "03990000111", "دکاندار")
    assert doc["registered_by"] == {"phone": "03990000999", "rank": "سب انسپکٹر", "belt": "K-0001", "name": "فرضی افسر"}
    assert [p["cnic"] for p in doc["nominated_suspects"]] == ["9999900002022"]
    assert [p["name"] for p in doc["witnesses"]] == ["فرضی احمد", "فرضی گواہ"]
    assert len(doc["case_diaries"]) == 2 and "03990000222" in doc["case_diaries"][0]["remarks"]
    assert doc["investigation_result"].startswith("ملزم فرضی ساجد")
    assert set(roster(doc)) == {"complainant", "nominated_suspects", "witnesses", "investigating_officers"}
    assert "Case diary 01 (05-07-2025" in doc["text"]


def test_facts_must_quote_the_document():
    doc = parse_fir_html(FIXTURE, ps_id="544")
    case = CaseFile()
    did = case.add_document(kind="fir", key="k", title="FIR 321/25", source="PSRMS", text=doc["text"])
    assert fir_facts(case, did, doc) >= 8
    assert all(f["quote"] in doc["text"] for f in case.facts.values())
    assert case.add_fact(did, "Invented", "this sentence is nowhere in the FIR") is None


class _Llm:
    model = "scripted"

    def __init__(self, answers: dict[str, dict]) -> None:
        self.answers = answers

    def generate_structured(self, *, prompt, schema, system=None, cache_kind=None, prompt_version="v1", **_):
        return schema.model_validate(self.answers[schema.__name__]), None


def test_ai_reader_keeps_only_quoted_facts():
    doc = parse_fir_html(FIXTURE, ps_id="544")
    case = CaseFile()
    did = case.add_document(kind="fir", key="k", title="FIR 321/25", source="PSRMS", text=doc["text"])
    llm = _Llm({"_Reading": {"summary": "An armed robbery; one accused arrested with the phone.", "facts": [
        {"statement": "The accused was arrested with the stolen phone.", "quote": "IMEI 350000000000001 برآمد ہوا",
         "kind": "event", "people": ["فرضی ساجد"]},
        {"statement": "The accused confessed to five robberies.", "quote": "confessed to five robberies", "kind": "statement"}]}})
    out = ai_read(case, did, llm)
    assert (out["added"], out["dropped"]) == (1, 1)
    assert case.documents[did]["ai_summary"].startswith("An armed robbery")


def test_people_are_found_in_documents_by_identifier():
    doc = parse_fir_html(FIXTURE)
    case = CaseFile()
    did = case.add_document(kind="fir", key="k", title="FIR", source="PSRMS", text=doc["text"])
    graph = {"nodes": [
        {"id": "p1", "kind": "person", "label": "Kamran", "data": {"phones": ["03990000222"]}},
        {"id": "p2", "kind": "person", "label": "Someone", "data": {"cnic": "9999900003033"}},
        {"id": "p3", "kind": "person", "label": "Nobody Here", "data": {"cnic": "9999911111111"}}], "edges": []}
    found = {f["pid"]: f for f in link_people(case, did, graph)}
    assert set(found) == {"p1", "p2"} and found["p1"]["strength"] == "identifier"
    assert "03990000222" in case.links[found["p1"]["id"]]["quote"]


# -- the whole flow on the EMS backend ---------------------------------------------------


@pytest.fixture
def ems_run():
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    http = _Http()
    mgr = memory_manager(s, backends={"ems": EmsBackend(http=http)})
    handle = mgr.start(GraphRunParams(cnic=SUBJECT, depth=1, max_persons=1, backend="ems", systems=["psrms", "cro"]),
                       wait=True)
    return mgr, handle, http


def test_a_run_reads_the_fir_file_its_lab_reports_and_the_cro_dossier(ems_run):
    mgr, handle, http = ems_run
    assert handle.status == "completed"
    fir_calls = [body for path, body in http.asked if path.endswith("/firfilereport")]
    assert len(fir_calls) == 1 and fir_calls[0]["fir_year"] == "25" and fir_calls[0]["ps_id"] == "544"
    assert [body for path, body in http.asked if path.endswith("/show-reports")] == [{"ps_id": 544, "fir_no": "321", "fir_year": "25"}]
    kinds = {d["kind"]: d for d in handle.case.documents.values()}
    assert set(kinds) == {"fir", "lab", "cro"}
    assert "matches the reference sample" in kinds["lab"]["text"] and http.downloads == ["https://labs.test/r1.pdf"]
    assert kinds["cro"]["images"], "the dossier's pictures are kept"
    subject = next(n for n in handle.builder.persons() if n.data.get("seed"))
    assert {p["id"] for p in kinds["cro"]["images"]} <= set(subject.data["images"])
    # The FIR file named the complainant: on the graph, as the one who filed it against the subject.
    edges = handle.builder.snapshot()["edges"]
    assert any(e["label"] == "Complainant against the subject in FIR 321/25" for e in edges)
    # A lab report found the subject by CNIC.
    assert any(link["pid"] == subject.id and link["doc"] == kinds["lab"]["id"] for link in handle.case.links.values())
    assert handle.case.report_status == "ready" and handle.case.report["targets"][0]["id"] == subject.id
    # The case file travels with the graph.
    assert mgr.get(handle.id)["graph"]["case"]["documents"]


def test_the_chat_reads_and_fetches_documents_within_a_budget(ems_run):
    from sherlocks.linkgraph.investigator import _Tools

    mgr, handle, _http = ems_run
    graph = handle.graph()
    tools = _Tools(graph, case=handle.case, agents=mgr._agents(handle.case, mgr.backend("ems"), graph=graph),
                   backend=mgr.backend("ems"), live_calls=1)
    names = tools.registry()
    assert {"evidence", "read_document", "search_evidence", "fetch_fir", "fetch_cro", "lookup"} <= set(names)
    fir = next(d for d in handle.case.documents.values() if d["kind"] == "fir")
    assert tools.read_document({"id": fir["id"].lower()})["facts"]
    assert tools.search_evidence({"text": "03990000222"})["matches"][0]["doc"] == fir["id"]
    # Already in the case file: no live call spent.
    assert tools.fetch_fir({"fir": "321/25"})["id"] == fir["id"] and tools.live_left == 1
    tools.lookup({"system": "psrms", "cnic": SUBJECT})
    with pytest.raises(ValueError, match="No live calls left"):
        tools.fetch_cro({"cro_no": "778"})


def test_case_report_drops_assessments_without_evidence():
    from sherlocks.evidence.case_report import assemble
    from sherlocks.evidence.report_pdf import render

    case = CaseFile()
    did = case.add_document(kind="lab", key="k", title="DNA report", source="Lab", text="Result: the DNA matches Kamran.")
    fid = case.add_fact(did, "The DNA matches Kamran.", "the DNA matches Kamran")
    graph = {"nodes": [{"id": "a", "kind": "person", "label": "Kamran", "data": {"seed": True, "flags": [], "firs": []}}],
             "edges": []}
    llm = _Llm({"_Written": {"executive_summary": f"Kamran is tied to the scene by DNA [{fid}].", "assessments": [
        {"statement": "Kamran was at the scene.", "confidence": "high", "basis": [fid, did]},
        {"statement": "Kamran leads a gang.", "confidence": "low", "basis": ["Z9"]}],
        "open_questions": [], "recommendations": ["Record Kamran's statement."]}})
    report = assemble(graph, case, llm=llm)
    assert [a["statement"] for a in report["assessments"]] == ["Kamran was at the scene."]
    assert fid in report["cited"] and report["model"] == "scripted"
    assert render(report, case, graph).startswith(b"%PDF")
    rule = assemble(graph, case)
    assert rule["model"] is None and "Kamran" in rule["executive_summary"]


def test_case_file_round_trips():
    case = CaseFile()
    did = case.add_document(kind="cro", key="cro:1", title="CRO 1", source="SAFE", text="Name: X")
    case.add_fact(did, "X has a dossier.", "Name: X")
    again = CaseFile.from_dict(json.loads(json.dumps(case.to_dict())))
    assert again.doc_for("cro:1")["id"] == did and again.cite("F1")["quote"] == "Name: X"
    assert not again.claim("cro:1")


def test_demo_documents_tell_the_story():
    src = DemoEvidence()
    doc = src.fir_document("45", "2023", "501")["doc"]
    assert any("03990000301" in d["remarks"] for d in doc["case_diaries"])
    labs = src.lab_reports("45", "2023", "501")["reports"]
    assert {r["category"] for r in labs} == {"DNA", "CHEMICAL"}
    assert src.download(labs[0]["links"][0]).startswith(b"%PDF")
    assert src.cro_dossier("D-1001")["pdf"].startswith(b"%PDF")


# -- the API -----------------------------------------------------------------------------


def test_report_and_documents_over_the_api():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sherlocks.api.auth import SessionAuth
    from sherlocks.api.linkgraph import build_router
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    s.api.api_key = "k"
    mgr = memory_manager(s, backends={"ems": EmsBackend(http=_Http())})
    app = FastAPI()
    app.include_router(build_router(s, lambda: mgr, SessionAuth(s)))
    client = TestClient(app)
    p, h = s.api.prefix, {"X-API-Key": "k"}
    run = mgr.start(GraphRunParams(cnic=SUBJECT, depth=1, max_persons=1, backend="ems", systems=["psrms"]), wait=True)

    stream = client.get(f"{p}/graph/runs/{run.id}/stream", headers=h).text
    assert "event: case" in stream and "FIR 321/25" in stream
    doc = client.get(f"{p}/graph/runs/{run.id}/documents/d1", headers=h).json()
    assert doc["kind"] == "fir" and doc["fact_rows"]
    pdf = client.get(f"{p}/graph/runs/{run.id}/report.pdf?key=k")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    saved = client.get(f"{p}/graph/runs/{run.id}/export", headers=h).json()
    posted = client.post(f"{p}/graph/report", headers=h, json={"graph": saved["graph"]})
    assert posted.status_code == 200 and posted.content.startswith(b"%PDF")


def test_a_broken_document_api_is_reported_not_left_pending():
    """SAFE answering 502 (seen live): the dossier is reported failed; nothing stays
    "being read", and the run's case file still works."""
    import requests

    from sherlocks.evidence.collector import CaseAgents
    from sherlocks.evidence.sources import EmsEvidence
    from sherlocks.settings import Settings

    class _Down(_Http):
        def send(self, req):
            if req.url.endswith("/cro-report-pdf"):
                raise requests.HTTPError("HTTP 502: Bad gateway")
            return super().send(req)

    case = CaseFile()
    agents = CaseAgents(case, EmsEvidence(_Down()), settings=Settings())
    assert agents.fetch_cro("10192") == {"error": "HTTPError: HTTP 502: Bad gateway"}
    assert not case.pending and case.attempts[-1]["status"] == "error"


def test_scanned_pages_are_read_by_the_vision_model():
    """No OCR engine: a page with no text layer goes to the VLM as the scan image."""
    import io

    from fpdf import FPDF
    from PIL import Image

    from sherlocks.evidence.pdf_reader import read_pdf

    buf = io.BytesIO()
    Image.new("RGB", (700, 990), (240, 240, 235)).save(buf, format="JPEG")
    pdf = FPDF()
    pdf.add_page()
    pdf.image(io.BytesIO(buf.getvalue()), x=0, y=0, w=210)
    scan = bytes(pdf.output())

    class _Vlm:
        def __init__(self):
            self.seen = []

        def read_image(self, image, prompt, *, mime="image/png"):
            self.seen.append((mime, len(image)))
            return "DNA ANALYSIS REPORT\\nResult: the profile matches reference sample R1 (scanned page)."

    vlm = _Vlm()
    content = read_pdf(scan, vision=vlm)
    assert [p.method for p in content.pages] == ["vision"] and "matches reference sample" in content.text
    assert vlm.seen and vlm.seen[0][0] == "image/jpeg"
    assert [p.method for p in read_pdf(scan).pages] == ["unread"]


def test_witnesses_of_the_same_case_are_only_weakly_linked():
    """Two witnesses of someone else's case are not witnesses of each other."""
    from sherlocks.linkgraph.extractors import extract_record
    from sherlocks.linkgraph.graph import GraphBuilder
    from sherlocks.linkgraph.images import MemoryImageStore
    from sherlocks.linkgraph.models import FirKey, PersonRef, SystemRecord
    from sherlocks.linkgraph.network import PersonNetwork

    # 1. PSRMS: the subject and another person are both witnesses in FIR 45/2023.
    b = GraphBuilder()
    me, _ = b.upsert_person(PersonRef(cnic="9999900000011"), depth=0, seed=True)
    payload = {"provider": "psrms", "hit": True, "status": "success", "summary": "", "data": {"firs": [
        {"person_type": "SUS", "person_name": "Me", "person_cnic": "9999900000011", "fir_no": "45", "fir_year": "2023", "ps_id": "1"},
        {"person_type": "SUS", "person_name": "Other Witness", "person_cnic": "9999900000029", "fir_no": "45", "fir_year": "2023", "ps_id": "1"},
        {"person_type": "WIT", "person_name": "The Accused", "person_cnic": "9999900000037", "fir_no": "45", "fir_year": "2023", "ps_id": "1"}]}}
    rec = extract_record("psrms", payload, b.person_ref(me), MemoryImageStore())
    sid, _ = b.add_record(me, rec)
    for rel in rec.related:
        b.link_related(sid, rel, depth=1, from_pid=me)
    edges = b.snapshot()["edges"]
    witness = next(n.id for n in b.persons() if n.data.get("cnic") == "9999900000029")
    accused = next(n.id for n in b.persons() if n.data.get("cnic") == "9999900000037")
    between = [e for e in edges if {e["source"], e["target"]} & {witness} and me in (e["source"], e["target"])]
    assert [e["kind"] for e in between] == ["weak"] and between[0]["label"] == "Both witnesses in FIR 45/2023"
    assert any(e["kind"] == "strong" and e["target"] == accused for e in edges)   # the accused stays a stated link

    # 2. Two people whose own records both list FIR 45/2023 as witnesses: weak, not "Same FIR".
    b2 = GraphBuilder()
    x, _ = b2.upsert_person(PersonRef(cnic="9999900000045"), depth=0)
    y, _ = b2.upsert_person(PersonRef(cnic="9999900000053"), depth=0)
    for pid in (x, y):
        r = SystemRecord(system="psrms", status="success", hit=True)
        r.firs.append(FirKey(fir_no="45", fir_year="2023", police_station="PS A", ps_id="1", role="Witness"))
        b2.add_record(pid, r, record_key=f"{pid}-own")
    b2.derive_shared_records()
    pair = [e for e in b2.snapshot()["edges"] if {e["source"], e["target"]} == {x, y}]
    assert [e["kind"] for e in pair] == ["weak"]

    # 3. The network does not treat co-witnesses on one FIR record as a stated link.
    graph = {"nodes": [{"id": i, "kind": "person", "label": i, "data": {}} for i in ("a", "w1", "w2")]
             + [{"id": "s:fir", "kind": "system", "label": "FIR", "data": {"owner": "a"}}],
             "edges": [{"id": "1", "source": "a", "target": "s:fir", "kind": "found_in", "label": "Accused"},
                       {"id": "2", "source": "w1", "target": "s:fir", "kind": "found_in", "label": "Witness"},
                       {"id": "3", "source": "w2", "target": "s:fir", "kind": "found_in", "label": "Witness"}]}
    net = PersonNetwork(graph)
    assert not net.G["w1"]["w2"]["stated"] and net.G["a"]["w1"]["stated"]


def test_a_tenancy_witness_is_linked_to_the_tenant_not_the_landlord():
    from sherlocks.linkgraph.extractors import extract_record
    from sherlocks.linkgraph.graph import GraphBuilder
    from sherlocks.linkgraph.images import MemoryImageStore
    from sherlocks.linkgraph.models import PersonRef

    row = {"tenant_id": 9, "raw": {
        "tenant_name": "Tenant Person", "tenant_cnic": "9999900000045",
        "owner_name": "Land Lord", "owner_cnic": "9999900000053",
        "tenant_witness_name_1": "Me Witness", "tenant_witness_cnic_1": "9999900000011",
        "tenant_witness_name_2": "Other Witness", "tenant_witness_cnic_2": "9999900000029",
        "property_address": "Block 7"}}
    payload = {"provider": "old_tenant", "hit": True, "status": "success", "summary": "",
               "raw": {"by_cnic": {"success": True, "data": [row]}}}
    b = GraphBuilder()
    me, _ = b.upsert_person(PersonRef(cnic="9999900000011"), depth=0, seed=True)
    rec = extract_record("old_tenant", payload, b.person_ref(me), MemoryImageStore())
    assert {r.relation for r in rec.related} == {"Tenancy witnessed by the subject", "Co-witness on a tenancy"}
    assert any(f.label == "Landlord of the tenancy witnessed" for f in rec.fields)   # kept, not linked
    sid, _ = b.add_record(me, rec)
    for rel in rec.related:
        b.link_related(sid, rel, depth=1, from_pid=me)
    snap = b.snapshot()
    tenant = next(n["id"] for n in snap["nodes"] if n["data"].get("cnic") == "9999900000045")
    to_tenant = next(e for e in snap["edges"] if e["target"] == tenant)
    assert to_tenant["relation"]["from"] == me and to_tenant["relation"]["to"] == tenant   # witness -> tenant
    assert "was witness to the tenancy of" in to_tenant["relation"]["sentence"]
    other = next(n["id"] for n in snap["nodes"] if n["data"].get("cnic") == "9999900000029")
    assert [e["kind"] for e in snap["edges"] if other in (e["source"], e["target"])] == ["weak"]
    assert not any(n["data"].get("cnic") == "9999900000053" for n in snap["nodes"])
