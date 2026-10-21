"""P8 Record writer: the target's record, for a report written before the case is described.

When the investigating officer has not yet said what case is being investigated, the report
cannot tell that case - but it can always set out what the records hold about the target,
in the order an officer would read a criminal record:

    past_cases   each earlier FIR naming the target: when it happened (date and time), the
                 police station, the offence, their part in it, where it stands (convicted,
                 acquitted, under trial ...), what the complainant said, and what the police
                 found against the target
    relations    every person the records tie to the target, and how: family, named in the
                 same FIR, the same phone, the same address (the same house or area - never
                 the same city alone), hotel stays together, tenancy / PRVS / TRUST / SBVS
                 records, vehicles, work
    hotels       the target's hotel stays, with check-in and check-out date and time
    reports      each earlier medical, forensic or laboratory report of those FIRs: the FIR
                 it belongs to, the report's heading, and what it says

Each earlier FIR file is read by the model (one call per FIR) into plain English: the
complainant's account, what was found against the target, and the outcome - taken only
from the case positions, the investigation result and the police systems' status. Without
the model the same rows are laid out from the records. A relation is how the records
connect two people, said in their words: never "inferred" or "corroborated", and never a
finding against anyone.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import Field, field_validator

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.team.runtime import PLAIN, Typed, ask, dumps, real_refs

logger = logging.getLogger(__name__)

OUTCOMES = ("convicted", "acquitted", "under trial", "challan submitted", "under investigation", "cancelled",
            "compromise", "not on record")
MOST_FIRS = 15            # earlier FIRs set out per target
MODEL_FIRS = 10           # of them read by the model
MOST_PEOPLE = 60          # people listed as related to a target
TEXT = 1500               # characters of an FIR's first information given to the writer
SAID = 500                # of one statement

# How a relation is grouped in the report, in the order it is read.
KINDS = {"family": "Family", "fir": "Named together in FIRs", "phone": "Phones", "address": "Same address or area",
         "hotel": "Hotel stays", "verification": "Tenancy, PRVS, TRUST and SBVS records", "vehicle": "Vehicles and licences",
         "work": "Work", "other": "Other links"}
_FAMILY = re.compile(r"\b(family|relative|sibling|household|husband|wife|father|mother|son|daughter|brother|sister|"
                     r"parent|child|uncle|cousin|nephew)\b", re.IGNORECASE)
_HOTEL = re.compile(r"hotel|co-stay|co-guest|room-mate", re.IGNORECASE)
_FIR = re.compile(r"\bfir\b|accus|complainant|victim|suspect", re.IGNORECASE)
_PHONE = re.compile(r"\bsim\b|phone|same number|same identifier|caller", re.IGNORECASE)
_ADDRESS = re.compile(r"address|locality|neighbour|lives near", re.IGNORECASE)
_PAPERS = re.compile(r"tenan|landlord|guarantor|verification|vouched|reference for|nominee|prvs", re.IGNORECASE)
_VEHICLE = re.compile(r"vehicle|driver|licence|challan", re.IGNORECASE)
_WORK = re.compile(r"employ|workplace|works at|colleague|posted at", re.IGNORECASE)
_RANK = {kind: i for i, kind in enumerate(KINDS)}
# Words a relation is never told in: how sure a link is belongs to the analysts, not to the brief.
_UNSAID = re.compile(r"\s*[—-]\s*inferred\b[^;]*|\binferred\b|\bcorroborat\w*|\(AI-normalised\)|\bAI:\s*|\(\d\.\d+\)",
                     re.IGNORECASE)
_DATE = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})|(\d{4})-(\d{2})-(\d{2})")


def plainly(text: Any) -> str:
    """A relation or a reason in the records' own words, without scores, "inferred" or an
    internal id in [brackets] (the record is named where the line stands)."""
    out = _UNSAID.sub(" ", real_refs(str(text or ""), set()))
    out = re.sub(r"\s+([,.;:)])", r"\1", re.sub(r"\s{2,}", " ", out)).strip(" ·;,-—")
    return out


def _sort_key(when: Any) -> str:
    """yyyy-mm-dd hh:mm from "30-09-2026 18:45" or "2026-09-30T18:45" - for ordering only."""
    text = str(when or "")
    m = _DATE.search(text)
    if not m:
        return ""
    day = f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m.group(1) else f"{m.group(4)}-{m.group(5)}-{m.group(6)}"
    t = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)", text[m.end():])
    return day + (f" {int(t.group(1)):02d}:{t.group(2)}" if t else "")


# -- earlier FIRs ---------------------------------------------------------------------------


class _PastCase(Typed):
    what_happened: str = Field(description="The complainant's account in two or three plain sentences: who did what "
                                           "to whom, where and when. Empty when the records do not say.")
    complainant_said: list[str] = Field(description="The main points of what the complainant stated - in the FIR and "
                                                    "in any later statement - one point each.")
    found_against: list[str] = Field(description="What the police found or recorded against THE TARGET in this case, "
                                                 "one point each: arrest (with the date), recovery, disclosure, "
                                                 "identification, a witness naming them, a report's finding. Only what "
                                                 "the records say; empty when they show nothing against the target.")
    outcome: str = Field(description="Where the case stands, one of: " + ", ".join(OUTCOMES) + ". Taken only from "
                                     "case_positions, investigation_result and status_in_police_systems; 'not on "
                                     "record' when they do not say.")
    outcome_detail: str = Field(description="A few words in English, not a sentence: the outcome as the records "
                                            "give it, translated, with its date (for example 'Acquitted by the "
                                            "trial court, 12-05-2025'). No Urdu. Empty when not on record.")

    @field_validator("complainant_said", "found_against", mode="before")
    @classmethod
    def _points(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) and value.strip() else ([] if isinstance(value, str) else value)


_SYSTEM = ("You are a senior investigating officer reading ONE earlier FIR in which the target is named, to brief a "
           "colleague on the target's criminal record. From the records below only, write in plain English (the "
           "records are often in Urdu: translate them; name_spellings gives the English spelling of name words - "
           "use it word by word and write only the words the record gives; a word not there is transliterated "
           "letter by letter): "
           "what happened as "
           "the complainant told it, the points of the complainant's statements, what the police found or recorded "
           "against the target in this case, and the outcome. The outcome comes ONLY from the case positions, the "
           "investigation result and the status in police systems - never guess it, never take it from how serious "
           "the offence is. Say what the records show about the target's part, no view on guilt. Never write the "
           "words 'inferred' or 'corroborated'. " + PLAIN + DATA_NOT_INSTRUCTIONS)


def _outcome(*texts: Any) -> str:
    """Where a case stands, from the status a record gives it - the record's own word, in English."""
    words = " ".join(str(t or "") for t in texts).lower()
    for outcome, pattern in (("convicted", r"convict|sentenced|سزا"), ("acquitted", r"acquit|بری"),
                             ("cancelled", r"cancel|a-class|b-class|c-class|اخراج|خارج"),
                             ("compromise", r"compromise|راضی نامہ|صلح"),
                             ("under trial", r"under trial|pending trial|trial|زیر سماعت"),
                             ("challan submitted", r"challan|چالان"),
                             ("under investigation", r"under investigation|investigation|زیر تفتیش")):
        if re.search(pattern, words):
            return outcome
    return "not on record"


