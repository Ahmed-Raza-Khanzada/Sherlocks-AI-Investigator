"""Shared helpers for provider adapters."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any

from bs4 import BeautifulSoup

from cdr_report_app.domain.provider_models import AttachmentArtifact, ProviderResult
from cdr_report_app.utils.phones import normalize_cnic, normalize_mobile
from cdr_report_app.utils.text import compact_whitespace


def dashed_cnic(value: str | None) -> str | None:
    digits = normalize_cnic(value)
    if not digits or len(digits) != 13:
        return None
    return f"{digits[:5]}-{digits[5:12]}-{digits[12]}"


def dashed_mobile(value: str | None) -> str | None:
    mobile = normalize_mobile(value)
    if not mobile or len(mobile) != 11:
        return None
    return f"{mobile[:4]}-{mobile[4:]}"


def mobile_10(value: str | None) -> str | None:
    mobile = normalize_mobile(value)
    if not mobile or len(mobile) != 11:
        return None
    return mobile[1:]


def is_transport_error(raw: Any) -> bool:
    return isinstance(raw, dict) and "error" in raw


def no_record_result(provider: str, summary: str = "No record found") -> ProviderResult:
    return ProviderResult(provider=provider, hit=False, status="no_record", summary=summary)


def invalid_input_result(provider: str, message: str) -> ProviderResult:
    return ProviderResult(provider=provider, hit=False, status="invalid_input", summary=message, errors=[message])


def error_result(provider: str, message: str, raw: Any = None) -> ProviderResult:
    return ProviderResult(provider=provider, hit=False, status="error", summary=message, raw=raw, errors=[message])


def not_configured_result(provider: str, missing: list[str]) -> ProviderResult:
    message = f"Provider not configured: missing {', '.join(missing)}"
    return ProviderResult(provider=provider, hit=False, status="error", summary=message, errors=[message])


def extract_base64_pdf(data: dict[str, Any]) -> bytes | None:
    for field in ["pdf", "data", "report", "file", "content", "pdf_data", "base64"]:
        value = data.get(field)
        if isinstance(value, str):
            payload = value.split("base64,", 1)[1] if "base64," in value else value
            try:
                return base64.b64decode(payload)
            except Exception:
                return None
    return None


def parse_psrms_fir_html(html_text: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")
    scope = soup.select_one("#inboxContent") or soup

    for tag in scope(["script", "style", "img", "link", "meta"]):
        tag.decompose()

    title = ""
    heading = scope.find(["h1", "h2", "h3"])
    if heading:
        title = compact_whitespace(" ".join(heading.stripped_strings))

    header_fields: list[dict[str, str]] = []
    seen_header_fields: set[tuple[str, str]] = set()
    for li in scope.select(".view-fir li"):
        li_text = compact_whitespace(" ".join(li.stripped_strings))
        if not li_text:
            continue
        if ":" in li_text:
            label, value = li_text.split(":", 1)
            label = compact_whitespace(label)
            value = compact_whitespace(value)
        else:
            label, value = "", li_text
        key = (label, value)
        if key in seen_header_fields:
            continue
        seen_header_fields.add(key)
        header_fields.append({"label": _normalize_fir_label(label), "value": value})

    fir_sections = _extract_semantic_fir_sections(scope)
    footer_fields = _extract_footer_fields(scope)
    main_narrative = _extract_main_fir_narrative(scope)

    # Specific Embedded Tables Extraction
    table_mappings = {
        "پوزیشن مقدمہ": "case_positions",
        "تفتیشی افسران": "investigating_officers",
        "نامعلوم ملزمان": "unknown_suspects",
        "نامزد ملزمان": "nominated_suspects",
        "گواہان": "witnesses",
        "مسروقہ مال": "stolen_property",
        "نتیجہ تفتیش": "investigation_result"
    }
    extracted_tables: dict[str, Any] = {val: [] for val in table_mappings.values()}
    extracted_tables["investigation_result"] = ""
    
    seen_tables = set()

    # Search for specific tables across the ENTIRE soup, not just inboxContent
    for heading_tr in soup.find_all("tr", class_="trHeading"):
        heading_text = compact_whitespace(" ".join(heading_tr.stripped_strings))
        target_key = None
        for k, v in table_mappings.items():
            if k in heading_text:
                target_key = v
                break
                
        next_table_wrapper = heading_tr.find_parent("table")
        if next_table_wrapper:
            actual_table = next_table_wrapper.find_next_sibling("table")
            if actual_table and actual_table not in seen_tables:
                seen_tables.add(actual_table)
                
                if target_key == "investigation_result":
                    extracted_tables[target_key] = compact_whitespace(" ".join(actual_table.stripped_strings))
                    continue
                
                headers = []
                header_row = actual_table.find("tr", class_="innerTr")
                if header_row:
                    headers = [compact_whitespace(" ".join(th.stripped_strings)) for th in header_row.find_all(["th", "td"])]
                
                rows = []
                for tr in actual_table.find_all("tr"):
                    if "innerTr" in (tr.get("class") or []) or "trHeading" in (tr.get("class") or []):
                        continue
                    cells = []
                    for td in tr.find_all(["td", "th"]):
                        text = compact_whitespace(" ".join(td.stripped_strings))
                        a_tag = td.find("a")
                        if a_tag and "href" in a_tag.attrs:
                            if text:
                                text = f"{text} ({a_tag['href']})"
                            else:
                                text = a_tag['href']
                        cells.append(text)
                        
                    if any(cells):
                        if headers:
                            row_dict = {}
                            for i, c in enumerate(cells):
                                header_key = headers[i] if i < len(headers) else f"Detail {i+1}"
                                row_dict[header_key] = c
                            rows.append(row_dict)
                        else:
                            row_dict = {f"Detail {i+1}": c for i, c in enumerate(cells)}
                            rows.append(row_dict)
                            
                if target_key:
                    extracted_tables[target_key] = rows

    detail_tables: list[dict[str, Any]] = []
    seen_table_signatures: set[tuple[str, tuple[str, ...], tuple[tuple[str, ...], ...]]] = set()
    # Search for fallback tables ONLY inside scope to avoid combinatorial explosion
    for table in scope.find_all("table"):
        if (table.get("id") or "").strip() == "PrintableFirTbl" or table in seen_tables:
            continue
        rows: list[list[str]] = []
        table_headers: list[str] = []
        for row in table.find_all("tr"):
            if "trHeading" in (row.get("class") or []):
                continue
            cells: list[str] = []
            header_cells = row.find_all("th")
            for cell in row.find_all(["td", "th"]):
                classes = cell.get("class", [])
                if "row-ID" in classes:
                    continue
                cell_text = compact_whitespace(" ".join(cell.stripped_strings))
                if cell_text:
                    cells.append(cell_text)
            if not cells:
                continue
            if header_cells and not table_headers:
                table_headers = cells
            else:
                rows.append(cells)

        if not rows and not table_headers:
            continue

        max_cols = max([len(table_headers)] + [len(r) for r in rows]) if (table_headers or rows) else 0
        if max_cols <= 1:
            continue

        if _should_skip_generic_fir_table(normalized_headers=table_headers, normalized_rows=rows, max_cols=max_cols):
            continue

        normalized_rows: list[list[str]] = []
        for r in rows:
            normalized_rows.append(r + [""] * (max_cols - len(r)))
            
        if not table_headers:
            guessed = _guess_fir_table_headers(normalized_rows if rows else [], max_cols)
            table_headers = guessed or [f"Detail {index}" for index in range(1, max_cols + 1)]
        elif len(table_headers) < max_cols:
            table_headers = table_headers + [f"Detail {index}" for index in range(len(table_headers) + 1, max_cols + 1)]
        title_tag = table.find_previous(["h3", "h4", "h5", "strong"])
        table_title = compact_whitespace(" ".join(title_tag.stripped_strings)) if title_tag else ""
        signature = (table_title, tuple(table_headers), tuple(tuple(r) for r in normalized_rows[:8]))
        if signature in seen_table_signatures:
            continue
        seen_table_signatures.add(signature)
        detail_tables.append(
            {
                "title": table_title,
                "headers": table_headers,
                "rows": normalized_rows[:80],
            }
        )

    paragraph_lines: list[str] = []
    seen_paragraphs: set[str] = set()
    for tag in scope.find_all(["p", "div"]):
        if tag.find_parent("table") is not None:
            continue
        classes = " ".join(tag.get("class", []))
        if "modal" in classes.lower():
            continue
        line = compact_whitespace(" ".join(tag.stripped_strings))
        if not line or len(line) < 6:
            continue
        if any(line == compact_whitespace(" ".join(item.values())) for item in header_fields):
            continue
        if line in seen_paragraphs:
            continue
        seen_paragraphs.add(line)
        paragraph_lines.append(line)

    body_lines: list[str] = []
    seen_lines: set[str] = set()
    for row in soup.select("table tr"):
        if row.find_parent("table") in seen_tables or "trHeading" in (row.get("class") or []):
            continue
        cells: list[str] = []
        for cell in row.find_all(["td", "th"]):
            classes = cell.get("class", [])
            if "row-ID" in classes:
                continue
            cell_text = compact_whitespace(" ".join(cell.stripped_strings))
            if cell_text:
                cells.append(cell_text)
        if not cells:
            continue
        line = " | ".join(cells)
        if len(line) < 3 or line in seen_lines:
            continue
        seen_lines.add(line)
        body_lines.append(line)

    sections_of_law = ""
    for row in scope.find_all("tr"):
        row_text = row.get_text(strip=True)
        if "مختصر کیفیت" in row_text:
            for td in row.find_all(["td", "th"], recursive=False):
                td_text = compact_whitespace(" ".join(td.stripped_strings))
                if td_text and "کیفیت" not in td_text and td_text != "3":
                    sections_of_law = td_text.replace("بجرم :", "").replace("بجرم", "").strip()
                    break
            if sections_of_law:
                break

    if os.environ.get("DEBUG_PSRMS_HTML", "0") == "1":
        debug_dir = Path(tempfile.gettempdir()) / "cdr_report_app"
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / "last_fir.html").write_text(html_text, encoding="utf-8")

    return {
        "title": title,
        "header_fields": header_fields,
        "fir_sections": fir_sections,
        "footer_fields": footer_fields,
        "main_narrative": main_narrative,
        "detail_tables": detail_tables,
        "paragraph_lines": paragraph_lines[:80],
        "body_lines": body_lines[:250],
        "sections_of_law": sections_of_law.strip(),
        **extracted_tables,
    }



def _normalize_fir_label(label: str) -> str:
    text = compact_whitespace(label).strip(": ")
    mapping = {
        "سیریل نمبر": "سیریل نمبر",
        "نمبر": "FIR نمبر",
        "تھانہ": "تھانہ",
        "ضلع": "ضلع",
        "تاریخ ووقت وقوعہ": "تاریخ و وقت وقوعہ",
        "تاریخ وقت وقوعہ": "تاریخ و وقت وقوعہ",
        "تاریخ و وقت وقوع": "تاریخ و وقت وقوعہ",
    }
    return mapping.get(text, text or "Field")


def _extract_semantic_fir_sections(scope: BeautifulSoup) -> list[dict[str, Any]]:
    fir_table = scope.find("table", id="PrintableFirTbl")
    if not fir_table:
        return []

    section_buckets: dict[str, dict[str, Any]] = {
        "Reporting and Complainant": {"title": "Reporting and Complainant", "pairs": [], "lines": []},
        "Offence and Legal Sections": {"title": "Offence and Legal Sections", "pairs": [], "lines": []},
        "Occurrence Details": {"title": "Occurrence Details", "pairs": [], "lines": []},
        "Investigation and Action": {"title": "Investigation and Action", "pairs": [], "lines": []},
        "Other FIR Details": {"title": "Other FIR Details", "pairs": [], "lines": []},
    }

    for row in fir_table.find_all("tr"):
        cells = []
        for cell in row.find_all(["td", "th"]):
            if "row-ID" in cell.get("class", []):
                continue
            text = compact_whitespace(" ".join(cell.stripped_strings))
            if text:
                cells.append(text)
        if not cells:
            continue

        section_name = _classify_fir_row(cells)
        bucket = section_buckets[section_name]
        pairs = _extract_pairs_from_fir_cells(cells)
        if pairs:
            for label, value in pairs:
                if not value:
                    continue
                pair_key = (label, value)
                existing = {(item["label"], item["value"]) for item in bucket["pairs"]}
                if pair_key not in existing:
                    bucket["pairs"].append({"label": label, "value": value})
            continue

        line = " | ".join(cells)
        if line and line not in bucket["lines"]:
            bucket["lines"].append(line)

    return [bucket for bucket in section_buckets.values() if bucket["pairs"] or bucket["lines"]]


def _extract_footer_fields(scope: BeautifulSoup) -> list[dict[str, str]]:
    footer_fields: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    candidates = []
    for tag in scope.find_all(["p", "div"]):
        text = compact_whitespace(" ".join(tag.stripped_strings))
        if "|" in text and any(token in text for token in ["بیلٹ", "ASI", "فون", "موبائل", "نام", "عہدہ"]):
            candidates.append(text)
    for line in candidates[-3:]:
        for chunk in [part.strip() for part in line.split("|") if part.strip()]:
            if ":" not in chunk:
                continue
            label, value = [compact_whitespace(part) for part in chunk.split(":", 1)]
            if not value:
                continue
            key = (_normalize_fir_label(label), value)
            if key in seen:
                continue
            seen.add(key)
            footer_fields.append({"label": key[0], "value": key[1]})
    return footer_fields


def _extract_main_fir_narrative(scope: BeautifulSoup) -> str:
    narrative = scope.find(id="ibtadayi_itla")
    if narrative:
        return compact_whitespace(" ".join(narrative.stripped_strings))
    return ""


def _classify_fir_row(cells: list[str]) -> str:
    joined = " ".join(cells)
    if any(token in joined for token in ["ذریعہ", "اطلاع دہندہ", "مستغیث", "فون نمبر", "روانگی", "رپورٹ نمبر", "تاریخ وقت رپورٹ"]):
        return "Reporting and Complainant"
    if any(token in joined for token in ["بجرم", "جرم", "دفعہ", "دفعات", "SAA", "PPC"]):
        return "Offence and Legal Sections"
    if any(token in joined for token in ["جائے وقوعہ", "وقوعہ", "مقام", "تاریخ وقوعہ", "وقت وقوعہ"]):
        return "Occurrence Details"
    if any(token in joined for token in ["تفتیش", "کارروائی", "افسر", "ASI", "چالان", "حاصل"]):
        return "Investigation and Action"
    return "Other FIR Details"


def _extract_pairs_from_fir_cells(cells: list[str]) -> list[tuple[str, str]]:
    if len(cells) == 2:
        pair = _pick_label_value(cells[0], cells[1])
        return [pair] if pair else []

    if len(cells) == 4:
        forward = [_pick_label_value(cells[0], cells[1]), _pick_label_value(cells[2], cells[3])]
        reverse = [_pick_label_value(cells[1], cells[0]), _pick_label_value(cells[3], cells[2])]
        forward_score = sum(1 for pair in forward if pair)
        reverse_score = sum(1 for pair in reverse if pair)
        chosen = reverse if reverse_score > forward_score else forward
        return [pair for pair in chosen if pair]

    return []


def _pick_label_value(left: str, right: str) -> tuple[str, str] | None:
    left_label = _looks_like_fir_label(left)
    right_label = _looks_like_fir_label(right)
    if left_label and not right_label:
        return (_normalize_fir_label(left), right)
    if right_label and not left_label:
        return (_normalize_fir_label(right), left)
    return None


def _looks_like_fir_label(text: str) -> bool:
    value = compact_whitespace(text)
    if not value:
        return False
    if value.endswith(":"):
        return True
    if any(token in value for token in ["ذریعہ", "رپورٹ", "اطلاع", "مستغیث", "فون", "بجرم", "دفعہ", "جائے وقوعہ", "وقوعہ", "تفتیش", "کارروائی", "تھانہ", "ضلع"]):
        return True
    return len(value) <= 28 and not any(char.isdigit() for char in value)


def _guess_fir_table_headers(rows: list[list[str]], max_cols: int) -> list[str]:
    sample_cells = [cell for row in rows[:8] for cell in row[:max_cols]]
    joined = " ".join(sample_cells).lower()

    if max_cols == 2:
        return ["Field", "Value"]

    if max_cols == 3:
        if any(token in joined for token in ["دفعہ", "جرم", "offence", "section", "saa", "ppc"]):
            return ["Law / Offence", "Description", "Remarks"]
        return ["Detail 1", "Detail 2", "Detail 3"]

    if max_cols == 4:
        if any(token in joined for token in ["تاریخ", "وقت", "report", "date", "time"]):
            return ["Field", "Location / Party", "Date / Time", "Remarks"]
        if any(token in joined for token in ["فون", "نام", "پولیس", "asi", "mobile", "person"]):
            return ["Person / Officer", "Role / Place", "Contact / Notes", "Remarks"]
        return ["Detail 1", "Detail 2", "Detail 3", "Detail 4"]

    if max_cols >= 5:
        return ["Detail 1", "Detail 2", "Detail 3", "Detail 4", "Detail 5"][:max_cols]

    return []


def _should_skip_generic_fir_table(normalized_headers: list[str], normalized_rows: list[list[str]], max_cols: int) -> bool:
    sample_cells = []
    sample_cells.extend(normalized_headers)
    for row in normalized_rows[:4]:
        sample_cells.extend(row)
    joined = " ".join(compact_whitespace(cell) for cell in sample_cells if compact_whitespace(cell))
    footer_tokens = ["ASI", "بیلٹ", "فون", "ٹیلی فون", "دستخط", "عہدہ", "نام", "موبائل"]
    if max_cols >= 6 and any(token in joined for token in footer_tokens):
        return True
    if not normalized_headers and len(normalized_rows) <= 2 and max_cols >= 6:
        return True
    return False


def html_attachment(source: str, label: str, html_text: str) -> AttachmentArtifact:
    parsed = parse_psrms_fir_html(html_text)
    return AttachmentArtifact(
        source=source,
        label=label,
        media_type="html",
        text_content=html_text,
        metadata=parsed,
    )


def render_psrms_fir_html_to_pdf(html_text: str, base_url: str = "https://psrms.sindhpolice.gov.pk/") -> bytes | None:
    if not html_text:
        return None

    browser = _find_browser_binary()
    if not browser:
        return None

    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup.find_all("script"):
        tag.decompose()

    for selector in ["#myMapModal", "#viewHeader", "#qrImage"]:
        for tag in soup.select(selector):
            tag.decompose()

    if soup.head:
        if not soup.head.find("base"):
            soup.head.insert(0, soup.new_tag("base", href=base_url))
        style = soup.new_tag("style")
        style.string = """
            @page { size: A4; margin: 4mm; }
            html, body {
                background: #ffffff !important;
                margin: 0 !important;
                padding: 0 !important;
                width: 100% !important;
                overflow: visible !important;
                zoom: 0.90;
                transform-origin: top left;
            }
            .ibox-title, .noprint, #viewHeader, #myMapModal, #qrImage { display: none !important; }
            a.btn, button, .btn { display: none !important; }
            #page-wrapper, .wrapper-content, .wrapper {
                margin: 0 !important;
                padding: 0 !important;
                width: 100% !important;
                max-width: 100% !important;
                overflow: visible !important;
            }
            table {
                width: 100% !important;
                max-width: 100% !important;
                table-layout: auto !important;
            }
            th, td {
                word-break: break-word !important;
                overflow-wrap: anywhere !important;
            }
        """
        soup.head.append(style)

    tmp_root = Path.cwd() / "output" / "_tmp_fir_render"
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmpdir = tempfile.mkdtemp(dir=str(tmp_root))
    try:
        html_path = os.path.join(tmpdir, "fir_report.html")
        pdf_path = os.path.join(tmpdir, "fir_report.pdf")
        with open(html_path, "w", encoding="utf-8") as handle:
            handle.write(str(soup))

        command = [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--allow-file-access-from-files",
            "--print-to-pdf-no-header",
            f"--print-to-pdf={pdf_path}",
            "--force-device-scale-factor=0.9",
            "--virtual-time-budget=7000",
            f"file:///{html_path.replace(os.sep, '/')}",
        ]

        try:
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
        except Exception:
            return None

        if not os.path.exists(pdf_path):
            return None
        with open(pdf_path, "rb") as handle:
            return handle.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def extract_scalar_details(payload: Any, max_items: int = 16) -> dict[str, str]:
    details: dict[str, str] = {}
    skip_keys = {
        "status",
        "status_code",
        "code",
        "summary",
        "provider",
        "hit",
        "matched",
        "errors",
        "content_type",
        "raw",
    }

    def add_item(key: str, value: object) -> None:
        if len(details) >= max_items:
            return
        text = compact_whitespace(value)
        if not text or text.lower() in {"none", "null", "nan", "false"}:
            return
        label = _humanize_detail_key(key)
        if label not in details:
            details[label] = text[:220]

    def walk(value: Any, prefix: str = "") -> None:
        if len(details) >= max_items or value is None:
            return
        if isinstance(value, dict):
            for key, nested in value.items():
                key_text = str(key).strip()
                if not key_text or key_text.lower() in skip_keys:
                    continue
                next_prefix = f"{prefix}.{key_text}" if prefix else key_text
                walk(nested, next_prefix)
            return
        if isinstance(value, list):
            if not value:
                return
            if all(not isinstance(item, (dict, list, tuple, set)) for item in value[:5]):
                joined = ", ".join(compact_whitespace(item) for item in value if compact_whitespace(item))
                if joined:
                    add_item(prefix or "value", joined)
                return
            for index, item in enumerate(value[:2], start=1):
                walk(item, f"{prefix}[{index}]")
            return
        add_item(prefix or "value", value)

    walk(payload)
    return details


def _find_browser_binary() -> str | None:
    candidates = [
        os.environ.get("CHROME_BIN"),
        shutil.which("chrome"),
        shutil.which("msedge"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _humanize_detail_key(key: str) -> str:
    base = re.sub(r"\[\d+\]", "", key.split(".")[-1])
    return re.sub(r"[_\-.]+", " ", base).strip().title() or "Detail"
