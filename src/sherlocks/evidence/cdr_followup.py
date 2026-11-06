"""After a CDR is read: what it says about the people of the case, and who behind it is searched.

For each CDR on the board, once (``cdr_profile.write``):

* the target's own CDR (he is the A party): his contacts, places and profile go on the board; his
  ``cdr_contacts_searched`` most-contacted numbers are searched like a target (``RunManager.expand_contacts``) - at
  most ``cdr_target_cdrs_per_case`` of his CDRs per case - and the first ``cdr_contacts_in_archive`` of them in the
  CDR archive (a CDR found there is read the same way: only its contact with the target);
* someone else's CDR the target is in (a B party): the contact history between them goes on the board and the
  subscriber is searched like a target. Nothing about the subscriber's other contacts, and his number is not
  searched in the archive again.

The searches run in a thread of their own, one case at a time; the chat is told when they start and finish, and
``case.dialog["cdr_contacts"]`` shows how far they are.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from sherlocks.evidence import cdr_profile

logger = logging.getLogger(__name__)

_RUNNING: set[int] = set()
_LOCK = threading.Lock()

STARTED = {"en": "Reading {what}: searching {n} number(s) like a target (the owner, then every system){archive}. "
                 "Each person found joins the graph as they come.",
           "roman": "{what} parh raha hoon: {n} number(s) ko target ki tarah search kar raha hoon (pehle malik, phir har "
                    "system){archive}. Har shakhs milte hi graph mein aa jayega.",
           "ur": "{what} پڑھ رہا ہوں: {n} نمبر ٹارگٹ کی طرح تلاش کر رہا ہوں (پہلے مالک، پھر ہر سسٹم){archive}۔ ہر شخص ملتے ہی گراف میں آ جائے گا۔"}
DONE = {"en": "Done: {n} number(s) of {what} searched - their owners and their own ties are on the graph and the "
              "case board. Ask me about any of them.",
        "roman": "Mukammal: {what} ke {n} number(s) search ho gaye - un ke malik aur un ke rishte graph aur case board par "
                 "hain. In mein se kisi ke baare mein poochein.",
        "ur": "مکمل: {what} کے {n} نمبر تلاش ہو گئے - ان کے مالک اور ان کے رشتے گراف اور کیس بورڈ پر ہیں۔ ان میں سے کسی کے بارے میں پوچھیں۔"}


def _people(got: dict[str, Any], case: Any, net: Any, most: int) -> list[dict[str, Any]]:
    """Whom to search, most-contacted first, each with the tie that brings them in."""
    s, doc = got["subject"], got["doc"]
    data = (case.documents.get(doc) or {}).get("data") or {}
    if got["mode"] == "own":
        out = []
        for i, c in enumerate([c for c in data.get("contacts") or [] if not c["service"]][:most], 1):
            out.append({"number": c["number"], "of": got["pid"], "rank": i, "doc": doc,
                        "label": f"In phone contact {c['records']} times (CDR of {s})",
                        "reasons": [(f"{c['number']} in the CDR of {s}: {c['records']} records, {c['first']} to "
                                     f"{c['last']} ({c['rows']})")]})
        return out
    return [{"number": s, "of": h["target"], "rank": 1, "doc": doc,
             "label": f"In phone contact {h['records']} times (CDR of {s})",
             "reasons": [(f"{h['other']} in the CDR of {s}: {h['records']} records, {h['first']} to {h['last']} "
                          f"({h['rows']})")]} for h in data.get("histories") or [] if h.get("target")]


def follow(case: Any, graph: dict[str, Any], *, contacts: Callable[..., int] | None, tools: Callable[[], Any] | None,
           settings: Any) -> list[str]:
    """Analyse every CDR on the board not analysed yet; start the searches it calls for. Returns what was done."""
    from sherlocks.linkgraph.network import PersonNetwork

    net = PersonNetwork(graph)
    cfg = getattr(settings, "evidence", None)
    most = int(getattr(cfg, "cdr_contacts_searched", 50))
    cdrs = int(getattr(cfg, "cdr_target_cdrs_per_case", 3))
    in_archive = int(getattr(cfg, "cdr_contacts_in_archive", 5))
    done: list[str] = []
    jobs: list[tuple[dict[str, Any], list[dict[str, Any]], list[str]]] = []
    for doc_id in [d["id"] for d in case.documents.values() if d.get("kind") == "cdr"]:
        try:
            got = cdr_profile.write(case, doc_id, net)
        except Exception:
            logger.exception("CDR profile of %s failed", doc_id)
            continue
        if not got:
            continue
        done.append(f"{doc_id}: {got['mode']} ({got['subject']})")
        if contacts is None:
            continue
        archive: list[str] = []
        if got["mode"] == "own":
            expanded = case.dialog.setdefault("cdr_expanded", [])
            if len(expanded) >= cdrs:
                done.append(f"{doc_id}: {cdrs} CDRs of the target already searched - its contacts are listed only")
                continue
            expanded.append(doc_id)
            archive = got["contacts"][:in_archive]
        people = _people(got, case, net, most)
        if people:
            jobs.append((got, people, archive))
    for got, people, archive in jobs:
        _start(case, got, people, archive, contacts, tools)
    if contacts is not None and not jobs and case.dialog.get("cdr_contacts_queue"):
        _run(case, contacts, tools)                  # searches left from before a restart
    return done


def _start(case: Any, got: dict[str, Any], people: list[dict[str, Any]], archive: list[str],
           contacts: Callable[..., int], tools: Callable[[], Any] | None) -> None:
    language = case.dialog.get("language") or "en"
    what = f"the CDR of {got['subject']}"
    with case._lock:
        queue = case.dialog.setdefault("cdr_contacts_queue", [])
        queue.append({"doc": got["doc"], "what": what, "people": people, "archive": archive})
    case.add_followup(STARTED.get(language, STARTED["en"]).format(
        what=what, n=len(people), archive=(f", and the first {len(archive)} in the CDR archive" if archive else "")),
        kind="cdr")
    _run(case, contacts, tools)


def _run(case: Any, contacts: Callable[..., int], tools: Callable[[], Any] | None) -> None:
    with _LOCK:
        if id(case) in _RUNNING:
            return                                   # the thread already running takes it from the queue
        _RUNNING.add(id(case))
    threading.Thread(target=_work, args=(case, contacts, tools), daemon=True, name="cdr-contacts").start()


def _work(case: Any, contacts: Callable[..., int], tools: Callable[[], Any] | None) -> None:
    from sherlocks.evidence import cdr_archive

    language = case.dialog.get("language") or "en"
    try:
        while True:
            with case._lock:
                queue = case.dialog.get("cdr_contacts_queue") or []
                job = queue.pop(0) if queue else None
            if job is None:
                return

            def progress(i: int, n: int, job: dict[str, Any] = job) -> None:
                with case._lock:
                    case.dialog["cdr_contacts"] = {"state": "searching", "what": job["what"], "done": i, "total": n}
                    case._bump("archive", "cdr_contacts")

            n = contacts(job["people"], after=progress)
            if job["archive"] and tools is not None:
                box = tools()
                box.live_left = max(int(getattr(box, "live_left", 0) or 0), len(job["archive"]))
                for number in job["archive"]:
                    try:
                        cdr_archive.fetch(box, number, reason="a top contact of the target's CDR", wait=True)
                    except Exception as exc:  # noqa: BLE001 - the next number is still searched
                        logger.info("Archive search of contact %s: %s", number, exc)
            with case._lock:
                case.dialog["cdr_contacts"] = {"state": "done", "what": job["what"], "done": n, "total": len(job["people"])}
                case._bump("archive", "cdr_contacts")
            case.add_followup(DONE.get(language, DONE["en"]).format(n=n, what=job["what"]), kind="cdr")
    except Exception:
        logger.exception("CDR contact searches failed")
    finally:
        with _LOCK:
            _RUNNING.discard(id(case))