_TITLES = {"mst", "mrs", "miss", "sho", "asi", "insp", "inspector", "pc", "hc", "son", "daughter", "wife", "alias"}
_URDU = re.compile(r"[\u0600-\u06ff]")


def _detail(outcome: str, recorded: list[str], latest: dict[str, Any]) -> str:
    """The record's own words for where a case stands, in English: an Urdu status gives way to its outcome."""
    words = [w for w in recorded if not _URDU.search(w)]
    if latest:
        said = str(latest.get("position") or latest.get("status") or "")
        said = outcome.capitalize() if _URDU.search(said) else said
        words.append(said + (f", {latest['date']}" if latest.get("date") else ""))
    return "; ".join(dict.fromkeys(w for w in words if w.strip()))


def _reports_of(case: Any, number: tuple[str, str]) -> list[dict[str, Any]]:
    """The medical, forensic and laboratory reports on the board that belong to this FIR."""
    from sherlocks.team.case_scope import REPORT_RECORDS, fir_number

    cards = (case.register or {}).get("cards") or {}
    out = []
    for doc in case.documents.values():
        record = (cards.get(doc["id"]) or {}).get("record")
        if doc.get("kind") != "lab" and record not in REPORT_RECORDS:
            continue
        named = fir_number((doc.get("ref") or {}).get("fir")) or fir_number(doc.get("title"))
        if named == number:
            out.append(doc)
    return out


