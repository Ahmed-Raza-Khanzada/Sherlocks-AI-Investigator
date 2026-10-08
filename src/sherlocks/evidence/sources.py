"""Where case documents come from.

* **FIR file report** - PSRMS ``Criminal_api/firfilereport`` (form post: key, fir_no,
  fir_year, ps_id) -> the printable FIR page as HTML.
* **Forensic lab reports** - Labs ``api/v1/show-reports`` (JSON: ps_id, fir_no,
  fir_year) -> DNA / chemical / FSL / medico-legal entries, each with PDF links.
* **CRO dossier** - SAFE ``api/cro-report-pdf`` (JSON: cro_no) -> the dossier PDF as
  base64: particulars, front/left/right poses, fingerprints.

Every call goes through :class:`~sherlocks.linkgraph.ems.EmsHttp`, so it is audited
(keys masked) like every other police-system query. Endpoints and keys come from
``EMS_*`` in .env, never from source.

Each source answers ``{"status": "hit" | "none" | "error", ...}``; it never raises for an
answer the system gives, only for transport failures the caller reports.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Protocol

from sherlocks.evidence.fir_document import parse_fir_html, roster
from sherlocks.linkgraph.ems import CONF, EmsHttp, Req

logger = logging.getLogger(__name__)

LAB_CATEGORIES = {
    "DNA": "DNA report", "CHEMICAL": "Chemical examiner report", "FORENSIC": "Forensic science (FSL) report",
    "MEDICOLEGAL": "Medico-legal (MLO) report",
}


class EvidenceSource(Protocol):
    name: str

    def fir_document(self, fir_no: str, fir_year: str, ps_id: str) -> dict[str, Any]: ...

    def lab_reports(self, fir_no: str, fir_year: str, ps_id: str) -> dict[str, Any]: ...

    def cro_dossier(self, cro_no: str) -> dict[str, Any]: ...

    def download(self, url: str) -> bytes: ...


def _year2(year: str) -> str:
    """PSRMS and Labs take the FIR year as two digits ("25")."""
    y = str(year or "").strip()
    return y[-2:] if len(y) == 4 else y


def lab_entries(body: Any) -> list[dict[str, Any]]:
    """The Labs answer -> one entry per lab case, with its report links."""
    data = body.get("data") if isinstance(body, dict) else None
    out: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return out
    for category, rows in data.items():
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            links = [str(x) for x in row.get("report_links") or [] if x]
            out.append({
                "category": str(category).upper(),
                "category_label": LAB_CATEGORIES.get(str(category).upper(), f"{str(category).title()} report"),
                "lab_token": str(row.get("lab_token") or ""), "unit_name": str(row.get("unit_name") or ""),
                "received_at": str(row.get("case_received_at") or ""), "links": links,
                "fir_no": str(row.get("fir_no") or ""), "fir_year": str(row.get("fir_year") or ""),
                "ps_id": str(row.get("ps_id") or ""),
            })
    return out


class EmsEvidence:
    """The live sources."""

    name = "ems"

    def __init__(self, http: EmsHttp | None = None) -> None:
        self.http = http or EmsHttp()

    def fir_document(self, fir_no: str, fir_year: str, ps_id: str) -> dict[str, Any]:
        if not CONF.get("psrms_fir_url"):
            return {"status": "error", "message": "EMS_PSRMS_FIR_URL is not set"}
        headers = {"Accept": "text/html,application/json", "Content-Type": "application/x-www-form-urlencoded"}
        if CONF.get("psrms_fir_cookie"):
            headers["Cookie"] = CONF["psrms_fir_cookie"]
        code, body = self.http.send(Req("POST", CONF["psrms_fir_url"], headers=headers, data={
            "PSRMS-API-KEY": CONF.get("psrms_key", ""), "fir_no": str(fir_no), "fir_year": _year2(fir_year),
            "ps_id": str(ps_id)}, timeout=60))
        html = str(body.get("text_response") or "") if isinstance(body, dict) else ""
        if not html:
            message = body.get("message") if isinstance(body, dict) else None
            return {"status": "none", "message": message or f"No FIR report (HTTP {code})"}
        if "ibtadayi_itla" not in html and "PrintableFirTbl" not in html:
            return {"status": "none", "message": "FIR report not found"}
        doc = parse_fir_html(html, fir_no=str(fir_no), fir_year=str(fir_year), ps_id=str(ps_id))
        doc["roster"] = roster(doc)
        return {"status": "hit", "doc": doc}

    def lab_reports(self, fir_no: str, fir_year: str, ps_id: str) -> dict[str, Any]:
        if not (CONF.get("labs_url") and CONF.get("labs_token")):
            return {"status": "error", "message": "EMS_LABS_URL / EMS_LABS_TOKEN not set"}
        try:
            ps = int(str(ps_id))
        except ValueError:
            ps = ps_id
        code, body = self.http.send(Req("POST", CONF["labs_url"], headers={
            "Content-Type": "application/json", "Accept": "application/json", "api-token": CONF["labs_token"]},
            json={"ps_id": ps, "fir_no": str(fir_no), "fir_year": _year2(fir_year)}, timeout=60))
        entries = lab_entries(body)
        if not entries:
            message = body.get("message") if isinstance(body, dict) else None
            return {"status": "none", "message": message or f"No lab report (HTTP {code})"}
        return {"status": "hit", "reports": entries}

    def cro_dossier(self, cro_no: str) -> dict[str, Any]:
        if not (CONF.get("safe_cro_url") and CONF.get("safe_key")):
            return {"status": "error", "message": "EMS_SAFE_CRO_URL / EMS_SAFE_KEY not set"}
        code, body = self.http.send(Req("POST", CONF["safe_cro_url"], headers={
            "Content-Type": "application/json", "Accept": "application/json", "x-api-key": CONF["safe_key"]},
            json={"cro_no": str(cro_no)}, timeout=90))
        data = body.get("data") if isinstance(body, dict) else None
        if not (isinstance(body, dict) and body.get("status") and isinstance(data, str) and data):
            message = body.get("message") if isinstance(body, dict) else None
            return {"status": "none", "message": message or f"No CRO dossier (HTTP {code})"}
        try:
            pdf = base64.b64decode(data, validate=False)
        except (ValueError, TypeError) as exc:
            return {"status": "error", "message": f"CRO dossier is not valid base64: {exc}"}
        if not pdf.startswith(b"%PDF"):
            return {"status": "error", "message": "CRO dossier is not a PDF"}
        return {"status": "hit", "pdf": pdf}

    def download(self, url: str) -> bytes:
        return self.http.fetch_bytes(url)


def evidence_source(backend: Any) -> EvidenceSource | None:
    """The document source that goes with a lookup backend: synthetic documents for the
    demo, the live systems otherwise (EMS shares its audited HTTP client)."""
    explicit = getattr(backend, "evidence", None)
    if explicit is not None:
        return explicit
    name = getattr(backend, "name", "")
    if name == "demo":
        from sherlocks.evidence.demo import DemoEvidence

        return DemoEvidence()
    from sherlocks.linkgraph.ems import EmsBackend

    if isinstance(backend, EmsBackend):
        # Share the backend's client: one audit log, and a test's fake client stays fake.
        return EmsEvidence(backend.http)
    # The Report_App "live" backend talks to the same police systems.
    return EmsEvidence()
