"""The case file: every document read during a run, the facts taken from them, and the
links they reveal - the shared memory of the investigation.

It grows while the graph builds (FIR documents, lab reports, CRO dossiers arrive as the
search reaches them), the chat adds to it when it fetches something, and the case
report is written from it. Everything in it is citable:

* ``D3``  - a document (FIR 604/2025 file report, a DNA report, a CRO dossier);
* ``F12`` - a fact, always with the verbatim ``quote`` it rests on and the document it
  came from;
* ``L4``  - an evidence link: a person of the graph found in a document by CNIC, phone,
  IMEI, plate or name, with the line that shows it;
* ``W3``  - a recorded statement (complainant, 161, 164, interrogation), with its speaker;
* ``X2``  - a contradiction between two claims, both sides quoted;
* ``K5``  - a lead: a hint worth following, with its next step and status.

It is also the **case board** every agent shares. The rules that keep it trustworthy:

* **Add, never rewrite.** A changed fact is a new fact with ``replaces``; the old one is
  kept, marked replaced. Entries built on something that changed are marked ``stale``
  and drop out of answers until the agent that made them redoes them.
* **Every entry says where it came from**: ``agent`` (who wrote it), ``event`` (the
  batch it belongs to - one upload, one chat turn, one CDR), ``rests_on`` (the ids and
  topics it was built from - what a correction follows), ``trust`` (the source's rank,
  see ``trust.py``) and ``tier`` (fact, inference or speculation).
* **Every change is logged** (``changes``) with its agent and event, and listeners are
  told - once per batch, never for a batch still open - so the agents that need a
  change wake up, and none wakes on its own writes.

The Sherlock team's work lives here too: hypotheses ``H#``, the summary and timeline,
ranked gaps, dossiers, Sherlock's quick views given in chat, the Question desk's
decisions, follow-up messages, and the audit log of every live call.

The case file travels with the graph (``graph["case"]``), so a graph the host portal
saved and posts back still carries its evidence; the board is also stored on its own
(``board_store.py``) so it outlives the run in memory.
"""

from __future__ import annotations

import itertools
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

_MAX_TEXT = 60_000
_SPACE = re.compile(r"\s+")
CITATION = re.compile(r"\b([DFLHQWXK]\d{1,4})\b")
TIERS = ("fact", "inference", "speculation")
# Trust rank of a source (1 = most trusted). See trust.py for how conflicts are settled.
# A recorded statement (161, 164, interrogation) is a reliable record that it was SAID, not
# that it is true: it ranks below the officer's own word and above what we derive.
TRUST_SYSTEM, TRUST_DOCUMENT, TRUST_OFFICER, TRUST_RECORDED, TRUST_DERIVED, TRUST_UNVERIFIED = 1, 2, 3, 4, 5, 6
_MAX_CHANGES = 3000

KIND_LABELS = {"fir": "FIR document", "lab": "Forensic / medical report", "cro": "CRO dossier",
               "upload": "Uploaded file", "cdr": "CDR / tower analysis", "notes": "Officer's statements",
               "graph": "Graph analysis"}
NOTES_KEY = "notes:officer"
# Roles the officer can give people on the case board.
ROLES = ("main suspect", "suspect", "victim", "complainant", "witness", "informer", "cleared")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def squash(text: str) -> str:
    return _SPACE.sub(" ", str(text or "")).strip()


def default_trust(doc_kind: str, by: str) -> int:
    """A fact's trust rank from where it came from: the officer's word, a document, an
    operator's CDR rows, our own analysis, or an unverified source."""
    if by == "officer" or doc_kind == "notes":
        return TRUST_OFFICER
    if by in ("graph", "sherlock", "derived"):
        return TRUST_DERIVED
    if by in ("osint", "web"):
        return TRUST_UNVERIFIED
    if by == "cdr" and doc_kind == "cdr":
        return TRUST_SYSTEM
    return TRUST_DOCUMENT


def quote_in(quote: str, text: str) -> bool:
    """Is ``quote`` really in ``text`` (whitespace and case aside)? A fact whose quote is
    not in its document is invented, and is dropped."""
    q = squash(quote).lower()
    return len(q) >= 6 and q in squash(text).lower()


