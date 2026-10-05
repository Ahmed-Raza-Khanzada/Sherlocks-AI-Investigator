"""The reading agents: turn a document into facts, and find the graph's people in it.

* :func:`fir_facts` - the FIR's structure as facts, by rule: who complained, who is
  accused, who witnessed, which sections, where and when, the case's position, the
  investigation result. Each quotes the document line it comes from.
* :func:`report_facts` - the header facts of a lab report or a CRO dossier.
* :func:`ai_read` - the model reads the free text (the narrative, the case diaries, a
  lab report's findings) and proposes facts. Every fact must carry a verbatim quote,
  and a quote that is not in the document drops the fact: the model cannot add to the
  evidence, only point at it.
* :func:`link_people` - finds every person of the graph a document mentions, by CNIC,
  phone or vehicle (an identifier link) or by full name (a name link, weaker).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from sherlocks.evidence.case_file import CaseFile, squash
from sherlocks.evidence.fir_document import station
from sherlocks.linkgraph.normalize import cnic13, mobile11, name_key

logger = logging.getLogger(__name__)

_CNIC = re.compile(r"(?<!\d)\d{5}-?\d{7}-?\d(?!\d)")
_PHONE = re.compile(r"(?<!\d)(?:\+?92|0)3\d{2}[\s-]?\d{7}(?!\d)")
_PLATE = re.compile(r"\b[A-Z]{2,4}[- ]?\d{1,4}\b")
_AI_TEXT = 9000


def _line_with(text: str, needle: str, prefix: str = "") -> str:
    """The document line holding ``needle`` - under ``prefix`` (its section) when given."""
    lines = text.splitlines()
    for line in lines:
        if needle in line and line.startswith(prefix):
            return line
    for line in lines:
        if needle in line:
            return line
    return needle


# --------------------------------------------------------------------------------------
# Rule facts
# --------------------------------------------------------------------------------------


def fir_facts(case: CaseFile, doc_id: str, doc: dict[str, Any]) -> int:
    """Structural facts of one FIR document. Returns how many were added."""
    text = case.documents[doc_id]["text"]
    label = f"FIR {doc.get('fir_no')}/{doc.get('fir_year')}"
    ps = station(doc)
    added = 0

    def fact(statement: str, needle: str, kind: str, people: list[str] | None = None, prefix: str = "") -> None:
        nonlocal added
        if needle and case.add_fact(doc_id, statement, _line_with(text, needle, prefix), kind=kind, people=people):
            added += 1

    if doc.get("sections"):
        fact(f"{label} was registered at {ps} under {doc['sections']}"
             + (f", reported {doc['reported']}" if doc.get("reported") else "") + ".", doc["sections"], "status", prefix="Sections")
    if doc.get("occurred"):
        fact(f"The occurrence in {label} took place {doc['occurred']}"
             + (f" at {doc['place']}" if doc.get("place") else "") + ".", doc["occurred"], "event", prefix="Occurred")
    c = doc.get("complainant") or {}
    if c.get("name"):
        ids = ", ".join(x for x in (f"CNIC {c['cnic']}" if c.get("cnic") else "", f"phone {c['phone']}" if c.get("phone") else "") if x)
        fact(f"The complainant of {label} is {c['name']}" + (f" s/o {c['father']}" if c.get("father") else "")
             + (f" ({ids})" if ids else "") + ".", c["name"], "person", [c["name"]], prefix="Complainant")
    for p in doc.get("nominated_suspects") or []:
        if p.get("name"):
            fact(f"{p['name']}" + (f" s/o {p['father']}" if p.get("father") else "") + f" is nominated as accused in {label}"
                 + (f" (CNIC {p['cnic']})" if p.get("cnic") else "") + ".", p["name"], "person", [p["name"]],
                 prefix="Nominated accused")
    for p in doc.get("arrested_suspects") or []:
        if p.get("name"):
            fact(f"{p['name']} is recorded as arrested in {label}.", p["name"], "person", [p["name"]],
                 prefix="Arrested accused")
    for p in doc.get("witnesses") or []:
        if p.get("name"):
            fact(f"{p['name']} is a witness in {label}" + (f" (CNIC {p['cnic']})" if p.get("cnic") else "") + ".",
                 p["name"], "person", [p["name"]], prefix="Witnesses")
    for p in doc.get("investigating_officers") or []:
        if p.get("name"):
            fact(f"{' '.join(x for x in (p.get('rank'), p['name']) if x)} investigated {label}"
                 + (f" from {p['date']}" if p.get("date") else "") + ".", p["name"], "person", [p["name"]],
                 prefix="Investigating officers")
    for p in doc.get("stolen_property") or []:
        if p.get("description"):
            fact(f"Property reported in {label}: {p['description']}" + (f" (value {p['value']})" if p.get("value") else "") + ".",
                 p["description"], "property", prefix="Stolen property")
    if doc.get("case_positions"):
        last = doc["case_positions"][-1]
        if last.get("position"):
            fact(f"Latest position of {label}: {last['position']}" + (f" ({last['date']})" if last.get("date") else "") + ".",
                 last["position"], "status", prefix="Case position")
    if doc.get("investigation_result"):
        fact(f"Investigation result of {label}: {doc['investigation_result'][:300]}", doc["investigation_result"][:80], "status",
             prefix="Investigation result")
    return added


def report_facts(case: CaseFile, doc_id: str) -> int:
    """A lab report / CRO dossier: the facts its own header lines state."""
    doc = case.documents[doc_id]
    added = 0
    for line in doc["text"].splitlines()[:6]:
        line = squash(line)
        if len(line) >= 12 and case.add_fact(doc_id, f"{doc['title']}: {line[:240]}", line, kind="report"):
            added += 1
    return added


# --------------------------------------------------------------------------------------
# AI reader
# --------------------------------------------------------------------------------------


class _ReadFact(BaseModel):
    statement: str = Field(description="One sentence in English stating the fact, naming people as written.")
    quote: str = Field(description="The exact words from the document this rests on, copied verbatim (Urdu stays Urdu).")
    kind: Literal["event", "identifier", "vehicle", "weapon", "location", "forensic", "statement", "status",
                  "person", "property"] = "event"
    people: list[str] = Field(default_factory=list, description="Names of the people the fact is about, as written.")


class _Reading(BaseModel):
    summary: str = Field(description="2-4 sentences in English: what this document establishes.")
    facts: list[_ReadFact] = Field(default_factory=list, description="Up to 10 facts that matter to an investigator.")


_READER_SYSTEM = (
    "You are a police case-file reader. You read ONE document (an FIR with its case diaries, a forensic or "
    "medical report, or a criminal record dossier; Urdu or English) and extract the facts an investigator "
    "needs: who did what, when, where; arrests, recoveries, confessions and who they name; phone numbers, "
    "IMEIs, vehicles, weapons; forensic results and what they matched; case status. Rules: every fact must "
    "be supported by a quote copied EXACTLY from the document (do not translate or tidy the quote); write the "
    "statement in English; never add anything the document does not say; prefer facts that name people or "
    "identifiers."
)


def ai_text(kind: str, doc: dict[str, Any]) -> str:
    """The part of a document worth the model's reading."""
    text = doc.get("text") or ""
    if kind == "fir":
        keep = [line for line in text.splitlines()
                if line.startswith(("First information", "Case diary", "Investigation result", "Offence", "Place"))]
        text = "\n".join(keep) or text
    return text[:_AI_TEXT]


