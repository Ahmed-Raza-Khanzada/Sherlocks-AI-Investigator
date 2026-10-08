"""Files the officer uploads: images, PDF, Word and Excel - nothing else.

The Document agent reads each one into a case document (``upload``): the vision model
transcribes and describes photos and scans, Word files give their text and pictures,
Excel files with call or tower data go to the CDR agent (``cdr.py``) - its own analysis
always, and the CDR server's analysis when the format is one it knows. Facts are taken
from what was read, the graph's people are found in it, and the Questioner asks what
the file leaves open (whose CDR is it, where and when the incident happened).
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

from sherlocks.evidence import cdr as cdr_agent
from sherlocks.evidence.pdf_reader import read_pdf
from sherlocks.evidence.readers import ai_read, link_people, report_facts

logger = logging.getLogger(__name__)

# Extension -> kind. Exactly the four formats allowed: image, PDF, Word, Excel.
ALLOWED = {".jpg": "image", ".jpeg": "image", ".png": "image", ".pdf": "pdf", ".docx": "docx", ".xlsx": "xlsx"}
ACCEPT = ",".join(ALLOWED)
_MIME = {"image": "image/jpeg", "pdf": "application/pdf",
         "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}

_IMAGE_PROMPT = ("This picture was uploaded to a police investigation. Transcribe every piece of text in it exactly "
                 "(Urdu or English, in its own script), then describe what it shows in 2-4 factual sentences: "
                 "people, vehicles and number plates, documents, places, objects. Do not guess identities.")


class UploadError(ValueError):
    """The file is not one of the allowed formats, or is broken or too large."""


def check_upload(name: str, content: bytes, max_mb: int = 25) -> str:
    """The file's kind, or :class:`UploadError` with the reason. The content must match
    the extension: a renamed .exe is refused."""
    ext = Path(name or "").suffix.lower()
    kind = ALLOWED.get(ext)
    if kind is None:
        raise UploadError(f"{ext or 'This file type'} is not accepted. Allowed: image (JPG, PNG), PDF, Word (.docx), Excel (.xlsx).")
    if not content:
        raise UploadError("The file is empty.")
    if len(content) > max_mb * 1_000_000:
        raise UploadError(f"The file is larger than {max_mb} MB.")
    ok = {
        "image": content[:3] == b"\xff\xd8\xff" or content[:8] == b"\x89PNG\r\n\x1a\n",
        "pdf": content[:5] == b"%PDF-",
        "docx": _zip_has(content, "word/document.xml"),
        "xlsx": _zip_has(content, "xl/workbook.xml"),
    }[kind]
    if not ok:
        raise UploadError(f"{name} is not a valid {ext} file (its content does not match the extension).")
    return kind


def _zip_has(content: bytes, member: str) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            return member in z.namelist()
    except zipfile.BadZipFile:
        return False


def read_docx(content: bytes) -> tuple[str, list[tuple[bytes, str]]]:
    """Paragraph text (tables included) and embedded pictures of a .docx."""
    from xml.etree import ElementTree as ET

    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
        lines = []
        for para in root.iter(f"{ns}p"):
            text = "".join(t.text or "" for t in para.iter(f"{ns}t")).strip()
            if text:
                lines.append(text)
        pictures = []
        for member in z.namelist():
            ext = member.rsplit(".", 1)[-1].lower()
            if member.startswith("word/media/") and ext in ("png", "jpg", "jpeg"):
                pictures.append((z.read(member), "jpg" if ext == "jpeg" else ext))
    return "\n".join(lines), pictures[:12]


def store_file(directory: Path, content: bytes, ext: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{hashlib.sha256(content).hexdigest()[:24]}{ext}"
    if not path.exists():
        path.write_bytes(content)
    return path


def graph_phones(graph: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Phone -> (person id, name) for everyone on the graph."""
    out: dict[str, tuple[str, str]] = {}
    for node in graph.get("nodes") or []:
        if node.get("kind") == "person":
            for phone in (node.get("data") or {}).get("phones") or []:
                out.setdefault(phone, (node["id"], node.get("label") or node["id"]))
    return out


