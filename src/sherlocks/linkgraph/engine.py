"""Breadth-first expansion: search a person everywhere, then everyone they link to.

Control flow is plain code, not an agent. At each depth level:

1. Every searchable person at this level is searched in every selected system -
   identity systems first, because a phone-only person needs the SIMs database to
   yield a CNIC before the CNIC-only systems (CRO, EVS, TRACS...) can be asked.
2. Each system answer becomes a record node; each person that record names becomes a
   person node one level deeper, joined by a strong edge.
3. FIRs found in PSRMS are opened (the FIR report) to add everyone else they name.

It stops at the requested depth or when the person budget is spent, whichever comes
first. People not searched are still drawn, marked with why. When the budget cannot
cover a whole depth, the most promising people are searched first (``_priority``);
with several starting people it can stop as soon as they are joined
(``stop_when_connected``).

Budget matters here more than speed: each searched person is ~20 logged queries
against live police systems. Answers are cached (``cache.py``), FIR reports are
fetched once per run however many people share them, and nobody is searched twice.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from sherlocks.linkgraph.backends import LookupBackend
from sherlocks.linkgraph.cache import ProviderCache, cache_key
from sherlocks.linkgraph.dossier import criminal_flags
from sherlocks.linkgraph.extractors import extract_record
from sherlocks.linkgraph.graph import GraphBuilder
from sherlocks.linkgraph.images import ImageStore
from sherlocks.linkgraph.models import GraphRunParams, PersonRef, SystemRecord
from sherlocks.linkgraph.normalize import cnic13, mobile11, name_key, parse_address, valid_email
from sherlocks.linkgraph.systems import DEFAULT_SYSTEMS, IDENTITY_SYSTEMS, SYSTEMS, system_label
from sherlocks.linkgraph.weak_links import LlmAddressParser, LlmPairReviewer, compute_weak_links
from sherlocks.settings import PROJECT_ROOT, Settings

logger = logging.getLogger(__name__)

Emit = Callable[[str, str], None]

# How much a relation says about whether searching the person it names is worth it.
# An investigating officer is named in every FIR - searching them rarely adds a lead.
_RELATION_VALUE = (
    ("investigating officer", -1.0), ("accused", 2.0), ("suspect", 2.0), ("complainant", 1.5),
    ("vehicle", 1.5), ("driver", 1.5), ("landlord", 1.0), ("tenant", 1.0), ("sim", 1.0),
    ("employer", 1.0), ("family", 1.0), ("witness", 0.5),
)


class Cancelled(Exception):
    pass


class _SyntheticResult:
    """A ToolResult-shaped wrapper so a Caller ID payload can join the OSINT tool
    results the social classifier reads. Not a real tool run."""

    is_hit = True
    summary = ""

    def __init__(self, tool: str, raw: Any, *, query: str | None = None) -> None:
        self.tool = tool
        self.status = None
        self.data = {"raw": raw, "query": query or ""}


class Expansion:
    def __init__(
        self,
        *,
        backend: LookupBackend,
        cache: ProviderCache,
        images: ImageStore,
        settings: Settings,
        params: GraphRunParams,
        builder: GraphBuilder | None = None,
        emit: Emit | None = None,
        progress: Callable[[int, str], None] | None = None,
        checkpoint: Callable[[], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        llm: Any = None,
    ) -> None:
        self.backend = backend
        self.cache = cache
        self.images = images
        self.settings = settings
        self.params = params
        self.builder = builder or GraphBuilder()
        self.builder.max_shared_owners = settings.linkgraph.shared_identifier_max
        self._emit = emit or (lambda level, message: logger.info("%s: %s", level, message))
        self._progress = progress or (lambda pct, message: None)
        self._checkpoint = checkpoint or (lambda: None)
        self._cancelled = cancelled or (lambda: False)
        self.llm = llm
        # The universe of systems, and their order, is the backend's - EMS runs a
        # different set (ARMS, NADRA, Excise, AVLC…) than the Shield-gateway backend.
        # fir_roster / caller_id / osint are driven by their own steps, not the per-system loop.
        special = {"fir_roster", "caller_id", "osint"}
        universe = [s["system"] for s in backend.systems_status() if s["system"] not in special] or DEFAULT_SYSTEMS
        requested = params.systems or universe
        self.systems = [s for s in universe if s in requested]
        self.stats: Counter[str] = Counter()
        self._stats_lock = threading.Lock()
        self._firs: dict[str, str | None] = {}
        self._firs_lock = threading.Lock()
        self._searched: set[str] = set()
        # system -> last error message, so the run can report which APIs to fix and why.
        self._errors: dict[str, str] = {}

    # -- plumbing -------------------------------------------------------------------

    def _count(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self.stats[key] += n

    def _check(self) -> None:
        if self._cancelled():
            raise Cancelled()

    def _fetch(self, key: str, system: str, call: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        cached = self.cache.get(key)
        if cached is not None:
            self._count("cache_hits")
            return {**cached, "_cached": True}
        self._check()
        self._count("upstream_calls")
        payload = call()
        self.cache.put(key, system, payload)
        return payload

    def query(self, system: str, cnic: str | None, phone: str | None) -> dict[str, Any]:
        return self._fetch(cache_key(self.backend.name, system, cnic, phone), system,
                           lambda: self.backend.lookup(system, cnic, phone))

    # -- one record -----------------------------------------------------------------

    def _ingest(self, pid: str, system: str, payload: dict[str, Any], depth: int, *,
                record_key: str | None = None, edge_label: str | None = None,
                queried: str | None = None) -> tuple[list[str], SystemRecord | None]:
        status = str(payload.get("status") or "error")
        # Record which identifier was sent, so the analyst can see exactly what each
        # system was asked and why it answered as it did.
        on = f" [searched: {queried}]" if queried else ""
        self.builder.note_lookup(pid, system, status, str(payload.get("summary") or "") + on)
        if not payload.get("hit"):
            if status == "error":
                message = str(payload.get("summary") or (payload.get("errors") or ["lookup failed"])[0])
                self._count("errors")
                if system not in self._errors:  # first failure of this system: one clear line
                    self._emit("error", f"{system_label(system)} FAILED on {queried or 'this person'} — "
                                        f"{message}. Fix this API; treated as unknown, not 'no record'.")
                self._errors[system] = f"on {queried}: {message}" if queried else message
            return [], None
        rec = extract_record(system, payload, self.builder.person_ref(pid), self.images,
                             related_cap=self.params.related_per_record)
        # A "hit" that yielded nothing usable (empty envelope, or a payload the extractor
        # could make no sense of) must not become a node. Belt-and-braces on top of the
        # backend's own no_record handling, and it also catches stale pre-fix cache rows.
        if not (rec.fields or rec.related or rec.firs or rec.stays or rec.vehicles
                or rec.flags or rec.organisations or rec.subject.images
                or rec.subject.name or rec.subject.cnic or rec.subject.phones):
            self.builder.note_lookup(pid, system, "no_record", "empty record — no node")
            return [], None
        sid, pid = self.builder.add_record(pid, rec, record_key=record_key, edge_label=edge_label)
        self._count("hits")
        who = self.builder.person(pid).get("name") or "subject"
        extra = f" → {len(rec.related)} linked person(s)" if rec.related else ""
        on = f" (searched {queried})" if queried else ""
        self._emit("hit", f"{who} · {system_label(system)}{on}: {rec.summary}{extra}{' (cached)' if rec.cached else ''}")
        found: list[str] = []
        for rel in rec.related:
            rpid, _ = self.builder.link_related(sid, rel, depth=depth + 1, from_pid=pid)
            found.append(rpid)
        return found, rec

    def _run_system(self, pid: str, system: str, cnic: str | None, phone: str | None, depth: int,
                    record_key: str | None = None) -> tuple[list[str], SystemRecord | None]:
        self._check()
        info = SYSTEMS[system]
        if info.needs == "cnic" and not cnic or info.needs == "phone" and not phone:
            self.builder.note_lookup(pid, system, "skipped", f"needs {info.needs}")
            return [], None
        # What this system was actually asked with, for the activity log.
        if info.needs == "cnic":
            queried = cnic
        elif info.needs == "phone":
            queried = phone
        else:
            queried = " & ".join(x for x in (cnic, phone) if x)
        try:
            payload = self.query(system, cnic, phone)
            return self._ingest(pid, system, payload, depth, record_key=record_key, queried=queried)
        except Cancelled:
            raise
        except Exception as exc:  # one broken system - or one malformed record - must not end the run
            logger.exception("Lookup %s failed", system)
            payload = {"provider": system, "hit": False, "status": "error", "summary": f"{type(exc).__name__}: {exc}"}
            return self._ingest(pid, system, payload, depth, record_key=record_key, queried=queried)

    # -- one person -----------------------------------------------------------------

    def search_person(self, pid: str, depth: int) -> list[str]:
        builder = self.builder
        pid = builder.canonical(pid)
        data = builder.person(pid)
        builder.set_search_status(pid, "searching")
        self._emit("info", f"Searching {data.get('name') or data.get('cnic') or data['phones'][0]} "
                           f"(depth {depth}) in {len(self.systems)} systems")
        found: list[str] = []
        firs: list[Any] = []

        def run(system: str, cnic: str | None, phone: str | None, key: str | None = None) -> None:
            nonlocal pid
            new, rec = self._run_system(pid, system, cnic, phone, depth, key)
            pid = builder.canonical(pid)
            found.extend(new)
            if rec is not None and system == "psrms":
                firs.extend(f for f in rec.firs if f.ps_id)

        def ids() -> tuple[str | None, str | None]:
            d = builder.person(pid)
            return d.get("cnic"), (d.get("phones") or [None])[0]

        # 1. Identity: a phone resolves to a CNIC; a CNIC to its SIMs.
        done: set[str] = set()
        cnic, phone = ids()
        if not cnic and phone:
            for system in (s for s in IDENTITY_SYSTEMS if s in self.systems):
                run(system, None, phone)
                done.add(system)
                cnic, phone = ids()
                if cnic:
                    break
        if cnic and not phone and "subscriber" in self.systems:
            run("subscriber", cnic, None)
            done.add("subscriber")
        cnic, phone = ids()

        # 2. Everything else, in parallel.
        rest = [s for s in self.systems if s not in done]
        with ThreadPoolExecutor(max_workers=max(1, self.settings.linkgraph.parallel_systems)) as pool:
            futures = [pool.submit(run, system, cnic, phone) for system in rest]
            for future in as_completed(futures):
                future.result()

        # 2b. Backfill by CNIC. A phone-only search may not resolve a CNIC from the SIMs
        #     database, yet PRVS / DLS / PSRMS / Old Tenant return one on the record. When
        #     that happens the CNIC-only systems (NADRA, CRO, ARMS, Watchlist…) already ran
        #     with nothing. Re-run every CNIC-capable system with the discovered CNIC so
        #     NADRA can confirm the person's identity from a CNIC found elsewhere.
        found_cnic, phone = ids()
        if found_cnic and not cnic:
            cnic = found_cnic
            backfill = [s for s in self.systems if SYSTEMS[s].needs in ("cnic", "either") and s not in IDENTITY_SYSTEMS]
            if backfill:
                self._emit("info", f"CNIC {found_cnic} found via another system — re-checking "
                                   f"{len(backfill)} systems by CNIC (NADRA, CRO, …) to confirm identity")
                with ThreadPoolExecutor(max_workers=max(1, self.settings.linkgraph.parallel_systems)) as pool:
                    for future in as_completed([pool.submit(run, s, found_cnic, None) for s in backfill]):
                        future.result()

        # 3. Every OTHER number this person holds. A person's SIMs can each carry their
        #    own records - a licence registered on one, a hotel check-in on another - and
        #    each SIM can belong to a different owner, so every number is run through the
        #    phone-driven systems, not just the first. Each number's answer is a distinct
        #    record node (record_key carries the number). CNIC-keyed systems (CRO, EVS,
        #    TRACS...) already cover the whole person via the CNIC and are not repeated.
        others = [p for p in builder.person(pid)["phones"] if p != phone][: max(0, self.settings.linkgraph.max_numbers_per_person - 1)]
        phone_systems = [s for s in self.systems if SYSTEMS[s].needs in ("phone", "either")]
        for other in others:
            self._emit("info", f"Also searching {builder.person(pid).get('name') or 'this person'}'s number {other} in {len(phone_systems)} systems")
            for system in phone_systems:
                run(system, None, other, f"{pid}:{other}")

        # 4. FIR reports: everyone else each FIR names.
        if self.params.include_fir_rosters:
            for fir in firs[: self.settings.linkgraph.max_firs_per_person]:
                found.extend(self._fir_roster(pid, fir, depth))

        # 5. Caller ID name tags - one per known number (each SIM can carry its own tags).
        #    Skipped on backends that do not provide it (EMS), so it never shows as FAILED.
        if self.params.include_caller_id and getattr(self.backend, "supports_caller_id", True):
            for number in ([phone] if phone else []) + others:
                payload = self._fetch(cache_key(self.backend.name, "caller_id", None, number), "caller_id",
                                      lambda n=number: self.backend.caller_id(n))
                new, _ = self._ingest(pid, "caller_id", payload, depth)
                found.extend(new)

        builder.set_search_status(pid, "searched")
        return found

    def _fir_roster(self, pid: str, fir: Any, depth: int) -> list[str]:
        key = fir.key
        with self._firs_lock:
            known = key in self._firs
            if not known:
                self._firs[key] = None
        if known:
            sid = self._firs.get(key)
            if sid:
                self.builder.attach(pid, sid, fir.role or "")
            return []
        payload = self._fetch(cache_key(self.backend.name, "fir_roster", None, key), "fir_roster",
                              lambda: self.backend.fir_roster(fir.fir_no, fir.fir_year, fir.ps_id))
        found, _ = self._ingest(pid, "fir_roster", payload, depth, record_key=key, edge_label=fir.role or "")
        if payload.get("hit"):
            with self._firs_lock:
                self._firs[key] = f"s:fir_roster:{key}"
        return found

    # -- the run --------------------------------------------------------------------

    def seed(self) -> str:
        cnic = cnic13(self.params.cnic)
        phone = mobile11(self.params.phone)
        if not (cnic or phone):
            raise ValueError("Give a valid 13-digit CNIC or a Pakistani mobile number (03XXXXXXXXX).")
        pid, _ = self.builder.upsert_person(PersonRef(cnic=cnic, phones=[phone] if phone else []), depth=0, seed=True)
        return pid

    def seed_all(self) -> list[str]:
        """All starting people. In multi-person mode each ``seeds`` entry is a starting
        person; otherwise the single cnic/phone/email. Every seed is marked, so the
        relations between them can be reported afterwards."""
        entries = list(self.params.seeds) if self.params.seeds else [
            type("S", (), {"cnic": self.params.cnic, "phone": self.params.phone, "email": self.params.email})()]
        pids: list[str] = []
        self.email_seeds: set[str] = set()
        for entry in entries:
            cnic, phone = cnic13(getattr(entry, "cnic", None)), mobile11(getattr(entry, "phone", None))
            email = valid_email(getattr(entry, "email", None))
            name = None
            if email and not (cnic or phone):
                name, phone, cnic = self._resolve_email(email)
            if not (cnic or phone or email):
                continue
            ref = PersonRef(cnic=cnic, name=name, phones=[phone] if phone else [])
            pid, _ = self.builder.upsert_person(ref, depth=0, seed=True)
            if email:
                data = self.builder.person(pid)
                data.setdefault("emails", [])
                if not any(e.get("email") == email for e in data["emails"]):
                    data["emails"].append({"email": email, "source": "search input"})
                if not (cnic or phone or name):
                    self.builder.nodes[pid].label = email   # nothing else to call them yet
                self.email_seeds.add(pid)
            if pid not in pids:
                pids.append(pid)
        if not pids:
            raise ValueError("Give at least one valid 13-digit CNIC, Pakistani mobile number (03XXXXXXXXX) or email.")
        return pids

    def _resolve_email(self, email: str) -> tuple[str | None, str | None, str | None]:
        """An email with no CNIC/phone: who is it? PRVS - the one police system that
        can be searched by email - is asked first, and gives a CNIC. Otherwise Pipl and
        FullContact, where a Pakistani mobile starts the police-system search. Returns
        ``(name, mobile, cnic)``."""
        from sherlocks.osint.custom_tools import fullcontact_lookup, pipl_lookup

        by_email = getattr(self.backend, "lookup_email", None)
        if callable(by_email):
            payload = self._fetch(cache_key(self.backend.name, "prvs_email", None, email), "prvs",
                                  lambda: by_email(email))
            profiles = (payload.get("raw") or {}).get("profiles") or [] if payload.get("hit") else []
            for prof in profiles:
                pd = prof.get("person_details") or {}
                cnic, phone = cnic13(pd.get("cnic")), mobile11(pd.get("mobile"))
                if cnic or phone:
                    self._emit("hit", f"Email {email} is on the PRVS profile of {pd.get('name') or 'someone'}"
                                      f" (CNIC {pd.get('cnic') or '-'}) - searching the police systems with it")
                    return pd.get("name"), phone, cnic
            if payload.get("status") == "error":
                self._emit("warn", f"PRVS search by email {email} failed: {payload.get('summary')}")

        osint = self.settings.osint
        if not (osint.pipl_api_key or osint.fullcontact_api_key):
            where = "not on PRVS" if callable(by_email) else "this data source cannot search by email"
            self._emit("warn", f"Search by email {email}: {where}, and no PIPL_API_KEY or FULLCONTACT_API_KEY to "
                               "resolve it to a phone - the police systems will not be searched, only OSINT.")
            return None, None, None
        name = phone = None
        if osint.pipl_api_key:
            try:
                body = pipl_lookup(osint.pipl_api_key, email=email) or {}
                person = body.get("person") or {}
                name = next((n.get("display") for n in person.get("names") or [] if n.get("display")), None)
                phone = next((mobile11(p.get("display_international") or p.get("number") or p.get("display"))
                              for p in person.get("phones") or []
                              if mobile11(p.get("display_international") or p.get("number") or p.get("display"))), None)
            except Exception as exc:  # noqa: BLE001
                self._emit("warn", f"Pipl lookup of {email} failed: {type(exc).__name__}: {exc}")
        if osint.fullcontact_api_key and not phone:
            try:
                body = fullcontact_lookup(osint.fullcontact_api_key, email=email) or {}
                name = name or body.get("fullName")
                phone = next((mobile11(p.get("value")) for p in (body.get("details") or {}).get("phones") or []
                              if mobile11(p.get("value"))), None)
            except Exception as exc:  # noqa: BLE001
                self._emit("warn", f"FullContact lookup of {email} failed: {type(exc).__name__}: {exc}")
        if phone:
            self._emit("hit", f"Email {email} belongs to {name or 'someone'} with Pakistani mobile {phone} "
                              "(person-data API) - searching the police systems with that number")
        else:
            self._emit("info", f"Email {email}: {'found ' + name if name else 'no person found'}"
                               ", but no Pakistani mobile - searching it online only")
        return name, phone, None

    def _priority(self, pid: str, origins: dict[str, set[str]]) -> tuple[float, str]:
        """How much searching this person is likely to add, and why - read from what is
        already known about them. Only orders the queue; never skips anyone the budget
        can afford."""
        data = self.builder.person(pid)
        score, why = 0.0, []
        if criminal_flags(data):
            score += 3
            why.append("criminal footprint")
        elif "fir_record" in (data.get("flags") or []):
            score += 1
            why.append("named in an FIR")
        best, best_relation = 0.0, ""
        for via in data.get("discovered_via") or []:
            relation = str(via.get("relation") or "")
            value = next((v for k, v in _RELATION_VALUE if k in relation.lower()), 0.5)
            if value > best:
                best, best_relation = value, relation
        score += best
        if best >= 1.5:
            why.append(f"named as: {best_relation}")
        sources = {v.get("from") for v in data.get("discovered_via") or []}
        if len(sources) > 1:
            score += len(sources) - 1
            why.append(f"named by {len(sources)} people")
        if len(origins.get(pid, ())) > 1:
            score += 5
            why.append("reached from more than one starting person")
        if data.get("cnic"):
            score += 0.5
        return score, ", ".join(why)

    def _seeds_joined(self, seeds: list[str]) -> bool:
        """Are all starting people in one piece, counting stated links only?"""
        import networkx as nx

        from sherlocks.linkgraph.network import PersonNetwork

        net = PersonNetwork(self.builder.snapshot())
        stated = net.G.edge_subgraph([(u, v) for u, v, d in net.G.edges(data=True) if d["stated"]])
        live = [self.builder.canonical(s) for s in seeds]
        if not all(s in stated for s in live):
            return False
        return len({min(nx.node_connected_component(stated, s)) for s in live}) == 1

    def run(self) -> dict[str, Any]:
        builder, params = self.builder, self.params
        seeds = self.seed_all()
        level, queued = list(seeds), set(seeds)
        max_persons = params.max_persons
        # Which starting people each person was reached from: someone reached from two
        # of them is where their networks meet.
        origins: dict[str, set[str]] = {s: {s} for s in seeds}
        for depth in range(params.depth):
            self._check()
            if params.stop_when_connected and len(seeds) > 1 and depth > 0 and self._seeds_joined(seeds):
                self._emit("info", "All starting people are now joined by stated links - stopping here "
                                   "(stop when connected). Remaining budget unspent.")
                break
            candidates = list(dict.fromkeys(builder.canonical(p) for p in level))
            if params.strategy == "priority":
                ranked = sorted(((self._priority(p, origins), p) for p in candidates), key=lambda x: -x[0][0])
                candidates = [p for _, p in ranked]
                budget_left = max_persons - len(self._searched)
                if len(candidates) > budget_left > 0:
                    first = ranked[0]
                    self._emit("info", f"Depth {depth}: budget covers {budget_left} of {len(candidates)} - most "
                                       f"promising first: {builder.person(first[1]).get('name') or first[1]}"
                                       + (f" ({first[0][1]})" if first[0][1] else ""))
            todo: list[str] = []
            for pid in candidates:
                data = builder.person(pid)
                if pid in self._searched or data["search_status"] == "searched":
                    continue
                if not data["searchable"]:
                    builder.set_search_status(pid, "not_searchable")
                elif len(self._searched) + len(todo) >= max_persons:
                    builder.set_search_status(pid, "budget")
                else:
                    todo.append(pid)
            if not todo:
                break
            self._searched.update(todo)
            self._emit("info", f"Depth {depth}: searching {len(todo)} person(s)")
            next_level: list[str] = []
            with ThreadPoolExecutor(max_workers=max(1, self.settings.linkgraph.parallel_persons)) as pool:
                futures = {pool.submit(self.search_person, pid, depth): pid for pid in todo}
                for index, future in enumerate(as_completed(futures), start=1):
                    found = future.result()
                    source = futures[future]
                    for other in found:
                        origins.setdefault(builder.canonical(other), set()).update(
                            origins.get(source, ()) | origins.get(builder.canonical(source), ()))
                    next_level.extend(found)
                    done = len(self._searched)
                    self._progress(min(90, int(90 * done / max(1, min(max_persons, done + len(todo) - index)))),
                                   f"Searched {done} person(s) at depth ≤ {depth}")
                    self._checkpoint()
            level = [p for p in dict.fromkeys(builder.canonical(p) for p in next_level) if p not in queued]
            queued.update(level)

        for node in builder.persons():
            if node.data["search_status"] == "pending":
                builder.set_search_status(node.id, "depth_limit" if node.data["searchable"] else "not_searchable")

        self._progress(92, "Deriving shared-record links")
        derived = builder.derive_shared_records()
        # Anyone searched by email gets the online search - the email is what they were
        # given by, and no police system can search by it.
        email_only = [s for s in self.email_seeds if s not in (seeds[:1] if params.include_osint else [])]
        for target in email_only:
            self._check()
            self._progress(92, "Searching the email online")
            self._osint(builder.canonical(target), full_tools=True)
            self._checkpoint()
        if params.include_osint:
            targets = self._osint_targets(seeds[0])
            if len(targets) > 1:
                self._emit("info", f"OSINT for {len(targets)} people (subject + {len(targets) - 1} found)")
            for index, target in enumerate(targets):
                self._check()
                self._progress(92, f"OSINT {index + 1}/{len(targets)}")
                self._osint(builder.canonical(target), full_tools=(index == 0))
                self._checkpoint()
        self._progress(95, "Scoring weak links" + (" (AI reviewing candidate pairs)" if self.llm else ""))
        ai = reviewer = None
        if params.ai_address_matching and self.llm is not None:
            ai = LlmAddressParser(self.llm, budget=self.settings.linkgraph.ai_address_calls_per_run)
            reviewer = LlmPairReviewer(self.llm, budget=self.settings.linkgraph.ai_pair_calls_per_run)
        weak = compute_weak_links(builder, min_score=self.settings.linkgraph.weak_min_score, ai=ai, reviewer=reviewer)
        ai_note = ""
        if ai and reviewer:
            ai_note = (f" · AI: {ai.calls} address parse(s), {reviewer.calls} pair review(s), "
                       f"{reviewer.kept} kept with evidence")
        self._emit("info", f"Derived {derived} shared-record link(s) and {weak} weak link(s){ai_note}")
        if self._errors:
            listed = ", ".join(sorted(self._errors))
            self._emit("error", f"{len(self._errors)} system(s) FAILED and need fixing: {listed}. "
                                "See 'Failed systems' for the error of each.")
        # Zero counts are information too ("0 upstream calls: all from cache"), and a
        # Counter omits them. failed_systems carries the message per broken API.
        return {"upstream_calls": 0, "cache_hits": 0, "hits": 0, "errors": 0,
                **builder.stats(), **self.stats, "failed_systems": dict(self._errors)}

    # -- OSINT ----------------------------------------------------------------------

    def _web_search(self) -> Any:
        """The free name-search client. A seam so tests never touch a real engine."""
        from sherlocks.linkgraph.websearch import FreeWebSearch

        log_dir = Path(self.settings.app.log_dir)
        return FreeWebSearch(delay_seconds=self.settings.osint.web_search_delay_seconds,
                             log_dir=log_dir if log_dir.is_absolute() else PROJECT_ROOT / log_dir)

    def _record_emails(self, pid: str) -> list[tuple[str, str]]:
        """(email, system) for every email this person's own records give *for them*.
        A record can name other people (a landlord, a reference) with their own emails,
        so an email counts only when the same entry names this person - by CNIC, phone
        or name - or sits in a field the extractor already attributed to them."""
        from sherlocks.osint.match import emails_in

        data = self.builder.person(pid)
        me_cnic, me_phones = data.get("cnic"), {p[-10:] for p in data.get("phones") or []}
        me_name = name_key(data.get("name"))
        found: dict[str, str] = {}

        def mine(entry: dict) -> bool:
            text = " ".join(str(v) for v in entry.values() if isinstance(v, (str, int)))
            digits = "".join(c for c in text if c.isdigit())
            if me_cnic and me_cnic in digits:
                return True
            if any(p and p in digits for p in me_phones):
                return True
            names = [str(v) for k, v in entry.items() if "name" in str(k).lower() and "father" not in str(k).lower()]
            return bool(me_name) and any(fuzz.token_set_ratio(name_key(n), me_name) >= 90 for n in names)

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                own = [e for k, v in value.items() if "mail" in str(k).lower() for e in emails_in(v)]
                if own and mine(value):
                    for e in own:
                        found.setdefault(e, source)
                for v in value.values():
                    walk(v)
            elif isinstance(value, list):
                for v in value:
                    walk(v)

        for entry in data.get("records") or []:
            node = self.builder.nodes.get(entry["node"])
            if node is None or entry["system"] in ("osint", "fir_roster"):
                continue
            source = system_label(entry["system"])
            for f in node.data.get("fields") or []:
                if "mail" in str(f.get("label", "")).lower():
                    for e in emails_in(f.get("value")):
                        found.setdefault(e, source)
            walk(node.data.get("raw"))
        for e in emails_in({k: v for k, v in (data.get("extra") or {}).items() if "mail" in k.lower()}):
            found.setdefault(e, "records")
        return list(found.items())

    def _osint_targets(self, seed: str) -> list[str]:
        """Who gets an online search. The subject always; with scope ``everyone``, the
        other named people too - nearest first, and those with an address or employer
        (which is what can confirm a profile) ahead of bare names."""
        targets = [seed]
        if self.params.osint_scope != "everyone":
            return targets
        others = [n for n in self.builder.persons()
                  if n.id != seed and n.data.get("name") and len(str(n.data.get("name")).split()) >= 2]
        others.sort(key=lambda n: (n.data.get("depth", 9), not (n.data["addresses"] or n.data["organisations"])))
        return targets + [n.id for n in others[: max(0, self.params.max_osint_people - 1)]]

    def _osint(self, pid: str, *, full_tools: bool = True) -> None:
        """Online footprint + social-media profiles for one person.

        Runs the OSINT tools (for the subject; ``full_tools``), and a free name search on
        the social platforms for anyone with a name, then classifies every discovered URL
        into a platform and scores whether it belongs to this person by corroborating it
        against their own name, address (city/area) and employer - the "confirm by
        personal info" step. Their Caller ID result (name + Facebook link) is folded in,
        since it is the one social path that works from a phone number. Everything
        stays a lead: findings never become a strong link.
        """
        if not self.settings.osint.enabled:
            self._emit("warn", "OSINT requested but osint.enabled is false on this deployment - skipped")
            return
        from sherlocks.linkgraph.social import (
            REQUIRED_PLATFORMS,
            SubjectInfo,
            phone_channels,
            platform_report,
            profiles_from_tool_results,
        )
        from sherlocks.osint.models import OsintSubject

        data = self.builder.person(pid)
        city = next((parse_address(a).get("city") for a in data["addresses"] if parse_address(a).get("city")), None)
        # Emails given in the search, then the ones the person's own records carry
        # (NADRA, SBVS, HRMIS… often hold one). The first drives the main scan; every
        # other one is pivoted on below.
        emails: dict[str, str] = {e["email"]: e["source"] for e in data.get("emails") or [] if e.get("email")}
        for email, source in self._record_emails(pid):
            emails.setdefault(email, source)
        subject = OsintSubject(full_name=data.get("name"), phone=(data.get("phones") or [None])[0],
                               cnic=data.get("cnic"), city=city, employer=(data["organisations"] or [None])[0],
                               email=next(iter(emails), None))
        if subject.is_empty():
            return
        self._emit("info", f"OSINT / social search for {subject.label()}"
                           + (f" in {city.title()}" if city else "")
                           + (" (can take minutes)" if full_tools else " (name search)"))

        results: list[Any] = []
        discovered: list[dict] = []
        if full_tools:
            from sherlocks.services.osint_service import build_agent

            try:
                agent = build_agent(self.settings)
                plan = agent.plan(subject)
                results = list(agent.client.run_planned(plan.tools)) if plan.tools else []
                discovered = agent.extract_identifiers(results, subject)
                skipped = sorted({f"{t.tool}: {t.skip_reason}" for t in plan.tools if getattr(t, "skip_reason", None)})
                if skipped:
                    self._emit("warn", "OSINT tools skipped - " + "; ".join(skipped))
            except Exception as exc:
                logger.exception("OSINT failed")
                self._emit("warn", f"OSINT tools failed: {type(exc).__name__}: {exc}")

        # Every email found anywhere - the records, or the tools' own results - is searched
        # online too (accounts, breaches, the person behind it). With none at all, the
        # name is searched on the person-data APIs and a result is kept only when the
        # records corroborate it; its emails are then searched the same way.
        pivot = _Pivot(self, pid)
        for item in discovered:
            if item.get("kind") == "email":
                emails.setdefault(str(item["value"]).lower(), f"OSINT ({item.get('tool')})")
        searched = {subject.email} if full_tools and subject.email else set()
        name_matches: list[Any] = []
        if not emails:
            name_matches, found = pivot.name_match(city)
            emails.update(found)
        more_results, more_found, searched = pivot.emails(emails, searched)
        results += more_results
        discovered += more_found
        match_usernames = [u for m in name_matches if m.status == "corroborated" for u in m.usernames]
        for m in name_matches:
            if m.status == "corroborated" and m.urls:
                results.append(_SyntheticResult("search_footprint", " ".join(m.urls)))

        # Free name search on the social platforms. Bright Data, when configured, is
        # the deep search; this is what finds anything at all without it.
        web_note = None
        free_search = bool(self.settings.osint.free_web_search and data.get("name"))
        if free_search:
            web = self._web_search().find_profiles(
                str(data["name"]), city, max_queries=self.settings.osint.web_search_queries_per_person)
            if web.hits:
                urls = " ".join(h.url for h in web.hits)
                results.append(_SyntheticResult("search_footprint", urls))
                for hit in web.hits:
                    results.append(_SyntheticResult("scrape_url", hit.text, query=hit.url))
            web_note = (f"free name search: {web.queries} quer{'y' if web.queries == 1 else 'ies'}"
                        f"{' via ' + '/'.join(web.engines) if web.engines else ''}, {len(web.hits)} profile result(s)")
            if web.stopped:
                web_note += f" - stopped: {web.stopped}"
                self._emit("warn", f"Free name search for {data['name']} stopped early: {web.stopped}")

        # Fold the person's Caller ID result (from step 5 of their search) into the
        # profile hunt: on a phone-only subject it is the sole social signal.
        caller = next((r for r in data["records"] if r["system"] == "caller_id"), None)
        caller_node = self.builder.nodes.get(caller["node"]) if caller else None
        if caller_node is not None:
            results.append(_SyntheticResult("caller_id", caller_node.data.get("raw")))

        info = SubjectInfo.build(name=data.get("name"), addresses=data["addresses"],
                                 organisations=data["organisations"],
                                 usernames=[str(d["value"]) for d in discovered if d.get("kind") == "username"]
                                 + match_usernames)
        profiles = profiles_from_tool_results(results, info) + phone_channels(data["phones"])

        # The required platforms first, always - each reported found-or-not - then the
        # secondary ones that turned up something.
        report = platform_report(profiles, data["phones"], brightdata_ready=self.settings.osint.brightdata_ready,
                                 free_search=free_search)
        confirmed = [p for p in profiles if p.status == "corroborated"]
        found_required = [r for r in report if r.required and r.found and r.platform not in ("WhatsApp",)]
        rec = SystemRecord(
            system="osint", status="success", hit=True,
            summary=(f"Social: {len(found_required)}/{len(REQUIRED_PLATFORMS)} core platforms found, "
                     f"{len(confirmed)} corroborated. {len(discovered)} other lead(s). All unverified."),
        )
        if web_note:
            rec.add_field("Searched", web_note)
        for email, source in emails.items():
            rec.add_field("Email", f"{email} - from {source}" + (" · searched online" if email in searched else ""))
        for m in name_matches[:3]:
            mark = {"corroborated": "✓", "possible": "?", "rejected": "✗"}[m.status]
            rec.add_field(f"Name match ({m.source})", f"{mark} {m.name} [{m.status}, {m.score:.2f}]: "
                                                      + ("; ".join(m.reasons) or "nothing in common with the records"))
        for row in report:
            best = row.best
            if best and (best.url or best.username):
                mark = "✓" if best.status == "corroborated" else "?"
                rec.add_field(row.platform, f"{mark} {best.url or best.username} [{best.confidence:.2f} · {row.note}]")
            else:
                rec.add_field(row.platform, f"— {row.note}")
        for item in discovered[:30]:
            if item.get("kind") not in ("url", "username"):
                rec.add_field(str(item.get("kind", "lead")).title(), f"{item.get('value')} ({item.get('tool')})")
        rec.raw = {
            "tools": [{"tool": getattr(r, "tool", "?"), "status": getattr(getattr(r, "status", None), "value", "?"),
                       "summary": getattr(r, "summary", "")} for r in results],
            "platform_report": [r.to_dict() for r in report],
            "profiles": [p.to_dict() for p in profiles],
            "discovered": discovered[:200],
            "web_search": web_note,
            "emails": [{"email": e, "source": src, "searched": e in searched} for e, src in emails.items()],
            "name_matches": [m.to_dict() for m in name_matches],
        }
        if rec.hit:
            _, pid = self.builder.add_record(pid, rec)
            osint_leads = [{"kind": d.get("kind"), "value": d.get("value")} for d in discovered[:200]]
            # Profile URLs are cross-person signals: two people whose name searches land on
            # the same account are worth a look. Corroborated ones carry most weight.
            osint_leads += [{"kind": "profile", "value": p.url or p.username} for p in confirmed]
            osint_leads += [{"kind": "email", "value": e} for e in emails
                            if not any(x.get("kind") == "email" and x.get("value") == e for x in osint_leads)]
            self.builder.person(pid)["osint"] = osint_leads
            self.builder.person(pid)["emails"] = [{"email": e, "source": src} for e, src in emails.items()]
            self._emit("hit", f"OSINT {data.get('name') or ''}: {rec.summary}".replace("  ", " "))


class _Pivot:
    """Second-round OSINT for one person: search each email found, and - when there is
    none - find the person by name on the person-data APIs, checked against the records."""

    def __init__(self, expansion: Expansion, pid: str) -> None:
        self.x, self.pid = expansion, pid
        self.osint = expansion.settings.osint
        self._agent: Any = None

    def _agent_or_none(self) -> Any:
        if self._agent is None:
            from sherlocks.services.osint_service import build_agent

            try:
                self._agent = build_agent(self.x.settings)
            except Exception as exc:  # noqa: BLE001
                self.x._emit("warn", f"Email search unavailable: {type(exc).__name__}: {exc}")
                self._agent = False
        return self._agent or None

    def emails(self, emails: dict[str, str], searched: set[str]) -> tuple[list[Any], list[dict], set[str]]:
        """Run the email tools (accounts, breaches, Pipl, FullContact, Hunter…) on every
        email not yet searched, up to ``osint.email_pivots``. New emails the results
        reveal are added to ``emails`` - one level deep, never a chain."""
        from sherlocks.osint.models import OsintSubject

        results: list[Any] = []
        discovered: list[dict] = []
        # A queue: emails the first searches reveal are searched too, but emails *those*
        # reveal are only recorded - one level of pivoting, within the budget.
        queue = [(e, 0) for e in emails if e not in searched]
        budget = max(0, self.osint.email_pivots)
        while queue and budget > 0:
            email, level = queue.pop(0)
            if email in searched:
                continue
            budget -= 1
            self.x._check()
            agent = self._agent_or_none()
            if agent is None:
                break
            self.x._emit("info", f"Email {email} (from {emails[email]}) - searching it online")
            try:
                plan = agent.plan(OsintSubject(email=email))
                runnable = [t for t in plan.tools if not t.skip_reason]
                found = list(agent.client.run_planned(runnable)) if runnable else []
            except Exception as exc:  # noqa: BLE001 - one bad email must not end the OSINT pass
                self.x._emit("warn", f"Email search for {email} failed: {type(exc).__name__}: {exc}")
                continue
            searched.add(email)
            results += found
            new = agent.extract_identifiers(found, OsintSubject(email=email))
            discovered += new
            hits = sum(1 for r in found if getattr(r, "is_hit", False))
            self.x._emit("hit" if hits else "info", f"Email {email}: {hits} tool(s) found something"
                                                     + (f", {len(new)} new lead(s)" if new else ""))
            for item in new:
                value = str(item.get("value") or "").lower()
                if item.get("kind") == "email" and value not in emails:
                    emails[value] = f"OSINT ({item.get('tool')}) via {email}"
                    if level == 0:
                        queue.append((value, 1))
        return results, discovered, searched

    def name_match(self, city: str | None) -> tuple[list[Any], dict[str, str]]:
        """No email known: look the name up (Pipl: name + city; Hunter: name + employer)
        and keep only candidates the records corroborate. Returns the scored candidates
        and the emails of the corroborated ones."""
        from sherlocks.osint.custom_tools import hunter_find_email, pipl_name_search
        from sherlocks.osint.match import KnownFacts, match_pipl

        data = self.x.builder.person(self.pid)
        name = data.get("name")
        if not (name and self.osint.name_match):
            return [], {}
        known = KnownFacts.from_person(data)
        matches: list[Any] = []
        emails: dict[str, str] = {}
        if self.osint.pipl_api_key:
            try:
                matches = match_pipl(known, pipl_name_search(name, self.osint.pipl_api_key, city=city))
            except Exception as exc:  # noqa: BLE001
                self.x._emit("warn", f"Pipl name search failed: {type(exc).__name__}: {exc}")
            kept = [m for m in matches if m.status == "corroborated"]
            self.x._emit("hit" if kept else "info",
                         f"Name search '{name}'{f' in {city.title()}' if city else ''} (Pipl): {len(matches)} candidate(s), "
                         f"{len(kept)} matching the records" + (f" - {'; '.join(kept[0].reasons)}" if kept else ""))
            for m in kept:
                for e in m.emails:
                    emails.setdefault(e, f"Pipl name match ({', '.join(m.reasons[:2])})")
        if not emails and self.osint.hunter_api_key and data.get("organisations"):
            org = data["organisations"][0]
            try:
                hit = hunter_find_email(name, org, self.osint.hunter_api_key)
            except Exception as exc:  # noqa: BLE001
                hit = None
                self.x._emit("warn", f"Hunter email-finder failed: {type(exc).__name__}: {exc}")
            if hit and int(hit.get("score") or 0) >= 70:
                emails[str(hit["email"]).lower()] = f"Hunter (name + employer {org}, confidence {hit.get('score')})"
                self.x._emit("hit", f"Hunter: likely work email for {name} at {org}: {hit['email']} ({hit.get('score')}%)")
        if not (self.osint.pipl_api_key or self.osint.hunter_api_key):
            self.x._emit("info", f"No email known for {name}; name matching needs PIPL_API_KEY or HUNTER_API_KEY")
        return matches, emails
