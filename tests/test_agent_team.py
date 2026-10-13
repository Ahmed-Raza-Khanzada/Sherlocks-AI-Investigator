"""The agent team: uploads (four formats), the CDR agent, the incident pin, the
Questioner, the Fact collector and the Validator. Demo world, synthetic files."""

from __future__ import annotations

import datetime as dt
import io
import time
import zipfile

import pytest

from sherlocks.evidence.case_file import CaseFile
from sherlocks.evidence.uploads import UploadError, check_upload, read_docx

KAMRAN, SAJID = "03990000101", "03990000201"


def _cdr_xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["MSISDN", "B Number", "Call Type", "Start Time", "Duration", "Site Address", "Latitude", "Longitude"])
    for i in range(24):
        ws.append(["92" + KAMRAN[1:], SAJID if i % 2 else "03990009999", "Call - Outgoing",
                   dt.datetime(2023, 3, 1, 21, 0) + dt.timedelta(minutes=10 * i), 30,
                   "Rashid Minhas Road" if i > 12 else "PECHS", 24.90 if i > 12 else 24.86, 67.09 if i > 12 else 67.06])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>")
    return buf.getvalue()


def test_only_the_four_formats_are_accepted():
    assert check_upload("cdr.xlsx", _cdr_xlsx()) == "xlsx"
    assert check_upload("statement.docx", _docx("x")) == "docx"
    assert check_upload("scan.pdf", b"%PDF-1.4 ...") == "pdf"
    assert check_upload("photo.JPG", b"\xff\xd8\xff\xe0" + b"0" * 10) == "image"
    for name, content in [("tool.exe", b"MZ"), ("cdr.csv", b"a,b"), ("fake.pdf", b"MZ not a pdf"), ("fake.xlsx", _docx("x"))]:
        with pytest.raises(UploadError):
            check_upload(name, content)
    assert read_docx(_docx("Witness saw a white Corolla"))[0] == "Witness saw a white Corolla"


@pytest.fixture
def demo_run():
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    s.app.output_dir = str(__import__("tempfile").mkdtemp())
    mgr = memory_manager(s)
    handle = mgr.start(GraphRunParams(cnic="9999900000011", depth=1, max_persons=5, backend="demo"), wait=True)
    return mgr, handle


def _wait(pred, seconds: float = 20.0) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.1)
    raise AssertionError("timed out")


def test_an_uploaded_cdr_is_read_linked_and_questioned(demo_run):
    mgr, handle = demo_run
    case = handle.case
    assert mgr.upload(handle.id, "kamran cdr.xlsx", _cdr_xlsx())["kind"] == "xlsx"
    _wait(lambda: any(d["kind"] == "cdr" for d in case.documents.values()))
    doc = next(d for d in case.documents.values() if d["kind"] == "cdr")
    assert "Subscriber of this CDR: 03990000101 (Kamran Ahmed on the graph)" in doc["text"]
    assert any("On the graph: 03990000201 is Sajid Mehmood" in f["statement"] for f in case.facts.values())
    edges = handle.builder.snapshot()["edges"]
    assert any(e["label"].startswith("In phone contact") and e["system"] == "cdr" for e in edges)
    assert mgr.upload_file(handle.id, doc["id"])[1] == "kamran cdr.xlsx"
    # The target is already named in FIRs: the Questioner first wants to know which case this is,
    # then the incident.
    assert [q["key"] for q in case.open_questions()][:2] == ["case:which", "incident:place"]

    # The officer pins it: the CDR is re-read against the point, the question closes.
    mgr.set_incident(handle.id, {"lat": 24.9, "lon": 67.09, "place": "Rashid Minhas Road", "date": "2023-03-01", "time": "23:00"})
    _wait(lambda: "Near the incident" in case.documents[doc["id"]]["text"])
    assert not any(q["key"] == "incident:place" and q["status"] == "open" for q in case.questions)
    assert any(f["by"] == "officer" and "Rashid Minhas Road" in f["statement"] for f in case.facts.values())

    # One target is the person under investigation: nobody is asked who the main suspect is.
    from sherlocks.evidence.questioner import gaps

    assert not any(q["key"] == "roles:main" for q in case.questions)
    assert not any(g["key"] == "roles:main" for g in gaps(case, handle.graph()))


def test_refused_upload_over_http(demo_run):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sherlocks.api.auth import SessionAuth
    from sherlocks.api.linkgraph import build_router

    mgr, handle = demo_run
    mgr.settings.api.api_key = "k"
    app = FastAPI()
    app.include_router(build_router(mgr.settings, lambda: mgr, SessionAuth(mgr.settings)))
    client = TestClient(app)
    url, h = f"{mgr.settings.api.prefix}/graph/runs/{handle.id}", {"X-API-Key": "k"}
    bad = client.post(f"{url}/uploads", headers=h, files={"file": ("x.exe", b"MZ")})
    assert bad.status_code == 422 and "not accepted" in bad.json()["detail"]
    ok = client.post(f"{url}/uploads", headers=h, files={"file": ("note.docx", _docx("The car was KDE-1234"))},
                     data={"note": "witness statement"})
    assert ok.status_code == 202
    _wait(lambda: any(d["kind"] == "upload" for d in handle.case.documents.values()))
    board = client.get(f"{url}/case", headers=h).json()
    assert board["questions"] and any(d["title"] == "Upload: note.docx" for d in board["documents"])
    assert client.post(f"{url}/incident", headers=h, json={"lat": 24.9, "lon": 67.1, "date": "2023-03-01"}).status_code == 200