def process_upload(agents: Any, *, name: str, content: bytes, note: str = "", owner: str | None = None,
                   upload_dir: Path, emit: Any = None) -> str:
    """Read one upload into the case file; returns its document id. Long work (the CDR
    server's analysis) happens here too, so call it from a worker thread."""
    say = emit or (lambda level, message: None)
    case = agents.case
    kind = check_upload(name, content, agents.cfg.max_upload_mb)
    ext = Path(name).suffix.lower()
    key = f"upload:{hashlib.sha256(content).hexdigest()[:16]}"
    existing = case.doc_for(key)
    if existing is not None:
        return existing["id"]
    if not case.claim(key):
        raise UploadError(f"{name} is already being read.")
    path = store_file(upload_dir, content, ext)
    graph = agents.graph()
    phones = graph_phones(graph)
    head = f"Uploaded file: {name} ({kind}, {len(content) // 1024} KB)" + (f". Officer's note: {note}" if note else "")
    vision = agents.vision()
    pictures: list[dict[str, Any]] = []
    unread: list[int] = []
    body = ""
    analysis: dict[str, Any] | None = None
    doc_kind = "upload"
    try:
        if kind == "image":
            image_id = agents.images.put(content, "png" if content[:4] == b"\x89PNG" else "jpg") if agents.images else None
            if image_id:
                pictures.append({"id": image_id, "page": 1, "caption": name})
            if vision is not None:
                try:
                    body = vision.read_image(content, _IMAGE_PROMPT, mime="image/png" if content[:4] == b"\x89PNG" else "image/jpeg")
                except Exception as exc:  # noqa: BLE001
                    logger.info("Vision model could not read %s: %s", name, exc)
            if not body:
                unread = [1]
        elif kind == "pdf":
            read = read_pdf(content, vision=vision)
            body, unread = read.text, read.unread_pages
            for img in read.images:
                image_id = agents.images.put(img.data, img.ext) if agents.images else None
                if image_id:
                    pictures.append({"id": image_id, "page": img.page, "caption": f"{name} page {img.page}"})
        elif kind == "docx":
            body, embedded = read_docx(content)
            for i, (data, ext2) in enumerate(embedded, 1):
                image_id = agents.images.put(data, ext2) if agents.images else None
                if image_id:
                    pictures.append({"id": image_id, "page": 1, "caption": f"{name} picture {i}"})
        else:
            analysis = cdr_agent.analyse(content, phones_on_graph={p: n for p, (_pid, n) in phones.items()},
                                         incident=case.incident, llm=agents.llm(), radius_km=agents.cfg.cdr_radius_km,
                                         owner_hint=_owner_phone(graph, owner))
            body = "\n".join(analysis["lines"])
            if analysis["kind"] in ("cdr", "tower_dump"):
                doc_kind = "cdr"
    except Exception as exc:
        case.attempt("upload", key, "error", f"{name}: {type(exc).__name__}: {exc}")
        raise UploadError(f"{name} could not be read: {exc}") from exc

    doc_id = case.add_document(
        kind=doc_kind, key=key, title=f"Upload: {name}", source="Uploaded by the officer",
        text=f"{head}\n{body}".strip(), summary=_summary(kind, analysis, body, unread),
        owners=[owner] if owner else [], images=pictures, unread_pages=unread,
        data={"path": str(path), "name": name, "file_kind": kind, "mime": _MIME[kind], "note": note,
              "analysis": {k: v for k, v in (analysis or {}).items() if k not in ("lines", "findings", "events")}})
    say("hit", f"📎 Read upload {name}: {_summary(kind, analysis, body, unread)} [{doc_id}]")
    if note:
        case.officer_note(f"About {name}: {note}", source="upload")

    # Facts: the CDR team's findings, each with its rows (R6); other files are read by the model.
    if analysis and analysis.get("findings"):
        from sherlocks.evidence.cdr_team import write_facts

        write_facts(case, doc_id, analysis, owner=owner)
        if sum(1 for d in case.documents.values() if d["kind"] == "cdr" and (d.get("data") or {}).get("path")) >= 2:
            case.plan("cdr:cross", f"a second CDR: {name}")          # R5 links the CDRs
    elif agents.llm() is not None and body.strip():
        try:
            ai_read(case, doc_id, agents.llm())
        except Exception as exc:  # noqa: BLE001
            logger.info("Reader could not read %s: %s", name, exc)
    else:
        report_facts(case, doc_id)
    link_people(case, doc_id, graph)
    if analysis and doc_kind == "cdr":
        _link_cdr_on_graph(agents, doc_id, analysis, phones, owner)
        if agents.cfg.cdr_auto_analyze:
            _cdr_server(agents, doc_id, name, content, analysis, say)
    from sherlocks.evidence.questioner import ask_gaps

    ask_gaps(case, graph)
    return doc_id


def _owner_phone(graph: dict[str, Any], owner: str | None) -> str | None:
    for node in graph.get("nodes") or []:
        if node.get("id") == owner:
            return ((node.get("data") or {}).get("phones") or [None])[0]
    return None


def _summary(kind: str, analysis: dict[str, Any] | None, body: str, unread: list[int]) -> str:
    if analysis:
        what = {"cdr": "CDR", "tower_dump": "tower dump / geofence"}.get(analysis.get("kind"), "spreadsheet")
        bits = [f"{what}, {analysis.get('rows', 0)} records"]
        if analysis.get("subject"):
            bits.append(f"subscriber {analysis['subject']}")
        if analysis.get("matches"):
            bits.append(f"{len(analysis['matches'])} number(s) already on the graph")
        if analysis.get("near_incident"):
            bits.append(f"{len(analysis['near_incident'])} record(s) near the incident")
        return " · ".join(bits)
    if unread and not body.strip():
        return f"{kind}: could not be read by machine"
    return f"{kind}: {len(body)} characters read" + (f", page(s) {', '.join(map(str, unread))} unread" if unread else "")


