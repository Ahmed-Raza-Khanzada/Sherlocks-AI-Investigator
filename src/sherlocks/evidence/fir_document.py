"""Read a PSRMS FIR file report (``Criminal_api/firfilereport``) into structured data.

The report is the printable FIR page: Urdu labels, a numbered "PrintableFirTbl" (the
complainant, the charges, the place of occurrence), the registering officer's footer,
the first-information narrative, and one ``trHeading`` + ``innerTable`` pair per
section - case positions, investigating officers, nominated accused, witnesses, stolen
property, the index of case diaries (ضمنیات) with each diary's remarks, and the
investigation result.

Everything is read by label, never by position alone: rows move between FIR types.
``text`` is the whole document as "label: value" lines - what agents quote from, so a
quote can always be checked against the document.
"""

from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, Tag

from sherlocks.linkgraph.normalize import cnic13, mobile11

_SPACE = re.compile(r"\s+")
_CNIC = re.compile(r"\d{5}-?\d{7}-?\d")
_PHONE = re.compile(r"(?<!\d)(?:\+?92|0)3\d{2}[\s-]?\d{7}(?!\d)")

# Section headings (``tr.trHeading``) -> key. Matched by substring.
SECTIONS = {
    "پوزیشن مقدمہ": "case_positions",
    "تفتیشی افسران": "investigating_officers",
    "نامزد ملزمان": "nominated_suspects",
    "نامعلوم ملزمان": "unknown_suspects",
    "گرفتار ملزمان": "arrested_suspects",
    "گواہان": "witnesses",
    "ضامن": "guarantors",
    "مسروقہ مال": "stolen_property",
    "برآمدگی": "recoveries",
    "انڈیکس ضمنیات": "case_diaries",
    "نتیجہ تفتیش": "investigation_result",
}

# The English names the report reader shows for each section.
SECTION_TITLES = {
    "case_positions": "Case position", "investigating_officers": "Investigating officers",
    "nominated_suspects": "Nominated accused", "unknown_suspects": "Unknown accused",
    "arrested_suspects": "Arrested accused", "witnesses": "Witnesses", "guarantors": "Guarantors",
    "stolen_property": "Stolen property", "recoveries": "Recoveries", "case_diaries": "Case diaries",
}

# Column headers -> normalised field, by substring (Urdu first, English for good measure).
_COLUMNS = [
    ("cnic", ("شناختی", "cnic")),
    ("father", ("ولدیت", "والد", "father")),
    ("phone", ("موبائل", "فون", "phone", "mobile")),
    ("address", ("سکونت", "پتہ", "address")),
    ("police_station", ("رہائشی تھانہ", "تھانہ")),
    ("name", ("نام", "name")),
    ("rank", ("عہدہ", "rank")),
    ("number", ("نمبر",)),
    ("date", ("تاریخ", "date")),
    ("position", ("پوزیشن",)),
    ("remarks", ("کیفیت", "remarks")),
    ("value", ("مالیت",)),
    ("description", ("تفصیل",)),
    ("duration", ("عرصہ",)),
]

# Rows of the numbered FIR table, by the row-ID cell.
_ROW_IDS = {"1": "reported", "2": "complainant", "3": "offence", "4": "place", "5": "delay", "6": "dispatched"}


def _clean(text: Any) -> str:
    return _SPACE.sub(" ", str(text or "").replace("\xa0", " ")).strip()


def _text(tag: Tag | None) -> str:
    return _clean(" ".join(tag.stripped_strings)) if tag is not None else ""


def _column(header: str) -> str:
    h = header.lower()
    if "نام" in h and ("ولد" in h or "father" in h):
        return "father"
    for key, needles in _COLUMNS:
        if any(n in h for n in needles):
            if key == "number" and "شمار" in h:
                return "serial"
            if key == "number" and "شناختی" in h:
                return "cnic"
            return key
    return h or "detail"


