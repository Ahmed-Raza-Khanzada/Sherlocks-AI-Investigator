"""What a CDR says about the people of the case, computed in code over every record (no model).

A CDR is read for whose it is:

* **the target's own CDR** (the target is the A party, the subscriber): every unique number he was in contact with
  (calls, SMS, in and out, talk time, first and last contact, days in touch, night contacts, the hours they talk,
  where he was), every unique place (tower / site) he used and how often, and his profile - whom he contacts most,
  his active hours and days, silences, the likely home area (night) and work area (day), handsets and SIMs, the
  longest calls, numbers seen only around the incident. His top contacts are then searched like a target
  (``linkgraph/runs.expand_contacts``), and the first few in the CDR archive;
* **someone else's CDR the target is in** (the target is a B party): only the contact history between the two -
  every call and SMS, its time, direction and length and the subscriber's tower at that moment - and the subscriber
  is searched. Nothing about his other contacts.

What is computed is written on the case board as one document per CDR (its tables, with the rows each line rests
on) and as facts, for the chat, Sherlock and the report.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from sherlocks.evidence import cdr_tools

logger = logging.getLogger(__name__)

KEY = "cdrprofile:"
FACT_CONTACTS = 50          # contact lines also kept as facts (the document holds every one)
FACT_PLACES = 30
MAX_TEXT = 600_000
_MOBILE = re.compile(r"^03\d{9}$")
_LANDLINE = re.compile(r"^0[1-9]\d{8,9}$")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def service(number: str) -> bool:
    """A short code, a bank or operator line, an international service id: not a person's phone."""
    n = cdr_tools.norm(number)
    return not (_MOBILE.match(n) or _LANDLINE.match(n))


def _rows(rs: list[dict[str, Any]], most: int = 12) -> str:
    rows = sorted({r["row"] for r in rs if r.get("row") is not None})
    shown = ", ".join(str(r) for r in rows[:most])
    return f"rows {shown}" + (f" (+{len(rows) - most})" if len(rows) > most else "")


