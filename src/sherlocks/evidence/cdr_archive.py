"""CDR archive (iCOP): the CDRs officers have already extracted, searched by a number.

The archive answers for a number wherever it appears - as the A party (the subscriber whose CDR was
extracted) or as the B party (one of that subscriber's contacts) - with one record per CDR file and a link to
download it. Each file found is fetched and read like an upload: the CDR team analyses it (``cdr.py``,
``cdr_team.py``), its findings become facts with their rows, and the CDR analyst can compute over it.

Two ways in:

* **automatically** (``sweep``, from the case team's agenda, in a thread of its own): every number of every
  target, then every number of each person tied directly to a target - never a police officer there on duty,
  nor the complainant or a witness of an FIR in which the target is accused (in the target's own FIRs - a victim's,
  a complainant's - the family who filed or witnessed are searched). A number is searched
  once; a number that joins the case later is searched on the next sweep. All numbers are searched first, then
  each CDR found is read;
* **from the chat**, for any number the officer asks about (the toolbox's ``cdr_archive``). A question about
  the call records searches again every number whose last search is older than ``AGAIN_AFTER_H`` (officers keep
  extracting CDRs into the archive), and the numbers the officer wrote in the chat are searched like the targets'.

The officer's chat shows it live: one line for the search (how many numbers, whose, how far), one line for
each CDR found ("found the CDR of X - processing", then "done", with what it put on the case board). What
was searched and found is written on the case board too (``Search of the iCOP CDR archive``), so the chat and
the report can say whose CDR the archive has and whose it has not.

Every search is a live call: it goes through the guard (the officer's systems, an identifier of the case, the
archive's own limits per case - apart from the NADRA, SIM and CRO calls) and is logged. Downloads are taken only from the archive's own host. The key lives in ``.env``
(``ICOP_CDR_ARCHIVE_KEY``), never in code.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SYSTEM = "cdr_archive"
MOST_FILES = 10                 # files fetched for one number
MOST_NUMBERS = 100              # numbers searched in one sweep (the targets' first)
TRIES = 2                       # a search that failed is tried again on this many sweeps
MOST_BYTES = 40 * 1024 * 1024
AGAIN_AFTER_H = 6.0             # asked about in the chat: a number searched longer ago than this is searched again
_UA = "Mozilla/5.0 (Sherlocks case system)"


def _conf(name: str) -> str:
    from sherlocks.linkgraph.ems import _ENV_FILE

    return os.environ.get(name) or _ENV_FILE.get(name) or ""


class Archive:
    """Client for the iCOP CDR archive."""

    def __init__(self, base: str | None = None, key: str | None = None, session: Any = None) -> None:
        import requests

        self.base = (base or _conf("ICOP_CDR_ARCHIVE_URL") or "").rstrip("/")
        self.key = key or _conf("ICOP_CDR_ARCHIVE_KEY")
        self.http = session or requests.Session()

    @property
    def ready(self) -> bool:
        return bool(self.base and self.key)

    def _headers(self, accept: str) -> dict[str, str]:
        return {"X-API-KEY": self.key, "Accept": accept, "User-Agent": _UA}

    def search(self, mobile: str) -> dict[str, Any]:
        """``{found, searched, message, records: [{...the archive's record}]}``."""
        r = self.http.post(f"{self.base}/search", json={"mobile": mobile},
                           headers={**self._headers("application/json"), "Content-Type": "application/json"}, timeout=60)
        r.raise_for_status()
        block = ((r.json() or {}).get("data") or {}).get("CDR_DATABASE") or {}
        return {"found": bool(block.get("status")) and bool(block.get("records")), "searched": block.get("searched_mobile"),
                "message": block.get("message"), "error": bool(block.get("isError")), "records": block.get("records") or []}

    def download(self, url: str) -> tuple[str, bytes]:
        """``(file name, content)`` of one archived CDR - only from the archive's own host."""
        if urlparse(url).netloc != urlparse(self.base).netloc or urlparse(url).scheme != "https":
            raise ValueError("The download link is not on the CDR archive's host")
        r = self.http.get(url, headers=self._headers("*/*"), timeout=180, stream=True)
        r.raise_for_status()
        content = r.raw.read(MOST_BYTES + 1, decode_content=True)
        if len(content) > MOST_BYTES:
            raise ValueError("The archived file is too large")
        m = re.search(r'filename="?([^";]+)"?', r.headers.get("content-disposition") or "")
        return (m.group(1) if m else ""), content


def key_of(record: dict[str, Any]) -> str:
    return f"cdrarchive:{record.get('analysis_type')}:{record.get('analysis_id')}:{record.get('file_index')}"


def _clean(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text in ("", "—", "-", "—", "None", "null") else text


def describe(record: dict[str, Any], number: str) -> str:
    """The archive's record in one line: whose CDR it is, where the number appears, the FIR and who extracted it."""
    party = "the subscriber (A party)" if record.get("in_a_party") else "a contact (B party)"
    fir = (f"FIR {_clean(record.get('fir_no'))}/{_clean(record.get('fir_year'))}, PS {_clean(record.get('police_station'))}"
           + (f" ({_clean(record.get('sections_of_law'))})" if _clean(record.get("sections_of_law")) else "")
           if record.get("has_fir") else "no FIR on the archive record")
    user = record.get("icop_user") or {}
    who = ", ".join(x for x in (_clean(user.get("name")), _clean(user.get("police_station")), _clean(user.get("district")))
                    if x)
    crime = ", ".join(x for x in (_clean(record.get("crime_date")), _clean(record.get("crime_place"))) if x)
    return (f"From the iCOP CDR archive: CDR of {_clean(record.get('a_party_number'))}; the searched number {number} is "
            f"{party} in it; {fir}" + (f"; crime {crime}" if crime else "") + (f"; extracted by {who}" if who else "") + ".")


def _who(tools: Any, number: str) -> dict[str, Any]:
    """The person on the case a number belongs to: name, id, and whether a target."""
    net = getattr(tools, "net", None)
    pid = net.resolve(number) if net is not None else None
    if not pid:
        return {"name": "", "pid": None, "role": "other"}
    return {"name": net.name(pid), "pid": pid, "role": "target" if net.data(pid).get("seed") else "contact"}


def row_name(record: dict[str, Any]) -> str:
    """The file's line in the chat and its name on the board: whose CDR, and which file of the archive."""
    from sherlocks.evidence.uploads import safe_name

    whose = re.sub(r"\D", "", _clean(record.get("a_party_number"))) or "unknown"
    return safe_name(f"CDR {whose} - iCOP archive {record.get('analysis_type')}-{record.get('analysis_id')}-"
                     f"{record.get('file_index')}.xlsx")


def _search(tools: Any, n: str, say: Any) -> dict[str, Any]:
    archive = Archive()
    if not archive.ready:
        raise ValueError("The CDR archive is not configured (ICOP_CDR_ARCHIVE_URL / ICOP_CDR_ARCHIVE_KEY)")
    tools._spend(SYSTEM, n)                     # the guard: the officer's systems, a number of the case, limits
    say("info", f"🗄️ Searching the CDR archive for {n}")
    try:
        found = archive.search(n)
    except Exception as exc:
        tools.case.attempt("cdr_archive", f"cdrarchive:search:{n}", "error", f"{n}: {type(exc).__name__}: {exc}")
        raise
    tools.case.attempt("cdr_archive", f"cdrarchive:search:{n}", "found" if found["found"] else "none",
                       f"{n}: {found['message'] or ''} ({len(found['records'])} file(s))")
    return found


def _queue(case: Any, n: str, records: list[dict[str, Any]], who: dict[str, Any],
           queued: set[str] | None = None) -> list[dict[str, Any]]:
    """Each file found and not on the board yet gets its line in the chat ("found - waiting to be read"). A file
    already queued for another number of the same search (``queued``) is read once."""
    have = {(d.get("data") or {}).get("archive_key"): d["id"] for d in case.documents.values()}
    queued = set() if queued is None else queued
    out = []
    for record in records:
        key = key_of(record)
        row = {"key": key, "party": record.get("party_type"), "a_party": record.get("a_party_number"),
               "fir": f"{_clean(record.get('fir_no'))}/{_clean(record.get('fir_year'))}" if record.get("has_fir") else None,
               "name": row_name(record)}
        if key in have:
            out.append({**row, "doc": have[key], "status": "already on the board"})
            continue
        if key in queued:
            out.append({**row, "status": "found for another number"})
            continue
        queued.add(key)
        case.upload_status(row["name"], "found", archive=True, number=n, who=who.get("name") or "",
                           role=who.get("role"), party=record.get("party_type"), a_party=_clean(record.get("a_party_number")),
                           fir=row["fir"], extracted_by=_clean((record.get("icop_user") or {}).get("name")))
        out.append({**row, "status": "found"})
    return out


def _read(tools: Any, n: str, records: list[dict[str, Any]], rows: list[dict[str, Any]], say: Any) -> None:
    """Download each queued file and read it onto the board as an upload (the CDR team analyses it)."""
    from sherlocks.evidence.uploads import process_upload

    case, agents = tools.case, tools.agents
    net = getattr(tools, "net", None)
    upload_dir = Path(agents.settings.output_path) / "uploads"
    archive = Archive()
    for record, row in zip(records, rows, strict=True):
        if row["status"] != "found":
            continue
        name = row["name"]
        try:
            case.upload_status(name, "downloading")
            _, content = archive.download(str(record.get("download_url") or ""))
            from sherlocks.evidence.cdr import as_xlsx

            content = as_xlsx(content)            # the archive also holds old-format Excel (.xls) files
            doc_id = process_upload(agents, name=name, content=content, note=describe(record, n),
                                    upload_dir=upload_dir, emit=say)
            owner = net.resolve(_clean(record.get("a_party_number"))) if net is not None else None
            doc = case.documents.get(doc_id) or {}
            with case._lock:
                doc.setdefault("data", {}).update({"archive_key": row["key"], "archive": {
                    k: record.get(k) for k in ("analysis_type", "analysis_id", "file_index", "party_type",
                                               "a_party_number", "b_party_number", "fir_no", "fir_year", "has_fir",
                                               "police_station", "sections_of_law", "crime_date", "crime_place")},
                    "found_by": n})
                doc["source"] = "iCOP CDR archive"
                if owner and owner not in doc.setdefault("owners", []):
                    doc["owners"].append(owner)       # the CDR is filed under the person whose number it is
            case.attempt("cdr_archive", row["key"], "read", f"{n}: {name} -> {doc_id}")
            row.update(doc=doc_id, status="read")
            say("hit", f"🗄️ CDR archive: {describe(record, n)} [{doc_id}]")
        except Exception as exc:  # noqa: BLE001 - the next file is still read
            logger.info("CDR archive file %s failed: %s", row["key"], exc)
            case.attempt("cdr_archive", row["key"], "error", f"{n}: {type(exc).__name__}: {exc}")
            if (case.dialog.get("uploads") or {}).get(name, {}).get("state") != "failed":
                case.upload_status(name, "failed", reason=f"{type(exc).__name__}: {exc}"[:200])
            row["status"] = f"failed: {type(exc).__name__}"


def fetch(tools: Any, number: str, *, emit: Any = None, reason: str = "", most: int = MOST_FILES,
          who: dict[str, Any] | None = None, wait: bool = True) -> dict[str, Any]:
    """Search the archive for ``number`` and read every CDR file it is in onto the case board (as uploads, through
    the CDR team). ``tools`` is the case's toolbox (its guard, budget and upload context). With ``wait=False``
    the files are read in a thread of their own (the chat shows each one's progress). Returns
    ``{number, found, files: [{key, doc, party, a_party, status}], message}``."""
    from sherlocks.linkgraph.normalize import mobile11

    say = emit or (lambda level, message: None)
    n = mobile11(number) or re.sub(r"\D", "", str(number or ""))
    if not n:
        raise ValueError("Give a mobile number")
    found = _search(tools, n, say)
    out: dict[str, Any] = {"number": n, "found": found["found"], "files": [], "message": found["message"]}
    who = {**_who(tools, n), **(who or {}), "number": n}
    if not found["found"]:
        say("info", f"🗄️ {n}: no CDR in the archive")
        _on_board(tools, who, out)
        return out
    records = found["records"][:most]
    agents = tools.agents
    if agents is None or getattr(agents, "settings", None) is None:
        out["note"] = "Found in the archive; files can be read only on a live case"
        out["files"] = [{"key": key_of(r), "party": r.get("party_type"), "a_party": r.get("a_party_number"),
                         "status": "not fetched"} for r in records]
        return out
    rows = _queue(tools.case, n, records, who)
    out["files"] = rows
    _on_board(tools, who, out)
    if wait:
        _read(tools, n, records, rows, say)
    elif any(r["status"] == "found" for r in rows):
        threading.Thread(target=_read, args=(tools, n, records, rows, say), daemon=True,
                         name=f"cdr-archive-{n}").start()
        out["note"] = "Found in the archive - each file is being read onto the case board now (the chat shows it)"
    return out


def _on_board(tools: Any, who: dict[str, Any], out: dict[str, Any]) -> None:
    """A search asked for in the chat is written on the case board like the sweep's: whose number, what was found."""
    try:
        x = {"pid": who.get("pid"), "name": who.get("name") or "", "role": who.get("role") or "asked",
             "of": who.get("of") or "", "number": out["number"]}
        record_on_board(tools.case, [x], [(x, {"rows": out["files"]})] if out["found"] else [], {"searched": 1})
        graph = getattr(tools, "graph", None)
        if graph is not None:
            tools.case.archive_status(**totals(tools.case, graph))
    except Exception:
        logger.exception("CDR archive search of %s not written on the board", out.get("number"))


def officer_numbers(case: Any) -> list[str]:
    """Mobile numbers the officer wrote - in the chat, on the map, in answers: the officer gave them for this case."""
    from sherlocks.evidence.guard import identifiers_in
    from sherlocks.linkgraph.normalize import mobile11

    if case is None:
        return []
    # Only what the officer wrote: the notes also keep what a file found came with (an archive record's own
    # numbers) - searching those searched the archive by itself, one CDR after another.
    notes = str((case.doc_for("notes:officer") or {}).get("text") or "").splitlines()
    texts = [line for line in notes if not re.match(r"^\[[^\]]*·\s*upload\]", line)]
    texts += [str(turn.get("q") or "") for turn in case.conversation]
    out: list[str] = []
    for text in texts:
        for x in sorted(identifiers_in(text)):
            if mobile11(x) == x and x not in out:
                out.append(x)
    return out


def _named_with(case: Any, number: str) -> tuple[str | None, str]:
    """Whom the officer's own words tie a number to: the person of an officer's fact that holds the number."""
    for f in case.facts.values():
        if f.get("by") == "officer" and number in re.sub(r"[\s-]", "", str(f.get("statement") or "")):
            pids, people = f.get("pids") or [], f.get("people") or []
            if pids or people:
                return (pids[0] if pids else None), (people[0] if people else "")
    return None, ""


def _fir_key(text: Any) -> tuple[str, str] | None:
    m = re.search(r"(\d{1,5})\s*/\s*(\d{2,4})", str(text or ""))
    return (m.group(1).lstrip("0"), m.group(2)[-2:]) if m else None


def _tied(net: Any, seeds: list[str]) -> list[tuple[str, str]]:
    """The people tied directly to a target, strongest ties first - never a police officer there on duty, nor the
    complainant or a witness of an FIR in which the target is accused."""
    from sherlocks.linkgraph.case_queries import role_kind
    from sherlocks.team.case_scope import is_officer
    from sherlocks.team.group_view import _NOT_THROUGH, _labels

    def against(t: str, o: str) -> bool:
        """Tied to the target only as the complainant or a witness of an FIR in which the target is accused: the other
        side of the target's case. In an FIR where the target is the complainant or the victim, they are the
        target's own people - the family of a person found dead - and are searched."""
        accused_in = {_fir_key(f.get("label")) for f in net.data(t).get("firs") or []
                      if role_kind([str(f.get("role") or "")]) == "accused"}
        named = [h for h in _labels(net, t, o) if _NOT_THROUGH.search(h)]
        return bool(named) and all(_fir_key(h) in accused_in or not _fir_key(h) for h in named)

    tied = [(t, o) for t in seeds for o in net.G.neighbors(t)
            if o not in seeds and not is_officer(net, o) and not against(t, o)]
    tied.sort(key=lambda x: (not net.G[x[0]][x[1]].get("stated"), -len(net.G[x[0]][x[1]]["links"])))
    return tied


def sweep_numbers(graph: dict[str, Any], case: Any = None) -> list[dict[str, Any]]:
    """Whose numbers the archive is searched for, in order: every target's own numbers, then the numbers of
    each person tied directly to a target (the strongest ties first) - never a police officer there on duty, nor
    someone tied to the target only as the complainant or a witness of an FIR in which the target is accused (the
    other side); in the target's own FIRs (complainant, victim) those people are the target's family and are searched.
    Then every number the officer wrote (given in the chat), unless it is a police officer's on duty there.
    ``[{number, pid, name, role: target | contact | given, of}]``."""
    from sherlocks.linkgraph.network import PersonNetwork
    from sherlocks.linkgraph.normalize import mobile11
    from sherlocks.team.case_scope import is_officer

    net = PersonNetwork(graph)
    seeds = net.seeds()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(pid: str, role: str, of: str = "") -> None:
        for p in net.data(pid).get("phones") or []:
            m = mobile11(p)
            if m and m not in seen:
                seen.add(m)
                out.append({"number": m, "pid": pid, "name": net.name(pid), "role": role, "of": of})

    for pid in seeds:
        add(pid, "target")
    tied = [(t, o) for t, o in _tied(net, seeds) if not _only_by_cdr(net, t, o)]
    for t, o in tied:
        add(o, "contact", net.name(t))
    for m in officer_numbers(case):
        if m in seen:
            continue
        pid = net.resolve(m)
        if pid and is_officer(net, pid):
            continue
        name = net.name(pid) if pid else ""
        if not pid:
            pid, name = _named_with(case, m)
            name = net.name(pid) if pid and pid in net.people else name
        seen.add(m)
        out.append({"number": m, "pid": pid, "name": name, "role": "given", "of": "the officer"})
    return out[:MOST_NUMBERS]


def _only_by_cdr(net: Any, t: str, o: str) -> bool:
    """Tied to the target only by a CDR (a number he called, the subscriber of a CDR he is in): searched in the
    archive only as the CDR follow-up decides (``cdr_followup``), never by the sweep."""
    links = net.G[t][o].get("links") or []
    return bool(links) and all(getattr(link, "system", "") == "cdr_contact" for link in links)


def without_a_number(graph: dict[str, Any]) -> list[str]:
    """The people tied directly to a target with no mobile number on record: the archive could not be searched for
    them."""
    from sherlocks.linkgraph.network import PersonNetwork
    from sherlocks.linkgraph.normalize import mobile11

    net = PersonNetwork(graph)
    out = [net.name(o) for _, o in _tied(net, net.seeds())
           if not any(mobile11(p) for p in net.data(o).get("phones") or [])]
    return list(dict.fromkeys(out))


def _searched(case: Any, again_after_h: float | None = None) -> tuple[set[str], dict[str, int]]:
    """The numbers searched (with ``again_after_h``: searched within that many hours), and failed searches."""
    from datetime import UTC, datetime, timedelta

    since = (datetime.now(UTC) - timedelta(hours=again_after_h)).isoformat(timespec="seconds") if again_after_h else ""
    done: set[str] = set()
    errors: dict[str, int] = {}
    for a in case.attempts:
        key = str(a.get("key") or "")
        if a.get("kind") == "cdr_archive" and key.startswith("cdrarchive:search:"):
            n = key.rsplit(":", 1)[1]
            if a.get("status") in ("found", "none") and str(a.get("at") or "") >= since:
                done.add(n)
            elif a.get("status") == "error" and str(a.get("at") or "") >= since:
                errors[n] = errors.get(n, 0) + 1
    return done, errors


def last_searched(case: Any) -> str | None:
    """When the archive was last searched for a number of this case."""
    times = [str(a.get("at") or "") for a in case.attempts if a.get("kind") == "cdr_archive"
             and str(a.get("key") or "").startswith("cdrarchive:search:") and a.get("status") in ("found", "none")]
    return max(times) if times else None


def live(tools: Any) -> bool:
    """A case on the real systems: a demo case (made-up people) never searches the police's archive."""
    return getattr(getattr(tools, "backend", None), "name", None) != "demo"


def to_search(case: Any, graph: dict[str, Any], *, again_after_h: float | None = None) -> list[dict[str, Any]]:
    """The sweep's numbers not searched yet (a failed search is tried again, ``TRIES`` times in all). With
    ``again_after_h``, a number last searched longer ago than that is due again too."""
    done, errors = _searched(case, again_after_h)
    return [x for x in sweep_numbers(graph, case) if x["number"] not in done and errors.get(x["number"], 0) < TRIES]


_SWEEPING: set[int] = set()
_SWEEP_LOCK = threading.Lock()


def sweep(case: Any, graph: dict[str, Any], tools: Any, *, emit: Any = None, again_after_h: float | None = None,
          searched: threading.Event | None = None) -> dict[str, Any] | None:
    """Every number of the targets and of the people tied to them not searched yet: searched in the archive, then
    every CDR found read onto the board. One sweep a case at a time. The progress is on the board for the chat
    (``case.dialog["cdr_archive"]``); what was found is written on the board. Returns the last status, or None
    when there was nothing to search. ``searched`` is set once every number has been searched (the files found
    are still being read), or when nothing is searched."""
    try:
        return _sweep(case, graph, tools, emit=emit, again_after_h=again_after_h, searched=searched)
    finally:
        if searched is not None:
            searched.set()


def _sweep(case: Any, graph: dict[str, Any], tools: Any, *, emit: Any, again_after_h: float | None,
           searched: threading.Event | None) -> dict[str, Any] | None:
    from sherlocks.evidence.guard import GuardError, NeedsConfirmation

    if tools is None or not Archive().ready or not live(tools):
        return None
    todo = to_search(case, graph, again_after_h=again_after_h)
    if not todo:
        return None
    with _SWEEP_LOCK:
        if id(case) in _SWEEPING:
            return None
        _SWEEPING.add(id(case))
    say = emit or (lambda level, message: None)
    targets = {x["pid"] for x in todo if x["role"] == "target"}
    contacts = {x["pid"] for x in todo if x["role"] == "contact"}
    status: dict[str, Any] = {"state": "searching", "total": len(todo), "searched": 0, "targets": len(targets),
                              "contacts": len(contacts), "found": [], "none": 0, "failed": 0, "current": None,
                              "stopped": None, "started": _now_iso(), "doc": None}
    found: list[tuple[dict[str, Any], dict[str, Any]]] = []
    queued: set[str] = set()
    try:
        case.archive_status(**status)
        tools.live_left = max(int(getattr(tools, "live_left", 0) or 0), len(todo))   # the archive's own limits apply
        for x in todo:
            case.archive_status(current=f"{x['name']} ({x['number']})", searched=status["searched"])
            try:
                hit = _search(tools, x["number"], say)
            except NeedsConfirmation:
                status["failed"] += 1
                hit = None
            except GuardError as exc:
                status["stopped"] = str(exc)
                break
            except Exception as exc:  # noqa: BLE001 - the other numbers are still searched
                logger.info("CDR archive sweep: %s: %s", x["number"], exc)
                status["failed"] += 1
                hit = None
            status["searched"] += 1
            if hit is None:
                continue
            if hit["found"]:
                records = hit["records"][:MOST_FILES]
                rows = _queue(case, x["number"], records, x, queued)
                found.append((x, {"records": records, "rows": rows}))
                status["found"].append({"name": x["name"], "number": x["number"], "role": x["role"], "files": len(rows)})
            else:
                status["none"] += 1
        status.update(state="processing" if any(r["status"] == "found" for _, f in found for r in f["rows"]) else "done",
                      current=None)
        case.archive_status(**status)
        if searched is not None:
            searched.set()
        for x, f in found:
            case.archive_status(current=f"{x['name']} ({x['number']})")
            if tools.agents is not None and getattr(tools.agents, "settings", None) is not None:
                _read(tools, x["number"], f["records"], f["rows"], say)
        docs = {r["key"]: r["doc"] for _, f in found for r in f["rows"] if r.get("doc")}
        for _, f in found:
            for r in f["rows"]:
                if not r.get("doc") and r["key"] in docs:
                    r["doc"] = docs[r["key"]]         # read under another number of this search
        status.update(state="done", current=None, finished=_now_iso(),
                      read=sum(1 for _, f in found for r in f["rows"] if r.get("status") == "read"))
        status["doc"] = record_on_board(case, todo, found, status)
        status.update(totals(case, graph))
        case.archive_status(**status)
        return status
    except Exception:
        logger.exception("CDR archive sweep failed")
        case.archive_status(state="done", current=None, stopped="the search stopped on an error")
        return status
    finally:
        with _SWEEP_LOCK:
            _SWEEPING.discard(id(case))


def totals(case: Any, graph: dict[str, Any]) -> dict[str, Any]:
    """The whole search so far, every sweep together: numbers searched, of how many targets and people tied to them,
    and whose CDR was found - a later sweep (new numbers) must not make the earlier ones look unsearched."""
    done, _ = _searched(case)
    wanted = sweep_numbers(graph, case)
    hit = {x["number"] for x in wanted if any(a.get("key") == f"cdrarchive:search:{x['number']}" and a.get("status") == "found"
                                              for a in case.attempts)}
    searched = [x for x in wanted if x["number"] in done]
    return {"all_numbers": len(done), "all_targets": len({x["pid"] for x in searched if x["role"] == "target"}),
            "all_contacts": len({x["pid"] for x in searched if x["role"] == "contact"}),
            "all_given": len([x for x in searched if x["role"] == "given"]), "last_searched": last_searched(case),
            "all_found": [{"name": x["name"], "number": x["number"], "role": x["role"]} for x in searched if x["number"] in hit]}


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


BOARD_KEY = "cdrarchive:searches"


def record_on_board(case: Any, todo: list[dict[str, Any]], found: list[tuple[dict[str, Any], dict[str, Any]]],
                    status: dict[str, Any]) -> str | None:
    """What the sweep searched and found, on the case board: one line (and fact) for each CDR found, one for
    each person whose numbers the archive has no CDR for."""
    from sherlocks.evidence.case_file import TRUST_SYSTEM

    def role_of(x: dict[str, Any]) -> str:
        if x["role"] == "target":
            return "target"
        if x["role"] == "given":
            return "given by the officer"
        return f"tied to {x['of']}" if x.get("of") else "asked about in the chat"

    hit = {x["number"] for x, _ in found}
    searched = todo[:status["searched"]]
    lines: list[tuple[str, list[str], list[str]]] = []
    for x, f in found:
        docs = list(dict.fromkeys(r["doc"] for r in f["rows"] if r.get("doc")))
        whose = ", ".join(sorted({re.sub(r"\D", "", str(r.get("a_party") or "")) for r in f["rows"]} - {""}))
        whom = f"{x['name']}'s number" if x.get("name") else "the number"
        lines.append((f"The iCOP CDR archive holds {len(f['rows'])} CDR file(s) with {whom} "
                      f"{x['number']} ({role_of(x)})" + (f" - the CDR of {whose}" if whose else "")
                      + (f"; read onto the board as {', '.join(docs)}" if docs else "") + ".", [x["pid"]],
                      [x["name"]] if x.get("name") else []))
    nothing: dict[str, list[str]] = {}
    who: dict[str, dict[str, Any]] = {}
    for x in searched:
        if x["number"] not in hit:
            key = x["pid"] or x["number"]
            nothing.setdefault(key, []).append(x["number"])
            who[key] = x
    for key, numbers in nothing.items():
        x = who[key]
        whose = f"{x['name']}'s number(s)" if x.get("name") else "the number"
        lines.append((f"No CDR in the iCOP CDR archive for {whose} {', '.join(numbers)} ({role_of(x)}).",
                      [x["pid"]], [x["name"]] if x.get("name") else []))
    if not lines:
        return None
    text = "\n".join(line for line, _, _ in lines)
    with case.batch("CDR archive", "archive"):
        doc = case.doc_for(BOARD_KEY)
        if doc is None:
            doc_id = case.add_document(kind="archive", key=BOARD_KEY, title="Search of the iCOP CDR archive",
                                       source="iCOP CDR archive", text=text,
                                       summary="Whose numbers were searched in the CDR archive, and whose CDR it holds")
        else:
            doc_id = doc["id"]
            missing = [line for line, _, _ in lines if line not in doc["text"]]
            if missing:
                case.append_text(doc_id, "\n".join(missing))
        for line, pids, names in lines:
            case.add_fact(doc_id, line, line, kind="cdr_archive", by="system", tier="fact", trust=TRUST_SYSTEM,
                          pids=[p for p in pids if p], people=names, topic="cdr_archive")
    return doc_id
