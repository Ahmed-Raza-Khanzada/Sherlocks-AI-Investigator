"""P13 Section writer: the headings and tables THIS case needs that the fixed sections do not give.

Every report has the same fixed sections (the case at a glance, the brief, the targets, the story, the
network, the record, the analysis, the action plan). A case also has its own shape: a gang's crimes across
many FIRs, the vehicles and weapons on record, money moved in a fraud, the arrests in a kidnapping. The
Section writer reads the finished parts of the report and lays out what else the officer needs as new
sections - each a table (when several items share the same details) or points - and says where each goes.

The model writes labelled lines; code reads them and keeps only what the material holds:

* a row or point whose numbers (FIR numbers, phones, CNICs, dates, amounts) are not in the material is
  dropped, and so is one that names nothing from the material - nothing is guessed;
* a heading that repeats a fixed section is dropped;
* record ids are kept only when they are on the board.

It is written for any kind of case: the prompt names kinds of sections, never one case's facts.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.team.runtime import TERMS, real_refs

logger = logging.getLogger(__name__)

MOST_SECTIONS = 4
MOST_ROWS = 25
MOST_COLUMNS = 6
PLACES = ("targets", "story", "network", "record", "end")

_SYSTEM = (
    "You are Sherlock, a senior detective, adding to a case report for the investigating officer. The report "
    "already has the sections listed in FIXED_SECTIONS. From the CASE_MATERIAL, decide what ELSE the officer needs "
    "to see for THIS case that those sections do not already set out, and lay it out as new sections - up to four, "
    "and only where the material holds enough for one. A good section gathers facts scattered across many records "
    "into one view the officer can act on. Use a table when several items share the same details (one row an item, "
    "one column a detail); points otherwise. Choose headings that fit this case - for example (only when the "
    "material has them): the targets' offences across their FIRs in date order; vehicles, weapons or property on "
    "record and whose; phone numbers and who holds them; places that recur across the records; money moved and "
    "between whom; arrests, recoveries and disclosures by date; reports awaited. "
    "Rules: every row and point comes from the material - copy names, numbers, dates and amounts exactly as the "
    "material gives them, and end with the record id in [brackets] where the material gives one (D#, F#, W#). "
    "Never repeat what a fixed section already holds (FIXED_SECTIONS says what each one holds - the targets' own "
    "phones and addresses, each report's results, the story, the statements are already there): gather facts ACROSS "
    "records instead. Never guess or fill a gap - a value the material does not give is '-', never 'implied' or "
    "'probably'; no view on anyone's guilt, and "
    "never present a SIM owner, a tenancy witness, a landlord or a guarantor as a lead. Plain English; names as "
    "the records write them.\n"
    "Write ONLY lines with these labels:\n"
    "SECTION: heading | one line on why the officer needs it\n"
    "PLACE: where it belongs - after targets, story, network or record, or at the end\n"
    "TABLE: column | column | ... (two to six columns)\n"
    "ROW: value | value | ... (one value a column, at most 25 rows)\n"
    "POINT: a point (instead of a table, or after it)\n"
    "Write NONE alone if the fixed sections already cover everything. No other text. " + TERMS)
_LINE = re.compile(r"^\s*[-*•]?\s*\**(SECTION|PLACE|TABLE|ROW|POINT|NONE)\**\s*\d*\s*:?\s*(.*)$")
_NUMBER = re.compile(r"\d[\d/\-.:]*\d|\d")
_WORD = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_EMPTY = re.compile(r"^(?:-|n/?a|none|nil|unknown|not (?:recorded|given|stated|known|specified|available))\.?$", re.IGNORECASE)
_GUESS = re.compile(r"\b(?:implied|presumably|probably|likely|assumed|appears to be)\b", re.IGNORECASE)
# Words that name the same kind of section: a "chemical findings" table repeats "Forensic and medical findings".
_SAME_KIND = {"chemical": "forensic", "dna": "forensic", "laboratory": "forensic", "lab": "forensic", "medical": "forensic",
              "toxicology": "forensic", "serology": "forensic", "mobile": "phone", "number": "phone", "sim": "phone",
              "chronology": "timeline", "contact": "connection", "associate": "connection"}
_COMMON = {"with", "from", "that", "this", "were", "have", "been", "their", "they", "case", "record", "records",
           "accused", "target", "targets", "unknown", "none", "date", "not", "given", "about", "after", "before"}


def _flat(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _cells(value: str) -> list[str]:
    return [_flat(c) for c in value.split("|")]


def read_sections(text: str, known: set[str]) -> list[dict[str, Any]]:
    """The sections, from the model's labelled lines."""
    out: list[dict[str, Any]] = []
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        label, value = m.group(1), real_refs(m.group(2).strip(), known)
        if label == "NONE":
            continue
        if label == "SECTION":
            heading, _, why = value.partition("|")
            if heading.strip():
                out.append({"heading": _flat(heading).strip("#*: ")[:90], "why": _flat(why), "place": "end",
                            "columns": [], "rows": [], "points": []})
            continue
        if not out:
            continue
        section = out[-1]
        if label == "PLACE":
            word = next((p for p in PLACES if p in value.lower()), "end")
            section["place"] = word
        elif label == "TABLE" and not section["columns"]:
            section["columns"] = [c for c in _cells(value) if c][:MOST_COLUMNS]
        elif label == "ROW" and section["columns"] and len(section["rows"]) < MOST_ROWS:
            cells = _cells(value)
            if len(cells) > len(section["columns"]):          # the extra cells belong to the last column
                cells = [*cells[:len(section["columns"]) - 1], " | ".join(cells[len(section["columns"]) - 1:])]
            section["rows"].append(cells + ["-"] * (len(section["columns"]) - len(cells)))
        elif label == "POINT" and value:
            section["points"].append(_flat(value))
    return out


