"""O4 Case reader: the whole board, read before any answer.

An answer used to start from what a search for the question's words found - so an answer
could come from an old FIR while the reports that settle the question, filed under another
FIR, were never in front of the writer.
So before every reply the board is read as a whole, by code - nothing is skipped and no
model is waited on:

* the case being worked: what the officer said, and the FIR that IS this case, if any;
* every FIR of each target - those the police systems list them in, and the FIR files that
  name them by CNIC or phone (a victim named in a relative's FIR) - each with
  what happened, where it stands, and its medical and laboratory reports with what was
  found in them;
* any other report or document about a target that belongs to no FIR;
* every document the officer uploaded, as read in full (``document_reader``): what it is, its
  dated steps, findings, leads and the people it names.

The reading is kept on the board (``case.dialog["case_read"]``) with the board's version,
and read again only when the board has changed. O1, Sherlock's view and the Answer checker
are given it first.
"""

from __future__ import annotations

import re
from typing import Any

MOST_FIRS = 12
MOST_REPORTS = 6
MOST_STEPS = 10           # an FIR's dated investigation steps given every turn (the rest: its FIR file)
MOST_DOC_ITEMS = 30         # an uploaded document's findings, leads and people given to the chat
MOST_DOC_STEPS = 80         # ... and its dated steps (an inquiry's chronology)


def _number(text: str) -> tuple[str, str]:
    m = re.search(r"(\d{1,5})\s*/\s*(\d{2,4})", str(text or ""))
    return (m.group(1).lstrip("0"), m.group(2)[-2:]) if m else ("", "")


def _reports(case: Any, number: tuple[str, str]) -> list[dict[str, Any]]:
    from sherlocks.team.officer_agent import page_line

    out = []
    for doc in case.documents.values():
        ref = doc.get("ref") or {}
        if doc.get("kind") == "fir" or _number(ref.get("labs_of") or ref.get("fir") or "") != number:
            continue
        facts = [f for f in (case.facts.get(i) for i in doc.get("facts") or [])
                 if f and not page_line(f, doc) and case.usable(f["id"]) and f.get("by") != "rule"]
        out.append({"id": doc["id"], "report": doc["title"],
                    "found": (doc.get("ai_summary") or doc.get("summary") or "")[:400],
                    "findings": [f"{f['id']}: {f['statement'][:180]}" for f in facts][:14]})
    return out[:MOST_REPORTS]


def _stands(data: dict[str, Any]) -> str:
    positions = [p for p in data.get("case_positions") or [] if isinstance(p, dict)]
    if positions:
        last = positions[-1]
        return f"{last.get('position') or last.get('status') or ''} ({last.get('date') or ''})".strip()
    return ""


def read(case: Any, net: Any) -> dict[str, Any]:
    """The board read as a whole, for the targets."""
    from sherlocks.linkgraph.case_queries import _firs
    from sherlocks.linkgraph.relevance import the_new_case

    inc = case.incident or {}
    this = _number(inc.get("fir") or "") if case.dialog.get("case_basis") == "fir_on_record" else ("", "")
    targets = []
    for pid in net.seeds():
        firs, under_fir = [], set()
        rows = _firs(net, pid, case)[:MOST_FIRS]
        for row in rows:
            number = _number(row["label"])
            doc = case.documents.get(row.get("doc") or "") or {}
            data = doc.get("data") or {}
            reports = _reports(case, number)
            under_fir |= {r["id"] for r in reports}
            firs.append({
                "fir": f"FIR {row['label']}" + (f", PS {row['ps']}" if row.get("ps") else ""),
                "is_this_case": bool(this[0]) and number == this,
                "their_part": ", ".join(row["roles"]),
                "offence": row.get("offence") or data.get("sections") or "",
                "occurred": row.get("occurred") or "",
                "what_happened": (doc.get("ai_summary") or "")[:400],
                # The record's own words: a summary can be wrong (who was last with him, when, how).
                "first_information": str(data.get("narrative") or "")[:900] or (doc.get("text") or "")[:900],
                "where_it_stands": _stands(data),
                # The IO's dated steps, as the full reader took them out of every case diary.
                "investigation_steps": [f"{e['fact']}: {e['when']}: {e['what']}"[:220]
                                        for e in (doc.get("file_reading") or {}).get("events") or []
                                        if case.usable(e["fact"])][:MOST_STEPS if len(rows) <= 3 or (bool(this[0]) and number == this) else 0],
                "file": doc.get("id"),
                "reports": reports})
        other = [{"id": d["id"], "title": d["title"], "says": (d.get("ai_summary") or d.get("summary") or "")[:300]}
                 for d in case.for_person(pid)["documents"]
                 if d["id"] not in under_fir and (case.documents.get(d["id"]) or {}).get("kind") not in ("fir", "graph", "notes")]
        targets.append({"name": net.name(pid), "firs": firs, "other_documents": other[:6]})
    return {"the_case": the_new_case(case), "targets": targets, "documents_uploaded": uploaded(case),
            "how_to_use": "Read this first: it is everything on the board about the targets. An FIR marked "
                          "is_this_case and its reports are the case being worked; the others are past records. "
                          "documents_uploaded are documents the officer gave (an inquiry report, a medical report, an audit, bank records, a "
                          "memo), read in full: their dated steps, findings, what they recommend investigating and the people they "
                          "name, each with its id."}


def uploaded(case: Any) -> list[dict[str, Any]]:
    """The documents the officer uploaded, as the Document reader read them in full (R7)."""
    from sherlocks.team.document_reader import outline

    out = []
    for doc in case.documents.values():
        r = doc.get("reading")
        if not r:
            if doc.get("kind") == "upload":
                # Read by the short reader (before the whole-document reader, or with no plain-text model): what it
                # holds is still given - its summary and the facts taken from it.
                out.append({"id": doc["id"], "title": doc.get("title"), "summary": doc.get("ai_summary") or doc.get("summary") or "",
                            "facts": [f"{f['id']}: {f['statement']}" for f in (case.facts.get(i) for i in doc.get("facts") or [])
                                      if f and case.usable(f["id"])][:MOST_DOC_ITEMS],
                            "read": "in part - upload it again to have it read in full"})
            continue
        o = outline(doc, most=MOST_DOC_ITEMS)
        o["dated_steps"] = [f"{e['fact']}: {e['when']} {e['time']}: {e['what']}".replace("  ", " ")
                            for e in r.get("events") or [] if case.usable(e["fact"])][:MOST_DOC_STEPS]
        out.append(o)
    return out[:4]


def ensure(case: Any, net: Any) -> dict[str, Any]:
    """The reading, made again only when the board has changed since."""
    kept = case.dialog.get("case_read") or {}
    if kept.get("version") == case.version and kept.get("reading"):
        return kept["reading"]
    reading = read(case, net)
    case.dialog["case_read"] = {"version": case.version, "reading": reading}
    return reading
