"""The CDR agent: call records, tower dumps and geofences uploaded as Excel.

Two ways to read one:

* **The CDR server** (CDR Report App, ``EMS_CDR_API_URL``) knows the operators' formats
  (Telenor, Zong, Jazz, Ufone, the unified export) and BTS dumps. It inspects the file,
  runs its analysis (with the incident date, place and FIR when the case board has them)
  and returns a PDF summary, which is read back as a case document. Its analysis also
  looks up top contacts in the police systems - live, logged queries.
* **Its own analysis**, always, and the only one for a format the server does not know
  or when the server is down: the columns are recognised by their headers (the model
  maps them when the headers are unfamiliar), and the file is analysed here - top
  contacts, contacts already on the graph, towers near the incident, the crime day,
  devices (IMEI).

Every finding is written as a line of the analysis text, so facts can quote it.
"""

from __future__ import annotations

import io
import logging
import math
import re
import time
from collections import Counter
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from sherlocks.linkgraph.ems import CONF, EmsHttp, Req
from sherlocks.linkgraph.normalize import mobile11

logger = logging.getLogger(__name__)

# -- the CDR server ----------------------------------------------------------------------


class CdrServer:
    """Client for the CDR Report App on the CDR server."""

    def __init__(self, http: EmsHttp | None = None) -> None:
        self.http = http or EmsHttp(timeout=120)
        self.base = (CONF.get("cdr_api_url") or "").rstrip("/")
        self.key = CONF.get("cdr_api_key") or ""

    @property
    def ready(self) -> bool:
        return bool(self.base and self.key)

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self.key, "Accept": "application/json"}

    def _post(self, path: str, *, files: Any = None, data: dict | None = None, json_body: Any = None,
              timeout: int = 180) -> tuple[int, Any]:
        return self.http.send(Req("POST", f"{self.base}{path}", headers=self._headers(), files=files, data=data,
                                  json=json_body, timeout=timeout))

    def inspect(self, name: str, content: bytes) -> dict[str, Any]:
        """Operator, row count and column mapping of a CDR file. No police lookups."""
        code, body = self._post("/reports/inspect", files={"file": (name, content)})
        return body if code < 400 and isinstance(body, dict) else {"error": body}

    def bts_inspect(self, name: str, content: bytes) -> dict[str, Any]:
        code, body = self._post("/bts/files/inspect", files={"file": (name, content)})
        return body if code < 400 and isinstance(body, dict) else {"error": body}

    def start(self, kind: str, name: str, content: bytes, request: dict[str, Any]) -> str:
        import json

        path = {"cdr": "/jobs/generate", "bts": "/bts/jobs/generate"}[kind]
        field = "file" if kind == "cdr" else "files"
        code, body = self._post(path, files=[(field, (name, content))],
                                data={"request_json": json.dumps(request)})
        if code >= 400 or not isinstance(body, dict) or not body.get("job_id"):
            raise RuntimeError(f"CDR server refused the job: {str(body)[:200]}")
        return str(body["job_id"])

    def wait(self, kind: str, job_id: str, *, timeout: float = 900, every: float = 5,
             cancelled: Any = None) -> bytes:
        """Poll a job until done; the report PDF."""
        status_path = {"cdr": f"/jobs/{job_id}/status", "bts": f"/bts/jobs/{job_id}/status"}[kind]
        pdf_path = {"cdr": f"/jobs/{job_id}/download", "bts": f"/bts/jobs/{job_id}/download/pdf"}[kind]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancelled and cancelled():
                raise RuntimeError("stopped")
            _code, body = self.http.send(Req("GET", f"{self.base}{status_path}", headers=self._headers(), timeout=60))
            state = str((body or {}).get("status") or "").lower() if isinstance(body, dict) else ""
            if state == "completed":
                return self.http.fetch_bytes(f"{self.base}{pdf_path}", headers=self._headers(), timeout=180)
            if state == "failed":
                raise RuntimeError(f"CDR server job failed: {str((body or {}).get('message') or '')[:200]}")
            time.sleep(every)
        raise TimeoutError("CDR server job did not finish in time")

    def provider(self, provider: str, subject: dict[str, Any]) -> dict[str, Any]:
        """One lookup through the CDR server: IMEI, nearest police station to a point, ..."""
        _code, body = self._post("/providers/test", json_body={"provider": provider, "subject": subject}, timeout=90)
        return body if isinstance(body, dict) else {"status": "error", "summary": str(body)[:200]}