def _names(net: Any, pid: str, data: dict[str, Any]) -> list[str]:
    """How the records spell, in English, the words of the names this FIR is likely to give in Urdu - single
    words, so the model cannot complete a first name into someone's full name."""
    named = [p.get("name") for key in ("complainant",) for p in [data.get(key)] if isinstance(p, dict)]
    named += [p.get("name") for key in ("nominated_suspects", "arrested_suspects", "witnesses", "accused")
              for p in data.get(key) or [] if isinstance(p, dict)]
    named += [n["name"] for n in net.neighbours(pid)]
    words = (w for n in [net.name(pid), *named] if n for w in re.findall(r"[A-Za-z][A-Za-z'-]+", str(n)))
    return sorted({w for w in words if len(w) > 2 and w.lower() not in _TITLES})[:80]


def _material(case: Any, net: Any, pid: str, row: dict[str, Any], doc: dict[str, Any] | None,
              reports: list[dict[str, Any]]) -> dict[str, Any]:
    """One earlier FIR, short enough for one call: its file, its statements, its reports."""
    from sherlocks.team.case_scope import NOT_EVIDENCE
    from sherlocks.team.report_graph import _diary_steps

    data = (doc or {}).get("data") or {}
    cards = (case.register or {}).get("cards") or {}
    said = [w for w in case.statements.values() if doc and w["doc"] == doc["id"]]
    return {
        "target": net.name(pid), "name_spellings": _names(net, pid, data), "their_part": row["role"], "fir": row["fir"], "police_station": row["police_station"],
        "sections": data.get("sections") or "", "offence": row["offence"], "occurred": row["occurred"],
        "registered": row["registered"], "place": row["place"], "complainant": row["complainant"],
        "first_information": str(data.get("narrative") or (doc or {}).get("text") or "")[:TEXT],
        "statements": [{"speaker": w["speaker"], "part": w.get("role") or "", "kind": w["kind"], "date": w.get("date") or "",
                        "text": w["text"][:SAID]} for w in said][:8],
        "case_positions": [{"date": p.get("date"), "position": p.get("position") or p.get("status") or p.get("remarks")}
                           for p in data.get("case_positions") or [] if isinstance(p, dict)][:8],
        "investigation_result": str(data.get("investigation_result") or "")[:500],
        "status_in_police_systems": row["status_recorded"],
        "arrested": [{k: p.get(k) for k in ("name", "father", "date") if p.get(k)} for p in data.get("arrested_suspects") or []][:8],
        "recoveries": [{k: v for k, v in p.items() if k in ("description", "name", "value", "date", "remarks")}
                       for p in data.get("recoveries") or []][:8],
        "stolen_property": [{k: v for k, v in p.items() if k in ("description", "name", "value")}
                            for p in data.get("stolen_property") or []][:6],
        "case_diaries": [s["when"] + ": " + s["what"] for s in _diary_steps(
            [d for d in data.get("case_diaries") or [] if isinstance(d, dict)])][:10],
        "facts": [f["statement"] for f in (case.facts_of(doc["id"]) if doc else []) if f.get("kind") not in NOT_EVIDENCE
                  and case.usable(f["id"])][:20],
        "reports": [{"title": (cards.get(r["id"]) or {}).get("title") or r["title"],
                     "says": (r.get("ai_summary") or r.get("summary") or "")[:300]} for r in reports][:6],
    }


def _from_records(given: dict[str, Any]) -> dict[str, Any]:
    """The same rows without the model: the records' own lines."""
    said = [f"{w['speaker']}" + (f" ({w['kind']}, {w['date']})" if w["date"] else f" ({w['kind']})") + f": {w['text'][:240]}"
            for w in given["statements"] if re.search(r"complain", w["part"], re.IGNORECASE)]
    found = [f"Arrested: {p.get('name')}" + (f", {p['date']}" if p.get("date") else "") for p in given["arrested"]
             if p.get("name") and (given["target"].split()[0].lower() in str(p["name"]).lower())]
    found += ["Recovered: " + ", ".join(str(v) for v in r.values() if v) for r in given["recoveries"]]
    found += given["facts"][:8]
    found += [f"{r['title']}: {r['says']}" for r in given["reports"] if r["says"]]
    if given["investigation_result"]:
        found.append(f"Investigation result: {given['investigation_result']}")
    return {"what_happened": given["first_information"][:600], "complainant_said": said[:4], "found_against": found[:10]}


