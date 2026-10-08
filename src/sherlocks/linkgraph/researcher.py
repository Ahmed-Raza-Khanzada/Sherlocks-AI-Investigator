"""The Research agent: when nothing gathered so far answers the question, it goes and gets
it - by rules, without needing the AI model.

It looks at what the question needs and what is missing, then calls the APIs for it:

* an FIR asked about whose file has not been read -> the FIR file (PSRMS);
* lab / DNA / medical reports for an FIR -> the Labs reports;
* a CRO dossier, photos, fingerprints -> SAFE;
* a person's job, vehicles, phones, hotel stays, weapons, criminal record... -> the police
  systems that hold it, chosen by the API router's catalog (``api_router.py``): CNIC-only
  systems after the CNIC is known, skipping systems already searched for that person -
  including the ones that found nothing - and saying so when no system holds the topic.
  These calls run in parallel.

Every call is within the per-question budget (``evidence.chat_live_calls``) and audited.
Fetched documents go onto the case board (the knowledge base picks them up); system
lookups come back as cited lines.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sherlocks.linkgraph.systems import system_label

logger = logging.getLogger(__name__)

_LAB = re.compile(r"\blab|dna|chemical|medical|medico|forensic|fsl|mlo|report|لیب|میڈیکل", re.IGNORECASE)
_CRO = re.compile(r"\bcro\b|dossier|fingerprint|photo|tasveer|تصویر", re.IGNORECASE)


def research(tools: Any, query: dict[str, Any], message: str, topics: list[str], *, files: bool = True,
             systems: bool = True) -> list[dict[str, Any]]:
    """Make the calls the question needs - only the kinds the Officer agent's plan allows
    (``files``: FIR files, lab reports, CRO dossiers; ``systems``: the API router's police
    systems for ``topics``). Returns what was done, one dict per call:
    ``{what, source, lines, document}``."""
    from sherlocks.linkgraph.case_queries import _firs, fir_document

    net, case = tools.net, tools.case
    done: list[dict[str, Any]] = []
    people = [p for p in query.get("people") or [] if p in net.people]

    def call(what: str, fn: Any, args: dict[str, str], source: str) -> dict[str, Any] | None:
        if tools.live_left <= 0:
            return None
        try:
            out = fn(args)
        except ValueError as exc:            # not available here / nothing to call with
            logger.info("Research agent skipped %s: %s", what, exc)
            return None
        except Exception as exc:  # noqa: BLE001 - one failed call does not stop the others
            logger.info("Research agent call failed (%s): %s", what, exc)
            done.append({"what": what, "source": source, "lines": [f"{what}: failed ({exc})"], "document": None})
            return None
        if isinstance(out, dict) and out.get("error"):
            done.append({"what": what, "source": source, "lines": [f"{what}: {out['error']}"], "document": None})
            return None
        return out

    # 1. FIRs whose file has not been read: named in the question, or the person's own.
    wanted = list(query.get("firs") or []) if files else []
    if files and not wanted and people and (query.get("unanswered") or query.get("type") in (
            "fir_details", "fir_status", "io_report", "serious_cases", "open", "hotels")):
        wanted = [r["label"] for r in _firs(net, people[0], case) if not r.get("doc")][:3]
    for label in wanted:
        if case is not None and fir_document(case, label) is None:
            out = call(f"FIR file {label}", tools.fetch_fir, {"fir": label}, "PSRMS")
            if out:
                done.append({"what": f"FIR file {label}", "source": out.get("id") or "PSRMS",
                             "lines": [str(out.get("summary") or out.get("title") or "")[:300]],
                             "document": out.get("id")})
        if _LAB.search(message):
            out = call(f"lab reports of FIR {label}", tools.fetch_lab_reports, {"fir": label}, "Labs")
            if out:
                docs = out.get("documents") or []
                done.append({"what": f"lab reports of FIR {label}", "source": "Labs",
                             "lines": [str(d.get("summary") or d.get("title") or "")[:240] for d in docs]
                             or [str(out.get("message") or "no report found")], "document": None})
    # 2. A CRO dossier.
    if files and people and _CRO.search(message):
        out = call("CRO dossier", tools.fetch_cro, {"who": net.name(people[0])}, "SAFE")
        if out:
            done.append({"what": "CRO dossier", "source": out.get("id") or "SAFE",
                         "lines": [str(out.get("summary") or "")[:300]], "document": out.get("id")})
    # 3. The police systems that hold what is asked - chosen by the API router's catalog.
    from concurrent.futures import ThreadPoolExecutor

    from sherlocks.linkgraph import api_router

    want_topics = api_router.topics_for(topics, query.get("type")) if systems else []
    for pid in people[:1] if want_topics else []:
        planned = api_router.plan(net, case, pid, want_topics, allowed=(getattr(tools, "officer", None) or {}).get("systems"))
        for topic in planned["unheld"]:
            done.append({"what": f"{topic} lookup", "source": "API router",
                         "lines": [f"No connected system holds {topic} information."], "document": None})
        calls = planned["calls"]
        # A SIMs lookup that finds the CNIC runs first; the rest together.
        first = [c for c in calls if c["system"] == "simsdb" and c["topic"] == "identity"]
        rest = [c for c in calls if c not in first]

        def one(c: dict[str, Any], _pid: str = pid) -> tuple[dict[str, Any], dict[str, Any] | None]:
            system = c["system"]
            return c, call(f"{system_label(system)} lookup for {net.name(_pid)}", tools.lookup,
                           {"system": system, "who": net.name(_pid)}, system_label(system))

        results = [one(c) for c in first]
        if rest:
            with ThreadPoolExecutor(max_workers=min(3, len(rest)), thread_name_prefix="research") as pool:
                results += list(pool.map(one, rest))
        for c, out in results:
            system = c["system"]
            if not out:
                continue
            lines = [*(f"employer / organisation: {o}" for o in out.get("organisations") or []),
                     *([f"designation: {out['designation']}"] if out.get("designation") else []),
                     *(f"vehicle: {v}" for v in out.get("vehicles") or []),
                     *(f"hotel stay: {st}" for st in out.get("stays") or []),
                     *out.get("fields", [])[:8], *[f"FIR {f}" for f in out.get("firs", [])[:6]],
                     *out.get("names", [])[:6]]
            seen: set[str] = set()
            lines = [x for x in lines if not ((k := re.sub(r"\W+", " ", x.split(":", 1)[-1]).strip().lower()) in seen
                                              or seen.add(k))]
            api_router.remember(case, pid, system, "found" if lines else "nothing found")
            done.append({"what": f"{system_label(system)} lookup", "source": system_label(system),
                         "lines": lines or [str(out.get("summary") or f"nothing found ({out.get('status')})")],
                         "document": None})
    return done


_NOTHING = re.compile(r"no record|nothing found|not found|no report found|failed|no connected system", re.IGNORECASE)


def facts_from(done: list[dict[str, Any]], subject: str | None) -> dict[str, Any]:
    """What the calls brought back, as a Facts-agent answer (``type: research``)."""
    # "no record" / "nothing found" is where it looked, not an answer: the Officer agent lists it.
    items = [{"text": line, "source": d["source"], "pid": None} for d in done for line in d["lines"]
             if line and not _NOTHING.search(line)]
    return {"type": "research", "subject": subject or "", "count": len(done), "items": items, "empty": not items,
            "calls": [d["what"] for d in done]}

