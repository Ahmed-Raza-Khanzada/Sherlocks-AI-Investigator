"""Synthetic case documents for the demo world - nothing leaves the machine.

The demo FIRs (``demo_data.FIRS``) get a file report in the real PSRMS markup, FIR
45/2023 gets a DNA and a chemical examiner report (PDFs with a text layer), and every
demo CRO number gets a dossier PDF with three poses and a fingerprint card. The story
the documents tell links the demo people the way an investigator would want to find:
the DNA on the recovered cloth matches Sajid, the case diary names Tariq's number as
the one the accused called, and Bilal identified Kamran.
"""

from __future__ import annotations

import base64
import io
from typing import Any

from sherlocks.evidence.fir_document import parse_fir_html, roster
from sherlocks.linkgraph.demo_data import FIRS, PEOPLE, _avatar_png
from sherlocks.linkgraph.normalize import dashed_cnic

_DIARIES = {
    ("45", "2023"): [
        ("01", "02-03-2023", "SI Zahid Iqbal",
         "Accused Sajid Mehmood was arrested near Rashid Minhas Road. A blood-stained shirt (Item 3) and a "
         "mobile phone were recovered from him. The phone's call record shows repeated calls to 03990000301 "
         "on the night of the occurrence."),
        ("02", "05-03-2023", "SI Zahid Iqbal",
         "Accused Sajid Mehmood stated that Kamran Ahmed planned the robbery and arranged the motorcycle "
         "KDE-1234. Item 3 was sent to the DNA lab with reference samples."),
    ],
    ("112", "2024"): [
        ("01", "11-06-2024", "SI Zahid Iqbal",
         "Witness Bilal Khan identified accused Kamran Ahmed as the person who issued the dishonoured cheque."),
    ],
}

_LABS = {
    ("45", "2023"): [
        ("DNA", "LAB-90001", "DNA Lab Karachi University", "10-03-2023 11:51:33",
         ["DNA ANALYSIS REPORT  -  Lab No. LAB-90001  (DEMO DATA)",
          "Reference: FIR 45/2023, PS Gulshan-e-Iqbal, offence 395/34 PPC",
          "Item 3: blood-stained shirt recovered from accused Sajid Mehmood.",
          "Reference sample R1: Sajid Mehmood (CNIC 99999-0000002-9).",
          "Result: The DNA profile obtained from Item 3 matches the reference sample R1 of Sajid Mehmood.",
          "Conclusion: Sajid Mehmood cannot be excluded as the source of the blood on Item 3."]),
        ("CHEMICAL", "LAB-90002", "Industrial Analytical Center @ KU", "12-03-2023 18:50:21",
         ["CHEMICAL EXAMINER REPORT  -  Lab No. LAB-90002  (DEMO DATA)",
          "Reference: FIR 45/2023, PS Gulshan-e-Iqbal",
          "Item 5: white powder recovered from the motorcycle KDE-1234.",
          "Result: Item 5 contains no narcotic substance."]),
    ],
}


def _pdf(lines: list[str], images: list[bytes] | None = None) -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 9, lines[0], new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for line in lines[1:]:
        pdf.multi_cell(0, 6, line, new_x="LMARGIN", new_y="NEXT")
    x = 12
    for image in images or []:
        pdf.image(io.BytesIO(image), x=x, y=150, w=40)
        x += 46
    return bytes(pdf.output())