def past_cases(case: Any, net: Any, pid: str, skip: set[str], llm: Any = None) -> list[dict[str, Any]]:
    """Every earlier FIR that names the target - newest first - with its file read when it is on the board."""
    from sherlocks.linkgraph.case_queries import _firs
    from sherlocks.team.case_scope import fir_number

    status: dict[str, list[str]] = {}
    for f in net.data(pid).get("firs") or []:
        if f.get("status") and fir_number(f.get("label")):
            status.setdefault("/".join(fir_number(f["label"])), []).append(str(f["status"]))
    rows = []
    for r in _firs(net, pid, case):
        number = fir_number(r["label"])
        if not number or "/".join(number) in skip:
            continue
        doc = case.documents.get(r["doc"]) if r.get("doc") else None
        data = (doc or {}).get("data") or {}
        positions = sorted((p for p in data.get("case_positions") or [] if isinstance(p, dict)
                            and (p.get("position") or p.get("status"))), key=lambda p: _sort_key(p.get("date")))
        latest = positions[-1] if positions else {}
        recorded = list(dict.fromkeys(status.get("/".join(number)) or []))
        complainant = data.get("complainant") or {}
        reports = _reports_of(case, number)
        rows.append({
            "fir": "/".join(number), "police_station": data.get("police_station") or r.get("ps") or "-",
            "offence": str(data.get("sections") or r.get("offence") or "-").strip(),
            "role": ", ".join(dict.fromkeys(str(x).split("(")[0].strip() for x in r["roles"])) or r.get("role_kind") or "named",
            "occurred": str(data.get("occurred") or r.get("occurred") or "").strip(),
            "registered": str(data.get("reported") or "").strip(), "place": str(data.get("place") or "")[:220],
            "complainant": str((complainant.get("name") if isinstance(complainant, dict) else complainant) or ""),
            "status_recorded": recorded + ([f"{latest.get('date') or ''} {latest.get('position') or latest.get('status')}".strip()]
                                           if latest else []),
            "outcome": (outcome := _outcome(*recorded, latest.get("position") or latest.get("status"),
                                            data.get("investigation_result"))),
            "outcome_detail": _detail(outcome, recorded, latest),
            "file_read": doc is not None, "doc": doc["id"] if doc else None, "reports": [d["id"] for d in reports],
            "what_happened": "", "complainant_said": [], "found_against": [], "by": "records",
            "sources": r.get("sources") or []})
    rows.sort(key=lambda x: _sort_key(x["occurred"]) or "0", reverse=True)
    rows = rows[:MOST_FIRS]

    def read(row: dict[str, Any], model: Any) -> bool:
        doc = case.documents.get(row["doc"]) if row["doc"] else None
        given = _material(case, net, pid, row, doc, [case.documents[d] for d in row["reports"]])
        out = ask(model, _PastCase, dumps(given), system=_SYSTEM, kind="report_past_case", version="v1", max_tokens=1200)
        if out is None:
            row.update(_from_records(given))
            return False
        said = out.model_dump()
        outcome = str(said.get("outcome") or "").strip().lower()
        if not (given["case_positions"] or given["investigation_result"] or given["status_in_police_systems"]):
            outcome = "not on record"                   # nothing on record says where it stands: no outcome is given
        detail = plainly(said["outcome_detail"])        # a few English words; a sentence or Urdu gives way to the records'
        row.update({"what_happened": plainly(said["what_happened"]),
                    "complainant_said": [plainly(x) for x in said["complainant_said"] if str(x).strip()][:6],
                    "found_against": [plainly(x) for x in said["found_against"] if str(x).strip()][:8],
                    "outcome": outcome if outcome in OUTCOMES else row["outcome"],
                    "outcome_detail": detail if len(detail) <= 50 and not _URDU.search(detail)
                    and outcome[:6] in detail.lower() else row["outcome_detail"], "by": "model"})
        return True

    with_file = [r for r in rows if r["file_read"]]
    if not with_file:
        return rows
    # The first FIR is read alone: when the model does not answer, the rest are laid out from
    # the records at once instead of each waiting out the model's time limit.
    answered = read(with_file[0], llm)
    rest = with_file[1:MODEL_FIRS] if answered else []
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda row: read(row, llm), rest))
    for row in with_file[1:] if not answered else with_file[MODEL_FIRS:]:
        read(row, None)
    return rows