def _person_fields(row: dict[str, str]) -> dict[str, str]:
    """A table row with CNIC / phone found by shape wherever they sit."""
    out = dict(row)
    joined = " ".join(row.values())
    if not out.get("cnic") and (m := _CNIC.search(joined)):
        out["cnic"] = m.group(0)
    if not out.get("phone") and (m := _PHONE.search(joined)):
        out["phone"] = m.group(0)
    if out.get("cnic"):
        out["cnic"] = cnic13(out["cnic"]) or out["cnic"]
    if out.get("phone"):
        out["phone"] = mobile11(out["phone"]) or out["phone"]
    return {k: v for k, v in out.items() if v}


def parse_complainant(text: str) -> dict[str, str]:
    """"<name> ولد <father>، پتہ : <address>، پیشہ : <occupation> شناختی کارڈ نمبر : <cnic> فون نمبر : <phone>"."""
    text = _clean(text)
    out: dict[str, str] = {"raw": text}
    if m := _CNIC.search(text):
        out["cnic"] = cnic13(m.group(0)) or m.group(0)
    if m := _PHONE.search(text):
        out["phone"] = mobile11(m.group(0)) or m.group(0)
    head = re.split(r"شناختی کارڈ|فون نمبر", text)[0]
    parts = re.split(r"\s+(ولد|ولدیت|بنت|زوجہ|دختر|s/o|d/o|w/o)\s+", head, maxsplit=1, flags=re.IGNORECASE)
    out["name"] = _clean(parts[0].split("،")[0].split("پتہ")[0])
    if len(parts) == 3:
        out["relation"] = parts[1]
        out["father"] = _clean(re.split(r"[،,]|پتہ", parts[2])[0])
    if m := re.search(r"پتہ\s*:?\s*(.+?)(?:[،,]?\s*پیشہ|$)", head):
        out["address"] = _clean(m.group(1)).strip("، ")
    if m := re.search(r"پیشہ\s*:?\s*(.+)$", head):
        out["occupation"] = _clean(m.group(1)).strip("، ")
    return {k: v for k, v in out.items() if v}


def _numbered_rows(scope: Tag) -> dict[str, str]:
    """The numbered FIR table: row-ID cell -> the value cell(s) of that row."""
    out: dict[str, str] = {}
    table = scope.find("table", class_="PrintableFirTbl") or scope.find("table", id="PrintableFirTbl")
    if table is None:
        return out
    for tr in table.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        ids = [i for i, td in enumerate(cells) if "row-ID" in (td.get("class") or [])]
        start = 0
        for i in ids:
            number = _text(cells[i])
            # The cells before a row-ID: the value(s), then the Urdu label (last).
            span = cells[start:i]
            start = i + 1
            if not span:
                continue
            value_cells = span[:-1] or span
            out[_ROW_IDS.get(number, number)] = _clean(" ".join(_text(td) for td in value_cells))
    return out


def _offence(scope: Tag) -> tuple[str, str]:
    """Sections of law, and the short description (property etc.)."""
    div = scope.find(id="sectionDiv")
    if div is None:
        return "", ""
    labels = [lab for lab in div.find_all("label") if _text(lab)]
    texts = []
    for lab in labels:
        parts = [_clean(s) for s in lab.stripped_strings if _clean(s)]
        texts.append(parts)
    sections: list[str] = []
    rest: list[str] = []
    seen_head = False
    for parts in texts:
        joined = " ".join(parts)
        if "بجرم" in joined:
            seen_head = True
            continue
        if seen_head and not sections:
            sections = parts
        else:
            rest.append(joined)
    return ", ".join(sections), " ".join(rest)