def crime_request(incident: dict[str, Any] | None) -> dict[str, Any]:
    """The case board's incident as the CDR server's ``crime`` block."""
    inc = incident or {}
    crime = {"fir_no": inc.get("fir"), "police_station": inc.get("police_station"), "crime_date": inc.get("date"),
             "crime_time": inc.get("time"), "crime_place": inc.get("place"),
             "crime_lat": str(inc["lat"]) if inc.get("lat") is not None else None,
             "crime_lng": str(inc["lon"]) if inc.get("lon") is not None else None}
    return {k: v for k, v in crime.items() if v}


# -- reading the table ---------------------------------------------------------------------

ROLES = ("a_party", "b_party", "datetime", "date", "time", "duration", "direction", "site", "address",
         "lat", "lon", "imei", "imsi")

# Header words -> role, checked in this order (a header like "Site Address" is an address).
_HEADERS: list[tuple[str, tuple[str, ...]]] = [
    ("imei", ("imei",)),
    ("imsi", ("imsi",)),
    ("lat", ("latitude", "lat")),
    ("lon", ("longitude", "long", "lng", "lon")),
    ("duration", ("duration", "dur", "seconds", "secs")),
    ("a_party", ("a party", "a-party", "aparty", "a number", "a_number", "anumber", "a no", "a_no", "msisdn",
                 "calling", "caller", "originating", "source number")),
    ("b_party", ("b party", "b-party", "bparty", "b number", "b_number", "bnumber", "b no", "b_no", "called",
                 "other party", "destination", "dialled", "dialed", "recipient", "contact number")),
    ("address", ("address", "location", "area", "place")),
    ("site", ("cell id", "cellid", "cell_id", "site id", "site_id", "siteid", "bts", "lac", "cgi", "tower", "cell")),
    ("direction", ("direction", "call type", "calltype", "call_type", "event type", "in/out", "type")),
    ("datetime", ("date time", "datetime", "date_time", "timestamp", "start time", "call time", "start")),
    ("date", ("date",)),
    ("time", ("time",)),
]


def _role(header: str) -> str | None:
    h = re.sub(r"\s+", " ", str(header or "").strip().lower())
    if not h or h.startswith("unnamed"):
        return None
    for role, words in _HEADERS:
        for w in words:
            if w in ("lat", "lon", "long", "cell", "type", "time", "date", "start", "lac", "bts"):
                if re.search(rf"(^|[^a-z]){re.escape(w)}([^a-z]|$)", h):
                    return role
            elif w in h:
                return role
    return None


class _Mapping(BaseModel):
    a_party: str | None = Field(default=None, description="Column with the subscriber's own number (A party)")
    b_party: str | None = Field(default=None, description="Column with the other party's number (B party)")
    datetime: str | None = None
    date: str | None = None
    time: str | None = None
    duration: str | None = None
    direction: str | None = None
    site: str | None = Field(default=None, description="Cell / site / tower id")
    address: str | None = Field(default=None, description="Tower or location address")
    lat: str | None = None
    lon: str | None = None
    imei: str | None = None


def load_sheet(content: bytes) -> tuple[list[str], list[list[Any]], str, list[int]]:
    """R1 Intake: the biggest sheet of a workbook - header row (found within the first
    rows), data rows, the sheet's name, and each data row's number in the sheet (as Excel
    shows it), so every finding can cite the rows it rests on."""
    import pandas as pd

    sheets = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, dtype=object)
    best_name, best = max(sheets.items(), key=lambda kv: kv[1].size, default=("", None))
    if best is None or best.empty:
        return [], [], best_name, []
    frame = best.dropna(how="all").dropna(axis=1, how="all")
    header_at = 0
    for i in range(min(25, len(frame))):
        cells = [str(v) for v in frame.iloc[i].tolist() if v is not None and str(v) != "nan"]
        if sum(1 for c in cells if _role(c)) >= 2:
            header_at = i
            break
    headers = [str(v).strip() if v is not None and str(v) != "nan" else f"col{j + 1}"
               for j, v in enumerate(frame.iloc[header_at].tolist())]
    body = frame.iloc[header_at + 1:]
    rows = [[None if (v is None or str(v) == "nan") else v for v in r] for r in body.values.tolist()]
    numbers = [int(i) + 1 for i in body.index.tolist()]
    return headers, rows, best_name, numbers