class _Llm:
    model = "scripted"

    def __init__(self, answers):
        self.answers = answers

    def generate_structured(self, *, prompt, schema, system=None, cache_kind=None, prompt_version="v1", **_):
        answer = self.answers.get(schema.__name__)
        if answer is None:
            raise RuntimeError("no scripted answer")
        return schema.model_validate(answer(prompt) if callable(answer) else answer), None


def _graph():
    return {"nodes": [{"id": "a", "kind": "person", "label": "Kamran Ahmed", "data": {"seed": True, "flags": [], "phones": [KAMRAN]}},
                      {"id": "b", "kind": "person", "label": "Sajid Mehmood", "data": {"seed": True, "flags": [], "phones": [SAJID]}}],
            "edges": [{"id": "e", "source": "a", "target": "b", "kind": "strong", "label": "Co-accused in FIR 45/2023"}]}


def test_language_and_intent():
    from sherlocks.linkgraph.conversation import detect_language, route

    assert detect_language("How is Kamran linked to Sajid?") == "en"
    assert detect_language("Kamran ka Sajid se kya taluq hai?") == "roman"
    assert detect_language("کامران کا ساجد سے کیا تعلق ہے؟") == "ur"
    assert route("kamran kahan tha?", []) == "question"
    assert route("Assalam o alaikum", []) == "greeting"
    assert route("Kamran main suspect hai", []) == "statement"
    assert route("Kamran Ahmed", [{"id": "Q1"}]) == "answer"


def test_a_chat_turn_runs_through_the_team():
    """Turn 1 (Roman Urdu, information): recorded, acknowledged in Roman Urdu, one question
    asked. Turn 2 (English question): investigated; the Conversation agent's draft cites a
    fact that does not exist, so the Validator has it rewritten."""
    from sherlocks.linkgraph.sherlock_team import run_turn

    case = CaseFile()
    graph = _graph()
    turn1 = list(run_turn(graph, "Kamran main suspect hai. Waqia 01-03-2023 ko hua.", case=case))
    final = turn1[-1]
    assert final["language"] == "roman" and final["intent"] == "statement"
    assert case.roles == {"a": "main suspect"} and case.incident["date"] == "2023-03-01"
    assert final["answer"].startswith("Note kar liya")
    assert "Map par pin kar dein" in final["answer"] and final["asking"]["key"] == "incident:place"
    agents = [e["agent"] for e in turn1 if e["type"] == "agent"]
    assert turn1[0]["type"] == "start" and agents[:2] == ["Conversation agent", "Fact collector"]

    seen = {}

    def draft(prompt):
        data = __import__("json").loads(prompt)
        if data.get("fix_these_problems"):               # sent back by the Answer checker, with the reasons
            seen["problems"] = data["fix_these_problems"]
            return {"first_line": "Kamran and Sajid are co-accused in FIR 45/2023 [PSRMS].", "answered": True,
                    "sections": [{"name": "Every connection", "body": "• Co-accused in FIR 45/2023 [PSRMS]"}]}
        seen["given"] = data["given"]
        return {"first_line": "Kamran and Sajid are brothers and co-accused [F9].", "answered": True,
                "sections": [{"name": "Every connection", "body": "• Co-accused in FIR 45/2023 [F9]", "status": "not on record"}]}

    def check(prompt):
        data = __import__("json").loads(prompt)
        seen["checker_given"] = data
        if "brothers" in data["draft_answer"]:
            return {"consistent": False, "issues": ["The draft says they are brothers; no record given says so."]}
        return {"consistent": True, "issues": []}

    llm = _Llm({"_Reading": {"act": "question", "asks_something": True, "type": "connection", "shape": "link",
                             "people": ["Kamran Ahmed", "Sajid Mehmood"], "depth": "detailed", "scope": "connection"},
                "_Draft": draft, "_Check": check})
    turn2 = list(run_turn(graph, "How are Kamran and Sajid connected?", llm=llm, case=case))
    final = turn2[-1]
    assert {"Question reader", "Officer agent", "Facts agent", "Validator"} <= {e["agent"] for e in turn2 if e["type"] == "agent"}
    # The Officer agent's draft said something no record says: the Answer checker sent it back
    # to him once, with the reason, and his second draft - same shape, same records - went out.
    assert final["checks"]["revised"] and "brothers" not in final["answer"] and "FIR 45/2023" in final["answer"]
    assert "brothers" in seen["problems"][0] and "## Every connection" in final["answer"]
    assert "F9" not in final["answer"]                                 # an id that is not on the board never reaches the officer
    assert final["query"]["type"] == "connection" and final["facts"]["count"] == 1
    # The checker judged the draft against the records it was written from and the officer's own
    # words - never against Sherlock's earlier replies.
    assert "earlier_conversation" not in seen["checker_given"] and "co-accused" in seen["checker_given"]["records_given"].lower()
    assert seen["checker_given"]["officer_said_earlier"] == ["Kamran main suspect hai. Waqia 01-03-2023 ko hua."]
    # The Officer agent drafted from the whole case: the roles, the dossiers, the officer's words.
    given = seen["given"]
    assert given["case"]["roles"] == {"Kamran Ahmed": "main suspect"}
    assert given["dossiers"][0]["role"] == "main suspect" and given["computed"]["count"] == 1
    assert "main suspect hai" in str(given["context"]["statements"])
    assert "chat_so_far_not_evidence" in given                        # the chat is context, never a source of facts
    assert {"paths", "compare"} <= set(given["sources"])              # a link question: the routes and what they share
    assert len(case.conversation) == 2