def _footer_officer(scope: Tag, soup: BeautifulSoup) -> dict[str, str]:
    """The registering officer: a row of "value | : label" pairs."""
    labels = {"ٹیلی فون": "phone", "عہدہ": "rank", "بیلٹ": "belt", "دستخط": "name"}
    for tr in soup.find_all("tr"):
        cells = [_text(td) for td in tr.find_all("td", recursive=False)]
        if not any("دستخط" in c for c in cells) or not any("عہدہ" in c for c in cells):
            continue
        out: dict[str, str] = {}
        for i, cell in enumerate(cells[1:], 1):
            key = next((k for needle, k in labels.items() if needle in cell), None)
            # The label cell reads ": عہدہ"; its value is the cell before it (RTL layout).
            if key and cell.startswith(":") and cells[i - 1] and not cells[i - 1].startswith(":"):
                out.setdefault(key, cells[i - 1])
        if out:
            if out.get("phone"):
                out["phone"] = mobile11(out["phone"]) or out["phone"]
            return out
    return {}


def _section_tables(soup: BeautifulSoup) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for heading in soup.find_all("tr", class_="trHeading"):
        title = _text(heading)
        key = next((v for k, v in SECTIONS.items() if k in title), None)
        wrapper = heading.find_parent("table")
        table = wrapper.find_next_sibling("table") if wrapper is not None else None
        if key is None or table is None:
            continue
        if key == "investigation_result":
            out[key] = _text(table)
            continue
        header_row = table.find("tr", class_="innerTr")
        headers = [_text(td) for td in header_row.find_all(["td", "th"])] if header_row else []
        rows: list[dict[str, str]] = []
        for tr in table.find_all("tr"):
            if tr is header_row:
                continue
            cells = tr.find_all(["td", "th"], recursive=False)
            texts = [_text(td) for td in cells]
            if not any(texts):
                continue
            if key == "case_diaries" and len(cells) == 1 and rows:
                # The remarks row under a diary entry.
                remark = texts[0]
                remark = re.sub(r"^ریمارکس\s*:?\s*", "", remark)
                rows[-1]["remarks"] = _clean(rows[-1].get("remarks", "") + " " + remark)
                continue
            row: dict[str, str] = {}
            for i, value in enumerate(texts):
                header = headers[i] if i < len(headers) else f"detail {i + 1}"
                row[header] = value
            rows.append(row)
        if key == "case_diaries":
            out[key] = [{"no": next(iter(r.values()), ""), "date": list(r.values())[1] if len(r) > 1 else "",
                         "officer": list(r.values())[2] if len(r) > 2 else "", "remarks": r.get("remarks", "")}
                        for r in rows]
        else:
            out[key] = rows
    return out


def normalise_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Urdu-headed rows -> {name, father, cnic, phone, address, ...}."""
    out = []
    for row in rows:
        norm: dict[str, str] = {}
        for header, value in row.items():
            key = _column(header)
            if value and key not in norm:
                norm[key] = value
        norm.pop("serial", None)
        out.append(_person_fields(norm))
    return out


def parse_fir_html(html: str, *, fir_no: str = "", fir_year: str = "", ps_id: str = "") -> dict[str, Any]:
    """The FIR report as a dict; see the module docstring. Never raises on odd markup -
    missing parts are simply absent."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "img", "link", "meta"]):
        tag.decompose()
    scope = soup.select_one("#inboxContent") or soup

    header: dict[str, str] = {}
    for li in soup.select(".view-fir li"):
        line = _text(li)
        if ":" in line:
            label, value = (_clean(x) for x in line.split(":", 1))
            header[label] = value
    number = header.get("نمبر", "")
    if not (fir_no and fir_year) and "/" in number:
        fir_no, fir_year = (x.strip() for x in number.split("/", 1))
    occurred = header.get("تاریخ ووقت وقوعہ") or header.get("تاریخ و وقت وقوعہ") or ""

    rows = _numbered_rows(scope)
    sections, offence = _offence(scope)
    complainant = parse_complainant(rows.get("complainant", ""))
    reported = rows.get("reported", "")
    narrative = _text(soup.find(id="ibtadayi_itla"))
    created = ""
    for div in soup.find_all("div"):
        t = _text(div)
        if t.startswith("Created By") and len(t) < 120:
            created = _clean(t.split(":", 1)[-1])
            break
    tables = _section_tables(soup)

    doc: dict[str, Any] = {
        "fir_no": str(fir_no or "").strip(), "fir_year": str(fir_year or "").strip(), "ps_id": str(ps_id or ""),
        "serial": header.get("سیریل نمبر", ""), "police_station": header.get("تھانہ", ""),
        "district": header.get("ضلع", ""), "occurred": occurred,
        "reported": _clean(re.sub(r"\(\s*\d+\s*\)\s*بحوالہ رپٹ نمبر", "", reported)) or reported,
        "dispatched": rows.get("dispatched", ""),
        "complainant": complainant, "sections": sections, "offence": offence,
        "place": rows.get("place", ""), "delay": rows.get("delay", ""),
        "registered_by": _footer_officer(scope, soup), "narrative": narrative, "created_by": created,
        "investigation_result": tables.pop("investigation_result", ""),
        "case_diaries": tables.pop("case_diaries", []),
    }
    for key, raw_rows in tables.items():
        doc[key] = normalise_rows(raw_rows)
        doc[f"{key}_raw"] = raw_rows
    doc["text"] = document_text(doc)
    return doc


