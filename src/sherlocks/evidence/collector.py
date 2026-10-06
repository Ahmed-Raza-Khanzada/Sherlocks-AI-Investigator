"""The evidence team that works alongside a graph run.

The graph expansion finds FIRs and criminal record numbers; this team turns them into
read evidence, in the background, without slowing the search:

1. **Collector** - fetches the FIR file report of every FIR the run opens, the
   forensic/medical lab reports filed against that FIR, and the CRO dossier of every
   CRO number found. One fetch per source per run; answers are cached like any
   provider answer.
2. **Reader** - structural facts by rule at once; then the model reads the narrative,
   case diaries and report text into quoted facts (``readers.ai_read``).
3. **Linker** - finds the graph's people inside each document (CNIC, phone, vehicle,
   name). An identifier found in a FIR file becomes a stated edge from that FIR to the
   person on the graph; everything else is kept as an evidence link in the case file.

The same object serves the chat's live tools (``fetch_fir``, ``fetch_lab_reports``,
``fetch_cro``) so what the officer asks for joins the same case file.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

from sherlocks.evidence.case_file import CaseFile
from sherlocks.evidence.fir_document import station
from sherlocks.evidence.pdf_reader import read_pdf
from sherlocks.evidence.readers import ai_read, fir_facts, link_people, report_facts
from sherlocks.evidence.sources import (  # noqa: F401 - re-exported for callers
    EvidenceSource,
    lab_entries,
)
from sherlocks.linkgraph.models import FirKey, SystemRecord

logger = logging.getLogger(__name__)

Emit = Callable[[str, str], None]


def fir_source_key(fir_no: str, fir_year: str, ps_id: str) -> str:
    year = str(fir_year).strip()
    year = year[-2:] if len(year) == 4 else year
    return f"fir:{str(fir_no).strip().lstrip('0')}/{year}/{str(ps_id).strip()}"


def _cro_numbers(rec: SystemRecord) -> list[str]:
    return [f.value.strip() for f in rec.fields if f.label.startswith("CRO No") and f.value.strip()]


class CaseAgents:
    def __init__(self, case: CaseFile, source: EvidenceSource | None, *, settings: Any, cache: Any = None,
                 images: Any = None, builder: Any = None, graph: Callable[[], dict[str, Any]] | None = None,
                 llm: Callable[[], Any] | None = None, emit: Emit | None = None, cancelled: Callable[[], bool] | None = None,
                 backend_name: str = "ems") -> None:
        self.case = case
        self.source = source
        self.settings = settings
        self.cfg = settings.evidence
        self.cache = cache
        self.images = images
        self.builder = builder
        self._graph = graph or (lambda: {"nodes": [], "edges": []})
        self._llm_factory = llm
        self._llm: Any = None
        self._llm_checked = False
        self.emit = emit or (lambda level, message: None)
        self.cancelled = cancelled or (lambda: False)
        self.backend_name = backend_name
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="evidence")
        self._futures: list[Future] = []
        self._lock = threading.Lock()
        self.fetched = 0
        self.ai_calls = 0
        self._claims = threading.local()

    # -- plumbing ------------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.source is not None)

    def llm(self) -> Any:
        with self._lock:
            if not self._llm_checked:
                self._llm_checked = True
                if self.cfg.ai_reader and self._llm_factory is not None:
                    try:
                        self._llm = self._llm_factory()
                    except Exception:
                        logger.exception("Evidence reader: no LLM")
            return self._llm

    def _claim(self, key: str) -> bool:
        """Claim a source key for this thread's task (released if the task crashes)."""
        if not self.case.claim(key):
            return False
        if not hasattr(self._claims, "keys"):
            self._claims.keys = []
        self._claims.keys.append(key)
        return True

    def vision(self) -> Any:
        """The vision-language model that reads scanned pages (the office Qwen server
        reads Urdu and English), or None."""
        with self._lock:
            if not hasattr(self, "_vision"):
                self._vision = None
                if self.cfg.ocr and getattr(self.settings.llm, "vision", False) and self._llm_factory is not None:
                    try:
                        client = self._llm_factory()
                        self._vision = client if hasattr(client, "read_image") else None
                    except Exception:
                        logger.exception("Evidence reader: no vision model")
            return self._vision

    def _budget(self) -> bool:
        with self._lock:
            if self.fetched >= self.cfg.max_documents:
                return False
            self.fetched += 1
            return True

    def _submit(self, fn: Callable[..., Any], *args: Any) -> None:
        if not self.enabled or self.cancelled():
            return
        if self._pool._shutdown:
            return

        def guarded() -> None:
            self._claims.keys = []
            try:
                fn(*args)
            except Exception:
                logger.exception("Evidence task %s failed", getattr(fn, "__name__", fn))
                for key in self._claims.keys:
                    if key in self.case.pending:  # nothing may stay "being read" forever
                        self.case.attempt(key.split(":", 1)[0], key, "error", "internal error while reading")

        with self._lock:
            try:
                self._futures.append(self._pool.submit(guarded))
            except RuntimeError:  # shut down between the check and the submit
                pass

    def drain(self, timeout: float = 300.0) -> None:
        """Wait for queued fetching and reading (new work may be queued meanwhile)."""
        deadline = __import__("time").monotonic() + timeout
        while True:
            with self._lock:
                pending = [f for f in self._futures if not f.done()]
            if not pending:
                return
            left = deadline - __import__("time").monotonic()
            if left <= 0:
                return
            wait(pending, timeout=left)

    def graph(self) -> dict[str, Any]:
        try:
            return self._graph()
        except Exception:  # noqa: BLE001
            return {"nodes": [], "edges": []}

    def stop(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _cached(self, key: str, make: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        full = f"{self.backend_name}|evidence|{key}"
        if self.cache is not None:
            hit = self.cache.get(full)
            if hit is not None:
                return hit
        try:
            value = make()
        except LookupError as exc:          # the system answered: nothing there
            value = {"status": "none", "message": str(exc)[:300]}
        except Exception as exc:  # noqa: BLE001 - a broken API costs this document only
            logger.info("Evidence fetch %s failed: %s", key, exc)
            value = {"status": "error", "message": f"{type(exc).__name__}: {exc}"[:300]}
        if self.cache is not None and value.get("status") in ("hit", "none"):
            try:
                self.cache.put(full, "evidence", value)
            except Exception:
                logger.exception("Could not cache evidence %s", key)
        return value

    def _read_pdf(self, key: str, fetch: Callable[[], bytes]) -> dict[str, Any]:
        """A PDF's pages and stored pictures (cached by source key)."""

        def make() -> dict[str, Any]:
            pdf = fetch()
            content = read_pdf(pdf, vision=self.vision())
            if content.error:
                return {"status": "error", "message": content.error}
            pictures = []
            for img in content.images:
                image_id = self.images.put(img.data, img.ext) if self.images is not None else None
                if image_id:
                    pictures.append({"id": image_id, "page": img.page, "width": img.width, "height": img.height})
            return {"status": "hit", "text": content.text, "summary": content.summary(), "images": pictures,
                    "unread_pages": content.unread_pages, "pages": len(content.pages),
                    "sha": hashlib.sha256(pdf).hexdigest()[:16]}

        return self._cached(key, make)

    # -- hooks from the expansion --------------------------------------------------------

    def on_fir(self, pid: str, fir: FirKey, payload: dict[str, Any]) -> None:
        """The expansion opened a FIR's file report."""
        if not (self.enabled and fir.ps_id):
            return
        key = fir_source_key(fir.fir_no, fir.fir_year, fir.ps_id)
        if self.case.doc_for(key) is not None:
            doc = self.case.doc_for(key)
            if pid not in doc["owners"]:
                doc["owners"].append(pid)
            return
        raw = payload.get("raw") if isinstance(payload, dict) else None
        self._submit(self._fir_task, pid, fir.fir_no, fir.fir_year, fir.ps_id,
                     raw if isinstance(raw, dict) and "text" in raw else None, f"s:fir_roster:{fir.key}")

    def on_record(self, pid: str, rec: SystemRecord) -> None:
        if not (self.enabled and self.cfg.cro_dossiers and rec.hit):
            return
        for cro_no in _cro_numbers(rec):
            self._submit(self._cro_task, pid, cro_no)

    # -- tasks ---------------------------------------------------------------------------

    def _fir_task(self, pid: str | None, fir_no: str, fir_year: str, ps_id: str, doc: dict[str, Any] | None,
                  sid: str | None = None) -> str | None:
        key = fir_source_key(fir_no, fir_year, ps_id)
        if not self._claim(key):
            existing = self.case.doc_for(key)
            return existing["id"] if existing else None
        if doc is None:
            if not self._budget():
                self.case.attempt("fir", key, "skipped", "document budget for this run is used up")
                return None
            found = self._cached(key, lambda: self.source.fir_document(fir_no, fir_year, ps_id))
            if found.get("status") != "hit":
                self.case.attempt("fir", key, found.get("status") or "error", found.get("message") or "")
                return None
            doc = found["doc"]
        label = f"FIR {fir_no}/{doc.get('fir_year') or fir_year}"
        roster = doc.get("roster") or {}
        people = [{"role": role, **{k: p.get(k) for k in ("name", "father", "cnic", "phone", "address") if p.get(k)}}
                  for role, rows in roster.items() for p in rows]
        doc_id = self.case.add_document(
            kind="fir", key=key, title=f"{label} · {station(doc)}", source="PSRMS FIR file",
            text=doc.get("text") or "", ref={"fir_no": str(fir_no), "fir_year": str(fir_year), "ps_id": str(ps_id)},
            owners=[pid] if pid else [], people=people,
            data={k: v for k, v in doc.items() if k not in ("text",) and not k.endswith("_raw")},
            summary=_fir_summary(doc))
        fir_facts(self.case, doc_id, doc)
        self._link(doc_id, sid=sid or f"s:fir_roster:{FirKey(fir_no=fir_no, fir_year=fir_year, police_station='', ps_id=ps_id).key}")
        counts = {k: len(v) for k, v in roster.items()}
        self.emit("hit", f"📄 Read {label} file: {doc.get('sections') or 'sections n/a'}"
                         + (f" · {counts.get('nominated_suspects', 0)} accused" if counts.get("nominated_suspects") else "")
                         + (f" · {counts.get('witnesses', 0)} witness(es)" if counts.get("witnesses") else "")
                         + f" · {len(doc.get('case_diaries') or [])} case diar(ies) [{doc_id}]")
        self._read_with_ai(doc_id)
        if self.cfg.lab_reports:
            self._submit(self._labs_task, pid, fir_no, fir_year, ps_id)
        return doc_id

    def _labs_task(self, pid: str | None, fir_no: str, fir_year: str, ps_id: str) -> list[str]:
        key = "labs:" + fir_source_key(fir_no, fir_year, ps_id)[4:]
        if not self._claim(key):
            return [d["id"] for d in self.case.documents.values() if d["ref"].get("labs_of") == key]
        if not self._budget():
            self.case.attempt("lab", key, "skipped", "document budget for this run is used up")
            return []
        found = self._cached(key, lambda: self.source.lab_reports(fir_no, fir_year, ps_id))
        label = f"FIR {fir_no}/{fir_year}"
        if found.get("status") != "hit":
            self.case.attempt("lab", key, found.get("status") or "error",
                              found.get("message") or f"No forensic/medical report filed for {label}")
            return []
        self.case.attempt("lab", key, "hit", f"{len(found['reports'])} lab case(s) for {label}")
        out = []
        for entry in found["reports"]:
            links = entry.get("links") or [""]
            for n, url in enumerate(links, 1):
                doc_id = self._lab_document(pid, entry, url, n, len(links), label, key)
                if doc_id:
                    out.append(doc_id)
        return out

    def _lab_document(self, pid: str | None, entry: dict[str, Any], url: str, n: int, of: int, label: str,
                      labs_key: str) -> str | None:
        key = f"labpdf:{url or entry.get('lab_token')}"
        if not self._claim(key):
            return None
        head = (f"{entry['category_label']} · {entry.get('lab_token') or '-'} · {entry.get('unit_name') or '-'} · "
                f"case received {entry.get('received_at') or '-'} · {label}")
        read: dict[str, Any] = {}
        if url and self._budget():
            read = self._read_pdf(key, lambda: self.source.download(url))
        text = head + ("\n" + read["text"] if read.get("text") else "")
        title = f"{entry['category_label']} {entry.get('lab_token') or ''} ({label})" + (f" · part {n}/{of}" if of > 1 else "")
        doc_id = self.case.add_document(
            kind="lab", key=key, title=title.strip(), source=entry.get("unit_name") or "Forensic lab", text=text,
            ref={"labs_of": labs_key, "lab_token": entry.get("lab_token"), "category": entry.get("category"),
                 "fir": label}, owners=[pid] if pid else [], urls=[url] if url else [],
            images=[{**p, "caption": f"Page {p['page']} picture"} for p in read.get("images") or []],
            unread_pages=read.get("unread_pages") or [],
            summary=head + (f" · {read['summary']}" if read.get("summary") else
                            (f" · report could not be read: {read.get('message')}" if read.get("status") == "error" else "")))
        report_facts(self.case, doc_id)
        self._link(doc_id)
        self.emit("hit", f"🧪 {entry['category_label']} {entry.get('lab_token') or ''} for {label}"
                         + (f": {read['summary']}" if read.get("summary") else "") + f" [{doc_id}]")
        self._read_with_ai(doc_id)
        return doc_id

    def _cro_task(self, pid: str | None, cro_no: str) -> str | None:
        key = f"cro:{cro_no}"
        if not self._claim(key):
            existing = self.case.doc_for(key)
            if existing and pid and pid not in existing["owners"]:
                existing["owners"].append(pid)
            return existing["id"] if existing else None
        if not self._budget():
            self.case.attempt("cro", key, "skipped", "document budget for this run is used up")
            return None

        def fetch() -> bytes:
            found = self.source.cro_dossier(cro_no)
            if found.get("status") != "hit":
                raise LookupError(found.get("message") or "No CRO dossier")
            return found["pdf"]

        read = self._read_pdf(key, fetch)
        if read.get("status") != "hit":
            self.case.attempt("cro", key, read.get("status") or "error", read.get("message") or "")
            return None
        name = self.builder.person(pid).get("name") if (self.builder is not None and pid) else None
        pictures = [{**p, "caption": f"CRO dossier picture {i} (page {p['page']})"} for i, p in enumerate(read["images"], 1)]
        doc_id = self.case.add_document(
            kind="cro", key=key, title=f"CRO dossier No. {cro_no}" + (f" · {name}" if name else ""), source="SAFE CRO",
            text=f"CRO dossier No. {cro_no}\n{read.get('text') or ''}", ref={"cro_no": cro_no},
            owners=[pid] if pid else [], images=pictures, unread_pages=read.get("unread_pages") or [],
            summary=f"CRO No. {cro_no}: {read.get('summary') or ''}")
        report_facts(self.case, doc_id)
        self._link(doc_id)
        portraits = [p["id"] for p in pictures if min(p["width"], p["height"]) >= 150 and p["height"] >= 0.8 * p["width"]]
        if pid and self.builder is not None and portraits:
            # Portrait-shaped dossier pictures are the subject's photographs; wide strips
            # (pose sheets, fingerprint cards) stay with the document.
            self.builder.add_images(pid, portraits[:3], "CRO dossier")
        self.emit("hit", f"🗂 CRO dossier {cro_no}" + (f" ({name})" if name else "")
                         + f": {read.get('summary') or ''} [{doc_id}]")
        self._read_with_ai(doc_id)
        return doc_id

    # -- reading and linking -------------------------------------------------------------

    def _link(self, doc_id: str, sid: str | None = None) -> None:
        try:
            graph = self._graph()
        except Exception:  # noqa: BLE001
            return
        links = link_people(self.case, doc_id, graph)
        doc = self.case.documents[doc_id]
        for person in links:
            if person["pid"] in doc["owners"]:
                continue
            if sid and person["strength"] == "identifier" and self.builder is not None:
                fir = doc["ref"]
                label = f"Mentioned in the file of FIR {fir.get('fir_no')}/{fir.get('fir_year')}"
                if self.builder.link_mention(sid, person["pid"], label=label, reason=f"{person['how']} [{doc_id}]"):
                    self.emit("hit", f"🔗 {person['name']}: {person['how']} - {doc['title']} [{doc_id}]")

    def _read_with_ai(self, doc_id: str) -> None:
        llm = self.llm()
        if llm is None:
            return
        with self._lock:
            if self.ai_calls >= self.cfg.ai_reader_calls_per_run:
                return
            self.ai_calls += 1
        try:
            result = ai_read(self.case, doc_id, llm)
        except Exception as exc:  # noqa: BLE001 - a model failure leaves the rule facts
            logger.info("AI reader failed on %s: %s", doc_id, exc)
            return
        if result["added"]:
            self.emit("info", f"🧠 Read {self.case.documents[doc_id]['title']}: {result['added']} fact(s) with quotes"
                              + (f", {result['dropped']} unquoted claim(s) discarded" if result["dropped"] else ""))
            self._link(doc_id)

    # -- live tools for the chat ---------------------------------------------------------

    def fetch_fir(self, fir_no: str, fir_year: str, ps_id: str, pid: str | None = None) -> dict[str, Any]:
        if self.source is None:
            return {"error": "No document source on this deployment"}
        key = fir_source_key(fir_no, fir_year, ps_id)
        doc = self.case.doc_for(key)
        doc_id = doc["id"] if doc else self._fir_task(pid, fir_no, fir_year, ps_id, None)
        if not doc_id:
            last = next((a for a in reversed(self.case.attempts) if a["key"] == key), None)
            return {"error": (last or {}).get("message") or "FIR document not available"}
        return {"document": doc_id}

    def fetch_lab_reports(self, fir_no: str, fir_year: str, ps_id: str, pid: str | None = None) -> dict[str, Any]:
        if self.source is None:
            return {"error": "No document source on this deployment"}
        ids = self._labs_task(pid, fir_no, fir_year, ps_id)
        if not ids:
            key = "labs:" + fir_source_key(fir_no, fir_year, ps_id)[4:]
            last = next((a for a in reversed(self.case.attempts) if a["key"] == key), None)
            return {"documents": [], "message": (last or {}).get("message") or "No lab report"}
        return {"documents": ids}

    def fetch_cro(self, cro_no: str, pid: str | None = None) -> dict[str, Any]:
        if self.source is None:
            return {"error": "No document source on this deployment"}
        doc_id = self._cro_task(pid, cro_no)
        if not doc_id:
            last = next((a for a in reversed(self.case.attempts) if a["key"] == f"cro:{cro_no}"), None)
            return {"error": (last or {}).get("message") or "CRO dossier not available"}
        return {"document": doc_id}


def _fir_summary(doc: dict[str, Any]) -> str:
    c = doc.get("complainant") or {}
    bits = [f"Sections {doc['sections']}" if doc.get("sections") else "",
            f"occurred {doc['occurred']}" if doc.get("occurred") else "",
            f"complainant {c['name']}" if c.get("name") else "",
            f"{len(doc.get('nominated_suspects') or [])} nominated accused" if doc.get("nominated_suspects") else "",
            f"{len(doc.get('witnesses') or [])} witness(es)" if doc.get("witnesses") else "",
            f"{len(doc.get('case_diaries') or [])} case diar(ies)" if doc.get("case_diaries") else ""]
    last = (doc.get("case_positions") or [{}])[-1]
    if last.get("position"):
        bits.append(f"position: {last['position']}")
    return " · ".join(b for b in bits if b)
