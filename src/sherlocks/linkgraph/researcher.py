"""The Research agent: when nothing gathered so far answers the question, it goes and gets
it - by rules, without needing the AI model.

It looks at what the question needs and what is missing, then calls the APIs for it:

* an FIR asked about whose file has not been read -> the FIR file (PSRMS);
* lab / DNA / medical reports for an FIR -> the Labs reports;
* a CRO dossier, photos, fingerprints -> SAFE;
* a person's job, vehicles, phones, hotel stays, weapons, criminal record... -> the police
  systems that hold it, skipping the ones already searched for that person.

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

# Topic (knowledge-base concept) -> the systems that hold it, most useful first.
SYSTEMS_FOR = {
    "work": ["evs", "hope", "hrmis", "prvs"],
    "vehicle": ["excise", "avlc", "tracs", "dls"],
    "phone": ["simsdb", "subscriber", "caller_id"],
    "hotel": ["hotel_eye"],
    "weapon": ["arms"],
    "address": ["prvs", "old_tenant", "dls", "subscriber"],
    "family": ["prvs", "old_tenant"],
    "property": ["old_tenant", "prvs"],
    "accused": ["psrms", "cro", "watchlist"],
    "fir": ["psrms", "cro"],
    "status": ["psrms", "cro"],
    "cro": ["cro"],
}
_TYPE_TOPIC = {"hotels": "hotel", "phones": "phone", "vehicles": "vehicle", "cases": "fir", "count_cases": "fir",
               "fir_status": "status", "serious_cases": "fir", "criminals_near": "accused"}
_LAB = re.compile(r"\blab|dna|chemical|medical|medico|forensic|fsl|mlo|report|لیب|میڈیکل", re.IGNORECASE)
_CRO = re.compile(r"\bcro\b|dossier|fingerprint|photo|tasveer|تصویر", re.IGNORECASE)


def _searched(net: Any, pid: str) -> set[str]:
    return {str(r.get("system") or "") for r in net.data(pid).get("records") or []}


def research(tools: Any, query: dict[str, Any], message: str, topics: list[str]) -> list[dict[str, Any]]:
    """Make the calls the question needs; returns what was done, one dict per call:
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
    wanted = list(query.get("firs") or [])
    if not wanted and people and query.get("type") in ("fir_details", "fir_status", "io_report", "serious_cases", "open"):
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
    if people and _CRO.search(message):
        out = call("CRO dossier", tools.fetch_cro, {"who": net.name(people[0])}, "SAFE")
        if out:
            done.append({"what": "CRO dossier", "source": out.get("id") or "SAFE",
                         "lines": [str(out.get("summary") or "")[:300]], "document": out.get("id")})
    # 3. The police systems that hold what is asked, not yet searched for this person.
    want_topics = [t for t in topics if t in SYSTEMS_FOR]
    if not want_topics and query.get("type") in _TYPE_TOPIC:
        want_topics = [_TYPE_TOPIC[query["type"]]]
    for pid in people[:1]:
        searched = _searched(net, pid)
        systems = [s for t in want_topics for s in SYSTEMS_FOR[t] if s not in searched]
        for system in list(dict.fromkeys(systems))[:3]:
            out = call(f"{system_label(system)} lookup for {net.name(pid)}", tools.lookup,
                       {"system": system, "who": net.name(pid)}, system_label(system))
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
            done.append({"what": f"{system_label(system)} lookup", "source": system_label(system),
                         "lines": lines or [str(out.get("summary") or f"nothing found ({out.get('status')})")],
                         "document": None})
    return done


def facts_from(done: list[dict[str, Any]], subject: str | None) -> dict[str, Any]:
    """What the calls brought back, as a Facts-agent answer (``type: research``)."""
    items = [{"text": line, "source": d["source"], "pid": None} for d in done for line in d["lines"] if line]
    return {"type": "research", "subject": subject or "", "count": len(done), "items": items, "empty": not items,
            "calls": [d["what"] for d in done]}