def _person_line(p: dict[str, str]) -> str:
    bits = [p.get("name") or p.get("description") or ""]
    if p.get("father"):
        bits.append(f"ولد {p['father']}")
    for key, label in (("rank", ""), ("cnic", "CNIC"), ("phone", "Phone"), ("address", "Address"),
                       ("date", "Date"), ("position", ""), ("value", "Value"), ("remarks", "")):
        if p.get(key):
            bits.append(f"{label} {p[key]}".strip())
    return " · ".join(b for b in bits if b)


def station(doc: dict[str, Any]) -> str:
    """"PS Gulshan-e-Iqbal" however the report spells it."""
    ps = _clean(doc.get("police_station") or "")
    if not ps:
        return f"PS id {doc.get('ps_id') or '-'}"
    return ps if ps.upper().startswith("PS ") else f"PS {ps}"


def document_text(doc: dict[str, Any]) -> str:
    """The FIR as quotable lines."""
    lines = [f"FIR {doc.get('fir_no')}/{doc.get('fir_year')} · {station(doc)} · "
             f"District {doc.get('district') or '-'} · Serial {doc.get('serial') or '-'}"]
    for label, key in (("Occurred", "occurred"), ("Reported", "reported"), ("Sections", "sections"),
                       ("Offence / property", "offence"), ("Place of occurrence", "place"),
                       ("Delay / action", "delay")):
        if doc.get(key):
            lines.append(f"{label}: {doc[key]}")
    c = doc.get("complainant") or {}
    if c:
        lines.append("Complainant: " + _person_line(c) + (f" · Occupation {c['occupation']}" if c.get("occupation") else ""))
    r = doc.get("registered_by") or {}
    if r:
        lines.append("Registered by: " + " · ".join(f"{k} {v}" for k, v in r.items()))
    if doc.get("narrative"):
        lines.append(f"First information (narrative): {doc['narrative']}")
    for key, title in SECTION_TITLES.items():
        if key == "case_diaries":
            continue
        for i, p in enumerate(doc.get(key) or [], 1):
            lines.append(f"{title} {i}: {_person_line(p)}")
    for d in doc.get("case_diaries") or []:
        lines.append(f"Case diary {d.get('no')} ({d.get('date')}, {d.get('officer')}): {d.get('remarks')}")
    if doc.get("investigation_result"):
        lines.append(f"Investigation result: {doc['investigation_result']}")
    if doc.get("created_by"):
        lines.append(f"Entered by: {doc['created_by']}")
    return "\n".join(lines)


def roster(doc: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Everyone the FIR names, by role - what the link graph adds as people."""
    out: dict[str, list[dict[str, str]]] = {}
    if (doc.get("complainant") or {}).get("name"):
        out["complainant"] = [doc["complainant"]]
    for key in ("nominated_suspects", "arrested_suspects", "witnesses", "guarantors", "investigating_officers"):
        rows = [r for r in doc.get(key) or [] if r.get("name")]
        if rows:
            out[key] = rows
    return out
