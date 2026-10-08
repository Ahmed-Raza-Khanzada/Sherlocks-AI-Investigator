"""The CDR team: every CDR, tower dump or CDR report turned into facts with their rows.

A CDR is not a document to read once: thousands of rows that need cleaning, lookups,
pattern work and mapping, and it gets more valuable when a second CDR or the incident pin
arrives. Six steps, mostly code - counting, distances and matching cannot hallucinate:

* **R1 Intake** (``cdr.load_sheet`` / ``cdr.map_columns``) - recognises the file, maps
  the columns (the model only for odd headers), cleans numbers and times, keeps each
  record's sheet row so every finding can cite it.
* **R2 Enricher** (:func:`enrich`) - the owner of the top contacts (SIMs, through the API
  router and the guard: the officer confirms first), the handset behind an IMEI and the
  nearest police station (CDR server), numbers already on the graph.
* **R3 Pattern analyst** (:func:`patterns`) - top contacts, timeline, silences, IMEI
  changes, night and day habits.
* **R4 Location analyst** (:func:`location`) - towers near the pin in the incident window,
  movement on the incident day, likely home and work areas. A tower is not a place:
  findings say "connected to a tower X km away", never "was at".
* **R5 Cross-CDR linker** (:func:`cross`) - with two or more CDRs: calls between the
  subjects, common contacts, both on the same tower within minutes.
* **R6 Fact writer** (:func:`write_facts`) - each finding a fact with file, sheet and rows;
  fact or inference, never mixed; filed under the number until the owner is confirmed;
  findings that depend on the incident rest on it (a correction marks them stale).

Re-runs (:func:`rerun`): pin moved or date corrected -> R4 (and R3's silences) again;
owner confirmed -> R6 re-files; a second CDR -> R5; the server report arrives -> compared
with the team's own analysis (:func:`compare_with_server`).
"""

from __future__ import annotations

import itertools
import logging
import statistics
from collections import Counter
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

CROSS_KEY = "cdr:cross"


def rows_text(rows: list[int], limit: int = 12) -> str:
    """Row numbers as ranges: [2, 3, 4, 9] -> "2-4, 9"."""
    rows = sorted({r for r in rows if r})
    out: list[str] = []
    start = prev = None
    for r in rows:
        if start is None:
            start = prev = r
        elif r == prev + 1:
            prev = r
        else:
            out.append(f"{start}-{prev}" if prev != start else f"{start}")
            start = prev = r
    if start is not None:
        out.append(f"{start}-{prev}" if prev != start else f"{start}")
    return ", ".join(out[:limit]) + (" …" if len(out) > limit else "")


def _fmt(when: datetime | None) -> str:
    return when.strftime("%Y-%m-%d %H:%M") if when else "-"


def finding(text: str, rows: list[int] | None = None, *, tier: str = "fact", rests_on: list[str] | None = None,
            kind: str = "telecom") -> dict[str, Any]:
    return {"text": text, "rows": rows_text(rows or []), "tier": tier, "rests_on": rests_on or [], "kind": kind}


# -- R3 Pattern analyst ------------------------------------------------------------------