def _link_cdr_on_graph(agents: Any, doc_id: str, analysis: dict[str, Any], phones: dict[str, tuple[str, str]],
                       owner: str | None) -> None:
    """A CDR whose subscriber is on the graph: each contact also on the graph becomes a
    stated link (the operator's record of their calls)."""
    subject = analysis.get("subject")
    owner = owner or (phones.get(subject or "") or (None,))[0]
    if not owner or agents.builder is None:
        return
    for m in analysis.get("matches") or []:
        pid = phones.get(m["phone"], (None,))[0]
        if pid and pid != owner:
            agents.builder.add_direct_strong(owner, pid, label=f"In phone contact ({m['events']} call(s)/SMS, uploaded CDR)",
                                             reasons=[f"{m['events']} record(s) {m['first']} to {m['last']} [{doc_id}]"],
                                             system="cdr")


def _cdr_server(agents: Any, doc_id: str, name: str, content: bytes, analysis: dict[str, Any], say: Any) -> None:
    """Hand a known-format file to the CDR server and read its PDF summary back."""
    if getattr(agents.source, "name", "") == "demo":
        return  # synthetic cases never reach the real CDR server
    server = cdr_agent.CdrServer(getattr(agents.source, "http", None))
    case = agents.case
    if not server.ready:
        return
    key = f"cdrjob:{case.documents[doc_id]['key']}"
    if not case.claim(key):
        return
    try:
        job_kind = None
        inspected = server.inspect(name, content)
        if not inspected.get("error") and int(inspected.get("row_count") or 0) > 0 and analysis.get("kind") == "cdr":
            job_kind = "cdr"
        else:
            bts = server.bts_inspect(name, content)
            if not bts.get("error") and (bts.get("provider") or bts.get("inferred_bts_id")):
                job_kind, inspected = "bts", bts
        if job_kind is None:
            case.attempt("cdr", key, "none", f"{name}: format not known to the CDR server - analysed by the CDR agent only")
            return
        crime = cdr_agent.crime_request(case.incident)
        if job_kind == "cdr":
            request = {"crime": crime, "report": {"msisdn": analysis.get("subject")}}
        else:
            request = {"specs": [{"spec_id": "spec_1", "filenames": [name], "provider": inspected.get("provider"),
                                  "bts_id": str(inspected.get("inferred_bts_id") or ""), "label": name}],
                       "include_cross_bts_common": True, "include_b_as_a": True, "include_movement": True}
        say("info", f"📶 CDR server is analysing {name} ({job_kind.upper()}) - this can take a few minutes")
        from sherlocks.evidence import guard

        guard.log_call(case, system=f"cdr_server:{job_kind}", identifier=analysis.get("subject"), agent="R1 Intake",
                       reason=f"officer uploaded {name}", officer=case.dialog.get("officer"), status="called")
        job = server.start(job_kind, name, content, request)
        pdf = server.wait(job_kind, job, cancelled=agents.cancelled)
        read = read_pdf(pdf, vision=agents.vision())
        pictures = []
        for img in read.images[:12]:
            image_id = agents.images.put(img.data, img.ext) if agents.images else None
            if image_id:
                pictures.append({"id": image_id, "page": img.page, "caption": f"CDR report page {img.page}"})
        server_doc = case.add_document(
            kind="cdr", key=key, title=f"CDR server analysis: {name}", source="CDR Report App",
            text=f"CDR server {job_kind.upper()} analysis of {name}\n{read.text}", images=pictures,
            summary=f"{job_kind.upper()} report from the CDR server · {read.summary()}",
            data={"job_id": job, "job_kind": job_kind, "of": doc_id}, unread_pages=read.unread_pages)
        if agents.llm() is not None:
            ai_read(case, server_doc, agents.llm())
        else:
            report_facts(case, server_doc)
        link_people(case, server_doc, agents.graph())
        from sherlocks.evidence.cdr_team import compare_with_server

        compare_with_server(case, doc_id, server_doc)          # the server is a second opinion
        say("hit", f"📶 CDR server report for {name} read [{server_doc}]")
    except Exception as exc:  # noqa: BLE001 - the CDR agent's own analysis stands
        logger.info("CDR server analysis of %s failed: %s", name, exc)
        case.attempt("cdr", key, "error", f"{name}: CDR server: {type(exc).__name__}: {exc}"[:300])


def safe_name(name: str) -> str:
    base = Path(name or "upload").name
    return re.sub(r"[^\w.\- ()]+", "_", base)[:120] or "upload"
