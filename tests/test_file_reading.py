"""Police systems' files read in full (FIR file with every case diary, lab report, CRO dossier) and the chat
reading the file's own words - synthetic data only (tests/fixtures/psrms_fir_report.html, invented people)."""

from __future__ import annotations

import json
from pathlib import Path

from sherlocks.evidence.case_file import CaseFile
from sherlocks.evidence.fir_document import parse_fir_html

FIXTURE = (Path(__file__).parent / "fixtures" / "psrms_fir_report.html").read_text(encoding="utf-8")


class _Lines:
    """A model that writes plain labelled lines: the part lines for each part, the whole lines once."""

    model = "scripted"

    def __init__(self, part: str, whole: str) -> None:
        self.part, self.whole, self.prompts = part, whole, []

    def generate_text(self, *, prompt, system="", **_):
        given = json.loads(prompt)
        self.prompts.append(given)
        if "part" in given:
            return self.part
        if "open_leads" in given:
            return ""
        return self.whole


def _fir_case() -> tuple[CaseFile, str, dict]:
    doc = parse_fir_html(FIXTURE, ps_id="544")
    case = CaseFile()
    did = case.add_document(kind="fir", key="fir:321/25/544", title="FIR 321/25", source="PSRMS FIR file",
                            text=doc["text"], ref={"fir_no": "321", "fir_year": "25", "ps_id": "544"},
                            data={k: v for k, v in doc.items() if k != "text"})
    return case, did, doc


PART = ("EVENT: 05-07-2025 | | The IO arrested فرضی ساجد and recovered the phone | ملزم فرضی ساجد کو گرفتار کیا گیا جس کے قبضہ سے موبائل فون\n"
        "SAID: فرضی ساجد | his companion is فرضی کامران, phone 03990000222 | ملزم نے بیان دیا کہ اس کا ساتھی فرضی کامران 03990000222 ہے\n"
        "LEAD: Trace فرضی کامران, phone 03990000222 | ملزم نے بیان دیا کہ اس کا ساتھی فرضی کامران 03990000222 ہے\n"
        "PERSON: فرضی کامران | companion of the accused | absconding\n")
WHOLE = "DOCUMENT: FIR file | PSRMS | 04-07-2025 | FIR 321/25\nSUMMARY: A robbery; one accused arrested, one absconding.\n"


def test_the_full_reader_reads_the_case_diaries_of_an_fir_file_onto_the_board():
    from sherlocks.evidence.readers import free_text
    from sherlocks.team import document_reader

    case, did, _ = _fir_case()
    llm = _Lines(PART, WHOLE)
    body = free_text("fir", case.documents[did])
    assert "Case diary 01" in body and "Investigation result" in body and "Witnesses" not in body
    reading = document_reader.read(case, did, llm, body=body, store="file_reading", leads=False)
    doc = case.documents[did]
    # Kept where the report does not take it for an officer's upload.
    assert doc["file_reading"] is reading and "reading" not in doc
    statements = [case.facts[f]["statement"] for f in doc["facts"]]
    assert any("فرضی کامران" in s and "03990000222" in s for s in statements)      # the diary's step is a fact
    assert reading["events"] and reading["events"][0]["when"] == "05-07-2025"
    # A past FIR's open angle stays a fact of that FIR - no lead on the board.
    assert reading["leads"] and not case.leads
    # The case reader's view of an upload leaves it out.
    from sherlocks.team.case_reader import uploaded

    assert uploaded(case) == []


def test_the_collector_reads_files_in_full_when_the_model_writes_plain_text():
    from sherlocks.evidence.collector import CaseAgents
    from sherlocks.settings import Settings

    case, did, _ = _fir_case()
    llm = _Lines(PART, WHOLE)
    agents = CaseAgents(case, None, settings=Settings(), llm=lambda: llm)
    agents._read_with_ai(did)
    assert case.documents[did].get("file_reading") and "document reader" in case.documents[did]["read_by"]
    # The part read is the FIR's own words, not its header.
    part = next(p for p in llm.prompts if "part" in p)
    assert "Case diary 02" in part["text"] and "Investigation result" in part["text"]


def test_the_chat_is_given_every_case_diary_of_a_long_fir_file():
    from sherlocks.linkgraph.investigator import _read_in_full, doc_text

    case, did, _ = _fir_case()
    doc = case.documents[did]
    doc["text"] = doc["text"].replace("Stolen property 1:", "Stolen property 1: " + "x" * 6000 + " ")
    got = doc_text(doc, 3500)
    assert len(got) <= 3500
    # Cut from the front, the diaries were lost; now the header is cut and the file's own words stay.
    assert "Case diary 01" in got and "Case diary 02" in got and "Investigation result" in got
    assert got.startswith("FIR 321/25")
    assert _read_in_full(doc) == {}
    from sherlocks.team import document_reader

    document_reader.read(case, did, _Lines(PART, WHOLE), body=doc["text"], store="file_reading", leads=False)
    full = _read_in_full(doc)["read_in_full"]
    assert full["dated_steps"][0].split(":")[0] in doc["facts"] and full["said"]


def test_a_reading_of_this_cases_fir_puts_its_leads_on_the_board():
    from sherlocks.evidence.collector import _of_this_case

    case, did, _ = _fir_case()
    assert _of_this_case(case, case.documents[did]) is False
    case.dialog["case_basis"] = "fir_on_record"
    case.incident = {**(case.incident or {}), "fir": "321/2025"}
    assert _of_this_case(case, case.documents[did]) is True
    case.incident["fir"] = "322/2025"
    assert _of_this_case(case, case.documents[did]) is False