def _haystack(material: Any) -> tuple[str, set[str]]:
    """The material as one lower-case text, and every run of digits in it (separators dropped)."""
    text = json.dumps(material, ensure_ascii=False, default=str).lower()
    digits = {re.sub(r"\D", "", n) for n in _NUMBER.findall(text)}
    return text, digits


def _date_forms(token: str) -> set[str]:
    """A date in either order: 12-10-2026 is also 2026-10-12."""
    parts = re.split(r"[/\-.]", token)
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return {"".join(parts), "".join(reversed(parts)), "".join(p.zfill(2) for p in parts),
                "".join(p.zfill(2) for p in reversed(parts))}
    return {re.sub(r"\D", "", token)}


def grounded(text: str, hay: tuple[str, set[str]]) -> bool:
    """Is a row or point drawn from the material? Every number of three or more digits in it is in the material
    (a phone, a CNIC, an FIR, a date, an amount), and at least one number or name in it is."""
    body = re.sub(r"\[[A-Z]\d+\]", " ", text)
    words, digits = hay
    found = False
    for token in _NUMBER.findall(body):
        forms = _date_forms(token)
        if len(re.sub(r"\D", "", token)) < 3:
            continue
        if not any(f in digits or any(f in d for d in digits if len(f) >= 6) for f in forms):
            return False
        found = True
    if found:
        return True
    return any(w.lower() not in _COMMON and w.lower() in words for w in _WORD.findall(body))


_SOURCE_COLUMN = re.compile(r"^(source|sources|record|records|ref|reference|from)\b", re.IGNORECASE)
_IDS = re.compile(r"\s*\[([A-Z]\d+(?:\s*,\s*[A-Z]\d+)*)\]")


def _sources_in_place(columns: list[str], rows: list[list[str]]) -> list[list[str]]:
    """A table with a source column gets the record ids the model wrote into other cells there."""
    at = next((i for i, c in enumerate(columns) if _SOURCE_COLUMN.match(c)), None)
    if at is None:
        return rows
    out = []
    for row in rows:
        ids = [m for i, cell in enumerate(row) if i != at for m in _IDS.findall(cell)]
        if ids:
            row = [_IDS.sub("", cell).strip() if i != at else cell for i, cell in enumerate(row)]
            given = "" if row[at] in ("", "-") else row[at] + " "
            row[at] = given + "[" + ", ".join(dict.fromkeys(x.strip() for m in ids for x in m.split(","))) + "]"
        out.append(row)
    return out


def _words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]{2,}", text.lower()):
        w = w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
        if w not in {"the", "and", "for", "case", "of", "on", "in", "their", "its", "from", "with", "this"}:
            out.add(_SAME_KIND.get(w, w))
    return out


def same_heading(a: str, b: str) -> bool:
    """Do two headings name the same section? (plurals and words for the same kind of thing count as one)"""
    wa, wb = _words(a), _words(b)
    return bool(wa) and len(wa & wb) / len(wa) >= 0.5


def _keys(text: str) -> tuple[list[str], set[str]]:
    """What a row is about: its numbers (three digits or more) and its distinctive words."""
    body = re.sub(r"\[[A-Z]\d+(?:,\s*[A-Z]\d+)*\]", " ", text)
    numbers = [n for n in _NUMBER.findall(body) if len(re.sub(r"\D", "", n)) >= 3]
    words = {w.lower() for w in _WORD.findall(body) if w.lower() not in _COMMON}
    return numbers, words