# -- relations ------------------------------------------------------------------------------


def _kind(label: str, sentence: str, system: str) -> str | None:
    """Which group a relation belongs to - from the record's own label and system. None:
    not a relation of the target (an investigating officer on their FIR)."""
    text = f"{label} {sentence}"
    if re.search(r"investigating officer|investigated fir", text, re.IGNORECASE):
        return None
    if _FAMILY.search(text):
        return "family"
    if _HOTEL.search(text) or system == "hotel_eye":
        return "hotel"
    if _PAPERS.search(text) or system in ("prvs", "old_tenant", "trust", "sbvs", "evs", "hope"):
        return "verification"
    if _FIR.search(text) or system in ("psrms", "cro", "fir_roster", "arms", "watchlist"):
        return "fir"
    if _PHONE.search(text) or system in ("simsdb", "subscriber", "caller_id"):
        return "phone"
    if _ADDRESS.search(text):
        return "address"
    if _VEHICLE.search(text) or system in ("dls", "tracs", "excise", "avlc"):
        return "vehicle"
    if _WORK.search(text) or system == "hrmis":
        return "work"
    return "other"


def _reasons(edge: dict[str, Any]) -> str:
    """What a link found by comparing records rests on - the two addresses, the father's name, the stay."""
    out = []
    for reason in edge.get("reasons") or []:
        reason = re.sub(r"^[^:]{1,60}\(\d(?:\.\d+)?\):\s*", "", str(reason))       # "Same address (0.85): " - the label again
        text = plainly(re.sub(r"\s*~\s*", " and ", reason))
        if text and text not in out:
            out.append(text)
    return "; ".join(out[:2])


def relations(net: Any, graph: dict[str, Any], pid: str, targets: list[str]) -> list[dict[str, Any]]:
    """Everyone the records tie to the target, and how - one row per person, each tie in words."""
    from sherlocks.linkgraph.relations import weak_relation
    from sherlocks.linkgraph.systems import system_label

    edges = {e.get("id"): e for e in graph.get("edges") or []}
    nodes = {n["id"]: n for n in graph.get("nodes") or []}
    if pid not in net.G:
        return []
    rows = []
    for other in net.G.neighbors(pid):
        if other not in net.people or other in targets:
            continue
        ties: list[dict[str, str]] = []
        for link in sorted(net.G[pid][other]["links"], key=lambda link: link.cost):
            edge = edges.get(link.edge) or {}
            if link.kind == "weak":
                a, b = (other, pid)
                how = weak_relation(link.label, a, b).sentence({a: net.name(other), b: net.name(pid)})
                why = _reasons(edge)
                how = plainly(how) + (f" - {why}" if why else "")
            elif link.kind == "shared":
                record = nodes.get(link.record or "") or {}
                how = f"Both are named on {record.get('label') or 'the same record'}"
            else:
                how = plainly(link.sentence or f"{net.name(other)}: {link.label}")
            kind = _kind(link.label or "", how, str(link.system or (nodes.get(link.record or "") or {}).get("data", {}).get("system") or ""))
            if kind is None or not how or any(t["how"] == how for t in ties):
                continue
            source = "records compared" if link.kind == "weak" else system_label(
                str(link.system or (nodes.get(link.record or "") or {}).get("data", {}).get("system") or "")) or "records"
            ties.append({"kind": kind, "how": how, "source": source})
        if not ties:
            continue
        flags = net.data(other).get("flags") or []
        rows.append({"pid": other, "name": net.name(other), "ties": ties[:4], "kind": min((t["kind"] for t in ties), key=_RANK.get),
                     "criminal": bool(net.criminal(other)), "police_officer": "police_officer" in flags,
                     "stated": bool(net.G[pid][other]["stated"])})
    rows.sort(key=lambda r: (_RANK[r["kind"]], not r["stated"], not r["criminal"], r["name"]))
    return rows[:MOST_PEOPLE]


# -- hotel stays ----------------------------------------------------------------------------


