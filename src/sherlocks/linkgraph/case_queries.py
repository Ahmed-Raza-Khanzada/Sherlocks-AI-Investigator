"""The Facts agent: exact answers to fact questions, computed from the graph and the case
board - never written by a model.

Every query returns ``{type, subject, count, items, empty}``; each item is one line with
its source, so the Conversation agent can phrase it and the Validator can check every
number against it.
"""

from __future__ import annotations

import re
from typing import Any

from sherlocks.linkgraph.dossier import criminal_flags
from sherlocks.linkgraph.network import PersonNetwork
from sherlocks.linkgraph.offences import classify, severity
from sherlocks.linkgraph.systems import system_label


def _item(text: str, source: str | None = None, pid: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"text": text, "source": source or "", "pid": pid, **extra}


def fir_key(label: str) -> str:
    """"1124/2025" and "1124/25" are the same FIR."""
    no, _, year = str(label or "").replace(" ", "").partition("/")
    return f"{no.lstrip('0')}/{year[-2:]}"


def _result(kind: str, subject: str | None, items: list[dict[str, Any]], *, count: int | None = None,
            extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": kind, "subject": subject, "count": len(items) if count is None else count, "items": items,
            "empty": not items, **(extra or {})}


def _parsed(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in "[{":
        import ast

        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return value
    return value


def fir_document(case: Any, label: str) -> dict[str, Any] | None:
    """The FIR file read for this FIR, if any."""
    if case is None:
        return None
    key = fir_key(label)
    for doc in case.documents.values():
        ref = doc.get("ref") or {}
        if doc.get("kind") == "fir" and fir_key(f"{ref.get('fir_no')}/{ref.get('fir_year')}") == key:
            return doc
    return None


def _cnic_in(rows: Any, cnic: str) -> bool:
    rows = _parsed(rows)
    rows = rows if isinstance(rows, list) else [rows] if isinstance(rows, dict) else []
    return any(re.sub(r"\D", "", str((r or {}).get("cnic") or "")) == cnic for r in rows if isinstance(r, dict))


def role_kind(roles: list[str], doc: dict[str, Any] | None = None, cnic: str | None = None) -> str:
    """accused | complainant | victim | witness | investigator | named - the FIR file decides
    when it names the person; else the systems' role words."""
    data = (doc or {}).get("data") or {}
    if cnic and data:
        if _cnic_in(data.get("complainant"), cnic):
            return "complainant"
        if _cnic_in(data.get("nominated_suspects"), cnic):
            return "accused"
    text = " ".join(roles).lower()
    if re.search(r"accus|suspect|nominat|mulzim|ملزم|نامزد", text):
        return "accused"
    if re.search(r"complain|muddai|مدعی|applicant", text):
        return "complainant"
    if re.search(r"affp|affected|victim|injured|متاثر", text):
        return "victim"
    if cnic and data and _cnic_in(data.get("witnesses"), cnic):
        return "witness"
    if re.search(r"witness|gawah|گواہ", text):
        return "witness"
    if re.search(r"investigat|\bio\b|registered", text):
        return "investigator"
    return "named"


def _firs(net: PersonNetwork, pid: str, case: Any = None) -> list[dict[str, Any]]:
    """The person's FIRs, one per FIR (roles from several systems merged)."""
    out: dict[str, dict[str, Any]] = {}
    for f in net.data(pid).get("firs") or []:
        label = str(f.get("label") or "").strip()
        if not label:
            continue
        key = fir_key(label)
        row = out.setdefault(key, {"label": label, "ps": f.get("ps") or "", "roles": [], "offence": f.get("offence"),
                                   "sources": []})
        if len(label) < len(row["label"]):
            row["label"] = label          # "1124/25" rather than "1124/2025"
        if f.get("date") and not row.get("date"):
            row["date"] = str(f["date"])
        role = f.get("role") or "named"
        if role not in row["roles"]:
            row["roles"].append(role)
        src = system_label(f.get("system") or "")
        if src and src not in row["sources"]:
            row["sources"].append(src)
        row["offence"] = row["offence"] or f.get("offence")
        if not row["ps"] and f.get("ps"):
            row["ps"] = f["ps"]
    cnic = re.sub(r"\D", "", str(net.data(pid).get("cnic") or ""))
    for row in out.values():
        doc = fir_document(case, row["label"])
        sections = ((doc or {}).get("data") or {}).get("sections")
        row["crimes"] = classify(f"{row['offence'] or ''} {sections or ''}")
        row["severity"] = severity(row["crimes"])
        row["doc"] = doc["id"] if doc else None
        data = (doc or {}).get("data") or {}
        row["occurred"] = str(data.get("occurred") or row.get("date") or "").strip()
        row["role_kind"] = role_kind(row["roles"], doc, cnic)
    return list(out.values())


def cases(net: PersonNetwork, pid: str, case: Any = None) -> dict[str, Any]:
    rows = _firs(net, pid, case)
    rows = sorted(rows, key=lambda r: -r["severity"])
    for r in rows:
        # Who else is accused in it: the co-accused, when he is accused too.
        r["co_accused"] = [n for n in accused_of(net, r["label"], case, exclude=pid)
                           if n != net.name(pid)] if r["role_kind"] == "accused" else []
    _relate_to_new_case(rows, net, pid, case)
    items = [_item(f"FIR {r['label']} at {r['ps'] or '-'}: {', '.join(r['roles'])}"
                   + (f" - {r['offence']}" if r["offence"] else ""), ", ".join(r["sources"]),
                   crimes=r["crimes"], fir=r["label"], role=r["role_kind"], ps=r["ps"], occurred=r["occurred"],
                   co_accused=r["co_accused"], relevance=r.get("relevance")) for r in rows]
    accused = sum(1 for r in rows if any(x in " ".join(r["roles"]).lower() for x in ("accused", "suspect")))
    return _result("cases", net.name(pid), items, extra={"accused_in": accused, "rows": rows})


def serious_cases(net: PersonNetwork, pid: str, crimes: set[str] | None = None, case: Any = None) -> dict[str, Any]:
    """The person's FIRs for the crimes asked about ("qatl", "gaari chori"), or - for
    "koi khatarnak FIR?" - the heinous and serious ones, most serious first."""
    # Accused first: "khatarnak FIR" is about what he is charged with, not where he complained.
    rows = sorted(_firs(net, pid, case), key=lambda r: (r["role_kind"] != "accused", -r["severity"]))
    if crimes:
        hits = [r for r in rows if set(r["crimes"]) & crimes]
    else:
        hits = [r for r in rows if r["severity"] >= 3] or [r for r in rows if r["severity"] >= 2]
    hits = sorted(hits, key=lambda r: (r["role_kind"] != "accused", -r["severity"]))
    rest = [r for r in rows if r not in hits]
    items = [_item(f"FIR {r['label']} at {r['ps'] or '-'}: {', '.join(r['roles'])}"
                   + (f" - {r['offence']}" if r["offence"] else ""), ", ".join(r["sources"]),
                   crimes=r["crimes"], fir=r["label"], role=r["role_kind"], ps=r["ps"], occurred=r["occurred"]) for r in hits]
    return _result("serious_cases", net.name(pid), items,
                   extra={"rows": hits, "rest": rest, "total": len(rows), "asked": sorted(crimes or [])})


_OUTCOMES = [
    ("convicted", re.compile(r"convict|sentenc|punish|saza|سزا", re.IGNORECASE)),
    ("acquitted", re.compile(r"acquit|\bbari\b|بری|discharg|released|honou?rably", re.IGNORECASE)),
    ("disposed / closed", re.compile(r"dispos|closed|cancel|خارج|اخراج|ڈسپوزل|داخل دفتر|[ABC] class|\([ABC]\)\s*کلاس|"
                                     r"عدم پتہ|untrac", re.IGNORECASE)),
    ("under trial", re.compile(r"trial|challan|چالان|court|عدالت|pending|sub ?judice|زیر سماعت", re.IGNORECASE)),
    ("under investigation", re.compile(r"investigat|زیر تفتیش|under inquiry|tafteesh", re.IGNORECASE)),
    ("on bail", re.compile(r"\bbail\b|zamanat|ضمانت", re.IGNORECASE)),
    ("proclaimed offender", re.compile(r"proclaim|absconder|مفرور|اشتہاری|\bPO\b", re.IGNORECASE)),
]


def outcome(status: str) -> str:
    for label, rx in _OUTCOMES:
        if rx.search(status or ""):
            return label
    return "status not stated"


def _fir_statuses(net: PersonNetwork, pid: str, case: Any) -> dict[str, list[tuple[str, str]]]:
    """FIR label -> [(status text, source)] from every place a status is recorded: the FIR
    entries, the CRO / PSRMS record fields ("FIR 45/2023": "PS | offence | status"), and
    the FIR file's case positions and investigation result."""
    out: dict[str, list[tuple[str, str]]] = {}

    def norm(label: str) -> str:
        no, _, year = label.replace(" ", "").partition("/")
        return f"{no.lstrip('0')}/{year[-2:]}"

    def add(label: str, status: str, source: str) -> None:
        status = str(status or "").strip(" |-")
        if status and status.lower() not in ("none", "n/a", "null", "-"):
            rows = out.setdefault(norm(label), [])
            if (status, source) not in rows:
                rows.append((status, source))

    for f in net.data(pid).get("firs") or []:
        if f.get("status"):
            add(str(f.get("label")), f["status"], system_label(f.get("system") or ""))
    for node in net.graph.get("nodes") or []:
        d = node.get("data") or {}
        if node.get("kind") != "system" or d.get("owner") != pid:
            continue
        for field in d.get("fields") or []:
            label = str(field.get("label") or "")
            if label.startswith("FIR ") and "/" in label:
                parts = [x.strip() for x in str(field.get("value") or "").split("|")]
                # The last part is the status - unless the record had none and it is the offence.
                if len(parts) >= 3 and not (outcome(parts[-1]) == "status not stated" and re.search(r"\d", parts[-1])):
                    add(label[4:], parts[-1], system_label(d.get("system") or ""))
    if case is not None:
        for doc in case.documents.values():
            if doc["kind"] != "fir":
                continue
            ref, data = doc.get("ref") or {}, doc.get("data") or {}
            label = f"{ref.get('fir_no')}/{ref.get('fir_year')}"
            positions = data.get("case_positions") or []
            if positions:
                last = positions[-1]
                add(label, f"{last.get('position')} ({last.get('date') or '-'})", f"FIR file {doc['id']}")
            if data.get("investigation_result"):
                add(label, "result: " + str(data["investigation_result"])[:140], f"FIR file {doc['id']}")
    return out


def fir_status(net: PersonNetwork, pid: str, case: Any = None) -> dict[str, Any]:
    """Each FIR of the person with every status recorded for it, and the outcome
    (convicted / acquitted / under trial / ...)."""
    rows = _firs(net, pid, case)
    statuses = _fir_statuses(net, pid, case)
    items, groups = [], {}
    for r in rows:
        no, _, year = r["label"].replace(" ", "").partition("/")
        found = statuses.get(f"{no.lstrip('0')}/{year[-2:]}", [])
        result = next((outcome(s) for s, _src in found if outcome(s) != "status not stated"), "status not stated")
        groups.setdefault(result, []).append(r["label"])
        # The same status from several systems is said once.
        merged: dict[str, str] = {}
        for status, _src in found:
            merged.setdefault(status.lower().removeprefix("result: "), status)
        # The latest recorded step ("challan 512 CrPC (08-09-2026)"), not the whole report text.
        steps = [v for k, v in merged.items() if k != result and not v.lower().startswith("result:")]
        if not steps and result == "status not stated":
            steps = [v[:120] for v in merged.values()]
        text = (f"FIR {r['label']} at {r['ps'] or '-'}" + (f" ({r['offence']})" if r["offence"] else "")
                + f": {result.upper()}" + (f" - {steps[-1][:90]}" if steps else ""))
        items.append(_item(text, ", ".join(dict.fromkeys(src for _, src in found)) or ", ".join(r["sources"]),
                           crimes=r["crimes"], fir=r["label"], outcome=result, role=r["role_kind"], ps=r["ps"], occurred=r["occurred"]))
    if len(rows) > 1:
        order = ["convicted", "acquitted", "under trial", "on bail", "under investigation", "proclaimed offender",
                 "disposed / closed", "status not stated"]
        # Convicted and acquitted are always counted - "none" is an answer to "kitni mein convicted?".
        summary = " · ".join(f"{k}: {len(groups.get(k, []))}" for k in order
                             if k in groups or k in ("convicted", "acquitted"))
        items.insert(0, _item(f"Overall - {summary}", overall=True))
    return _result("fir_status", net.name(pid), items, count=len(rows),
                   extra={"outcomes": {k: len(v) for k, v in groups.items()}, "by_outcome": groups, "rows": rows})


def _names(rows: Any, limit: int = 4) -> list[str]:
    rows = _parsed(rows)
    rows = rows if isinstance(rows, list) else [rows] if isinstance(rows, dict) else []
    return [str(r.get("name")).strip() for r in rows if isinstance(r, dict) and str(r.get("name") or "").strip()
            not in ("", "0")][:limit]


def fir_details(net: PersonNetwork, pid: str, case: Any = None, only: list[str] | None = None) -> dict[str, Any]:
    """Each FIR as a short story: the allegation in words, the person's role, who filed it,
    when and where, what the complainant says happened, the accused and the latest step -
    from the FIR file when it has been read, else from the records."""
    keys = {fir_key(x) for x in only or []}
    rows = [r for r in _firs(net, pid, case) if not keys or fir_key(r["label"]) in keys]
    rows.sort(key=lambda r: (r["role_kind"] != "accused", -r["severity"]))
    _relate_to_new_case(rows, net, pid, case)
    statuses = _fir_statuses(net, pid, case)
    items = []
    for r in rows:
        doc = case.documents.get(r["doc"]) if case is not None and r.get("doc") else None
        data = (doc or {}).get("data") or {}
        complainant = _parsed(data.get("complainant"))
        found = statuses.get(fir_key(r["label"]), [])
        detail = {
            "fir": r["label"], "ps": r["ps"] or data.get("police_station") or "", "role": r["role_kind"],
            "crimes": r["crimes"], "sections": data.get("sections") or r["offence"] or "",
            "occurred": data.get("occurred") or "", "place": str(data.get("place") or "")[:120],
            "complainant": (complainant.get("name") or str(complainant.get("raw") or "").split("،")[0]).strip()
            if isinstance(complainant, dict) else str(complainant or "")[:60],
            "accused": _names(data.get("nominated_suspects")),
            "story": re.sub(r"\s+", " ", str(data.get("narrative") or ""))[:320],
            "outcome": next((outcome(st) for st, _ in found if outcome(st) != "status not stated"), "status not stated"),
        }
        text = (f"FIR {r['label']} at {detail['ps'] or '-'}"
                + (f" ({detail['sections']})" if detail["sections"] else "")
                + (f"; occurred {detail['occurred']}" if detail["occurred"] else "")
                + (f"; complainant {detail['complainant']}" if detail["complainant"] else "")
                + (f"; accused: {', '.join(detail['accused'])}" if detail["accused"] else "")
                + (f" - \"{detail['story']}…\"" if detail["story"] else ""))
        items.append(_item(text, ", ".join(filter(None, [*r["sources"], f"FIR file {doc['id']}" if doc else ""])),
                           crimes=r["crimes"], fir=r["label"], role=r["role_kind"], detail=detail,
                           relevance=r.get("relevance")))
    return _result("fir_details", net.name(pid), items, extra={"rows": rows})


def _criminal_line(net: PersonNetwork, pid: str) -> str:
    firs = _firs(net, pid)
    flags = [f.replace("_", " ") for f in criminal_flags(net.data(pid))]
    return (f"{net.name(pid)} - " + ", ".join(flags)
            + (f"; FIRs: {', '.join(r['label'] for r in firs[:5])}" if firs else ""))


def criminals_near(net: PersonNetwork, pid: str, hops: int = 1) -> dict[str, Any]:
    """People with a criminal record among the person's connections. Stated links count;
    inferred ones are listed apart."""
    if pid not in net.G:
        return _result("criminals_near", net.name(pid), [])
    stated, inferred = [], []
    frontier, seen = {pid}, {pid}
    for depth in range(1, hops + 1):
        nxt = set()
        for node in frontier:
            for other in net.G.neighbors(node):
                if other in seen:
                    continue
                seen.add(other)
                nxt.add(other)
                if not net.criminal(other):
                    continue
                link = net.best_link(node, other)
                how = (link.sentence or link.label or "linked")
                row = _item(_criminal_line(net, other) + f" (link: {how}" + (f", {depth} steps away" if depth > 1 else "")
                            + ")", system_label(link.system or ""), other)
                (stated if net.G[node][other]["stated"] else inferred).append(row)
        frontier = nxt
    return _result("criminals_near", net.name(pid), stated, extra={"inferred": inferred, "hops": hops})


def criminals_all(net: PersonNetwork) -> dict[str, Any]:
    rows = [p for p in net.people if net.criminal(p)]
    rows.sort(key=lambda p: -len(_firs(net, p)))
    return _result("criminals_all", None, [_item(_criminal_line(net, p), None, p) for p in rows],
                   extra={"people": len(net.people)})


def profile(net: PersonNetwork, pid: str, case: Any = None) -> dict[str, Any]:
    d = net.data(pid)
    items = []
    if d.get("cnic"):
        items.append(_item(f"CNIC {d['cnic']}"))
    if d.get("father_name"):
        items.append(_item(f"Father: {d['father_name']}"))
    if d.get("phones"):
        items.append(_item(f"Phones: {', '.join(d['phones'][:4])}"))
    if case is not None and case.roles.get(pid):
        items.append(_item(f"Role you gave: {case.roles[pid]}", "officer"))
    rows = _firs(net, pid)
    items.append(_item(f"FIRs: {len(rows)}" + (f" ({', '.join(r['label'] for r in rows[:6])})" if rows else "")))
    flags = criminal_flags(d)
    if flags:
        items.append(_item("Criminal record: " + ", ".join(f.replace("_", " ") for f in flags)))
    if d.get("stays"):
        items.append(_item(f"Hotel stays: {len(d['stays'])}", "Hotel Eye"))
    links = net.neighbours(pid) if pid in net.G else []
    if links:
        items.append(_item(f"Direct links: {len(links)} ({sum(1 for x in links if x['stated'])} stated)"))
    records = sorted({system_label(r.get("system") or "") for r in d.get("records") or []})
    if records:
        items.append(_item("Found in: " + ", ".join(records)))
    if case is not None:
        docs = case.for_person(pid)["documents"]
        if docs:
            items.append(_item(f"Case documents: {len(docs)} ({', '.join(x['id'] for x in docs[:6])})"))
    return _result("profile", net.name(pid), items, count=len(rows))


def associates(net: PersonNetwork, pid: str) -> dict[str, Any]:
    links = net.neighbours(pid) if pid in net.G else []
    items = [_item(f"{x['name']}: {x['relation']}{'' if x['stated'] else ' (inferred)'}", x["via"], x["id"])
             for x in links]
    return _result("associates", net.name(pid), items, extra={"stated": sum(1 for x in links if x["stated"])})


_ALONGSIDE = re.compile(r"^(?P<x>.+?) is accused in FIR (?P<fir>\S+) alongside (?P<y>.+)$")


def _hop_text(net: PersonNetwork, hop: dict[str, Any], case: Any) -> str:
    """A hop in plain words, without the charge list; "X is accused in FIR 345/26 alongside Y"
    is corrected when the FIR file shows Y filed it (older graphs read it that way)."""
    text = re.sub(r"\s*\(charges:.*?\)(?=\s*(?:—|$))", "", str(hop.get("relation") or "")).strip()
    m = _ALONGSIDE.match(text)
    if m and case is not None:
        for pid, other in ((hop.get("from"), m.group("y")), (hop.get("to"), m.group("y"))):
            if pid in net.people and net.name(pid) == other.strip():
                row = next((r for r in _firs(net, pid, case) if fir_key(r["label"]) == fir_key(m.group("fir"))), None)
                if row and row["role_kind"] in ("complainant", "victim"):
                    who = "the complainant" if row["role_kind"] == "complainant" else "the affected party"
                    return f"{m.group('x')} is accused in FIR {m.group('fir')}, in which {other.strip()} is {who}"
    return text


def connection(net: PersonNetwork, a: str, b: str, case: Any = None) -> dict[str, Any]:
    routes = net.paths(a, b, k=3)
    told = [{"inferred": r["inferred"], "hops": [_hop_text(net, h, case) for h in r["hops"]],
             "source": ", ".join(dict.fromkeys(h["via"] for h in r["hops"] if h["via"]))} for r in routes]
    items = [_item(f"{'Inferred' if r['inferred'] else 'Stated'} route, {len(r['hops'])} step(s): "
                   + " → ".join(r["hops"]), r["source"]) for r in told]
    return _result("connection", f"{net.name(a)} and {net.name(b)}", items,
                   extra={"routes": told, "a": net.name(a), "b": net.name(b)})


def _day(value: Any) -> str | None:
    from sherlocks.linkgraph.sherlock_team import _iso_date

    return _iso_date(str(value or "")) if value else None


def stays_at(stays: list[dict[str, Any]], day: str) -> list[dict[str, Any]]:
    """The stays that cover ``day`` (check-in to check-out; a stay with no check-out, the
    check-in day and the day after)."""
    from datetime import date, timedelta

    out = []
    for s in stays:
        start = _day(s.get("check_in"))
        if not start:
            continue
        end = _day(s.get("check_out")) or (date.fromisoformat(start) + timedelta(days=1)).isoformat()
        if start <= day <= end:
            out.append(s)
    return out


def hotels(net: PersonNetwork, pid: str, case: Any = None) -> dict[str, Any]:
    """The person's hotel stays - and, for each moment that matters (the date of each of
    his FIRs, the new case's incident), whether a stay covers it."""
    stays = net.data(pid).get("stays") or []
    items = [_item(f"{s.get('hotel')} ({s.get('district') or '-'}), room {s.get('room') or '-'}, "
                   f"{s.get('check_in') or '-'} → {s.get('check_out') or '-'}", "Hotel Eye")
             for s in stays]
    moments = [{"what": f"FIR {r['label']}", "day": _day(r.get("occurred")), "doc": r.get("doc"),
                "sources": r.get("sources") or []} for r in _firs(net, pid, case)]
    inc = (case.incident or {}) if case is not None else {}
    if inc.get("date"):
        moments.append({"what": "the new case", "day": _day(inc["date"]), "doc": None, "sources": ["officer"]})
    at_times = [{**m, "stays": [f"{s.get('hotel')} ({s.get('district') or '-'}), {s.get('check_in') or '-'} → "
                                f"{s.get('check_out') or '-'}" for s in stays_at(stays, m["day"])] if m["day"] else []}
                for m in moments]
    return _result("hotels", net.name(pid), items, extra={"at_times": at_times, "stays_known": bool(stays)})


def phones(net: PersonNetwork, pid: str) -> dict[str, Any]:
    return _result("phones", net.name(pid), [_item(p) for p in net.data(pid).get("phones") or []])


def vehicles(net: PersonNetwork, pid: str) -> dict[str, Any]:
    return _result("vehicles", net.name(pid), [_item(v) for v in net.data(pid).get("vehicles") or []])


def documents(net: PersonNetwork, pid: str, case: Any) -> dict[str, Any]:
    if case is None:
        return _result("documents", net.name(pid), [])
    mine = case.for_person(pid)
    items = [_item(f"[{d['id']}] {d['title']}: {(d.get('summary') or '')[:160]}") for d in mine["documents"]]
    items += [_item(f"[{f['id']}] {f['statement']}") for f in mine["facts"][:8]]
    return _result("documents", net.name(pid), items, count=len(mine["documents"]))


def graph_stats(net: PersonNetwork, case: Any = None) -> dict[str, Any]:
    criminals = [p for p in net.people if net.criminal(p)]
    firs = {f.get("label") for p in net.people for f in net.data(p).get("firs") or [] if f.get("label")}
    items = [_item(f"People on the graph: {len(net.people)}"), _item(f"With a criminal record: {len(criminals)}"),
             _item(f"FIRs: {len(firs)}"), _item(f"Links: {net.G.number_of_edges()}")]
    if case is not None:
        items.append(_item(f"Case documents read: {len(case.documents)}"))
    return _result("graph_stats", None, items, count=len(net.people))


def summary(net: PersonNetwork, case: Any = None, findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Where the case stands, in full: who the targets are and their FIRs (crimes in
    words, their role in each), what connects them (or their main associates), what each
    document read establishes, Sherlock's assessment, the strongest patterns, the incident."""
    from sherlocks.linkgraph.offences import labels

    items = []
    seeds = net.seeds()
    # 1. The targets and their FIRs.
    for pid in seeds:
        rows = sorted(_firs(net, pid, case), key=lambda r: -r["severity"])
        role = case.roles.get(pid) if case is not None else None
        firs = "; ".join(f"FIR {r['label']}" + (f" at {r['ps']}" if r["ps"] else "")
                         + f" ({labels(r['crimes']) if r['crimes'] else (r['offence'] or 'offence not recorded')}"
                         f", {r['role_kind']})" for r in rows[:6])
        items.append(_item(f"Target {net.name(pid)}" + (f" ({role})" if role else "")
                           + f": named in {len(rows)} FIR(s)" + (f" - {firs}" if firs else "")
                           + (". Has a criminal record." if net.criminal(pid) else "."),
                           ", ".join(dict.fromkeys(src for r in rows for src in r["sources"])) or None, pid))
    # 2. What connects them: routes between targets, else each target's main associates.
    for i, a in enumerate(seeds):
        for b in seeds[i + 1:]:
            for r in net.paths(a, b, k=1):
                items.append(_item(f"{net.name(a)} ↔ {net.name(b)}: " + " → ".join(h["relation"] for h in r["hops"]),
                                   ", ".join(dict.fromkeys(h["via"] for h in r["hops"] if h["via"]))))
    if len(seeds) == 1 and seeds[0] in net.G:
        close = [c for c in net.neighbours(seeds[0]) if c["stated"]][:5]
        if close:
            items.append(_item(f"{net.name(seeds[0])} is linked to: " + "; ".join(
                f"{c['name']} ({c['relation']}" + (", criminal record" if c["criminal"] else "") + ")" for c in close),
                ", ".join(dict.fromkeys(c["via"] for c in close if c["via"]))))
    # 3. What the documents establish.
    if case is not None:
        live = {f["id"]: f for f in case.live_facts()} if hasattr(case, "live_facts") else dict(case.facts)
        for doc in case.documents.values():
            if doc["kind"] in ("notes", "graph"):
                continue
            facts = [live[f] for f in doc.get("facts") or [] if f in live]
            facts.sort(key=lambda f: ({"ai": 0, "rule": 1, "cdr": 2}.get(f.get("by"), 3), f.get("trust") or 4))
            what = (doc.get("ai_summary") or doc.get("summary") or "").strip()
            points = "; ".join(f"{f['statement'][:180]} [{f['id']}]" for f in facts[:3])
            if what or points:
                items.append(_item(f"{doc['title']}: " + (what[:240] + (". " if points else "") if what else "")
                                   + points, doc["id"]))
    # 4. Sherlock's assessment.
    for h in sorted((h for h in getattr(case, "hypotheses", {}).values() if not h.get("stale")),
                    key=lambda h: {"supported": 0, "open": 1, "weakened": 2, "ruled out": 3}.get(h["status"], 4))[:3]:
        items.append(_item(f"Sherlock's assessment: {h['statement']} ({h['status']}, {h['confidence']} confidence)",
                           h["id"]))
    # 5. The strongest patterns the rules found.
    for f in [f for f in findings or [] if f["tier"] in ("stated", "corroborated")][:3]:
        items.append(_item(f"{f['title']}: {f['summary']}"))
    inc = (case.incident or {}) if case is not None else {}
    if inc:
        items.append(_item("Incident: " + ", ".join(f"{k} {v}" for k, v in inc.items()
                                                    if k in ("offence", "place", "date", "time", "fir") and v), "officer"))
    criminals = sum(1 for p in net.people if net.criminal(p))
    docs = len([d for d in case.documents.values() if d["kind"] not in ("notes", "graph")]) if case is not None else 0
    items.append(_item(f"{len(net.people)} people on the graph, {criminals} with a criminal record, {docs} case document(s) read"))
    return _result("summary", None, items)


# What the investigating officer concluded, from the words of the report (168 / 173 CrPC
# report, final report, case diaries). Order matters: the first that fits decides.
IO_VERDICTS: list[tuple[str, re.Pattern[str]]] = [(k, re.compile(p, re.IGNORECASE)) for k, p in [
    ("b_class", r"\(?\bB\)?\s*(?:class|کلاس)|بی کلاس|جھوٹا|جھوٹی|false case|malicious"),
    ("c_class", r"\(?\bC\)?\s*(?:class|کلاس)|سی کلاس|دیوانی|civil (?:nature|dispute)|ناقابل دست اندازی"),
    ("a_class", r"\(?\bA\)?\s*(?:class|کلاس)|اے کلاس|عدم پتہ|untraced"),
    ("challan_absconding", r"انٹریم چالان|interim challan|چالان\s*512|512\s*(?:ض\s*ف|cr\.?p\.?c)"),
    ("released_169", r"\b169\b|۱۶۹"),
    ("innocent", r"بے ?گناہ|بےقصور|بے قصور|ملوث نہیں|innocent|not involved"),
    ("challan", r"قابل چالان|چالان قطع|چالان پیش|چالان ارسال|مکمل چالان|قابل مواخذہ|\bchallan\b|\b173\b|charge ?sheet|"
                r"ملوث پایا|قصوروار"),
    ("disposed", r"ڈسپوزل|اخراج|dispos|cancel"),
    ("investigating", r"زیر تفتیش|under investigation"),
    ("challan", r"چالان"),
]]
_CONCLUDES = re.compile(r"استدعا|لہذا|لہٰذا|حاصلہ تفتیش|روشنی میں|request|therefore|concluded", re.IGNORECASE)


def _sentences(text: str) -> list[str]:
    return [x.strip(" -:،") for x in re.split(r"[۔\n]|(?<=\S)\s{2,}|(?<=[.!?])\s", str(text or "")) if len(x.strip()) > 15]


def io_verdict(doc: dict[str, Any] | None) -> dict[str, Any]:
    """``{verdict, quote, io, source}`` - the IO's conclusion in an FIR file, quoted."""
    data = (doc or {}).get("data") or {}
    result = _sentences(data.get("investigation_result"))
    diaries = _parsed(data.get("case_diaries")) or []
    diary_text = [str(d.get("remarks") or "") for d in diaries[-2:] if isinstance(d, dict)]
    positions = _parsed(data.get("case_positions")) or []
    last_position = str(positions[-1].get("position") or "") if isinstance(positions, list) and positions and \
        isinstance(positions[-1], dict) else ""
    # The concluding sentence first ("... چالان قطع کرنے کی استدعا ..."), then the rest of the report, the
    # latest diaries and the case position.
    concluding = [x for x in result if _CONCLUDES.search(x)][-2:]
    pools = [concluding, result[-3:], [y for t in diary_text for y in _sentences(t)][-4:], [last_position]]
    for pool in pools:
        for sentence in reversed(pool):
            for key, rx in IO_VERDICTS:
                m = rx.search(sentence)
                if m:
                    start = max(0, m.start() - 140)
                    if start:                                   # begin on a whole word
                        start = sentence.find(" ", start) + 1 or start
                    end = sentence.rfind(" ", 0, m.end() + 80) if len(sentence) > m.end() + 80 else len(sentence)
                    quote = ("…" if start else "") + sentence[start:max(end, m.end())].strip() + (
                        "…" if end < len(sentence) else "")
                    return {"verdict": key, "quote": quote, "io": _io_name(data), "source": doc.get("id") if doc else None}
    return {"verdict": "unknown", "quote": (concluding or result[-1:] or [""])[-1][:220], "io": _io_name(data),
            "source": doc.get("id") if doc else None}


def _io_name(data: dict[str, Any]) -> str:
    ios = _parsed(data.get("investigating_officers")) or []
    if isinstance(ios, list) and ios and isinstance(ios[-1], dict):
        last = ios[-1]
        return " ".join(x for x in (str(last.get("rank") or "").title(), str(last.get("name") or "")) if x).strip()
    return ""


def io_report(net: PersonNetwork, pid: str | None, case: Any = None, only: list[str] | None = None) -> dict[str, Any]:
    """What the investigating officer concluded in each of the person's FIRs (or the FIRs
    asked about): sent up for trial, cancelled (A / B / C class), released under 169, still
    under investigation - with the IO's name and the report's own words."""
    keys = {fir_key(x) for x in only or []}
    if pid:
        rows = [r for r in _firs(net, pid, case) if not keys or fir_key(r["label"]) in keys]
    else:
        rows = [{"label": lab, "ps": "", "crimes": [], "role_kind": None, "doc": (fir_document(case, lab) or {}).get("id"),
                 "sources": []} for lab in only or []]
    rows.sort(key=lambda r: (r.get("role_kind") != "accused", -(r.get("severity") or 0)))
    items, unread = [], []
    for r in rows:
        doc = case.documents.get(r["doc"]) if case is not None and r.get("doc") else None
        if doc is None:
            unread.append(r["label"])
            continue
        v = io_verdict(doc)
        data = doc.get("data") or {}
        detail = {"fir": r["label"], "ps": r.get("ps") or data.get("police_station") or "", "role": r.get("role_kind"),
                  "crimes": r.get("crimes") or classify(str(data.get("sections") or "")), **v}
        items.append(_item(f"FIR {r['label']}: {v['verdict']} - {v['quote']}", f"FIR file {doc['id']}",
                           fir=r["label"], crimes=detail["crimes"], role=r.get("role_kind"), detail=detail))
    return _result("io_report", net.name(pid) if pid else None, items,
                   extra={"unread": unread, "verdicts": [i["detail"]["verdict"] for i in items]})


def fir_file_details(net: PersonNetwork, case: Any, labels: list[str]) -> dict[str, Any]:
    """"FIR 345/26 ki tafseel" with nobody in focus: straight from the FIR file."""
    items = []
    for lab in labels:
        doc = fir_document(case, lab)
        if not doc:
            continue
        data = doc.get("data") or {}
        complainant = _parsed(data.get("complainant"))
        positions = _parsed(data.get("case_positions")) or []
        last = positions[-1].get("position") if isinstance(positions, list) and positions and isinstance(positions[-1], dict) else ""
        crimes = classify(str(data.get("sections") or data.get("offence") or ""))
        detail = {"fir": lab, "ps": data.get("police_station") or "", "role": None, "crimes": crimes,
                  "occurred": data.get("occurred") or "",
                  "complainant": (complainant.get("name") or str(complainant.get("raw") or "").split("،")[0]).strip()
                  if isinstance(complainant, dict) else "",
                  "accused": _names(data.get("nominated_suspects")),
                  "story": re.sub(r"\s+", " ", str(data.get("narrative") or ""))[:320],
                  "outcome": outcome(str(last or "")) if last else ""}
        items.append(_item(f"FIR {lab}", f"FIR file {doc['id']}", crimes=crimes, fir=lab, detail=detail))
    return _result("fir_details", None, items)


def _relate_to_new_case(rows: list[dict[str, Any]], net: PersonNetwork, pid: str, case: Any) -> None:
    """Each previous FIR row gets ``relevance``: how it bears on the new case (when the
    officer has described the new case)."""
    if case is None:
        return
    from sherlocks.linkgraph.relevance import fir_relevance, new_case

    new = new_case(case)
    if not new["known"]:
        return
    targets = list(net.seeds())
    linked = {net.name(p) for t in targets if t in net.G for p in [t, *[c["id"] for c in net.neighbours(t)]]}
    me = net.name(pid).lower()
    for r in rows:
        others = [n for n in accused_of(net, r["label"], case, exclude=pid) if n.lower() != me]   # not himself
        r["relevance"] = fir_relevance(r, new, linked=linked, people_in=others)


def fact_answer(net: PersonNetwork, case: Any, kb: Any, asks_for: str, people: list[str] | None = None,
                firs: list[str] | None = None) -> dict[str, Any]:
    """One specific fact asked ("investigating officer", "date of occurrence", "witnesses",
    "weapon used"...): searched in the case's own FIR files and their facts - the FIRs of
    the people asked about, the FIRs named, the incident's FIR, else every FIR file read -
    so the answer is that fact, not the whole case."""
    from sherlocks.linkgraph.knowledge import _expand, tokens

    docs: set[str] = set()
    for pid in people or []:
        docs |= {r["doc"] for r in _firs(net, pid, case) if r.get("doc")}
    for label in [*(firs or []), *([str((case.incident or {}).get("fir"))] if (case.incident or {}).get("fir") else [])]:
        doc = fir_document(case, label)
        if doc:
            docs.add(doc["id"])
    if not docs:
        docs = {d["id"] for d in case.documents.values() if d["kind"] in ("fir", "lab", "cro")}
    doc_of = {fid: f["doc"] for fid, f in case.facts.items()}
    want = {t for t in _expand(tokens(asks_for)) if not t.startswith("s:")}
    stems = {w[:5] for w in tokens(asks_for) if len(w) > 2 and w not in ("the", "who", "what", "this", "case")}

    def closeness(text: str) -> int:
        # The entry's own label ("investigating officer: ...", "occurred: ...") counts double.
        label, _, body = text.partition(":")
        in_label = sum(1 for st in stems if any(w.startswith(st) for w in tokens(label)))
        in_body = sum(1 for st in stems if any(w.startswith(st) for w in tokens(body)))
        return 2 * in_label + in_body

    scored, seen = [], set()
    for rank, e in enumerate(kb.search(asks_for, k=80)):
        doc = e.source if e.source in docs else doc_of.get(e.source)
        if doc not in docs or not (want & {t for t in _expand(tokens(e.text)) if not t.startswith("s:")}):
            continue
        key = e.text.lower()[:160]
        if key in seen:
            continue
        seen.add(key)
        scored.append((closeness(e.text), rank, e, doc))
    best = max((x[0] for x in scored), default=0)
    items = []
    for _score, _rank, e, doc in sorted((x for x in scored if x[0] >= best - 1), key=lambda x: (-x[0], x[1]))[:4]:
        title = case.documents[doc]["title"] if doc in case.documents else ""
        text = re.sub(r"\s*\(quote: [^)]*\)", "", e.text).strip()     # the evidence block shows the quote
        label, sep, value = text.partition(": ")
        closest = sep and any(label.lower().startswith(st) or st in label.lower() for st in stems)
        items.append(_item(text, e.source, doc=title, value=value if closest else text))
    subject = asks_for[:1].upper() + asks_for[1:]
    return _result("fact", subject, items, extra={"asks_for": asks_for, "docs": sorted(docs)})


def accused_of(net: PersonNetwork, label: str, case: Any = None, exclude: str | None = None) -> list[str]:
    """Who is accused in an FIR: from its file when read, else everyone the records name
    as accused in it."""
    doc = fir_document(case, label) if case is not None else None
    names = _names(((doc or {}).get("data") or {}).get("nominated_suspects"))
    if names:
        return names
    key = fir_key(label)
    out = []
    for pid in net.people:
        if pid == exclude:
            continue
        for f in net.data(pid).get("firs") or []:
            if fir_key(str(f.get("label") or "")) == key and role_kind([str(f.get("role") or "")]) == "accused":
                out.append(net.name(pid))
                break
    return out


def frame_by_role(result: dict[str, Any], net: PersonNetwork, pid: str, case: Any, role: str,
                  yes_no: bool) -> dict[str, Any]:
    """A question about FIRs in one role ("FIR on him" = accused, "did he file" =
    complainant...): which of the person's FIRs match it, and the others - each with its
    accused - so the answer says yes or no for that role and mentions the rest."""
    rows = result.get("rows") or _firs(net, pid, case)
    for r in rows:
        r.setdefault("accused", accused_of(net, r["label"], case, exclude=pid))
    matching = [r for r in rows if r["role_kind"] == role]
    others = [r for r in rows if r["role_kind"] != role]
    framed = {**result, "asked_role": role, "yes_no": yes_no, "matching": matching, "others": others, "rows": rows}
    if result.get("type") in ("cases", "count_cases"):
        framed["count"] = len(matching)          # the count asked for is the count in that role
    return framed


def run(query: dict[str, Any], net: PersonNetwork, case: Any = None) -> dict[str, Any] | None:
    """Answer a structured query from the Understanding agent; ``None`` for types the
    Facts agent does not answer (open questions go to the Investigator)."""
    kind, people = query.get("type"), query.get("people") or []
    first = people[0] if people else None
    if kind == "criminals_all":
        return criminals_all(net)
    if kind == "graph_stats":
        return graph_stats(net, case)
    if kind == "summary":
        from sherlocks.linkgraph.scenarios import find_scenarios

        return summary(net, case, find_scenarios(net.graph, net=net))
    if kind == "connection" and len(people) >= 2:
        return connection(net, people[0], people[1], case)
    if kind == "io_report" and case is not None and (first or query.get("firs")):
        return io_report(net, first, case, query.get("firs") or [])
    if kind == "fir_details" and query.get("firs") and case is not None:
        mine = fir_details(net, first, case, query["firs"]) if first else None
        return mine if mine and not mine["empty"] else fir_file_details(net, case, query["firs"])
    if first is None:
        return None
    handlers = {
        "cases": lambda: cases(net, first, case), "count_cases": lambda: cases(net, first, case),
        "fir_status": lambda: fir_status(net, first, case),
        "serious_cases": lambda: serious_cases(net, first, set(query.get("crimes") or []), case),
        "fir_details": lambda: fir_details(net, first, case, query.get("firs") or []),
        "criminals_near": lambda: criminals_near(net, first, int(query.get("hops") or 1)),
        "profile": lambda: profile(net, first, case), "associates": lambda: associates(net, first),
        "hotels": lambda: hotels(net, first, case), "phones": lambda: phones(net, first),
        "vehicles": lambda: vehicles(net, first), "documents": lambda: documents(net, first, case),
    }
    handler = handlers.get(kind)
    result = handler() if handler else None
    if result is not None and query.get("role") and kind in ("cases", "count_cases", "fir_status", "serious_cases",
                                                             "fir_details", "io_report"):
        result = frame_by_role(result, net, first, case, query["role"], bool(query.get("yes_no")))
    return result