def _repeats(text: str, shown: dict[str, list[tuple[str, set[str]]]]) -> str | None:
    """The section with one ITEM (an exhibit, a contact, a step of the story, an FIR) that already holds this row:
    its numbers (a date in either order) and most of its words. The same people named across many items, or one
    FIR number in many places, is not a repeat. None when the row is new."""
    numbers, words = _keys(text)
    for name, items in shown.items():
        for item_text, digits in items:
            if numbers and not all(any(f in digits or (len(f) >= 5 and any(f in d for d in digits))
                                       for f in _date_forms(n)) for n in numbers):
                continue
            share = sum(w in item_text for w in words) / len(words) if words else 1.0
            if (numbers and share >= 0.6) or (not numbers and len(words) >= 2 and share >= 0.7):
                return name
    return None


def keep(sections: list[dict[str, Any]], material: Any, fixed: list[str],
         shown: dict[str, list[str]] | None = None) -> tuple[list[dict[str, Any]], int]:
    """Only what the material holds, and only what the report does not already show. -> (sections, rows dropped).
    ``shown``: each fixed section's items (its table rows and list points) - a row already there is a repeat and goes,
    and a section that is mostly repeats goes whole, whatever its heading. Each section kept is "shown" for the
    next, so two added sections never hold the same rows either."""
    hay = _haystack(material)
    seen = {name: [_haystack(x) for x in items] for name, items in (shown or {}).items()}
    dropped = 0
    out: list[dict[str, Any]] = []
    for s in sections:
        if any(same_heading(s["heading"], f) for f in fixed) or any(same_heading(s["heading"], o["heading"]) for o in out):
            continue
        rows = [r for r in s["rows"] if grounded(" ".join(r), hay) and not _GUESS.search(" ".join(r))]
        points = [p for p in s["points"] if grounded(p, hay) and not _GUESS.search(p)]
        dropped += len(s["rows"]) - len(rows) + len(s["points"]) - len(points)
        if seen:
            new_rows = [r for r in rows if not _repeats(" ".join(r), seen)]
            new_points = [p for p in points if not _repeats(p, seen)]
            if len(new_rows) + len(new_points) < 0.4 * (len(rows) + len(points)):
                dropped += len(rows) + len(points)
                continue                                 # mostly what the report already shows, under a new heading
            dropped += len(rows) - len(new_rows) + len(points) - len(new_points)
            rows, points = new_rows, new_points
        if len(s["columns"]) < 2:
            rows = []
        rows = _sources_in_place(s["columns"], rows)
        columns = s["columns"]
        # A column empty in every row ("not recorded" throughout) says nothing: it goes.
        keep_at = [i for i in range(len(columns)) if not rows or any(not _EMPTY.match(r[i].strip()) for r in rows)]
        if rows and len(keep_at) < len(columns):
            columns, rows = [columns[i] for i in keep_at], [[r[i] for i in keep_at] for r in rows]
        if len(columns) < 2:
            rows = []
        if rows or points:
            out.append({**s, "columns": columns, "rows": rows, "points": points})
            seen[s["heading"]] = [_haystack(x) for x in [*rows, *points]]
        if len(out) >= MOST_SECTIONS:
            break
    return out, dropped


def write(material: dict[str, Any], fixed: list[str], known: set[str], llm: Any = None,
          holds: dict[str, str] | None = None, shown: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """``{sections: [{heading, why, place, columns, rows, points}], dropped}`` - empty without a model. ``holds``: what
    each fixed section holds, so the writer gathers across them instead of repeating one."""
    if llm is None or not hasattr(llm, "generate_text"):
        return {"sections": [], "dropped": 0}
    listed = {title: (holds or {}).get(title, "") for title in fixed}
    prompt = json.dumps({"FIXED_SECTIONS": listed, "CASE_MATERIAL": material}, ensure_ascii=False, default=str)[:80000]
    try:
        text = llm.generate_text(prompt=prompt, system=_SYSTEM + " " + DATA_NOT_INSTRUCTIONS, cache_kind="report_sections",
                                 prompt_version="v2", max_tokens=3500)
    except Exception as exc:  # noqa: BLE001 - the report goes out with its fixed sections
        logger.info("Section writer failed: %s", exc)
        return {"sections": [], "dropped": 0}
    sections, dropped = keep(read_sections(text, known), material, fixed, shown)
    return {"sections": sections, "dropped": dropped}
