"""The case file: every document read during a run, the facts taken from them, and the
links they reveal - the shared memory of the investigation.

It grows while the graph builds (FIR documents, lab reports, CRO dossiers arrive as the
search reaches them), the chat adds to it when it fetches something, and the case
report is written from it. Everything in it is citable:

* ``D3``  - a document (FIR 604/2025 file report, a DNA report, a CRO dossier);
* ``F12`` - a fact, always with the verbatim ``quote`` it rests on and the document it
  came from;
* ``L4``  - an evidence link: a person of the graph found in a document by CNIC, phone,
  IMEI, plate or name, with the line that shows it.

The case file travels with the graph (``graph["case"]``), so a graph the host portal
saved and posts back still carries its evidence.
"""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime
from typing import Any

_MAX_TEXT = 60_000
_SPACE = re.compile(r"\s+")
CITATION = re.compile(r"\b([DFL]\d{1,4})\b")

KIND_LABELS = {"fir": "FIR document", "lab": "Forensic / medical report", "cro": "CRO dossier"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def squash(text: str) -> str:
    return _SPACE.sub(" ", str(text or "")).strip()


def quote_in(quote: str, text: str) -> bool:
    """Is ``quote`` really in ``text`` (whitespace and case aside)? A fact whose quote is
    not in its document is invented, and is dropped."""
    q = squash(quote).lower()
    return len(q) >= 6 and q in squash(text).lower()


class CaseFile:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.documents: dict[str, dict[str, Any]] = {}
        self.facts: dict[str, dict[str, Any]] = {}
        self.links: dict[str, dict[str, Any]] = {}
        # What was asked of the document systems and what they said (a lab with nothing
        # filed is worth knowing, and is not a document).
        self.attempts: list[dict[str, Any]] = []
        self.keys: dict[str, str] = {}          # source key -> document id
        self.pending: set[str] = set()          # source keys being fetched
        self.report: dict[str, Any] | None = None
        self.report_status = "none"             # none | building | ready | failed
        self.version = 0

    # -- writing -----------------------------------------------------------------------

    def _bump(self) -> None:
        self.version += 1

    def claim(self, key: str) -> bool:
        """Reserve a source key for fetching; False if it is done or in progress."""
        with self._lock:
            if key in self.keys or key in self.pending:
                return False
            self.pending.add(key)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            self.pending.discard(key)

    def attempt(self, kind: str, key: str, status: str, message: str = "") -> None:
        with self._lock:
            self.attempts.append({"kind": kind, "key": key, "status": status, "message": message[:300], "at": _now()})
            self.pending.discard(key)
            self._bump()

    def add_document(self, *, kind: str, key: str, title: str, source: str, text: str,
                     summary: str = "", ref: dict[str, Any] | None = None, owners: list[str] | None = None,
                     images: list[dict[str, Any]] | None = None, people: list[dict[str, Any]] | None = None,
                     data: dict[str, Any] | None = None, urls: list[str] | None = None,
                     unread_pages: list[int] | None = None) -> str:
        with self._lock:
            if key in self.keys:
                doc = self.documents[self.keys[key]]
                for pid in owners or []:
                    if pid not in doc["owners"]:
                        doc["owners"].append(pid)
                self.pending.discard(key)
                self._bump()
                return doc["id"]
            doc_id = f"D{len(self.documents) + 1}"
            self.documents[doc_id] = {
                "id": doc_id, "kind": kind, "key": key, "title": title, "source": source,
                "summary": summary, "text": (text or "")[:_MAX_TEXT], "ref": ref or {}, "owners": list(owners or []),
                "images": images or [], "people": people or [], "data": data or {}, "urls": urls or [],
                "unread_pages": unread_pages or [], "fetched_at": _now(), "facts": [], "read_by": [],
            }
            self.keys[key] = doc_id
            self.pending.discard(key)
            self._bump()
            return doc_id

    def add_fact(self, doc_id: str, statement: str, quote: str, *, kind: str = "fact", people: list[str] | None = None,
                 pids: list[str] | None = None, by: str = "rule", check_quote: bool = True) -> str | None:
        """A fact from a document. With ``check_quote`` the quote must be in the document's
        text, or the fact is refused."""
        with self._lock:
            doc = self.documents.get(doc_id)
            statement, quote = squash(statement), squash(quote)
            if doc is None or not statement:
                return None
            if check_quote and not quote_in(quote, doc["text"]):
                return None
            for fid in doc["facts"]:
                if self.facts[fid]["statement"].lower() == statement.lower():
                    return fid
            fact_id = f"F{len(self.facts) + 1}"
            self.facts[fact_id] = {"id": fact_id, "doc": doc_id, "statement": statement[:500], "quote": quote[:600],
                                   "kind": kind, "people": people or [], "pids": pids or [], "by": by}
            doc["facts"].append(fact_id)
            self._bump()
            return fact_id

    def add_link(self, doc_id: str, pid: str, name: str, how: str, quote: str, *, strength: str) -> str | None:
        with self._lock:
            for link in self.links.values():
                if link["doc"] == doc_id and link["pid"] == pid:
                    return link["id"]
            link_id = f"L{len(self.links) + 1}"
            self.links[link_id] = {"id": link_id, "doc": doc_id, "pid": pid, "name": name, "how": how,
                                   "quote": squash(quote)[:400], "strength": strength}
            self._bump()
            return link_id

    def mark_read(self, doc_id: str, by: str) -> None:
        with self._lock:
            doc = self.documents.get(doc_id)
            if doc is not None and by not in doc["read_by"]:
                doc["read_by"].append(by)
                self._bump()

    def set_report(self, report: dict[str, Any] | None, status: str) -> None:
        with self._lock:
            self.report, self.report_status = report, status
            self._bump()

    # -- reading -----------------------------------------------------------------------

    def doc_for(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            doc_id = self.keys.get(key)
            return self.documents.get(doc_id) if doc_id else None

    def facts_of(self, doc_id: str) -> list[dict[str, Any]]:
        with self._lock:
            doc = self.documents.get(doc_id) or {}
            return [self.facts[f] for f in doc.get("facts", [])]

    def for_person(self, pid: str) -> dict[str, Any]:
        with self._lock:
            docs = [d for d in self.documents.values() if pid in d["owners"]
                    or any(link["pid"] == pid and link["doc"] == d["id"] for link in self.links.values())]
            return {"documents": [{k: d[k] for k in ("id", "kind", "title", "summary")} for d in docs],
                    "facts": [f for f in self.facts.values() if pid in f["pids"]],
                    "links": [link for link in self.links.values() if link["pid"] == pid]}

    def cite(self, ref: str) -> dict[str, Any] | None:
        """What a citation id points at, for display."""
        with self._lock:
            if ref.startswith("D") and ref in self.documents:
                d = self.documents[ref]
                return {"id": ref, "type": "document", "title": d["title"], "source": d["source"], "doc": ref}
            if ref.startswith("F") and ref in self.facts:
                f = self.facts[ref]
                return {"id": ref, "type": "fact", "title": f["statement"], "quote": f["quote"], "doc": f["doc"],
                        "source": self.documents[f["doc"]]["title"]}
            if ref.startswith("L") and ref in self.links:
                link = self.links[ref]
                return {"id": ref, "type": "link", "title": f"{link['name']}: {link['how']}", "quote": link["quote"],
                        "doc": link["doc"], "source": self.documents[link["doc"]]["title"]}
        return None

    def citations_in(self, text: str) -> list[dict[str, Any]]:
        seen: list[dict[str, Any]] = []
        for ref in dict.fromkeys(CITATION.findall(text or "")):
            hit = self.cite(ref)
            if hit:
                seen.append(hit)
        return seen

    def search(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        """Lines of any document that contain every word of ``query``."""
        words = [w.lower() for w in squash(query).split() if len(w) > 1]
        out: list[dict[str, Any]] = []
        with self._lock:
            for doc in self.documents.values():
                for line in doc["text"].splitlines():
                    low = line.lower()
                    if words and all(w in low for w in words):
                        out.append({"doc": doc["id"], "title": doc["title"], "line": squash(line)[:400]})
                        if len(out) >= limit:
                            return out
        return out

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {"documents": len(self.documents), "facts": len(self.facts), "links": len(self.links),
                    "pending": len(self.pending)}

    def index(self) -> dict[str, Any]:
        """Everything but document texts - what a viewer is streamed."""
        with self._lock:
            return {
                "version": self.version, "counts": self.counts(),
                "documents": [{k: v for k, v in d.items() if k not in ("text", "data")} for d in self.documents.values()],
                "facts": list(self.facts.values()), "links": list(self.links.values()),
                "attempts": self.attempts[-60:], "report_status": self.report_status,
            }

    def document(self, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            doc = self.documents.get(doc_id)
            if doc is None:
                return None
            return {**doc, "fact_rows": [self.facts[f] for f in doc["facts"]],
                    "link_rows": [link for link in self.links.values() if link["doc"] == doc_id]}

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"version": self.version, "documents": list(self.documents.values()),
                    "facts": list(self.facts.values()), "links": list(self.links.values()),
                    "attempts": list(self.attempts), "report": self.report, "report_status": self.report_status}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CaseFile:
        case = cls()
        if not isinstance(data, dict):
            return case
        for doc in data.get("documents") or []:
            if isinstance(doc, dict) and doc.get("id"):
                doc = {"facts": [], "owners": [], "images": [], "people": [], "read_by": [], "text": "",
                       "data": {}, "urls": [], "unread_pages": [], "ref": {}, "summary": "", **doc}
                case.documents[doc["id"]] = doc
                case.keys[doc.get("key") or doc["id"]] = doc["id"]
        for fact in data.get("facts") or []:
            if isinstance(fact, dict) and fact.get("id") and fact.get("doc") in case.documents:
                case.facts[fact["id"]] = fact
        for link in data.get("links") or []:
            if isinstance(link, dict) and link.get("id") and link.get("doc") in case.documents:
                case.links[link["id"]] = link
        case.attempts = [a for a in data.get("attempts") or [] if isinstance(a, dict)]
        case.report = data.get("report") if isinstance(data.get("report"), dict) else None
        case.report_status = str(data.get("report_status") or ("ready" if case.report else "none"))
        case.version = int(data.get("version") or 0)
        return case