def patterns(events: list[dict[str, Any]], subject: str | None, incident: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Silences, IMEI changes, day / night habits."""
    out: list[dict[str, Any]] = []
    timed = sorted((e for e in events if e["when"]), key=lambda e: e["when"])
    if len(timed) >= 6:
        gaps = [(b["when"] - a["when"], a, b) for a, b in itertools.pairwise(timed)]
        typical = statistics.median(g.total_seconds() for g, _, _ in gaps) or 60
        threshold = max(6 * 3600, 3 * typical)
        long_gaps = sorted((g for g in gaps if g[0].total_seconds() >= threshold), key=lambda g: -g[0].total_seconds())
        inc = incident or {}
        day = str(inc.get("date") or "")[:10]
        for gap, a, b in long_gaps[:2]:
            hours = gap.total_seconds() / 3600
            around = bool(day) and a["when"].strftime("%Y-%m-%d") <= day <= b["when"].strftime("%Y-%m-%d")
            text = (f"Silent around the incident: no activity from {_fmt(a['when'])} to {_fmt(b['when'])} "
                    f"({hours:.0f} h)" if around else
                    f"Silence: no activity from {_fmt(a['when'])} to {_fmt(b['when'])} ({hours:.0f} h)") + "."
            out.append(finding(text, [a["row"], b["row"]], rests_on=["incident:date"] if around else []))
    # IMEI changes: the handset behind the SIM switched.
    last_imei, last_row = None, None
    for e in timed:
        imei = e["imei"] if len(e["imei"]) >= 14 else None
        if imei and last_imei and imei != last_imei:
            out.append(finding(f"IMEI changed on {_fmt(e['when'])}: {last_imei} -> {imei}.", [last_row, e["row"]]))
        if imei:
            last_imei, last_row = imei, e["row"]
    # Habits: when this number is active.
    if len(timed) >= 10:
        evening = sum(1 for e in timed if 18 <= e["when"].hour < 24)
        morning = sum(1 for e in timed if 6 <= e["when"].hour < 12)
        busiest = Counter(e["when"].hour for e in timed).most_common(1)[0][0]
        out.append(finding(f"Habit: busiest hour {busiest:02d}:00; {evening} evening and {morning} morning "
                           f"record(s) of {len(timed)}.", [], tier="fact", kind="telecom_habit"))
    return out


# -- R4 Location analyst -------------------------------------------------------------------


def location(events: list[dict[str, Any]], incident: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Movement on the incident day; likely home and work areas (inferences)."""
    out: list[dict[str, Any]] = []
    place = lambda e: e["addr"] or e["site"]
    timed = sorted((e for e in events if e["when"] and place(e)), key=lambda e: e["when"])
    day = str((incident or {}).get("date") or "")[:10]
    if day:
        that_day = [e for e in timed if e["when"].strftime("%Y-%m-%d") == day]
        path: list[dict[str, Any]] = []
        for e in that_day:
            if not path or place(path[-1]) != place(e):
                path.append(e)
        if len(path) >= 2:
            steps = " -> ".join(f"{place(e)} ({e['when'].strftime('%H:%M')})" for e in path[:8])
            out.append(finding(f"Incident day movement (towers, not exact places): {steps}.", [e["row"] for e in path[:8]],
                               rests_on=["incident:date"]))
    night = Counter(place(e) for e in timed if e["when"].hour >= 22 or e["when"].hour < 6)
    work = Counter(place(e) for e in timed if 9 <= e["when"].hour < 18 and e["when"].weekday() < 6)
    if night and night.most_common(1)[0][1] >= 5:
        where, n = night.most_common(1)[0]
        rows = [e["row"] for e in timed if place(e) == where and (e["when"].hour >= 22 or e["when"].hour < 6)]
        out.append(finding(f"Most night-time activity on the tower at {where} ({n} record(s)): likely home area.",
                           rows, tier="inference", kind="telecom_area"))
    if work and work.most_common(1)[0][1] >= 5:
        where, n = work.most_common(1)[0]
        if not night or where != night.most_common(1)[0][0]:
            rows = [e["row"] for e in timed if place(e) == where and 9 <= e["when"].hour < 18]
            out.append(finding(f"Most daytime activity on the tower at {where} ({n} record(s)): likely work area.",
                               rows, tier="inference", kind="telecom_area"))
    return out


# -- R5 Cross-CDR linker -------------------------------------------------------------------


def cross(analyses: list[dict[str, Any]], *, window_min: int = 10) -> list[dict[str, Any]]:
    """Between CDRs: calls between the subjects, common contacts, the same tower at the
    same time. ``analyses`` items carry ``doc``, ``subject`` and ``events``."""
    out: list[dict[str, Any]] = []
    for i, a in enumerate(analyses):
        for b in analyses[i + 1:]:
            sa, sb = a.get("subject"), b.get("subject")
            refs = [a["doc"], b["doc"]]
            if sa and sb:
                between = [e for e in a["events"] if sb in (e["a"], e["b"])]
                if between:
                    times = sorted(e["when"] for e in between if e["when"])
                    out.append(finding(f"Calls between {sa} and {sb}: {len(between)} record(s), {_fmt(times[0]) if times else '-'}"
                                       f" to {_fmt(times[-1]) if times else '-'} [{a['doc']}].", [e["row"] for e in between],
                                       rests_on=refs, kind="cdr_cross"))
            ca = {(e["b"] if e["a"] == sa else e["a"]) for e in a["events"]} - {None, sa, sb}
            cb = {(e["b"] if e["a"] == sb else e["a"]) for e in b["events"]} - {None, sa, sb}
            common = sorted(ca & cb)
            if common:
                out.append(finding(f"Common contacts of {sa or a['doc']} and {sb or b['doc']}: {', '.join(common[:8])}"
                                   + (f" (+{len(common) - 8})" if len(common) > 8 else "") + f" [{a['doc']}, {b['doc']}].",
                                   [], rests_on=refs, kind="cdr_cross"))
            together = []
            by_site: dict[str, list[dict[str, Any]]] = {}
            for e in b["events"]:
                key = e["site"] or e["addr"]
                if key and e["when"]:
                    by_site.setdefault(key, []).append(e)
            for e in a["events"]:
                key = e["site"] or e["addr"]
                if not key or not e["when"]:
                    continue
                for f in by_site.get(key, []):
                    if abs((e["when"] - f["when"]).total_seconds()) <= window_min * 60:
                        together.append((e, f))
                        break
            if together:
                e, f = together[0]
                out.append(finding(f"Both {sa or a['doc']} and {sb or b['doc']} on the tower at {e['addr'] or e['site']} "
                                   f"within {window_min} minutes ({_fmt(e['when'])}), {len(together)} time(s): they may have "
                                   f"been together [{a['doc']}, {b['doc']}].", [e["row"] for e, _f in together],
                                   tier="inference", rests_on=refs, kind="cdr_cross"))
    return out


# -- R6 Fact writer ------------------------------------------------------------------------


def write_facts(case: Any, doc_id: str, analysis: dict[str, Any], *, owner: str | None = None) -> list[str]:
    """Every finding as a fact quoting the analysis text, with its rows, tier and what it
    rests on. Filed under the owner only once the owner is known."""
    doc = case.documents.get(doc_id)
    if doc is None:
        return []
    sheet = analysis.get("sheet")
    owners = [owner] if owner else list(doc.get("owners") or [])
    written = []
    with case.batch("R6 Fact writer", "cdr", doc=doc_id):
        for f in analysis.get("findings") or []:
            if f["text"] not in doc["text"]:
                case.append_text(doc_id, f["text"])
            rows = f"{sheet}, rows {f['rows']}" if sheet and f.get("rows") else (f"rows {f['rows']}" if f.get("rows") else None)
            fid = case.add_fact(doc_id, f["text"], f["text"], kind=f.get("kind") or "telecom", by="cdr",
                                tier=f.get("tier", "fact"), rests_on=list(f.get("rests_on") or []) + [f"owner:{doc_id}"],
                                rows=rows, pids=owners)
            if fid:
                written.append(fid)
    return written


def refile(case: Any, doc_id: str, owner: str) -> int:
    """The owner of a CDR is confirmed: its findings move to that person."""
    n = 0
    with case.batch("R6 Fact writer", "refile", doc=doc_id):
        for fid in (case.documents.get(doc_id) or {}).get("facts") or []:
            f = case.facts[fid]
            if f.get("by") == "cdr" and owner not in (f.get("pids") or []):
                f["pids"] = [owner]
                f["stale"] = False
                case._bump("fact", fid)
                n += 1
    return n


# -- comparing with the CDR server's report ---------------------------------------------------


def compare_with_server(case: Any, own_doc: str, server_doc: str) -> list[str]:
    """The server report and the team's own analysis side by side: top contacts one
    names and the other does not are flagged (inference: either may be right)."""
    import re

    own = case.documents.get(own_doc) or {}
    server = case.documents.get(server_doc) or {}
    own_top = re.findall(r"Top contact \d+: (\d{11})", own.get("text", ""))
    server_numbers = Counter(re.findall(r"\b03\d{9}\b", server.get("text", "")))
    missing = [n for n in own_top[:5] if n not in server_numbers]
    extra = [n for n, _c in server_numbers.most_common(8) if n not in own_top and n != (own.get("data") or {}).get(
        "analysis", {}).get("subject")][:3]
    lines = []
    if missing:
        lines.append(f"Differs from the CDR server report [{server_doc}]: top contact(s) {', '.join(missing)} "
                     "in our own analysis are not in it.")
    if extra:
        lines.append(f"Differs from the CDR server report [{server_doc}]: it names {', '.join(extra)}, not among our "
                     "top contacts.")
    if not lines:
        lines.append(f"Agrees with the CDR server report [{server_doc}] on the top contacts.")
    analysis = {"findings": [finding(t, tier="inference", rests_on=[server_doc], kind="cdr_compare") for t in lines]}
    return write_facts(case, own_doc, analysis)


# -- R2 Enricher ----------------------------------------------------------------------------


def contacts_to_enrich(case: Any, doc_id: str, graph: dict[str, Any], top: int = 5) -> list[str]:
    """Top contacts of a CDR not on the graph - whose owner R2 would look up."""
    from sherlocks.evidence.uploads import graph_phones

    analysis = ((case.documents.get(doc_id) or {}).get("data") or {}).get("analysis") or {}
    on_graph = graph_phones(graph)
    return [n for n in analysis.get("top_contacts") or [] if n not in on_graph][:top]


def enrich(case: Any, doc_id: str, tools: Any, graph: dict[str, Any]) -> list[str]:
    """Owners of the top contacts (SIMs) and the handset behind each IMEI (CDR server),
    through the guarded tools: only numbers the officer confirmed, within the budget."""
    from sherlocks.evidence.guard import GuardError

    lines: list[dict[str, Any]] = []
    if doc_id not in (case.dialog.get("confirmed_docs") or []):
        return []                                     # the officer has not agreed to these lookups
    for number in contacts_to_enrich(case, doc_id, graph):
        try:
            out = tools.lookup({"system": "simsdb", "phone": number})
        except (GuardError, ValueError) as exc:
            logger.info("R2 skipped %s: %s", number, exc)
            continue
        owner = out.get("subject") or ""
        cnic = next((f.split(": ", 1)[1] for f in out.get("fields") or [] if f.lower().startswith("cnic")), "")
        lines.append(finding(f"Owner of {number}: {owner or 'not found'}" + (f" (CNIC {cnic})" if cnic else "")
                             + " [SIMs].", tier="fact", rests_on=[doc_id], kind="telecom_owner"))
    analysis = ((case.documents.get(doc_id) or {}).get("data") or {}).get("analysis") or {}
    for imei in (analysis.get("imeis") or [])[:2]:
        try:
            out = tools.cdr_lookup({"provider": "imei", "imei": imei})
        except (GuardError, ValueError) as exc:
            logger.info("R2 skipped IMEI %s: %s", imei, exc)
            break
        lines.append(finding(f"IMEI {imei}: {out.get('summary') or 'no handset record'} [CDR server].",
                             rests_on=[doc_id], kind="telecom_imei"))
    return write_facts(case, doc_id, {"findings": lines}) if lines else []


# -- re-runs ----------------------------------------------------------------------------------


def _events_of(doc: dict[str, Any], incident: dict[str, Any] | None, phones: dict[str, str], radius_km: float) -> dict[str, Any]:
    from pathlib import Path

    from sherlocks.evidence import cdr

    path = Path((doc.get("data") or {}).get("path") or "")
    if not path.is_file():
        return {}
    owner_phone = ((doc.get("data") or {}).get("analysis") or {}).get("subject")
    return cdr.analyse(path.read_bytes(), phones_on_graph=phones, incident=incident, llm=None,
                       owner_hint=owner_phone, radius_km=radius_km, keep_events=True)


def rerun(case: Any, graph: dict[str, Any], task: str, *, radius_km: float = 2.0) -> list[str]:
    """Redo part of the CDR team's work after the board changed (see the module doc)."""
    from sherlocks.evidence.uploads import graph_phones

    phones = {p: n for p, (_pid, n) in graph_phones(graph).items()}
    written: list[str] = []
    docs = [d for d in case.documents.values() if d["kind"] == "cdr" and (d.get("data") or {}).get("path")]
    if task.startswith("cdr:refile:"):
        doc_id = task.split(":", 2)[2]
        doc = case.documents.get(doc_id)
        if doc and doc.get("owners"):
            refile(case, doc_id, doc["owners"][0])
        return written
    if task in ("cdr:location", "cdr:patterns"):
        for doc in docs:
            analysis = _events_of(doc, case.incident, phones, radius_km)
            if not analysis:
                continue
            new = set(write_facts(case, doc["id"], analysis))
            written += sorted(new)
            # Findings of the old incident that no longer hold: out of answers.
            for fid in doc.get("facts") or []:
                f = case.facts[fid]
                if fid not in new and any(t.startswith("incident:") for t in f.get("rests_on") or []):
                    case.mark_stale([fid], "the incident changed - re-analysed")
        return written
    if task == "cdr:cross":
        analyses = []
        for doc in docs:
            a = _events_of(doc, case.incident, phones, radius_km)
            if a.get("events"):
                analyses.append({"doc": doc["id"], "subject": a.get("subject"), "events": a["events"]})
        if len(analyses) < 2:
            return written
        found = cross(analyses)
        if not found:
            return written
        with case.batch("R5 Cross-CDR linker", "cdr_cross"):
            target = case.doc_for(CROSS_KEY)
            text = "\n".join(f["text"] for f in found)
            doc_id = target["id"] if target else case.add_document(
                kind="cdr", key=CROSS_KEY, title="Cross-CDR analysis", source="CDR team (R5)", text=text,
                summary=f"{len(analyses)} CDRs compared")
            written += write_facts(case, doc_id, {"findings": found})
    return written
