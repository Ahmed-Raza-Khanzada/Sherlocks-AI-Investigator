"""CDR tools: the fixed analytics over call records, as plain functions.

Every function takes ``calls`` - the records of all CDRs on the case board, one dict a record - and returns
small lists or dicts that name the file and sheet rows each result rests on. They are the building blocks of
the CDR analyst (``team/cdr_analyst.py``): for a question no function answers, the analyst writes a few lines
of Python that combine them, and that code runs in the sandbox (``cdr_sandbox.py``) - the records never go to
the model, only what the code computed.

Standard library only: this module is loaded into the sandbox as it is.

A record::

    {"file": "D12", "title": "Upload: x.xlsx", "subject": "03001234567",   # the CDR's own number (A party)
     "row": 14, "a": "03001234567", "b": "03111234567", "other": "03111234567",
     "when": "2026-01-01 10:02:20", "date": "2026-01-01", "hour": 10,
     "kind": "call" | "sms" | "data" | "", "way": "in" | "out" | "", "duration": 74,
     "site": "41001-123", "addr": "Saddar, Karachi", "lat": 24.86, "lon": 67.01, "imei": "35…", "imsi": "41…"}
"""

from __future__ import annotations

import itertools
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

MOST = 50          # rows any function returns at most


def norm(number: Any) -> str:
    """A phone number as the records write it: 0399-0000123, +923990000123, 3990000123 -> 03990000123."""
    digits = re.sub(r"\D", "", str(number or ""))
    if digits.startswith("92") and len(digits) == 12:
        digits = digits[2:]
    if len(digits) == 10 and digits.startswith("3"):
        digits = "0" + digits
    return digits