def _fingerprints() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (220, 160), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    for finger in range(5):
        cx = 22 + finger * 44
        for r in range(4, 20, 3):
            draw.ellipse((cx - r, 70 - r * 1.3, cx + r, 70 + r * 1.3), outline=(40, 40, 40))
    draw.text((60, 140), "DEMO PRINTS", fill=(0, 0, 0))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def demo_fir_html(fir_no: str, fir_year: str, ps_id: str) -> str | None:
    fir = FIRS.get((str(fir_no), str(fir_year), str(ps_id)))
    if fir is None:
        return None
    c = PEOPLE.get(fir["complainant"]) if fir.get("complainant") else None

    def rows(entries: list[Any]) -> str:
        out = []
        for i, entry in enumerate(entries, 1):
            if isinstance(entry, tuple):
                out.append(f"<tr><td>{i}</td><td>{entry[0]}</td><td>{entry[1]}</td><td></td><td></td></tr>")
            else:
                p = PEOPLE[entry]
                out.append(f"<tr><td>{i}</td><td>{p.name}</td><td>{p.father}</td><td>{dashed_cnic(p.cnic)}</td>"
                           f"<td>{p.address}</td></tr>")
        return "".join(out)

    def section(heading: str, header: str, body: str) -> str:
        return (f"<table><tr class='trHeading'><td>{heading}</td></tr></table>"
                f"<table class='innerTable'><tr class='innerTr'>{header}</tr>{body}</table>")

    person_head = "<td>نمبر شمار</td><td>نام</td><td>ولدیت</td><td>شناختی کارڈ نمبر</td><td>سکونت</td>"
    diaries = "".join(
        f"<tr><td>{no}</td><td>{date}</td><td>{officer}</td></tr><tr><td colspan='3'><strong>ریمارکس:</strong>{text}</td></tr>"
        for no, date, officer, text in _DIARIES.get((str(fir_no), str(fir_year)), []))
    complainant = (f"{c.name} ولد {c.father}، پتہ : {c.address}، پیشہ : تاجر شناختی کارڈ نمبر : "
                   f"{dashed_cnic(c.cnic)} فون نمبر : {c.phones[0]}") if c else "سرکار بذریعہ پولیس"
    io_name = PEOPLE[fir["io"][0]].name if fir.get("io") else "-"
    return f"""<html><body><div id='inboxContent'>
<div class='report-hd view-fir'><ul>
<li><label>سیریل نمبر :</label><label>90{fir_no}</label></li>
<li><label>نمبر :</label><label>{fir_no}/{str(fir_year)[-2:]}</label></li>
<li><label>تھانہ :</label><label>{fir['ps']}</label></li>
<li><label>ضلع :</label><label>Karachi East</label></li>
<li><label>تاریخ ووقت وقوعہ :</label><label>01-03-{fir_year} 11:00 PM</label></li></ul></div>
<table class='PrintableFirTbl'>
<tr><td>زریعہ ڈاک</td><td>روانگی</td><td class='row-ID'>6</td><td>02-03-{fir_year} 09:00 AM</td><td>تاریخ ووقت رپورٹ</td><td class='row-ID'>1</td></tr>
<tr><td colspan='3'><label>{complainant}</label></td><td colspan='2'>نام و سکونت اطلاع دہندہ مستغیث</td><td class='row-ID'>2</td></tr>
<tr><td colspan='3'><div id='sectionDiv'><label>بجرم :</label><label>{fir['offence']}</label><label>(DEMO DATA) {fir['status']}</label></div></td><td colspan='2'>مختصر کیفیت جرم</td><td class='row-ID'>3</td></tr>
<tr><td colspan='3'><label>Rashid Minhas Road, Gulshan-e-Iqbal, Karachi</label></td><td colspan='2'>جائے وقوعہ</td><td class='row-ID'>4</td></tr>
</table>
<p id='ibtadayi_itla'>(DEMO DATA) The complainant reported the offence at {fir['ps']}. The accused fled on motorcycle KDE-1234.</p>
</div>
{section('تفتیشی افسران', '<td>نمبر شمار</td><td>عہدہ</td><td>نام</td><td>تاریخ تفتیش</td>',
         f"<tr><td>1</td><td>SI</td><td>{io_name}</td><td>02-03-{fir_year}</td></tr>")}
{section('نامزد ملزمان', person_head, rows(fir['accused']))}
{section('گواہان', person_head, rows(fir['witnesses']))}
{section('انڈیکس ضمنیات', '<td>ضمنی نمبر</td><td>تاریخ ضمنی</td><td>تفتیشی افسر</td>', diaries)}
<table><tr class='trHeading'><td>نتیجہ تفتیش</td></tr></table><table class='innerTable'><tr><td>{fir['status']}</td></tr></table>
</body></html>"""


class DemoEvidence:
    name = "demo"

    def fir_document(self, fir_no: str, fir_year: str, ps_id: str) -> dict[str, Any]:
        html = demo_fir_html(fir_no, fir_year, ps_id)
        if html is None:
            return {"status": "none", "message": "FIR report not found"}
        doc = parse_fir_html(html, fir_no=str(fir_no), fir_year=str(fir_year), ps_id=str(ps_id))
        doc["roster"] = roster(doc)
        return {"status": "hit", "doc": doc}

    def lab_reports(self, fir_no: str, fir_year: str, ps_id: str) -> dict[str, Any]:
        entries = _LABS.get((str(fir_no), str(fir_year)))
        if not entries:
            return {"status": "none", "message": "Report retrieved successfully (no lab case for this FIR)"}
        return {"status": "hit", "reports": [
            {"category": cat, "category_label": {"DNA": "DNA report", "CHEMICAL": "Chemical examiner report"}[cat],
             "lab_token": token, "unit_name": unit, "received_at": received,
             "links": [f"demo://labs/{token}.pdf"], "fir_no": str(fir_no), "fir_year": str(fir_year), "ps_id": str(ps_id)}
            for cat, token, unit, received, _ in entries]}

    def cro_dossier(self, cro_no: str) -> dict[str, Any]:
        if not str(cro_no).startswith("D-"):
            return {"status": "none", "message": "No CRO record"}
        try:
            person = list(PEOPLE.values())[int(str(cro_no)[2:]) - 1000]
        except (ValueError, IndexError):
            return {"status": "none", "message": "No CRO record"}
        poses = [base64.b64decode(_avatar_png(f"{person.key}-{pose}")) for pose in ("front", "left", "right")]
        lines = [f"CRO DOSSIER  -  CRO No. {cro_no}  (DEMO DATA)",
                 f"Name: {person.name}   Father: {person.father}   CNIC: {dashed_cnic(person.cnic)}",
                 f"Address: {person.address}", f"Mobile: {person.phones[0]}",
                 "Category: Accused  -  Modus operandi: armed robbery on motorcycle",
                 "Associates on record: Kamran Ahmed, Sajid Mehmood",
                 "Pictures below: front, left and right pose; fingerprint card on record."]
        return {"status": "hit", "pdf": _pdf(lines, poses + [_fingerprints()])}

    def download(self, url: str) -> bytes:
        token = url.rsplit("/", 1)[-1].removesuffix(".pdf")
        for entries in _LABS.values():
            for _, t, _, _, lines in entries:
                if t == token:
                    return _pdf(lines)
        raise FileNotFoundError(url)
