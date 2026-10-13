"""Recorded statements ``W#``: what a complainant, a witness or an accused said, as the
record has it - the FIR's account, statements under sections 161 and 164, supplementary
statements, interrogation notes.

The model reads the document and copies each statement out word for word; code keeps it
only when those words are really in the document. A ``W#`` is a reliable record that the
person said it - whether it is true is what S6 and S7 test.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.team.runtime import Typed, ask

READS = ("fir", "upload", "lab", "cro")


class _Recorded(Typed):
    speaker: str = Field(description="Who said it, the name as written.")
    father: str = Field(default="", description="The speaker's father's name, if the record gives it (s/o, ولد).")
    role: Literal["complainant", "witness", "accused", "victim", "officer", "other"]
    kind: Literal["fir", "161", "164", "supplementary", "interrogation", "other"] = Field(
        description="fir: the account in the FIR; 161 / 164: statement under that section; interrogation: notes of "
                    "questioning the accused.")
    date: str | None = Field(default=None, description="When it was recorded, YYYY-MM-DD, if the record says.")
    recorded_by: str = ""
    page: int | None = None
    text: str = Field(description="The statement itself, copied EXACTLY from the document, in its own script.")


class _RecordedList(Typed):
    statements: list[_Recorded] = Field(default_factory=list)


_SYSTEM = ("You read ONE police record and copy out every statement a person made in it: the complainant's account "
           "in an FIR, a witness or accused statement under section 161 or 164, a supplementary statement, "
           "interrogation notes. One entry per speaker per statement. Copy the words exactly as written (Urdu stays "
           "Urdu) - never summarise, translate or tidy. A report's own findings (a doctor's opinion, a lab result) "
           "are not a statement: leave them out. Return an empty list when the record holds no statement. "
           + DATA_NOT_INSTRUCTIONS)


def extract(case: Any, net: Any, doc_id: str, llm: Any) -> list[str]:
    """A2 on one document: its recorded statements, each kept only if its words are in it."""
    from sherlocks.team.entities import lookup

    doc = case.documents[doc_id]
    if doc["kind"] not in READS or "W" in doc.get("read_by", []) or not (doc.get("text") or "").strip():
        return []
    out = ask(llm, _RecordedList, f"Record: {doc['title']} ({doc['source']})\n\n{doc['text'][:12000]}",
              system=_SYSTEM, kind="recorded_statements", max_tokens=2200)
    if out is None:
        return []                                   # read again on the next run
    added = []
    with case.batch("A2 Card reader", "statements", doc=doc_id):
        for st in out.statements[:20]:
            who = lookup(case, net, st.speaker, source=doc_id, father=st.father)
            ref = case.add_statement(doc_id, speaker=st.speaker, role=st.role, kind=st.kind, text=st.text, date=st.date,
                                     recorded_by=st.recorded_by, page=st.page, father=st.father,
                                     pid=who["pid"] if who["state"] == "same" else None)
            if ref:
                added.append(ref)
        case.mark_read(doc_id, "W")
    return added


def of(case: Any, net: Any, who: str | None) -> dict[str, Any]:
    """The Statements tool: every recorded statement and claim by or about a person, in
    order, with what S7 found between the versions."""
    from sherlocks.linkgraph.normalize import name_key

    pid = net.resolve(who) if who else None
    key = name_key(net.name(pid) if pid else who or "")

    def mine(name: str, p: str | None) -> bool:
        return not who or (pid is not None and p == pid) or (bool(key) and name_key(name) == key)

    rows = sorted((w for w in case.statements.values() if mine(w["speaker"], w.get("pid"))),
                  key=lambda w: (w.get("date") or "9999", w["id"]))
    claims = [c for c in case.claims.values() if mine(c["about"], c.get("about_pid")) or mine(c["said_by"], c.get("said_pid"))]
    return {"who": net.name(pid) if pid else who,
            "statements": [{"id": w["id"], "speaker": w["speaker"], "role": w["role"], "kind": w["kind"],
                            "date": w.get("date"), "recorded_by": w.get("recorded_by"), "page": w.get("page"),
                            "document": w["doc"], "text": w["text"][:1500]} for w in rows],
            "claims": [{"id": c["id"], "said_by": c["said_by"], "about": c["about"], "topic": c["topic"],
                        "value": c["value"], "when": c.get("when_from"), "where": c.get("where"),
                        "quote": c["quote"][:200], "source": c["source"]} for c in claims][:60],
            "analysis": case.analysis.get(pid or key) if (pid or key) else None,
            "empty": not rows and not claims}