def as_xlsx(content: bytes) -> bytes:
    """An old-format Excel workbook (.xls) as an .xlsx one, every sheet and cell value kept; anything else as it is."""
    if content[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return content
    import io

    import xlrd
    from openpyxl import Workbook

    book = xlrd.open_workbook(file_contents=content)
    out = Workbook()
    out.remove(out.active)
    for sheet in book.sheets():
        ws = out.create_sheet(sheet.name[:31] or "Sheet")
        for r in range(sheet.nrows):
            row = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                elif cell.ctype == xlrd.XL_CELL_NUMBER and float(cell.value).is_integer():
                    row.append(int(cell.value))
                else:
                    row.append(cell.value)
            ws.append(row)
    buf = io.BytesIO()
    out.save(buf)
    return buf.getvalue()


def load_table(content: bytes) -> tuple[list[str], list[list[Any]], str]:
    """The biggest sheet of a workbook: header row (found within the first rows), data rows."""
    headers, rows, name, _numbers = load_sheet(content)
    return headers, rows, name


def map_columns(headers: list[str], rows: list[list[Any]], llm: Any = None) -> dict[str, int]:
    """Role -> column index. By header words; the model fills in unfamiliar headers."""
    out: dict[str, int] = {}
    for i, h in enumerate(headers):
        role = _role(h)
        if role and role not in out:
            out[role] = i
    if llm is not None and not ({"a_party", "b_party"} <= set(out) and ({"datetime", "date"} & set(out))):
        sample = [dict(zip(headers, [str(v)[:40] if v is not None else "" for v in r], strict=False)) for r in rows[:4]]
        try:
            mapping, _ = llm.generate_structured(
                prompt=f"Columns of a telecom file: {headers}\nSample rows: {sample}\nWhich column holds each field? "
                       "Use the exact column names; null when absent.",
                schema=_Mapping, system="You map telecom CDR / tower dump columns. Answer JSON only.",
                cache_kind="cdr_columns", prompt_version="v1")
            for role, col in mapping.model_dump().items():
                if col and col in headers and role not in out:
                    out[role] = headers.index(col)
        except Exception as exc:  # noqa: BLE001 - header words are the fallback
            logger.info("Column mapping by model failed: %s", exc)
    return out


def _number(value: Any) -> str | None:
    text = re.sub(r"\D", "", str(value or ""))
    if not text:
        return None
    return mobile11(text) or (text if len(text) >= 4 else None)


def _when(row: list[Any], cols: dict[str, int]) -> datetime | None:
    import pandas as pd

    raw = None
    if "datetime" in cols:
        raw = row[cols["datetime"]]
    elif "date" in cols:
        raw = f"{row[cols['date']]} {row[cols['time']]}" if "time" in cols else row[cols["date"]]
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        ts = pd.to_datetime(str(raw), dayfirst=True, errors="coerce")
        return None if pd.isna(ts) else ts.to_pydatetime()
    except (ValueError, TypeError):
        return None


def _float(value: Any) -> float | None:
    try:
        f = float(str(value).strip())
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    a = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2
    return 12742 * math.asin(math.sqrt(a))


def _fmt(when: datetime | None) -> str:
    return when.strftime("%Y-%m-%d %H:%M") if when else "-"


def _pair(value: Any, header: str) -> tuple[float | None, float | None]:
    """(lat, lon) from one cell that holds both ("67.0811, 24.8607"). A value outside -90..90 is the longitude;
    otherwise the header's own order decides ("Longitude and Latitude" gives the longitude first)."""
    found = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", str(value or ""))]
    if len(found) != 2:
        return None, None
    x, y = found
    if abs(x) > 90 >= abs(y):
        return y, x
    if abs(y) > 90 >= abs(x):
        return x, y
    h = header.lower()
    lon_first = "lon" in h and h.find("lon") < h.find("lat") if "lat" in h else False
    return (y, x) if lon_first else (x, y)


def _way(raw: str) -> tuple[str, str]:
    """("call" | "sms" | "data" | "", "in" | "out" | "") from an operator's call type ("Outgoing SMS", "MTC", "MOC")."""
    t = raw.lower()
    kind = "sms" if "sms" in t else "data" if re.search(r"\b(gprs|data|internet)\b", t) else "call" if t else ""
    way = ("in" if re.search(r"\b(in|incoming|mtc|mt|terminat|received)", t)
           else "out" if re.search(r"\b(out|outgoing|moc|mo|originat|dialled|sent)", t) else "")
    return kind, way


def _seconds(value: Any) -> int | None:
    """A duration in seconds: 74, "74", "00:01:14", "1:14"."""
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return int(float(text))
    parts = text.split(":")
    if 2 <= len(parts) <= 3 and all(p.strip().isdigit() for p in parts):
        total = 0
        for p in parts:
            total = total * 60 + int(p)
        return total
    return None


_AT_END = re.compile(r"\s*[|;,]\s*(-?\d{1,3}\.\d{3,})\s*[|;,]\s*(-?\d{1,3}\.\d{3,})\s*$")


def read_events(content: bytes, llm: Any = None) -> dict[str, Any]:
    """R1 Intake: every record of a CDR / tower dump as one clean event - ``{kind: cdr | tower_dump | table |
    empty, headers, sheet, cols, rows, events}``. An event: a, b, when, row (the sheet row), dir (as written),
    kind (call | sms | data), way (in | out), duration (seconds), site, addr, lat, lon, imei, imsi."""
    headers, rows, sheet, numbers = load_sheet(content)
    if not rows:
        return {"kind": "empty", "headers": headers, "sheet": sheet, "cols": {}, "rows": 0, "events": []}
    cols = map_columns(headers, rows, llm)
    if not ({"a_party", "b_party"} & set(cols)):
        return {"kind": "table", "headers": headers, "sheet": sheet, "cols": {}, "rows": len(rows), "events": []}
    both = "lat" in cols and "lon" not in cols or ("lat" in cols and cols.get("lat") == cols.get("lon"))

    def cell(r: list[Any], role: str) -> Any:
        return r[cols[role]] if role in cols and cols[role] < len(r) else None

    events = []
    for r, number in zip(rows, numbers, strict=False):
        raw = str(cell(r, "direction")).strip() if cell(r, "direction") is not None else ""
        kind, way = _way(raw)
        if both:
            lat, lon = _pair(cell(r, "lat"), headers[cols["lat"]])
        else:
            lat, lon = _float(cell(r, "lat")), _float(cell(r, "lon"))
        addr = str(cell(r, "address")).strip() if cell(r, "address") is not None else ""
        tail = _AT_END.search(addr)
        if tail and (lat is None or lon is None):
            # Some operators write the tower's coordinates after its address: "... KARACHI (4G)|24.8543|67.0232".
            lat, lon = _pair(f"{tail.group(1)} {tail.group(2)}", "lat lon")
            addr = addr[:tail.start()].strip()
        events.append({
            "a": _number(cell(r, "a_party")), "b": _number(cell(r, "b_party")), "when": _when(r, cols), "row": number,
            "dir": raw, "kind": kind, "way": way, "duration": _seconds(cell(r, "duration")),
            "site": str(cell(r, "site")).strip() if cell(r, "site") is not None else "",
            "addr": addr, "lat": lat, "lon": lon,
            "imei": re.sub(r"\D", "", str(cell(r, "imei") or ""))[:15],
            "imsi": re.sub(r"\D", "", str(cell(r, "imsi") or ""))[:15],
        })
    a_counts = Counter(e["a"] for e in events if e["a"])
    distinct_a = len(a_counts)
    kind = "tower_dump" if distinct_a > 25 and distinct_a > len(events) * 0.05 else "cdr"
    return {"kind": kind, "headers": headers, "sheet": sheet, "cols": cols, "rows": len(events), "events": events}


def analyse(content: bytes, *, phones_on_graph: dict[str, str], incident: dict[str, Any] | None = None,
            llm: Any = None, owner_hint: str | None = None, radius_km: float = 2.0,
            keep_events: bool = False) -> dict[str, Any]:
    """The CDR team's analysis of an uploaded CDR / tower dump (``cdr_team.py`` has the
    team). Returns ``{kind, lines, findings, subject, matches, near_incident, columns,
    rows, sheet, top_contacts, imeis}``; ``lines`` is the quotable analysis text and each
    of ``findings`` cites its rows. ``keep_events`` adds the cleaned records (for R5)."""
    from sherlocks.evidence import cdr_team
    from sherlocks.evidence.cdr_team import finding

    read = read_events(content, llm)
    headers, sheet, cols, events = read["headers"], read["sheet"], read["cols"], read["events"]
    if read["kind"] == "empty":
        return {"kind": "empty", "lines": ["The workbook has no data rows."], "rows": 0, "findings": []}
    if read["kind"] == "table":
        return {"kind": "table", "lines": [f"Sheet {sheet}: {read['rows']} rows, columns: {', '.join(headers[:30])}."],
                "rows": read["rows"], "columns": {}, "findings": [], "sheet": sheet}
    a_counts = Counter(e["a"] for e in events if e["a"])
    distinct_a = len(a_counts)
    kind = read["kind"]
    times = sorted(e["when"] for e in events if e["when"])
    lines: list[str] = []
    findings: list[dict[str, Any]] = []

    def say(text: str, rows_of: list[dict[str, Any]] | None = None, *, fact: bool = True,
            rests_on: list[str] | None = None) -> None:
        lines.append(text)
        if fact:
            findings.append(finding(text, [e["row"] for e in rows_of or []], rests_on=rests_on))

    say(f"{'Tower dump / geofence' if kind == 'tower_dump' else 'CDR'} file: {len(events)} records, sheet "
        f"{sheet}, period {_fmt(times[0] if times else None)} to {_fmt(times[-1] if times else None)}.", fact=False)
    say("Columns recognised: " + ", ".join(f"{role}={headers[i]}" for role, i in cols.items()) + ".", fact=False)
    out: dict[str, Any] = {"kind": kind, "rows": len(events), "columns": {r: headers[i] for r, i in cols.items()},
                           "matches": [], "near_incident": [], "sheet": sheet}

    matches: dict[str, dict[str, Any]] = {}
    if kind == "cdr":
        subject = owner_hint or (a_counts.most_common(1)[0][0] if a_counts else None)
        out["subject"] = subject
        others = Counter()
        for e in events:
            other = e["b"] if e["a"] == subject or not e["a"] else e["a"]
            if other and other != subject:
                others[other] += 1
        if subject:
            say(f"Subscriber of this CDR: {subject}"
                + (f" ({phones_on_graph[subject]} on the graph)" if subject in phones_on_graph else "") + ".",
                [e for e in events if e["a"] == subject])
        out["top_contacts"] = [num for num, _n in others.most_common(15)]
        for i, (num, n) in enumerate(others.most_common(15), 1):
            mine = [e for e in events if num in (e["a"], e["b"])]
            seen = [e["when"] for e in mine if e["when"]]
            name = phones_on_graph.get(num)
            say(f"Top contact {i}: {num}{f' ({name})' if name else ''} - {n} call(s)/SMS, first "
                f"{_fmt(min(seen) if seen else None)}, last {_fmt(max(seen) if seen else None)}.", mine)
        for num, n in others.items():
            if num in phones_on_graph:
                matches[num] = {"phone": num, "name": phones_on_graph[num], "events": n}
    else:
        out["subject"] = None
        out["top_contacts"] = []
        for num, n in a_counts.most_common():
            if num in phones_on_graph:
                matches[num] = {"phone": num, "name": phones_on_graph[num], "events": n}
        say(f"{distinct_a} distinct numbers used the tower(s).", fact=False)
        for num, n in a_counts.most_common(10):
            say(f"Frequent number: {num}{f' ({phones_on_graph[num]})' if num in phones_on_graph else ''} - {n} record(s).",
                [e for e in events if e["a"] == num])
    for m in sorted(matches.values(), key=lambda m: -m["events"]):
        mine = [e for e in events if m["phone"] in (e["a"], e["b"])]
        seen = sorted(e["when"] for e in mine if e["when"])
        m["first"], m["last"] = _fmt(seen[0] if seen else None), _fmt(seen[-1] if seen else None)
        say(f"On the graph: {m['phone']} is {m['name']} - {m['events']} record(s) in this file, "
            f"{m['first']} to {m['last']}.", mine)
    out["matches"] = list(matches.values())

    sites = Counter((e["addr"] or e["site"]) for e in events if (e["addr"] or e["site"]))
    for place, n in sites.most_common(6):
        say(f"Frequent location: {place} - {n} record(s).", fact=False)
    imeis = Counter(e["imei"] for e in events if len(e["imei"]) >= 14)
    out["imeis"] = [i for i, _n in imeis.most_common(5)]
    if imeis:
        say("Device IMEI(s): " + ", ".join(f"{i} ({n})" for i, n in imeis.most_common(5)) + ".",
            [e for e in events if e["imei"] in dict(imeis.most_common(5))][:40])
    if times:
        night = sum(1 for t in times if t.hour < 5)
        say(f"Night activity (00:00-05:00): {night} of {len(times)} records.", fact=False)

    inc = incident or {}
    if inc.get("date"):
        day = str(inc["date"])[:10]
        that_day = [e for e in events if e["when"] and e["when"].strftime("%Y-%m-%d") == day]
        if that_day:
            that_day.sort(key=lambda e: e["when"])
            say(f"Incident day {day}: {len(that_day)} record(s), first {_fmt(that_day[0]['when'])}, "
                f"last {_fmt(that_day[-1]['when'])}.", that_day, rests_on=["incident:date"])
            for e in that_day[:12]:
                say(f"Incident day record: {_fmt(e['when'])} {e['dir']} {e['a'] or ''}->{e['b'] or ''} at "
                    f"{e['addr'] or e['site'] or '-'}.", [e], rests_on=["incident:date"])
        else:
            say(f"Incident day {day}: no records in this file.", rests_on=["incident:date"])
    if inc.get("lat") is not None and inc.get("lon") is not None and ("lat" in cols and "lon" in cols):
        near = []
        for e in events:
            if e["lat"] is None or e["lon"] is None:
                continue
            d = km(float(inc["lat"]), float(inc["lon"]), e["lat"], e["lon"])
            if d <= radius_km:
                near.append((d, e))
        near.sort(key=lambda x: (x[1]["when"] or datetime.min))
        out["near_incident"] = [{"km": round(d, 2), "when": _fmt(e["when"]), "number": e["a"] or e["b"],
                                 "place": e["addr"] or e["site"], "row": e["row"]} for d, e in near[:200]]
        say(f"Towers within {radius_km:g} km of the incident point: {len(near)} record(s). A tower covers roughly "
            "0.5-5 km: this shows a connection near the scene, not presence at it.", [e for _d, e in near][:60],
            rests_on=["incident:place"])
        for d, e in near[:10]:
            who = e["a"] if kind == "tower_dump" else (e["b"] or e["a"])
            say(f"Near the incident: {_fmt(e['when'])}, connected to a tower {d:.2f} km from the pin, {who or '-'}"
                f"{f' ({phones_on_graph[who]})' if who in phones_on_graph else ''} ({e['addr'] or e['site'] or '-'}).",
                [e], rests_on=["incident:place", "incident:date"])
    # R3 and R4: patterns and places over the whole file.
    if kind == "cdr":
        for f in cdr_team.patterns(events, out.get("subject"), inc) + cdr_team.location(events, inc):
            lines.append(f["text"])
            findings.append(f)
    out["lines"] = lines
    out["findings"] = findings
    if keep_events:
        out["events"] = events
    return out