def _t(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _rows(records: list[dict[str, Any]]) -> dict[str, list[int]]:
    """Which rows of which file: {"D12": [3, 4, 9]}."""
    out: dict[str, list[int]] = defaultdict(list)
    for r in records:
        out[r["file"]].append(r["row"])
    return {f: sorted(set(rows))[:MOST] for f, rows in out.items()}


FIELDS = ("when", "subject", "other", "a", "b", "kind", "way", "duration", "site", "addr", "lat", "lon", "imei", "file", "row")


def brief(r: dict[str, Any]) -> dict[str, Any]:
    """One record with the fields every function returns: when, subject, other, a, b, kind, way, duration, site,
    addr, lat, lon, imei, file, row."""
    return {k: r.get(k) for k in FIELDS}


def files(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each CDR on the board. Returns [{"file", "title", "subject", "records", "first", "last", "contacts"}]."""
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in calls:
        by[r["file"]].append(r)
    out = []
    for f, rs in by.items():
        times = sorted(r["when"] for r in rs if r["when"])
        out.append({"file": f, "title": rs[0].get("title"), "subject": rs[0].get("subject"), "records": len(rs),
                    "first": times[0] if times else None, "last": times[-1] if times else None,
                    "contacts": len({r["other"] for r in rs if r.get("other")})})
    return out


def involving(calls: list[dict[str, Any]], number: Any) -> list[dict[str, Any]]:
    """Every record where this number is either party (A or B), in any CDR. Returns a list of records."""
    n = norm(number)
    return [r for r in calls if n and n in (r.get("a"), r.get("b"))]


def of(calls: list[dict[str, Any]], number: Any) -> list[dict[str, Any]]:
    """The records of the CDR(s) whose own number this is. Returns a list of records."""
    n = norm(number)
    return [r for r in calls if r.get("subject") == n]


def top_contacts(calls: list[dict[str, Any]], number: Any = None, n: int = 10, kind: str | None = None) -> list[dict[str, Any]]:
    """The numbers this number (or every CDR's own number, when none is given) talks to most: calls, SMS,
    in / out, total talk time in seconds, first and last contact, and the rows. Returns [{"number", "records", "calls",
    "sms", "outgoing", "incoming", "talk_seconds", "first", "last", "subjects", "rows"}], most first."""
    rows = of(calls, number) if number else calls
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("other"):
            by[r["other"]].append(r)
    out = []
    for other, rs in by.items():
        times = sorted(r["when"] for r in rs if r["when"])
        out.append({"number": other, "records": len(rs), "calls": sum(r.get("kind") == "call" for r in rs),
                    "sms": sum(r.get("kind") == "sms" for r in rs), "outgoing": sum(r.get("way") == "out" for r in rs),
                    "incoming": sum(r.get("way") == "in" for r in rs),
                    "talk_seconds": sum(r.get("duration") or 0 for r in rs if r.get("kind") == "call"),
                    "first": times[0] if times else None, "last": times[-1] if times else None,
                    "subjects": sorted({r["subject"] for r in rs if r.get("subject")}), "rows": _rows(rs)})
    out.sort(key=lambda x: (-x["records"], -x["talk_seconds"]))
    return out[:n]


def between(calls: list[dict[str, Any]], x: Any, y: Any) -> list[dict[str, Any]]:
    """Every record between two numbers, in time order, from any CDR that holds them (a call in both CDRs once).
    Returns [brief records]."""
    a, b = norm(x), norm(y)
    rows = [r for r in calls if {r.get("a"), r.get("b")} == {a, b}]
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r["when"] or ""):
        key = (r["when"], r.get("kind"), r.get("duration"))     # the same call in both parties' CDRs, once
        if key in seen:
            continue
        seen.add(key)
        out.append(brief(r))
    return out[:MOST]


def common_contacts(calls: list[dict[str, Any]], numbers: list[Any] | None = None, least: int = 2) -> list[dict[str, Any]]:
    """Numbers in touch with ``least`` or more of the given numbers (every CDR's own number by default) - who
    links them. Returns [{"number", "with": {subject number: records}, "rows"}], most connected first."""
    subjects = [norm(n) for n in numbers] if numbers else sorted({r["subject"] for r in calls if r.get("subject")})
    touched: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for r in calls:
        for s in subjects:
            if s in (r.get("a"), r.get("b")):
                other = r.get("b") if r.get("a") == s else r.get("a")
                if other and other not in subjects:
                    touched[other][s].append(r)
    out = [{"number": other, "with": {s: len(rs) for s, rs in by.items()},
            "rows": _rows([r for rs in by.values() for r in rs])}
           for other, by in touched.items() if len(by) >= least]
    out.sort(key=lambda x: (-len(x["with"]), -sum(x["with"].values())))
    return out[:MOST]


def window(calls: list[dict[str, Any]], start: Any, end: Any, number: Any = None) -> list[dict[str, Any]]:
    """Records between two times ("2026-01-01 10:00", "2026-01-01 14:00"), of one number or all, in order.
    Returns [brief records]."""
    s, e = _t(start), _t(end)
    rows = involving(calls, number) if number else calls
    rows = [r for r in rows if (t := _t(r["when"])) and (s is None or t >= s) and (e is None or t <= e)]
    return [brief(r) for r in sorted(rows, key=lambda r: r["when"])][:MOST]


def on_day(calls: list[dict[str, Any]], day: Any, number: Any = None) -> list[dict[str, Any]]:
    """Records on one date ("2026-01-01"), of one number or all, in order. Returns the same rows as window()."""
    d = str(day)[:10]
    return window(calls, f"{d} 00:00:00", f"{d} 23:59:59", number)


def daily(calls: list[dict[str, Any]], number: Any = None) -> list[dict[str, Any]]:
    """Records per day that has any. Returns [{"date", "records", "calls", "sms", "contacts"}] in date order."""
    rows = involving(calls, number) if number else calls
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("date"):
            by[r["date"]].append(r)
    return [{"date": d, "records": len(rs), "calls": sum(r.get("kind") == "call" for r in rs),
             "sms": sum(r.get("kind") == "sms" for r in rs), "contacts": len({r.get("other") for r in rs if r.get("other")})}
            for d, rs in sorted(by.items())]


def by_hour(calls: list[dict[str, Any]], number: Any = None) -> dict[int, int]:
    """Records per hour of the day. Returns {0: count, 1: count, ... 23: count}."""
    rows = involving(calls, number) if number else calls
    counts = Counter(r["hour"] for r in rows if r.get("hour") is not None)
    return {h: counts.get(h, 0) for h in range(24)}


def night(calls: list[dict[str, Any]], number: Any = None, start: int = 0, end: int = 5) -> dict[str, Any]:
    """Records from ``start`` to ``end`` o'clock (23 to 5 runs across midnight). Returns {"count", "of",
    "contacts": [{"number", "count"}], "records": [brief records, at most 50], "rows"}."""
    rows = involving(calls, number) if number else calls

    def late(h: int) -> bool:
        return start <= h < end if start < end else (h >= start or h < end)

    hits = [r for r in rows if r.get("hour") is not None and late(r["hour"])]
    return {"count": len(hits), "of": len(rows),
            "contacts": [{"number": n, "count": c} for n, c in Counter(r.get("other") for r in hits if r.get("other")).most_common(20)],
            "records": [brief(r) for r in sorted(hits, key=lambda r: r["when"] or "")][:MOST], "rows": _rows(hits)}


def silences(calls: list[dict[str, Any]], number: Any = None, hours: float = 6) -> list[dict[str, Any]]:
    """Gaps of ``hours`` or more with no record (the phone off or unused), longest first. Returns [{"from", "to",
    "hours", "last_before", "first_after", "file", "rows"}]."""
    rows = sorted((r for r in (of(calls, number) if number else calls) if r["when"]), key=lambda r: r["when"])
    out = []
    for p, q in itertools.pairwise(rows):
        gap = (_t(q["when"]) - _t(p["when"])).total_seconds() / 3600
        if gap >= hours:
            out.append({"from": p["when"], "to": q["when"], "hours": round(gap, 1), "last_before": p.get("other"),
                        "first_after": q.get("other"), "file": p["file"], "rows": [p["row"], q["row"]]})
    out.sort(key=lambda x: -x["hours"])
    return out[:MOST]


def devices(calls: list[dict[str, Any]], number: Any = None) -> list[dict[str, Any]]:
    """Handsets (IMEI) and SIMs (IMSI) used by a number, in order of first use - a change is a new phone or SIM.
    Returns [{"imei", "imsi", "records", "first", "last", "subjects", "rows"}]."""
    rows = of(calls, number) if number else calls
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r.get("imei") or r.get("imsi"):
            by[(r.get("imei") or "", r.get("imsi") or "")].append(r)
    out = []
    for (imei, imsi), rs in by.items():
        times = sorted(r["when"] for r in rs if r["when"])
        out.append({"imei": imei, "imsi": imsi, "records": len(rs), "first": times[0] if times else None,
                    "last": times[-1] if times else None, "subjects": sorted({r["subject"] for r in rs if r.get("subject")}),
                    "rows": _rows(rs)})
    return sorted(out, key=lambda x: x["first"] or "")


def places(calls: list[dict[str, Any]], number: Any = None, n: int = 10) -> list[dict[str, Any]]:
    """The towers / sites a number connects through most. Returns [{"place", "site", "lat", "lon", "records",
    "first", "last", "night_records", "day_records", "rows"}], most first."""
    rows = of(calls, number) if number else calls
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        key = r.get("addr") or r.get("site")
        if key:
            by[key].append(r)
    out = []
    for key, rs in by.items():
        times = sorted(r["when"] for r in rs if r["when"])
        hours = Counter(r["hour"] for r in rs if r.get("hour") is not None)
        out.append({"place": key, "site": rs[0].get("site"), "lat": rs[0].get("lat"), "lon": rs[0].get("lon"),
                    "records": len(rs), "first": times[0] if times else None, "last": times[-1] if times else None,
                    "night_records": sum(v for h, v in hours.items() if h < 6),
                    "day_records": sum(v for h, v in hours.items() if 9 <= h < 18), "rows": _rows(rs)})
    out.sort(key=lambda x: -x["records"])
    return out[:n]


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in km between two points."""
    p = math.pi / 180
    a = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2
    return 12742 * math.asin(math.sqrt(a))


def near(calls: list[dict[str, Any]], lat: float, lon: float, radius_km: float = 2.0, start: Any = None,
         end: Any = None) -> list[dict[str, Any]]:
    """Records on towers within ``radius_km`` of a point, optionally in a time window. A tower covers 0.5-5 km:
    a connection near a place, never presence at it. Returns [brief record + "km"]."""
    s, e = _t(start), _t(end)
    out = []
    for r in calls:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        t = _t(r["when"])
        if (s and (not t or t < s)) or (e and (not t or t > e)):
            continue
        d = km(float(lat), float(lon), r["lat"], r["lon"])
        if d <= radius_km:
            out.append({**brief(r), "km": round(d, 2)})
    return sorted(out, key=lambda r: r["when"] or "")[:MOST]


def together(calls: list[dict[str, Any]], x: Any, y: Any, minutes: int = 15) -> list[dict[str, Any]]:
    """Times two numbers used the same tower within ``minutes`` of each other (from their own CDRs). Returns
    [{"site", "addr", "when_a", "when_b", "minutes", "rows"}]."""
    a, b = of(calls, x), of(calls, y)
    by_site: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in b:
        if r.get("site") and r["when"]:
            by_site[r["site"]].append(r)
    out = []
    for r in a:
        if not (r.get("site") and r["when"]):
            continue
        for q in by_site.get(r["site"], []):
            gap = abs((_t(r["when"]) - _t(q["when"])).total_seconds()) / 60
            if gap <= minutes:
                out.append({"site": r["site"], "addr": r.get("addr"), "when_a": r["when"], "when_b": q["when"],
                            "minutes": round(gap, 1), "rows": {r["file"]: [r["row"]], q["file"]: [q["row"]]}})
    return sorted(out, key=lambda x: x["when_a"])[:MOST]


def first_last(calls: list[dict[str, Any]], number: Any) -> dict[str, Any]:
    """A number's first and last record in the CDRs. Returns {"number", "records", "first": brief record,
    "last": brief record}."""
    rows = sorted((r for r in involving(calls, number) if r["when"]), key=lambda r: r["when"])
    if not rows:
        return {"number": norm(number), "records": 0}
    return {"number": norm(number), "records": len(rows), "first": brief(rows[0]), "last": brief(rows[-1])}


def before_after(calls: list[dict[str, Any]], number: Any, moment: Any, hours: float = 2) -> dict[str, Any]:
    """A number's records in the ``hours`` before and after a moment (an incident time). Returns {"before": [rows as
    window()], "after": [...]}."""
    m = _t(moment)
    if m is None:
        return {"error": "give the moment as YYYY-MM-DD HH:MM"}
    span = timedelta(hours=hours)
    return {"before": window(calls, m - span, m, number), "after": window(calls, m, m + span, number)}


FUNCTIONS = (brief, files, involving, of, top_contacts, between, common_contacts, window, on_day, daily, by_hour, night,
             silences, devices, places, near, together, first_last, before_after, km, norm)