def ai_read(case: CaseFile, doc_id: str, llm: Any) -> dict[str, Any]:
    """Let the model read one document. Returns ``{summary, added, dropped}``."""
    doc = case.documents[doc_id]
    body = ai_text(doc["kind"], doc)
    if not body.strip():
        return {"summary": "", "added": 0, "dropped": 0}
    reading, _ = llm.generate_structured(
        prompt=f"Document: {doc['title']} ({doc['source']})\n\n{body}\n\nExtract the facts as JSON.",
        schema=_Reading, system=_READER_SYSTEM, cache_kind="evidence_read", prompt_version="v1", max_tokens=2500)
    added = dropped = 0
    for f in reading.facts[:10]:
        if case.add_fact(doc_id, f.statement, f.quote, kind=f.kind, people=f.people[:6], by="ai"):
            added += 1
        else:
            dropped += 1
    if reading.summary.strip():
        with case._lock:
            doc["ai_summary"] = squash(reading.summary)[:900]
    case.mark_read(doc_id, "ai")
    return {"summary": reading.summary, "added": added, "dropped": dropped}


# --------------------------------------------------------------------------------------
# Linker
# --------------------------------------------------------------------------------------


def _people_index(graph: dict[str, Any]) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict[str, str]]:
    cnics: dict[str, str] = {}
    phones: dict[str, str] = {}
    plates: dict[str, str] = {}
    names: dict[str, str] = {}
    name_count: dict[str, int] = {}
    for node in graph.get("nodes") or []:
        if node.get("kind") != "person":
            continue
        d = node.get("data") or {}
        if d.get("cnic"):
            cnics[d["cnic"]] = node["id"]
        for p in d.get("phones") or []:
            phones[p] = node["id"]
        for v in d.get("vehicles") or []:
            key = re.sub(r"[\s-]", "", str(v)).upper()
            if len(key) >= 4:
                plates[key] = node["id"]
        key = name_key(node.get("label") or "")
        if key and len(key.split()) >= 2:
            name_count[key] = name_count.get(key, 0) + 1
            names[key] = node["id"]
    # A name two people share points at neither.
    names = {k: v for k, v in names.items() if name_count.get(k) == 1}
    return cnics, phones, plates, names


def link_people(case: CaseFile, doc_id: str, graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Every graph person this document mentions. Adds ``L`` links; returns them with
    ``pid`` and ``strength`` ("identifier" or "name")."""
    doc = case.documents[doc_id]
    text = doc["text"]
    labels = {n["id"]: n.get("label") or n["id"] for n in graph.get("nodes") or [] if n.get("kind") == "person"}
    cnics, phones, plates, names = _people_index(graph)
    found: dict[str, tuple[str, str, str]] = {}

    for m in _CNIC.finditer(text):
        pid = cnics.get(cnic13(m.group(0)) or "")
        if pid and pid not in found:
            found[pid] = (f"CNIC {m.group(0)} appears in the document", _line_with(text, m.group(0)), "identifier")
    for m in _PHONE.finditer(text):
        pid = phones.get(mobile11(m.group(0)) or "")
        if pid and pid not in found:
            found[pid] = (f"phone {m.group(0)} appears in the document", _line_with(text, m.group(0)), "identifier")
    upper = re.sub(r"[\s-]", "", text.upper())
    for plate, pid in plates.items():
        if plate in upper and pid not in found:
            found[pid] = (f"vehicle {plate} appears in the document", plate, "identifier")
    low = f" {name_key(text)} "
    for key, pid in names.items():
        if pid not in found and f" {key} " in low:
            found[pid] = ("named in the document", key, "name")

    out = []
    for pid, (how, quote, strength) in found.items():
        link_id = case.add_link(doc_id, pid, labels.get(pid, pid), how, quote, strength=strength)
        out.append({"id": link_id, "pid": pid, "name": labels.get(pid, pid), "how": how, "strength": strength})
    return out