def _t(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _hours(rs: list[dict[str, Any]], most: int = 2) -> list[int]:
    return [h for h, _ in Counter(r["hour"] for r in rs if r.get("hour") is not None).most_common(most)]


def _place(rs: list[dict[str, Any]]) -> str:
    c = Counter(r.get("addr") or r.get("site") for r in rs if r.get("addr") or r.get("site"))
    return c.most_common(1)[0][0] if c else ""


def _night(r: dict[str, Any]) -> bool:
    return r.get("hour") is not None and (r["hour"] >= 23 or r["hour"] < 5)


def contacts(calls: list[dict[str, Any]], subject: str) -> list[dict[str, Any]]:
    """Every unique number the subject was in contact with, most first."""
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in cdr_tools.of(calls, subject):
        if r.get("other"):
            by[r["other"]].append(r)
    out = []
    for number, rs in by.items():
        times = sorted(r["when"] for r in rs if r.get("when"))
        out.append({"number": number, "records": len(rs),
                    "calls_out": sum(r.get("kind") == "call" and r.get("way") == "out" for r in rs),
                    "calls_in": sum(r.get("kind") == "call" and r.get("way") == "in" for r in rs),
                    "sms_out": sum(r.get("kind") == "sms" and r.get("way") == "out" for r in rs),
                    "sms_in": sum(r.get("kind") == "sms" and r.get("way") == "in" for r in rs),
                    "talk_seconds": sum(r.get("duration") or 0 for r in rs if r.get("kind") == "call"),
                    "first": times[0] if times else None, "last": times[-1] if times else None,
                    "days": len({r["date"] for r in rs if r.get("date")}), "night": sum(_night(r) for r in rs),
                    "hours": _hours(rs), "where": _place(rs), "service": service(number), "rows": _rows(rs)})
    out.sort(key=lambda x: (x["service"], -x["records"], -x["talk_seconds"]))
    return out


def places(calls: list[dict[str, Any]], subject: str) -> list[dict[str, Any]]:
    """Every unique tower / site the subject used, most first."""
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in cdr_tools.of(calls, subject):
        key = r.get("addr") or r.get("site")
        if key:
            by[key].append(r)
    out = []
    for key, rs in by.items():
        times = sorted(r["when"] for r in rs if r.get("when"))
        out.append({"place": key, "site": rs[0].get("site"), "lat": rs[0].get("lat"), "lon": rs[0].get("lon"),
                    "records": len(rs), "calls": sum(r.get("kind") == "call" for r in rs),
                    "sms": sum(r.get("kind") == "sms" for r in rs), "night": sum(_night(r) for r in rs),
                    "day": sum(9 <= (r.get("hour") or -1) < 18 for r in rs),
                    "contacts": len({r["other"] for r in rs if r.get("other")}),
                    "first": times[0] if times else None, "last": times[-1] if times else None, "rows": _rows(rs)})
    out.sort(key=lambda x: -x["records"])
    return out


def profile(calls: list[dict[str, Any]], subject: str, incident: dict[str, Any] | None = None) -> dict[str, Any]:
    """The subject's habits, from his own CDR."""
    own = cdr_tools.of(calls, subject)
    people = [c for c in contacts(calls, subject) if not c["service"]]
    where = places(calls, subject)
    hours = Counter(r["hour"] for r in own if r.get("hour") is not None)
    days = Counter(_t(r["when"]).weekday() for r in own if _t(r.get("when")))
    dates = sorted({r["date"] for r in own if r.get("date")})
    home = max((p for p in where if p["night"]), key=lambda p: p["night"], default=None)
    work = max((p for p in where if p["day"]), key=lambda p: p["day"], default=None)
    longest = sorted((r for r in own if r.get("kind") == "call" and r.get("duration")), key=lambda r: -(r["duration"] or 0))[:5]
    around: list[str] = []
    day = _t(f"{str((incident or {}).get('date') or '')[:10]}T00:00")
    if day:
        lo, hi = day - timedelta(days=1), day + timedelta(days=2)
        for c in people:
            first, last = _t(c["first"]), _t(c["last"])
            if first and last and lo <= first and last < hi:
                around.append(c["number"])
    times = sorted(r["when"] for r in own if r.get("when"))
    return {"subject": subject, "records": len(own), "first": times[0] if times else None,
            "last": times[-1] if times else None, "active_days": len(dates),
            "contacts": len(people), "services": sum(1 for c in contacts(calls, subject) if c["service"]),
            "top": people[:10], "busiest_hours": [h for h, _ in hours.most_common(3)],
            "night": sum(v for h, v in hours.items() if h >= 23 or h < 5),
            "weekdays": [WEEKDAYS[d] for d, _ in days.most_common(2)],
            "per_day": round(len(own) / len(dates), 1) if dates else 0,
            "silences": cdr_tools.silences(own, None, 12)[:3], "home": home, "work": work, "places": len(where),
            "devices": cdr_tools.devices(own), "longest": [cdr_tools.brief(r) for r in longest],
            "only_around_incident": around}


def history(calls: list[dict[str, Any]], subject: str, other: str) -> dict[str, Any]:
    """Every record between the subject (the CDR's own number) and ``other``, with the subject's tower at the time."""
    rs = sorted((r for r in cdr_tools.of(calls, subject) if r.get("other") == cdr_tools.norm(other)),
                key=lambda r: r.get("when") or "")
    times = [r["when"] for r in rs if r.get("when")]
    longest = max((r for r in rs if r.get("kind") == "call"), key=lambda r: r.get("duration") or 0, default=None)
    return {"subject": subject, "other": cdr_tools.norm(other), "records": len(rs),
            "calls": sum(r.get("kind") == "call" for r in rs), "sms": sum(r.get("kind") == "sms" for r in rs),
            "from_subject": sum(r.get("way") == "out" for r in rs), "to_subject": sum(r.get("way") == "in" for r in rs),
            "talk_seconds": sum(r.get("duration") or 0 for r in rs if r.get("kind") == "call"),
            "first": times[0] if times else None, "last": times[-1] if times else None,
            "days": len({r["date"] for r in rs if r.get("date")}), "hours": _hours(rs, 3),
            "longest": cdr_tools.brief(longest) if longest else None,
            "events": [{"when": r.get("when"), "kind": r.get("kind"), "way": r.get("way"), "duration": r.get("duration"),
                        "where": r.get("addr") or r.get("site") or "", "row": r.get("row")} for r in rs],
            "rows": _rows(rs)}


# -- whose CDR, and what goes on the board -------------------------------------------------------


def _phones_of(net: Any, pids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pid in pids:
        for p in net.data(pid).get("phones") or []:
            n = cdr_tools.norm(p)
            if n:
                out.setdefault(n, pid)
    return out


def whose(case: Any, doc_id: str, net: Any) -> dict[str, Any]:
    """``{mode: own | contact | none, subject, pid, targets: [{pid, number}]}`` - the target's own CDR (he is the A
    party), someone else's CDR he is in (a B party), or neither."""
    doc = case.documents.get(doc_id) or {}
    subject = cdr_tools.norm(((doc.get("data") or {}).get("analysis") or {}).get("subject"))
    targets = _phones_of(net, list(net.seeds()))
    if subject and subject in targets:
        return {"mode": "own", "subject": subject, "pid": targets[subject], "targets": []}
    from sherlocks.team import cdr_analyst

    calls = [r for r in cdr_analyst.records(case) if r["file"] == doc_id]
    met = sorted({r["other"] for r in calls if r.get("other") in targets})
    if subject and met:
        return {"mode": "contact", "subject": subject, "pid": net.resolve(subject),
                "targets": [{"pid": targets[n], "number": n} for n in met]}
    return {"mode": "none", "subject": subject, "pid": None, "targets": []}


def _clock(h: int) -> str:
    return f"{h:02d}:00-{(h + 1) % 24:02d}:00"


def _mins(seconds: int) -> str:
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


def _who(net: Any, number: str) -> str:
    pid = net.resolve(number) if net is not None else None
    return f" ({net.name(pid)})" if pid else ""


def lines_own(net: Any, name: str, pro: dict[str, Any], people: list[dict[str, Any]], where: list[dict[str, Any]]) -> list[str]:
    s = pro["subject"]
    out = [(f"Profile of {name} ({s}) from the own CDR of {s}: {pro['records']} records, {pro['first']} to "
            f"{pro['last']}, active on {pro['active_days']} day(s), about {pro['per_day']} a day; {pro['contacts']} "
            f"unique contact number(s) and {pro['services']} service number(s); {pro['places']} place(s).")]
    if pro["top"]:
        out.append(f"{name} ({s}) is in contact most with: " + "; ".join(
            f"{c['number']}{_who(net, c['number'])} {c['records']} times" for c in pro["top"][:5]) + ".")
    if pro["busiest_hours"]:
        out.append(f"{name} ({s}) is most active at " + ", ".join(_clock(h) for h in pro["busiest_hours"])
                   + f"; {pro['night']} record(s) between 23:00 and 05:00; busiest days: {', '.join(pro['weekdays'])}.")
    if pro["home"]:
        h = pro["home"]
        out.append(f"Likely home area of {name} ({s}): {h['place']} - {h['night']} night record(s) there ({h['rows']}).")
    if pro["work"]:
        w = pro["work"]
        out.append(f"Likely day / work area of {name} ({s}): {w['place']} - {w['day']} daytime record(s) ({w['rows']}).")
    for d in pro["devices"][:3]:
        out.append(f"Handset of {s}: IMEI {d.get('imei') or '-'}, SIM {d.get('imsi') or '-'}, {d['records']} records, "
                   f"{d.get('first')} to {d.get('last')}.")
    for g in pro["silences"]:
        out.append(f"{s} silent from {g.get('from')} to {g.get('to')} ({g.get('hours')} h).")
    for r in pro["longest"][:3]:
        out.append(f"Long call of {s}: {r.get('when')} with {r.get('other')}{_who(net, str(r.get('other')))}, "
                   f"{_mins(int(r.get('duration') or 0))} (row {r.get('row')}).")
    if pro["only_around_incident"]:
        out.append(f"Numbers {s} was in contact with only around the incident date: "
                   + ", ".join(pro["only_around_incident"][:10]) + ".")
    out.append(f"Every unique contact of {s}:")
    for i, c in enumerate(people, 1):
        out.append(f"  {i}. {c['number']}{_who(net, c['number'])}{' [service]' if c['service'] else ''}: "
                   f"{c['records']} records - calls {c['calls_out']} out / {c['calls_in']} in, SMS {c['sms_out']} out / "
                   f"{c['sms_in']} in, talk {_mins(c['talk_seconds'])}; {c['first']} to {c['last']}, {c['days']} day(s), "
                   f"{c['night']} at night; usually " + (", ".join(_clock(h) for h in c["hours"]) or "-")
                   + (f"; {s} mostly at {c['where']}" if c["where"] else "") + f" ({c['rows']}).")
    out.append(f"Every unique place {s} used:")
    for i, p in enumerate(where, 1):
        out.append(f"  {i}. {p['place']}" + (f" (site {p['site']})" if p.get("site") and p["site"] != p["place"] else "")
                   + f": {p['records']} records - {p['calls']} calls, {p['sms']} SMS, {p['contacts']} contact(s), "
                   f"{p['night']} at night, {p['day']} in the day; {p['first']} to {p['last']} ({p['rows']}).")
    return out


def lines_contact(net: Any, subject: str, h: dict[str, Any], target: str, number: str) -> list[str]:
    a = f"{subject}{_who(net, subject)}"
    out = [f"{target}'s number {number} is in the CDR of {a} {h['records']} time(s): {h['calls']} call(s), "
           f"{h['sms']} SMS; {h['from_subject']} from {subject} to {target}, {h['to_subject']} from {target} to "
           f"{subject}; talk {_mins(h['talk_seconds'])}; {h['first']} to {h['last']} on {h['days']} day(s)"
           + (f"; usually {', '.join(_clock(x) for x in h['hours'])}" if h["hours"] else "") + f" ({h['rows']})."]
    if h["longest"]:
        lo = h["longest"]
        out.append(f"Longest call between {subject} and {number}: {lo.get('when')}, "
                   f"{_mins(int(lo.get('duration') or 0))} (row {lo.get('row')}).")
    out.append(f"Every contact between {subject} and {target} ({number}):")
    for e in h["events"][:200]:
        way = {"out": f"{subject} -> {number}", "in": f"{number} -> {subject}"}.get(str(e.get("way")), "-")
        out.append(f"  {e.get('when')} · {e.get('kind') or '-'} · {way}"
                   + (f" · {_mins(int(e['duration']))}" if e.get("duration") else "")
                   + (f" · {subject} at {e['where']}" if e.get("where") else "") + f" (row {e.get('row')}).")
    return out


def write(case: Any, doc_id: str, net: Any) -> dict[str, Any] | None:
    """Compute what the CDR ``doc_id`` says about the people of the case and put it on the board once. Returns
    ``{mode, subject, pid, targets, contacts: [numbers to search, most first], doc}`` or None."""
    from sherlocks.evidence.case_file import TRUST_SYSTEM
    from sherlocks.team import cdr_analyst

    doc = case.documents.get(doc_id) or {}
    if doc.get("kind") != "cdr" or (doc.get("data") or {}).get("profiled"):
        return None
    who = whose(case, doc_id, net)
    with case._lock:
        doc.setdefault("data", {})["profiled"] = who["mode"]
    if who["mode"] == "none":
        return None
    calls = [r for r in cdr_analyst.records(case) if r["file"] == doc_id]
    s = who["subject"]
    if who["mode"] == "own":
        name = net.name(who["pid"])
        people, where = contacts(calls, s), places(calls, s)
        pro = profile(calls, s, case.incident)
        lines = lines_own(net, name, pro, people, where)
        title, summary = (f"Call records of {name} ({s})",
                          f"{len(people)} unique contacts, {len(where)} places; his own CDR [{doc_id}]")
        data = {"mode": "own", "of": doc_id, "subject": s, "pid": who["pid"], "profile": pro, "contacts": people,
                "places": where}
        head = lines[:lines.index(f"Every unique contact of {s}:")]
        # On the board as facts: the profile, the top contacts and the places he uses most; the document holds all.
        facts = head + [x.strip() for x in lines if x.startswith("  ")][:FACT_CONTACTS] + [
            x.strip() for x in lines[lines.index(f"Every unique place {s} used:") + 1:]][:FACT_PLACES]
        pids = [who["pid"]]
        search = [c["number"] for c in people if not c["service"]]
    else:
        lines, data = [], {"mode": "contact", "of": doc_id, "subject": s, "pid": who["pid"], "histories": []}
        for t in who["targets"]:
            h = history(calls, s, t["number"])
            data["histories"].append({**h, "target": t["pid"]})
            lines += lines_contact(net, s, h, net.name(t["pid"]), t["number"])
        title = f"Contact history: {s} and " + ", ".join(net.name(t["pid"]) for t in who["targets"])
        summary = f"the CDR of {s} [{doc_id}]: only its contact with the target(s)"
        facts = [x for x in lines if not x.startswith("  ") and not x.endswith(":")]
        pids = [t["pid"] for t in who["targets"]] + ([who["pid"]] if who["pid"] else [])
        search = [s]
    text = "\n".join(lines)
    with case.batch("CDR team", "cdr"):
        pdoc = case.add_document(kind="cdr_profile", key=f"{KEY}{doc_id}", title=title, source=doc.get("title") or doc_id,
                                 text=text, summary=summary, data=data, max_text=MAX_TEXT)
        for line in facts:
            case.add_fact(pdoc, line, line, kind="cdr_profile", by="cdr", tier="fact", trust=TRUST_SYSTEM,
                          pids=[p for p in pids if p], topic="call_records")
    return {**who, "contacts": search, "doc": pdoc}


def for_report(case: Any, net: Any) -> dict[str, Any]:
    """What the report's "Call records" section shows: each analysed CDR's tables, and who is behind each number."""
    out: dict[str, Any] = {"own": [], "contact": []}
    for doc in case.documents.values():
        if doc.get("kind") != "cdr_profile":
            continue
        d = doc.get("data") or {}

        def name(number: str) -> str:
            pid = net.resolve(number)
            return net.name(pid) if pid else ""

        if d.get("mode") == "own":
            out["own"].append({"name": net.name(d["pid"]) if d.get("pid") in net.people else d.get("subject"),
                               "subject": d["subject"], "profile": d["profile"], "doc": doc["id"], "source": doc.get("source"),
                               "contacts": [{**c, "owner": name(c["number"])} for c in d.get("contacts") or []],
                               "places": d.get("places") or []})
        elif d.get("mode") == "contact":
            for h in d.get("histories") or []:
                out["contact"].append({**h, "subject_name": name(h["subject"]),
                                       "target_name": net.name(h["target"]) if h.get("target") in net.people else "",
                                       "doc": doc["id"], "source": doc.get("source")})
    return out