class CaseFile:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ctx = threading.local()                 # the agent and event writing now
        self._events = itertools.count(1)
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self.run_id: str | None = None
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
        # The rest of the case board the agent team shares.
        self.incident: dict[str, Any] | None = None   # {lat, lon, place, date, time, fir, nearest_ps}
        self.roles: dict[str, str] = {}               # person id -> role stated by the officer
        self.questions: list[dict[str, Any]] = []     # open / answered questions to the officer
        self.conversation: list[dict[str, Any]] = []  # {q, a, at, checks}
        self.assessment: dict[str, Any] | None = None
        self.seen: dict[str, int] = {}                # highest D/F/L ids already told to the officer
        # Dialogue state: who is being discussed (pronouns resolve to them), what was asked.
        self.dialog: dict[str, Any] = {"focus": [], "last_type": None, "announced": 0}
        # The Sherlock team's work and the board's bookkeeping.
        self.hypotheses: dict[str, dict[str, Any]] = {}   # H# -> statement, status, for / against
        self.summary: dict[str, Any] | None = None        # S2: text, timeline, board version
        self.gaps: list[dict[str, Any]] = []              # S4: ranked, what would move the case
        self.dossiers: dict[str, dict[str, Any]] = {}     # A4: one card per person
        self.views: list[dict[str, Any]] = []             # Sherlock's quick views given in chat
        self.desk: list[dict[str, Any]] = []              # Question desk decisions, with reasons
        self.followups: list[dict[str, Any]] = []         # messages sent after a reply finished
        self.calls: list[dict[str, Any]] = []             # audit log of live calls
        self.history: list[dict[str, Any]] = []           # how the assessment changed, and why
        self.agenda: list[dict[str, Any]] = []            # background work still to do
        self.changes: list[dict[str, Any]] = []           # every change, with agent and event
        self.change_seq = 0
        # The Sherlock team's casework (plan v5).
        self.statements: dict[str, dict[str, Any]] = {}       # W#: recorded statements, by speaker
        self.claims: dict[str, dict[str, Any]] = {}           # C#: who said what, about whom, when, where
        self.contradictions: dict[str, dict[str, Any]] = {}   # X#: two claims that disagree
        self.leads: dict[str, dict[str, Any]] = {}            # K#: hints, with next step and status
        self.entities: dict[str, dict[str, Any]] = {}         # A5: a mention -> same / maybe / different
        self.register: dict[str, Any] = {"cards": {}, "pending": [], "custody": []}   # S10
        self.matrix: dict[str, Any] | None = None             # S1: evidence x hypothesis
        self.challenges: list[dict[str, Any]] = []            # S9: the Skeptic's, and S1's replies
        self.presence: dict[str, Any] | None = None           # who was where in the incident window
        self.analysis: dict[str, dict[str, Any]] = {}         # S7: each person's versions
        self.insights: list[dict[str, Any]] = []              # Q3: findings told to the officer
        self.claimed: dict[str, int] = {}                     # source id -> board version S5 read it at
        self.traces: list[dict[str, Any]] = []                # how the agents worked on each question (team/trace.py)

    # -- writing -----------------------------------------------------------------------

    def _bump(self, entry: str = "board", ref: str | None = None) -> None:
        """Count a change and log it with the agent and event writing it."""
        self.version += 1
        self.change_seq += 1
        change = {"seq": self.change_seq, "version": self.version, "entry": entry, "id": ref,
                  "agent": self.current_agent(), "event": self.current_event(), "at": _now()}
        self.changes.append(change)
        del self.changes[:-_MAX_CHANGES]
        if not self.current_event():
            self._notify(change)

    # -- who is writing: agents and their batches ----------------------------------------

    def current_agent(self) -> str:
        return getattr(self._ctx, "agent", None) or "system"

    def current_event(self) -> str | None:
        return getattr(self._ctx, "event", None)

    @contextmanager
    def acting(self, agent: str) -> Iterator[None]:
        """Writes in this block are recorded as ``agent``'s."""
        before = getattr(self._ctx, "agent", None)
        self._ctx.agent = agent
        try:
            yield
        finally:
            self._ctx.agent = before

    @contextmanager
    def batch(self, agent: str, kind: str, **about: Any) -> Iterator[str]:
        """One event: an upload read, a chat turn, a CDR analysed, a correction. Every
        write inside carries its id; listeners hear about it once, when it ends - so a
        CDR's hundreds of facts wake the Sherlock team once, not hundreds of times."""
        outer_event, outer_agent = getattr(self._ctx, "event", None), getattr(self._ctx, "agent", None)
        if outer_event:            # nested: part of the batch already open
            self._ctx.agent = agent
            try:
                yield outer_event
            finally:
                self._ctx.agent = outer_agent
            return
        with self._lock:
            event = f"E{next(self._events)}-{self.change_seq}"
        self._ctx.event, self._ctx.agent = event, agent
        start = self.change_seq
        try:
            yield event
        finally:
            self._ctx.event, self._ctx.agent = None, outer_agent
            with self._lock:
                written = [c for c in self.changes if c["seq"] > start and c.get("event") == event]
                done = {"seq": self.change_seq, "version": self.version, "entry": "batch", "id": event,
                        "agent": agent, "event": event, "kind": kind, "count": len(written),
                        "entries": sorted({c["entry"] for c in written}), "at": _now(), **about}
            if written:
                self._notify(done)

    def listen(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Call ``fn(change)`` after every change outside a batch, and once per batch."""
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def _notify(self, change: dict[str, Any]) -> None:
        for fn in list(self._listeners):
            try:
                fn(change)
            except Exception:
                import logging

                logging.getLogger(__name__).exception("Case board listener failed")

    def changes_since(self, seq: int) -> list[dict[str, Any]]:
        with self._lock:
            return [c for c in self.changes if c["seq"] > seq]

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
            self.attempts.append({"kind": kind, "key": key, "status": status, "message": message[:300], "at": _now(),
                                  "agent": self.current_agent()})
            self.pending.discard(key)
            self._bump("attempt", key)

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
                self._bump("document", doc["id"])
                return doc["id"]
            doc_id = f"D{len(self.documents) + 1}"
            self.documents[doc_id] = {
                "id": doc_id, "kind": kind, "key": key, "title": title, "source": source,
                "summary": summary, "text": (text or "")[:_MAX_TEXT], "ref": ref or {}, "owners": list(owners or []),
                "images": images or [], "people": people or [], "data": data or {}, "urls": urls or [],
                "unread_pages": unread_pages or [], "fetched_at": _now(), "facts": [], "read_by": [],
                "agent": self.current_agent(), "event": self.current_event(),
            }
            self.keys[key] = doc_id
            self.pending.discard(key)
            self._bump("document", doc_id)
            return doc_id

    def add_fact(self, doc_id: str, statement: str, quote: str, *, kind: str = "fact", people: list[str] | None = None,
                 pids: list[str] | None = None, by: str = "rule", check_quote: bool = True, tier: str = "fact",
                 trust: int | None = None, rests_on: list[str] | None = None, topic: str | None = None,
                 rows: str | None = None, page: int | None = None, replaces: str | None = None) -> str | None:
        """A fact from a document. With ``check_quote`` the quote must be in the document's
        text, or the fact is refused. ``rests_on`` lists the ids and topics (``incident:date``)
        it was built from; ``replaces`` names the fact it corrects (kept, marked replaced)."""
        with self._lock:
            doc = self.documents.get(doc_id)
            statement, quote = squash(statement), squash(quote)
            if doc is None or not statement:
                return None
            if check_quote and not quote_in(quote, doc["text"]):
                return None
            for fid in doc["facts"]:
                old = self.facts[fid]
                if (old["statement"].lower() == statement.lower() and not old.get("replaced_by")
                        and old.get("topic") == topic):
                    if old.get("stale"):           # redone: the same finding holds again
                        old["stale"] = False
                        self._bump("fact", fid)
                    return fid
            fact_id = f"F{len(self.facts) + 1}"
            self.facts[fact_id] = {"id": fact_id, "doc": doc_id, "statement": statement[:500], "quote": quote[:600],
                                   "kind": kind, "people": people or [], "pids": pids or [], "by": by,
                                   "tier": tier if tier in TIERS else "fact",
                                   "trust": trust or default_trust(doc["kind"], by),
                                   "agent": self.current_agent(), "event": self.current_event(), "at": _now(),
                                   "rests_on": list(dict.fromkeys(rests_on or [])), "topic": topic,
                                   "rows": rows, "page": page, "replaces": replaces, "replaced_by": None,
                                   "stale": False}
            doc["facts"].append(fact_id)
            if replaces and replaces in self.facts:
                self.facts[replaces]["replaced_by"] = fact_id
            self._bump("fact", fact_id)
            return fact_id

    def add_link(self, doc_id: str, pid: str, name: str, how: str, quote: str, *, strength: str) -> str | None:
        with self._lock:
            for link in self.links.values():
                if link["doc"] == doc_id and link["pid"] == pid:
                    return link["id"]
            link_id = f"L{len(self.links) + 1}"
            self.links[link_id] = {"id": link_id, "doc": doc_id, "pid": pid, "name": name, "how": how,
                                   "quote": squash(quote)[:400], "strength": strength, "agent": self.current_agent(),
                                   "event": self.current_event()}
            self._bump("link", link_id)
            return link_id

    # -- the officer's own words, the incident, roles, questions ----------------------------

    def officer_note(self, text: str, *, source: str = "chat") -> tuple[str, str]:
        """Append what the officer said to the "Officer's statements" document, so facts
        taken from it can quote it like any document. Returns (doc id, the line)."""
        with self._lock:
            line = f"[{_now()[:16].replace('T', ' ')} · {source}] {squash(text)[:1500]}"
            doc = self.doc_for(NOTES_KEY)
            if doc is None:
                doc_id = self.add_document(kind="notes", key=NOTES_KEY, title="Officer's statements",
                                           source="The investigating officer", text=line,
                                           summary="What the officer stated in the chat, on the map and in answers")
            else:
                doc_id = doc["id"]
                doc["text"] = (doc["text"] + "\n" + line)[-_MAX_TEXT:]
                self._bump("statement", doc_id)
            return doc_id, line

    def set_incident(self, incident: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            clean = {k: v for k, v in incident.items() if v not in (None, "")}
            self.incident = {**(self.incident or {}), **clean, "at": _now()}
            self._bump("incident", ",".join(sorted(clean)))
            return self.incident

    def set_role(self, pid: str, role: str) -> None:
        with self._lock:
            self.roles[pid] = role
            self._bump("role", pid)

    def ask(self, key: str, text: str, *, action: str = "text", about: str | None = None,
            options: list[dict[str, str]] | None = None, by: str = "Questioner",
            priority: str = "orange") -> dict[str, Any] | None:
        """Ask the officer something once. ``key`` identifies the gap ("incident:place",
        "cdr_owner:D4"), so the same question is never asked twice."""
        with self._lock:
            if any(q["key"] == key for q in self.questions):
                return None
            q = {"id": f"Q{len(self.questions) + 1}", "key": key, "text": text, "action": action, "about": about,
                 "options": options or [], "status": "open", "answer": None, "by": by, "asked_at": _now(),
                 "times": 0, "asked_turns": [], "answer_turn": None,
                 "priority": priority if priority in ("red", "orange", "green") else "orange"}
            self.questions.append(q)
            self._bump("question", q["id"])
            return q

    def answer(self, qid: str, answer: str, *, status: str = "answered") -> dict[str, Any] | None:
        """Record the officer's answer to a question (``status`` "dont_know" for "pata
        nahi": an answer too, never asked again). A question answered again - a
        correction - keeps the earlier answer in ``answers``."""
        with self._lock:
            q = next((q for q in self.questions if q["id"] == qid), None)
            if q is None:
                return None
            if q.get("answer") and q["status"] != "open":
                q.setdefault("answers", []).append({"answer": q["answer"], "turn": q.get("answer_turn"),
                                                    "at": q.get("answered_at")})
            q["status"], q["answer"], q["answered_at"] = status, squash(answer)[:500], _now()
            q["answer_turn"] = len(self.conversation) + 1
            self._bump("question", qid)
            return q

    def close_question(self, key: str, answer: str = "") -> None:
        with self._lock:
            for q in self.questions:
                if q["key"] == key and q["status"] == "open":
                    q["status"], q["answer"], q["answered_at"] = "answered", answer[:500], _now()
                    q["answer_turn"] = len(self.conversation) + 1
                    self._bump("question", q["id"])

    def add_trace(self, trace: dict[str, Any], keep: int = 30) -> None:
        """The trace of one chat turn (which agent did what). Not part of the board's version:
        it describes the work, it is not evidence."""
        with self._lock:
            self.traces.append(trace)
            del self.traces[:-keep]

    def open_questions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [q for q in self.questions if q["status"] == "open"]

    def add_turn(self, question: str, answer: str, checks: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.conversation.append({"q": question[:2000], "a": answer[:4000], "at": _now(), "checks": checks or {},
                                      "turn": len(self.conversation) + 1})
            del self.conversation[:-60]
            self._bump("turn", str(len(self.conversation)))

    def set_assessment(self, assessment: dict[str, Any]) -> None:
        with self._lock:
            self.assessment = {**assessment, "at": _now(), "agent": self.current_agent()}
            self._bump("assessment", "S1")

    def append_text(self, doc_id: str, text: str) -> None:
        with self._lock:
            doc = self.documents.get(doc_id)
            if doc is not None:
                doc["text"] = (doc["text"] + "\n" + text)[:_MAX_TEXT]
                self._bump("document", doc_id)

    def mark_read(self, doc_id: str, by: str) -> None:
        with self._lock:
            doc = self.documents.get(doc_id)
            if doc is not None and by not in doc["read_by"]:
                doc["read_by"].append(by)
                self._bump("document", doc_id)

    def set_report(self, report: dict[str, Any] | None, status: str) -> None:
        with self._lock:
            self.report, self.report_status = report, status
            self._bump("report", status)

    # -- corrections: what replaced what, and what rests on it ----------------------------

    def live_facts(self) -> list[dict[str, Any]]:
        """Facts an answer may use: not replaced by a correction, not stale."""
        with self._lock:
            return [f for f in self.facts.values() if not f.get("replaced_by") and not f.get("stale")]

    def usable(self, ref: str) -> bool:
        with self._lock:
            f = self.facts.get(ref)
            return f is None or not (f.get("replaced_by") or f.get("stale"))

    def dependents(self, roots: list[str]) -> list[str]:
        """Every fact and hypothesis built on ``roots`` (fact ids or topic keys such as
        ``incident:date``), directly or through other entries."""
        with self._lock:
            found: list[str] = []
            frontier = set(roots)
            while frontier:
                nxt: set[str] = set()
                for fid, f in self.facts.items():
                    if fid not in found and fid not in roots and frontier & set(f.get("rests_on") or []):
                        found.append(fid)
                        nxt.add(fid)
                for hid, h in self.hypotheses.items():
                    if hid not in found and frontier & set((h.get("for") or []) + (h.get("against") or [])
                                                           + (h.get("rests_on") or [])):
                        found.append(hid)
                frontier = nxt
            return found

    def mark_stale(self, ids: list[str], reason: str) -> list[str]:
        """Entries that rest on something changed: out of answers until redone."""
        marked = []
        with self._lock:
            for ref in ids:
                entry = self.facts.get(ref) or self.hypotheses.get(ref)
                if entry is not None and not entry.get("stale"):
                    entry["stale"], entry["stale_reason"] = True, reason[:200]
                    marked.append(ref)
                    self._bump("stale", ref)
        return marked

    def statement_for(self, topic: str) -> dict[str, Any] | None:
        """The officer's current statement on a topic (``incident:date``, ``role:<pid>``)."""
        with self._lock:
            hits = [f for f in self.facts.values() if f.get("topic") == topic and f.get("by") == "officer"
                    and not f.get("replaced_by")]
            return hits[-1] if hits else None

    # -- the Sherlock team's work ---------------------------------------------------------

    def set_hypothesis(self, statement: str, *, status: str = "open", tier: str = "inference",
                       confidence: str = "medium", people: list[str] | None = None, support: list[str] | None = None,
                       against: list[str] | None = None, hid: str | None = None, cause: str = "") -> str:
        """Add or update one hypothesis. A change of status or confidence is kept in
        ``history`` with what caused it."""
        with self._lock:
            if hid is None:
                hid = next((h["id"] for h in self.hypotheses.values()
                            if squash(h["statement"]).lower() == squash(statement).lower()), None)
            now = _now()
            if hid is None or hid not in self.hypotheses:
                hid = f"H{len(self.hypotheses) + 1}"
                self.hypotheses[hid] = {"id": hid, "statement": squash(statement)[:400], "status": status,
                                        "tier": tier if tier in TIERS else "inference", "confidence": confidence,
                                        "people": people or [], "for": support or [], "against": against or [],
                                        "agent": self.current_agent(), "at": now, "updated_at": now, "stale": False}
                self.history.append({"id": hid, "change": "new", "before": None, "after": status, "cause": cause[:300],
                                     "at": now, "version": self.version + 1})
            else:
                h = self.hypotheses[hid]
                before = (h["status"], h["confidence"])
                h.update({"statement": squash(statement)[:400] or h["statement"], "status": status, "confidence": confidence,
                          "tier": tier if tier in TIERS else h["tier"], "people": people or h["people"],
                          "for": support if support is not None else h["for"],
                          "against": against if against is not None else h["against"], "updated_at": now, "stale": False})
                if before != (status, confidence):
                    self.history.append({"id": hid, "change": "updated", "before": f"{before[0]} ({before[1]})",
                                         "after": f"{status} ({confidence})", "cause": cause[:300], "at": now,
                                         "version": self.version + 1})
            del self.history[:-300]
            self._bump("hypothesis", hid)
            return hid

    def set_summary(self, summary: dict[str, Any]) -> None:
        with self._lock:
            self.summary = {**summary, "at": _now(), "board_version": self.version}
            self._bump("summary", "S2")

    def set_gaps(self, gaps: list[dict[str, Any]]) -> None:
        with self._lock:
            self.gaps = gaps[:20]
            self._bump("gaps", "S4")

    def set_dossier(self, pid: str, card: dict[str, Any]) -> None:
        with self._lock:
            self.dossiers[pid] = {**card, "at": _now(), "board_version": self.version}
            self._bump("dossier", pid)

    def add_view(self, question: str, view: str, *, turn: int, people: list[str] | None = None) -> dict[str, Any]:
        """Sherlock's quick view given in chat, kept so his full assessment can confirm or
        change it - and so the report can say which."""
        with self._lock:
            v = {"id": f"V{len(self.views) + 1}", "question": question[:400], "view": view[:1500], "turn": turn,
                 "people": people or [], "status": "given", "at": _now()}
            self.views.append(v)
            self._bump("view", v["id"])
            return v

    def log_desk(self, decision: dict[str, Any]) -> None:
        with self._lock:
            self.desk.append({**decision, "at": _now()})
            del self.desk[:-200]
            self._bump("desk", decision.get("key"))

    def add_followup(self, text: str, *, turn: int | None = None, kind: str = "followup") -> dict[str, Any]:
        """A message to the officer after a reply went out (a slow lookup, Sherlock's view)."""
        with self._lock:
            f = {"id": f"M{len(self.followups) + 1}", "text": text[:4000], "turn": turn, "kind": kind, "at": _now()}
            self.followups.append(f)
            del self.followups[:-100]
            self._bump("followup", f["id"])
            return f

    def log_call(self, call: dict[str, Any]) -> None:
        """One live call to a police system or the CDR server, for the audit trail."""
        with self._lock:
            seq = int(self.dialog.get("call_seq") or 0) + 1
            self.dialog["call_seq"] = seq
            self.calls.append({"id": f"call {seq}", **call, "agent": call.get("agent") or self.current_agent(),
                               "at": _now()})
            del self.calls[:-2000]
            self._bump("call", call.get("system"))

    def plan(self, task: str, reason: str) -> None:
        """Background work to do (survives a restart: the board is stored with it)."""
        with self._lock:
            if not any(a["task"] == task for a in self.agenda):
                self.agenda.append({"task": task, "reason": reason[:200], "at": _now()})
                self._bump("agenda", task)

    def done(self, task: str) -> None:
        with self._lock:
            before = len(self.agenda)
            self.agenda = [a for a in self.agenda if a["task"] != task]
            if len(self.agenda) != before:
                self._bump("agenda", task)


    # -- plan v5: statements, claims, contradictions, leads, the matrix -------------------

    def _entry(self, store: dict[str, dict[str, Any]], prefix: str, entry: str, key: str, data: dict[str, Any],
               *, keep: tuple[str, ...] = ()) -> str:
        """Add an entry under a stable ``key`` (so a re-run updates it and never makes a
        second one), or update the one already there. ``keep``: fields a re-run must not
        reset (a status the officer's answer set)."""
        with self._lock:
            old = next((e for e in store.values() if e.get("key") == key), None)
            if old is not None:
                held = {k: old[k] for k in keep if k in old}
                changed = any(old.get(k) != v for k, v in data.items() if k not in keep)
                old.update({**data, **held, "updated_at": _now()})
                if changed:
                    self._bump(entry, old["id"])
                return old["id"]
            ref = f"{prefix}{len(store) + 1}"
            store[ref] = {"id": ref, "key": key, **data, "agent": self.current_agent(), "event": self.current_event(),
                          "at": _now(), "board_version": self.version}
            self._bump(entry, ref)
            return ref

    def add_statement(self, doc_id: str, *, speaker: str, role: str, kind: str, text: str, date: str | None = None,
                      recorded_by: str = "", page: int | None = None, pid: str | None = None,
                      father: str = "") -> str | None:
        """A recorded statement ``W#``: what a complainant, witness or accused said, as
        the record has it. It proves that it was said - not that it is true."""
        text = squash(text)
        doc = self.documents.get(doc_id)
        if doc is None or len(text) < 12 or not quote_in(text[:200], doc["text"]):
            return None
        key = f"{doc_id}:{squash(speaker).lower()}:{kind}:{date or ''}:{text[:60].lower()}"
        return self._entry(self.statements, "W", "recorded", key, {
            "doc": doc_id, "speaker": squash(speaker)[:120], "role": role, "kind": kind, "date": date,
            "recorded_by": squash(recorded_by)[:120], "page": page, "pid": pid, "text": text[:4000],
            "father": squash(father)[:80]})

    def add_claim(self, source: str, *, said_by: str, about: str, topic: str, value: str, quote: str,
                  when_from: str | None = None, when_to: str | None = None, where: str = "",
                  tolerance_min: int = 0, trust: int = TRUST_DOCUMENT, said_pid: str | None = None,
                  about_pid: str | None = None, about_state: str = "maybe", page: int | None = None,
                  certainty: str = "stated", event: str = "", background: bool = False, kind: str = "record") -> str:
        key = f"{source}:{squash(about).lower()}:{topic}:{squash(value).lower()[:80]}:{when_from or ''}"
        return self._entry(self.claims, "C", "claim", key, {
            "source": source, "said_by": squash(said_by)[:120], "said_pid": said_pid, "about": squash(about)[:120],
            "about_pid": about_pid, "about_state": about_state, "topic": topic, "value": squash(value)[:300],
            "when_from": when_from, "when_to": when_to, "where": squash(where)[:160], "tolerance_min": tolerance_min,
            "quote": squash(quote)[:400], "trust": trust, "page": page, "certainty": certainty,
            "occasion": squash(event)[:80], "background": background, "kind": kind})

    def set_contradiction(self, key: str, data: dict[str, Any]) -> str:
        """``X#``. A re-run keeps the status an answer gave it (explained, resolved)."""
        return self._entry(self.contradictions, "X", "contradiction", key, {"status": "open", **data},
                           keep=("status", "explained_by"))

    def set_lead(self, key: str, data: dict[str, Any]) -> str:
        return self._entry(self.leads, "K", "lead", key, {"status": "open", **data}, keep=("status", "reason"))

    def move(self, ref: str, status: str, reason: str = "") -> dict[str, Any] | None:
        """Change the status of a contradiction or a lead, with why (an answer, a record)."""
        with self._lock:
            entry = self.contradictions.get(ref) or self.leads.get(ref)
            if entry is None or entry.get("status") == status:
                return entry
            entry.setdefault("trail", []).append({"from": entry.get("status"), "to": status, "why": reason[:300],
                                                   "at": _now()})
            entry["status"], entry["reason"], entry["updated_at"] = status, reason[:300], _now()
            self._bump("contradiction" if ref in self.contradictions else "lead", ref)
            return entry

    def set_entity(self, key: str, data: dict[str, Any]) -> str:
        return self._entry(self.entities, "E", "entity", key, data, keep=("decided",))

    def set_card(self, doc_id: str, card: dict[str, Any]) -> None:
        """S10: the typed card of one record (FIR, MLO, DNA...), every field with its quote."""
        with self._lock:
            self.register.setdefault("cards", {})[doc_id] = {**card, "doc": doc_id, "at": _now()}
            self._bump("register", doc_id)

    def set_register(self, *, pending: list[dict[str, Any]], custody: list[dict[str, Any]]) -> None:
        with self._lock:
            if self.register.get("pending") != pending or self.register.get("custody") != custody:
                self.register["pending"], self.register["custody"] = pending, custody
                self._bump("register", "S10")

    def set_matrix(self, matrix: dict[str, Any]) -> None:
        with self._lock:
            self.matrix = {**matrix, "at": _now(), "board_version": self.version}
            self._bump("matrix", "S1")

    def set_challenges(self, challenges: list[dict[str, Any]]) -> None:
        with self._lock:
            self.challenges = [{**c, "at": c.get("at") or _now()} for c in challenges][-40:]
            self._bump("challenge", "S9")

    def set_presence(self, presence: dict[str, Any]) -> None:
        with self._lock:
            self.presence = {**presence, "at": _now(), "board_version": self.version}
            self._bump("presence", "matrix")

    def set_analysis(self, pid: str, analysis: dict[str, Any]) -> None:
        with self._lock:
            self.analysis[pid] = {**analysis, "at": _now()}
            self._bump("analysis", pid)

    def tell(self, ref: str, text: str, *, turn: int, state: str = "") -> dict[str, Any]:
        """Q3's ledger: a finding told to the officer, so it is never told twice."""
        with self._lock:
            told = {"ref": ref, "state": state, "text": text[:1200], "turn": turn, "at": _now()}
            self.insights.append(told)
            del self.insights[:-300]
            self._bump("insight", ref)
            return told

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
            if ref.startswith("H") and ref in self.hypotheses:
                h = self.hypotheses[ref]
                return {"id": ref, "type": "hypothesis", "title": h["statement"], "quote": f"{h['status']}, {h['confidence']}",
                        "doc": None, "source": "Sherlock's assessment"}
            if ref.startswith("W") and ref in self.statements:
                w = self.statements[ref]
                return {"id": ref, "type": "statement", "title": f"{w['speaker']} ({w['role']}, {w['kind']})",
                        "quote": w["text"][:400], "doc": w["doc"], "source": self.documents[w["doc"]]["title"]}
            if ref.startswith("X") and ref in self.contradictions:
                x = self.contradictions[ref]
                return {"id": ref, "type": "contradiction", "title": x.get("text") or "", "doc": None,
                        "quote": f"{x.get('severity')}, {x.get('status')}", "source": "Contradiction finder"}
            if ref.startswith("K") and ref in self.leads:
                k = self.leads[ref]
                return {"id": ref, "type": "lead", "title": k.get("hint") or "", "quote": k.get("status") or "",
                        "doc": None, "source": "Lead spotter"}
            q = next((q for q in self.questions if q["id"] == ref), None) if ref.startswith("Q") else None
            if q is not None:
                return {"id": ref, "type": "question", "title": q["text"], "quote": q.get("answer") or "",
                        "doc": None, "source": "Question ledger"}
        return None

    def known_ids(self) -> set[str]:
        """Every id an answer or the report may cite."""
        with self._lock:
            return (set(self.documents) | set(self.facts) | set(self.links) | set(self.hypotheses)
                    | {q["id"] for q in self.questions} | set(self.statements) | set(self.contradictions)
                    | set(self.leads))

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
                "incident": self.incident, "roles": dict(self.roles), "questions": list(self.questions),
                "assessment": self.assessment, "turns": len(self.conversation),
                "hypotheses": list(self.hypotheses.values()), "summary": self.summary, "gaps": self.gaps[:10],
                "followups": self.followups[-20:], "agenda": list(self.agenda),
                "statements": [{k: v for k, v in w.items() if k != "text"} | {"text": w["text"][:600]}
                               for w in self.statements.values()],
                "contradictions": list(self.contradictions.values()), "leads": list(self.leads.values()),
                "matrix": self.matrix, "challenges": self.challenges[-12:], "presence": self.presence,
                "register": {"cards": len(self.register.get("cards") or {}),
                             "pending": self.register.get("pending") or []},
                "claims": len(self.claims), "critical_waiting": list(self.dialog.get("critical_waiting") or []),
                "analysis": list(self.analysis.values()), "team_run": (self.dialog.get("team_runs") or [None])[-1],
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
            return {"version": self.version, "run_id": self.run_id, "documents": list(self.documents.values()),
                    "facts": list(self.facts.values()), "links": list(self.links.values()),
                    "attempts": list(self.attempts), "report": self.report, "report_status": self.report_status,
                    "incident": self.incident, "roles": dict(self.roles), "questions": list(self.questions),
                    "conversation": list(self.conversation), "assessment": self.assessment, "seen": dict(self.seen),
                    "dialog": dict(self.dialog), "hypotheses": list(self.hypotheses.values()), "summary": self.summary,
                    "gaps": list(self.gaps), "dossiers": dict(self.dossiers), "views": list(self.views),
                    "desk": list(self.desk), "followups": list(self.followups), "calls": list(self.calls),
                    "history": list(self.history), "agenda": list(self.agenda), "change_seq": self.change_seq,
                    "statements": list(self.statements.values()), "claims": list(self.claims.values()),
                    "contradictions": list(self.contradictions.values()), "leads": list(self.leads.values()),
                    "entities": list(self.entities.values()), "register": dict(self.register),
                    "matrix": self.matrix, "challenges": list(self.challenges), "presence": self.presence,
                    "analysis": dict(self.analysis), "insights": list(self.insights), "claimed": dict(self.claimed),
                    "traces": list(self.traces)}

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
        case.incident = data.get("incident") if isinstance(data.get("incident"), dict) else None
        case.roles = {str(k): str(v) for k, v in (data.get("roles") or {}).items()}
        case.questions = [q for q in data.get("questions") or [] if isinstance(q, dict) and q.get("id")]
        case.conversation = [t for t in data.get("conversation") or [] if isinstance(t, dict)]
        case.assessment = data.get("assessment") if isinstance(data.get("assessment"), dict) else None
        case.seen = {str(k): int(v) for k, v in (data.get("seen") or {}).items() if str(v).isdigit()}
        if isinstance(data.get("dialog"), dict):
            case.dialog = {"focus": [], "last_type": None, "announced": 0, **data["dialog"]}
        case.run_id = data.get("run_id") if isinstance(data.get("run_id"), str) else None
        case.hypotheses = {h["id"]: h for h in data.get("hypotheses") or [] if isinstance(h, dict) and h.get("id")}
        case.summary = data.get("summary") if isinstance(data.get("summary"), dict) else None
        case.dossiers = {str(k): v for k, v in (data.get("dossiers") or {}).items() if isinstance(v, dict)}
        for name in ("gaps", "views", "desk", "followups", "calls", "history", "agenda"):
            setattr(case, name, [x for x in data.get(name) or [] if isinstance(x, dict)])
        case.change_seq = int(data.get("change_seq") or 0)
        for name in ("statements", "claims", "contradictions", "leads", "entities"):
            setattr(case, name, {e["id"]: e for e in data.get(name) or [] if isinstance(e, dict) and e.get("id")})
        if isinstance(data.get("register"), dict):
            case.register = {"cards": {}, "pending": [], "custody": [], **data["register"]}
        for name in ("matrix", "presence"):
            setattr(case, name, data.get(name) if isinstance(data.get(name), dict) else None)
        case.challenges = [c for c in data.get("challenges") or [] if isinstance(c, dict)]
        case.insights = [c for c in data.get("insights") or [] if isinstance(c, dict)]
        case.analysis = {str(k): v for k, v in (data.get("analysis") or {}).items() if isinstance(v, dict)}
        case.claimed = {str(k): int(v) for k, v in (data.get("claimed") or {}).items() if isinstance(v, int)}
        case.traces = [t for t in data.get("traces") or [] if isinstance(t, dict)][-30:]
        return case
