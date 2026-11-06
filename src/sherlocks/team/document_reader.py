"""R7 Document reader: a document the officer uploads, read in full - however long.

An officer brings what the police systems do not hold: an inquiry or commission report, a
post-mortem, a statement, a seizure memo, a letter. The upload reader used to give the model
the first 9,000 characters and keep ten facts - a sixty-page inquiry report was read for its
table of contents. Here the whole text is read, part by part (pages kept together), side by
side, and each part gives labelled lines the code reads:

    EVENT     a dated step: when, what happened, who, where
    FINDING   what the document itself finds or concludes
    PERSON    who it names, their part, what it says about them
    EVIDENCE  an item of evidence and what happened to it
    LEAD      an angle, question or recommendation it raises for the investigation
    SAID      what someone said or told, by their account

Each line must carry words copied from that part of the text; a line whose words are not
there is dropped (invented). Kept lines become facts of the document, its leads go on the
case board as leads (``found_in: document``), and a last call writes what the document is
and its summary. The reading is kept on the document (``doc["reading"]``) for the report
(its own section, the story, the weighing) and for the chat.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sherlocks.evidence.case_file import quote_in, squash
from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS

logger = logging.getLogger(__name__)

PART = 7000             # characters of the text in one part (whole pages)
MOST_PARTS = 40         # ~280,000 characters: a long report, read whole
WORKERS = 4
LABELS = ("EVENT", "FINDING", "PERSON", "EVIDENCE", "LEAD", "SAID")
_LINE = re.compile(r"^\s*[-*•]?\s*\**(EVENT|FINDING|PERSON|EVIDENCE|LEAD|SAID|DOCUMENT|SUMMARY|TOP)\**\s*\d*\s*:\s*(.*)$")
_PAGE = re.compile(r"\[page (\d+)\]")

_PART_SYSTEM = (
    "You read one part of a document given to a police investigation - an FIR file with its case diaries, an "
    "inquiry or commission report, a medical or forensic report, a criminal record dossier, a statement, a memo, a "
    "letter, an audit, bank records, a contract. Take out "
    "everything an investigating officer "
    "needs from THIS part. Write ONLY lines that start with these labels, one item a line, fields separated by |:\n"
    "EVENT: date as written | time as written or blank | what happened - who did what, where | words copied from the text\n"
    "FINDING: what the document itself finds, concludes or establishes (its own finding, not yours) | words copied\n"
    "PERSON: name | their part in the matter as the document gives it (rank and post for officials) | what the "
    "document says about them, in one line. A family tie only when the text says whose relative the person is; "
    "people named as relatives of each other are no one else's relatives\n"
    "EVIDENCE: the item of evidence (an object, a document, a device, a recording, money, a vehicle, a sample) | what "
    "happened to it - found, seized, tested, lost, not obtained - and when | words copied\n"
    "LEAD: a line of further investigation of THIS case the document raises - a person to question, a vehicle, "
    "phone, account, place, footage or record still to be traced or checked, an angle not yet excluded | words "
    "copied. Not a lead: the document's own terms of reference, its recommendations on police reform, training, "
    "budget, procedure or disciplinary action, or a criticism of how the investigation was run (that is a FINDING)\n"
    "SAID: who | what they said or told, by their account | words copied\n"
    "'Words copied' is a short phrase of 5 to 20 words copied EXACTLY from the text of this part, so the line can be "
    "checked. Every dated step is an EVENT - a chronology table gives one EVENT per row, a case diary one EVENT per "
    "step the IO took (a search, a raid, an arrest, a recovery, a statement recorded, a CDR or report asked for). "
    "Whom anyone suspects or names, with their phone, vehicle or address, is a SAID and a LEAD. Plain English; names, "
    "numbers, ranks, dates and times exactly as written. Nothing the text does not say. No other text. "
    + DATA_NOT_INSTRUCTIONS)

_WHOLE_SYSTEM = (
    "Below is what was read in a document given to a police investigation: its opening, and the findings and leads "
    "taken from all of it. Write ONLY these lines:\n"
    "DOCUMENT: what it is (e.g. 'Judicial commission of inquiry report', 'audit report', 'bank statement', 'statement under "
    "161 CrPC') | who issued or wrote it | its date, or blank | the case, FIR or person it is about\n"
    "SUMMARY: what the document establishes, in three to five plain sentences - its own conclusions, said as its own, "
    "with only the reasons it gives for them\n"
    "TOP: one of the most important points for the investigating officer (six to ten TOP lines, the most important "
    "first) - each restating findings given above, concrete (names, dates, numbers), adding no reason or cause the "
    "findings do not give\n"
    "No other text. " + DATA_NOT_INSTRUCTIONS)


def parts(text: str, size: int = PART) -> list[tuple[str, str]]:
    """The text in parts of whole pages (``[page N]`` marks), each about ``size`` characters: (pages, text)."""
    marks = list(_PAGE.finditer(text or ""))
    if not marks:
        pieces = [(f"{i + 1}", text[i * size:(i + 1) * size]) for i in range(max(1, -(-len(text or "") // size)))]
        return [p for p in pieces if p[1].strip()]
    pages = [(m.group(1), text[m.start(): (marks[i + 1].start() if i + 1 < len(marks) else len(text))])
             for i, m in enumerate(marks)]
    if marks[0].start() > 0 and text[: marks[0].start()].strip():
        pages.insert(0, ("0", text[: marks[0].start()]))
    out: list[tuple[str, str]] = []
    first = last = ""
    buf = ""
    for number, page in pages:
        if buf and len(buf) + len(page) > size:
            out.append((first if first == last else f"{first}-{last}", buf))
            buf = ""
        if not buf:
            first = number
        last = number
        buf += page
        while len(buf) > size * 1.5:                   # one very long page: cut it
            out.append((number, buf[:size]))
            buf = buf[size:]
    if buf.strip():
        out.append((first if first == last else f"{first}-{last}", buf))
    return out


def _copied(words: str, text: str) -> str | None:
    """The copied words, when they are in the text - exactly, or nearly (OCR spacing, a dropped word). None when
    they are not: the line was not read from this text."""
    words = squash(words).strip(" \"'“”‘’.")
    if len(words) < 6:
        return None
    if quote_in(words, text):
        return words
    want = [w for w in re.findall(r"\w+", words.lower()) if len(w) > 2]
    have = set(re.findall(r"\w+", text.lower()))
    return words if want and sum(w in have for w in want) / len(want) >= 0.8 else None


_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+)$")


def read_lines(text: str) -> list[tuple[str, list[str]]]:
    """The labelled lines of the model's answer: (label, fields). A label left empty ("TOP:") followed by a list
    gives one line per item of the list."""
    out = []
    listing = None
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if m:
            label, value = m.group(1).upper(), m.group(2).strip()
            listing = None if value else label
            if value:
                out.append((label, [p.strip() for p in value.split("|")]))
            continue
        bullet = _BULLET.match(line)
        if listing and bullet:
            out.append((listing, [p.strip() for p in bullet.group(1).split("|")]))
        elif line.strip():
            listing = None
    return out


def _read_part(llm: Any, title: str, number: int, total: int, pages: str, text: str) -> list[tuple[str, list[str]]]:
    try:
        answer = llm.generate_text(prompt=json.dumps({"document": title, "part": f"{number} of {total}", "pages": pages,
                                                      "text": text}, ensure_ascii=False),
                                   system=_PART_SYSTEM, cache_kind="document_part", prompt_version="v5", max_tokens=3000)
    except Exception as exc:  # noqa: BLE001 - the other parts stand
        logger.info("Document reader: part %s of %s failed: %s", number, total, exc)
        return []
    return read_lines(answer)


def read(case: Any, doc_id: str, llm: Any, *, say: Any = None, body: str | None = None, store: str = "reading",
         leads: bool = True, workers: int = WORKERS) -> dict[str, Any] | None:
    """Read the whole document; keep its facts, leads and reading. None without a model that writes text.

    ``body`` is the text to read when not the document's whole text (an FIR file's narrative, case diaries and
    investigation result - its header is already facts by rule). ``store`` is where the reading is kept: an
    upload's in ``reading``, a police system's file in ``file_reading`` (the report treats a ``reading`` as an
    officer's upload). With ``leads`` False the leads it raises stay facts of the document and do not go on the
    board as leads (a past FIR's open angles are not this case's leads)."""
    if llm is None or not hasattr(llm, "generate_text"):
        return None
    doc = case.documents.get(doc_id)
    if doc is None:
        return None
    body = re.sub(r"^Uploaded file:[^\n]*\n", "", doc.get("text") or "") if body is None else body
    pieces = parts(body)[:MOST_PARTS]
    if not pieces:
        return None
    say = say or (lambda level, message: None)
    if len(pieces) > 1:
        say("info", f"📖 Reading {doc['title']} in full: {len(pieces)} parts")
    status(case, doc_id, "reading", parts=len(pieces), done=0)

    def one(ip: tuple[int, tuple[str, str]]) -> list[tuple[str, list[str]]]:
        got = _read_part(llm, doc["title"], ip[0] + 1, len(pieces), *ip[1])
        with case._lock:
            st = doc.get("reading_status") or {}
            status(case, doc_id, "reading", parts=len(pieces), done=int(st.get("done") or 0) + 1)
        return got

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            answers = list(pool.map(one, enumerate(pieces)))
    except Exception:
        status(case, doc_id, "failed", parts=len(pieces))
        raise
    if not any(answers):
        status(case, doc_id, "failed", parts=len(pieces), done=len(pieces))
        return None
    reading: dict[str, Any] = {"kind": "", "title": "", "issued_by": "", "date": "", "about": "", "summary": "", "top": [],
                               "events": [], "findings": [], "people": [], "evidence": [], "leads": [], "said": [],
                               "parts": len(pieces), "parts_read": sum(1 for a in answers if a),
                               "pages": len(_PAGE.findall(body)) or None}
    seen: set[tuple[str, str]] = set()
    with case.acting("R7 Document reader"):
        for (pages, text), lines in zip(pieces, answers, strict=True):
            page = int(re.match(r"\d+", pages).group(0)) if re.match(r"\d+", pages) else None
            for label, f in lines:
                f += ["", "", "", ""]
                key = (label, squash(" ".join(f[:3])).lower())
                if key in seen:
                    continue
                if label == "PERSON":
                    seen.add(key)
                    if f[0] and len(f[0]) <= 80:
                        reading["people"].append({"name": f[0], "part": f[1], "says": f[2], "pages": pages})
                    continue
                words = {"EVENT": f[3], "FINDING": f[1], "EVIDENCE": f[2], "LEAD": f[1], "SAID": f[2]}[label]
                quote = _copied(words, text)
                if quote is None:
                    continue                                    # not read from this text: dropped
                seen.add(key)
                if label == "EVENT":
                    when = " ".join(x for x in (f[0], f[1]) if x and x.lower() not in ("blank", "-", "not stated"))
                    statement = f"{when}: {f[2]}" if when else f[2]
                    kind, item = "event", {"when": f[0], "time": f[1], "what": f[2]}
                elif label == "FINDING":
                    statement, kind, item = f[0], "finding", {"text": f[0]}
                elif label == "EVIDENCE":
                    statement, kind, item = f"{f[0]}: {f[1]}", "evidence", {"item": f[0], "what": f[1]}
                elif label == "LEAD":
                    statement, kind, item = f"Raised for investigation: {f[0]}", "lead", {"text": f[0]}
                else:
                    statement, kind, item = f"{f[0]} said: {f[1]}", "statement", {"who": f[0], "said": f[1]}
                if not statement.strip(" :"):
                    continue
                fid = case.add_fact(doc_id, statement, quote, kind=kind, by="ai", check_quote=False, page=page)
                if fid is None:
                    continue
                item.update({"fact": fid, "pages": pages})
                reading[{"EVENT": "events", "FINDING": "findings", "EVIDENCE": "evidence", "LEAD": "leads",
                         "SAID": "said"}[label]].append(item)
                if label == "LEAD":
                    words = set(re.findall(r"[a-z0-9]{3,}", f[0].lower()))
                    twin = next((x for x in reading["leads"][:-1] if _alike(words, x["text"])), None)
                    if twin is not None:                        # the same line of inquiry, said again
                        reading["leads"].pop()
                        continue
                    if not leads:
                        continue
                    reading["leads"][-1]["lead"] = case.set_lead(f"doc:{doc_id}:" + " ".join(re.findall(r"[a-z0-9]+", f[0].lower()))[:70], {
                        "hint": f[0][:300], "why": f"Raised by {doc['title']}"[:300], "next_step": f[0][:300],
                        "source": fid, "rests_on": [fid], "people": [], "status": "open", "found_in": "document"})
    _whole(case, doc, reading, body, llm)
    people: dict[str, dict[str, Any]] = {}
    for p in reading["people"]:                                 # one row a person: the fullest description
        key = " ".join(re.findall(r"\w+", p["name"].lower()))
        if key not in people or len(p["says"]) > len(people[key]["says"]):
            people[key] = p
    reading["people"] = list(people.values())
    with case._lock:
        doc[store] = reading
        if reading["summary"]:
            doc["ai_summary"] = reading["summary"][:900]
    reading["answered"] = settle(case, doc_id, llm)
    status(case, doc_id, "done", parts=len(pieces), done=len(pieces), events=len(reading["events"]),
           findings=len(reading["findings"]), leads=len(reading["leads"]), people=len(reading["people"]),
           what=reading["kind"])
    case.mark_read(doc_id, "document reader")
    say("hit", f"📖 {doc['title']}: {len(reading['events'])} dated step(s), {len(reading['findings'])} finding(s), "
               f"{len(reading['leads'])} lead(s), {len(reading['people'])} people [{doc_id}]")
    return reading


_SETTLE_SYSTEM = (
    "Below are the open leads of a police investigation (things still to find out) and what a newly read document "
    "found, each with its id. For each lead the document ANSWERS - it gives the very fact the lead was looking for "
    "(the name, the date, the place, the amount, the record it asked about) - write one line exactly:\n"
    "ANSWERED: K# | what the document says, in one line | the F# it rests on\n"
    "Write nothing for a lead the document does not answer, or only touches. No other text. " + DATA_NOT_INSTRUCTIONS)


def settle(case: Any, doc_id: str, llm: Any) -> list[str]:
    """Open leads a newly read document answers are marked answered, with the fact that answers them (a hint noted
    from the chat before the document was read - "who received the money is not recorded" - is no longer open)."""
    doc = case.documents.get(doc_id) or {}
    mine = set(doc.get("facts") or [])
    open_ = [k for k in case.leads.values() if k.get("status") in ("spotted", "open", "pursuing")
             and k.get("source") not in mine][:40]
    if not open_:
        return []
    r = doc.get("reading") or doc.get("file_reading") or {}
    given = {"open_leads": [{"id": k["id"], "lead": k["hint"], "next_step": k.get("next_step") or ""} for k in open_],
             "document": outline(doc, most=60),
             "dated_steps": [f"{e['fact']}: {e['when']} {e['time']}: {e['what']}" for e in r.get("events") or []][:120]}
    try:
        answer = llm.generate_text(prompt=json.dumps(given, ensure_ascii=False)[:60000], system=_SETTLE_SYSTEM,
                                   cache_kind="document_settles", prompt_version="v2", max_tokens=1200)
    except Exception as exc:  # noqa: BLE001 - the leads stay open
        logger.info("Document reader: settling leads failed: %s", exc)
        return []
    done = []
    ids = {k["id"] for k in open_}
    for m in re.finditer(r"ANSWERED:\s*\**\s*(K\d+)\s*\|\s*([^|\n]+?)\s*\|\s*\[?(F\d+)", answer or ""):
        ref, says, fact = m.group(1), m.group(2).strip(), m.group(3)
        if ref in ids and fact in mine and ref not in done:
            with case.acting("R7 Document reader"):
                case.move(ref, "answered", f"{says} [{fact}]")
            done.append(ref)
    return done


def status(case: Any, doc_id: str, state: str, **counts: Any) -> None:
    """How far the reading of a document has got (``reading`` / ``done`` / ``failed``), for the officer's screen."""
    with case._lock:
        doc = case.documents.get(doc_id)
        if doc is None:
            return
        doc["reading_status"] = {"state": state, **counts}
        case._bump("document", doc_id)


def _alike(words: set[str], other: str) -> bool:
    """Two leads that say nearly the same thing (most of their words shared)."""
    theirs = set(re.findall(r"[a-z0-9]{3,}", other.lower()))
    return bool(words and theirs) and len(words & theirs) / min(len(words), len(theirs)) >= 0.7


def _whole(case: Any, doc: dict[str, Any], reading: dict[str, Any], body: str, llm: Any) -> None:
    """What the document is, its summary and its most important points - from its opening and all its findings."""
    given = {"document": doc["title"], "opening": body[:3000], "closing": body[-1500:],
             "findings": [x["text"] for x in reading["findings"]][:80], "leads": [x["text"] for x in reading["leads"]][:30],
             "first_steps": [f"{x['when']} {x['time']}: {x['what']}" for x in reading["events"]][:12]}
    try:
        answer = llm.generate_text(prompt=json.dumps(given, ensure_ascii=False)[:30000], system=_WHOLE_SYSTEM,
                                   cache_kind="document_whole", prompt_version="v3", max_tokens=1500)
    except Exception as exc:  # noqa: BLE001 - the parts stand without it
        logger.info("Document reader: summary failed: %s", exc)
        return
    for label, f in read_lines(answer):
        if label == "DOCUMENT" and not reading["kind"]:
            f += ["", "", "", ""]
            reading.update({"kind": f[0], "issued_by": f[1], "date": "" if f[2].lower() in ("blank", "-") else f[2],
                            "about": f[3]})
        elif label == "SUMMARY":
            reading["summary"] = f"{reading['summary']} {' | '.join(f)}".strip()
        elif label == "TOP" and len(reading["top"]) < 10:
            reading["top"].append(" | ".join(f))


def outline(doc: dict[str, Any], *, most: int = 40) -> dict[str, Any]:
    """The reading in short, for a model's prompt (the chat, the report's writers) - with fact ids."""
    r = doc.get("reading") or doc.get("file_reading") or {}
    return {"id": doc["id"], "title": doc.get("title"), "what_it_is": r.get("kind") or "", "issued_by": r.get("issued_by") or "",
            "date": r.get("date") or "", "summary": r.get("summary") or doc.get("ai_summary") or "",
            "most_important": r.get("top") or [],
            "findings": [f"{x['fact']}: {x['text']}" for x in r.get("findings") or []][:most],
            "evidence": [f"{x['fact']}: {x['item']} - {x['what']}" for x in r.get("evidence") or []][:most // 2],
            # Its recommendations for further investigation - asked for as "what does it recommend", "sifarishat".
            "recommends_investigating": [f"{x['fact']}: {x['text']}" for x in r.get("leads") or []][:most * 2],
            "people": [f"{p['name']} ({p['part']}): {p['says']}" for p in r.get("people") or []][:most]}