def hotels(net: Any, pid: str) -> list[dict[str, Any]]:
    """The target's hotel stays, newest first, with anyone on the graph who stayed there at the same time."""
    from sherlocks.linkgraph.weak_links import _stay_overlap

    out = []
    for s in net.data(pid).get("stays") or []:
        with_them = []
        for other in net.people:
            if other == pid:
                continue
            for t in net.data(other).get("stays") or []:
                hit = _stay_overlap(s, t)
                if hit and hit[0] >= 0.7 and net.name(other) not in with_them:
                    with_them.append(net.name(other))
        out.append({"hotel": s.get("hotel") or "-", "district": s.get("district") or "-", "room": s.get("room") or "-",
                    "check_in": str(s.get("check_in") or "-").replace("T", " "),
                    "check_out": str(s.get("check_out") or "-").replace("T", " "), "with": with_them[:6]})
    out.sort(key=lambda s: _sort_key(s["check_in"]) or "0", reverse=True)
    return out


# -- earlier reports ------------------------------------------------------------------------


def reports(case: Any, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each medical, forensic and laboratory report of the target's earlier FIRs: its FIR,
    its heading and what it says."""
    cards = (case.register or {}).get("cards") or {}
    out = []
    for row in cases:
        for d in row["reports"]:
            doc, card = case.documents[d], cards.get(d) or {}
            received = re.search(r"case received ([\d\-: ]+)", doc.get("summary") or "")
            says = plainly(doc.get("ai_summary") or "")
            found = [f"{str(f.get('name') or '').replace('_', ' ').capitalize()}: {f.get('value')}"
                     + (f" ({f['when']})" if f.get("when") else "") for f in card.get("fields") or [] if f.get("value")][:8]
            if not says and not found:
                says = re.sub(r"^.*?·\s*case received [\d\-: ]+\s*·?\s*", "", doc.get("summary") or "").strip() or (
                    "The report is on file but its text could not be read.")
            out.append({"fir": row["fir"], "case": f"{row['offence']}, PS {row['police_station']}".strip(", "),
                        "heading": card.get("title") or doc.get("title"), "from": doc.get("source") or "",
                        "received": received.group(1).strip() if received else "", "says": says, "found": found,
                        "doc": d})
    return out


def build(case: Any, net: Any, graph: dict[str, Any], scope: dict[str, Any], llm: Any = None) -> dict[str, Any]:
    """The target's record: ``{targets: [{pid, name, past_cases, relations, hotels, reports, overview}]}``."""
    skip = set(scope.get("fir_numbers") or [])
    out = []
    for pid in scope["targets"][:3]:
        cases = past_cases(case, net, pid, skip, llm)
        related = relations(net, graph, pid, scope["targets"])
        stays = hotels(net, pid)
        out.append({"pid": pid, "name": net.name(pid), "past_cases": cases, "relations": related, "hotels": stays,
                    "reports": reports(case, cases), "overview": overview(net.name(pid), cases, related, stays)})
    return {"targets": out, "kinds": KINDS}


def overview(name: str, cases: list[dict[str, Any]], related: list[dict[str, Any]], stays: list[dict[str, Any]]) -> list[str]:
    """The record in a few lines, for the top of the report."""
    lines = []
    if cases:
        counts: dict[str, int] = {}
        for c in cases:
            counts[c["outcome"]] = counts.get(c["outcome"], 0) + 1
        split = ", ".join(f"{n} {'with no outcome on record' if o == 'not on record' else o}"
                          for o, n in sorted(counts.items(), key=lambda x: OUTCOMES.index(x[0])))
        lines.append(f"{name} is named in {len(cases)} earlier FIR(s): {split}.")
        latest = cases[0]
        lines.append(f"Most recent: FIR {latest['fir']}, PS {latest['police_station']} ({latest['offence']})"
                     + (f", incident {latest['occurred']}" if latest["occurred"] else "") + f" - {latest['outcome']}.")
    else:
        lines.append(f"No earlier FIR names {name} in the police systems searched.")
    if related:
        groups: dict[str, int] = {}
        for r in related:
            groups[r["kind"]] = groups.get(r["kind"], 0) + 1
        lines.append(f"{len(related)} people are connected to {name} in the records: "
                     + ", ".join(f"{KINDS[k][:1].lower()}{KINDS[k][1:]} {n}" for k, n in sorted(groups.items(), key=lambda x: _RANK[x[0]])) + ".")
    if stays:
        lines.append(f"{len(stays)} hotel stay(s) on record; the latest at {stays[0]['hotel']} ({stays[0]['district']}), "
                     f"check-in {stays[0]['check_in']}.")
    return lines
