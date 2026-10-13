"""S10 Fact collector: the evidence register.

A2 reads each record on its own; S10 files what every record says into one register, so
nothing in a record is missed or read twice:

* **one typed card per record** (FIR, MLO, postmortem, DNA, chemical, forensic, recovery
  memo, site plan, CRO) - the model reads the record and fills the card; every field
  keeps its page and the exact quote, and a field whose quote is not in the record is
  dropped (``quote_in``);
* **four views** over the cards - by record, by person, by topic, by time;
* **links across records** by exhibit / sample number, **chain of custody** with the days
  between steps, and **pending reports** (asked for, not received) - all in code.

S10 collects and links; it never judges. What the facts mean is S6's and S1's work.
"""

from __future__ import annotations

import itertools
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from sherlocks.evidence.case_file import quote_in
from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.team.runtime import Typed, ask, asking

RECORDS = ("fir", "mlo", "postmortem", "dna", "chemical", "forensic", "recovery_memo", "site_plan", "cro",
           "statement", "other", "system")
TOPICS = ("presence", "time", "place", "weapon", "injury", "vehicle", "phone", "money", "relation", "role",
          "identity", "recovery", "sample", "custody", "status", "address", "stays", "other")
# A police system's record is filed under the topic its catalog entry answers.
SYSTEM_TOPIC = {"identity": "identity", "vehicle": "vehicle", "phone": "phone", "address": "address", "hotel": "stays",
                "record": "status"}
STEPS = ("collected", "sealed", "sent", "received", "result")
READS = ("fir", "lab", "cro", "upload")
CUSTODY_GAP_DAYS = 7

Record = Literal["fir", "mlo", "postmortem", "dna", "chemical", "forensic", "recovery_memo", "site_plan", "cro",
                 "statement", "other"]
Topic = Literal["presence", "time", "place", "weapon", "injury", "vehicle", "phone", "money", "relation", "role",
                "identity", "recovery", "sample", "custody", "status", "address", "stays", "other"]


class _Field(Typed):
    model_config = asking("name", "value", "topic", "person", "when", "quote")

    name: str = Field(description="What the field is: 'cause of death', 'injury 1', 'weapon opinion', 'accused 2 role'.")
    value: str = Field(description="What the record says, in English, exact numbers and names.")
    topic: Topic
    person: str = Field(default="", description="The person it is about, the name as written; empty if none.")
    father: str = ""
    cnic: str = ""
    when: str | None = Field(default=None, description="The date or date-time the fact refers to, ISO (YYYY-MM-DD or "
                                                       "YYYY-MM-DDTHH:MM), if the record gives it.")
    where: str = ""
    exhibit: str = Field(default="", description="Exhibit / parcel / sample number it concerns, exactly as written.")
    step: Literal["", "collected", "sealed", "sent", "received", "result"] = Field(
        default="", description="For an exhibit or sample: which step of its handling this field records.")
    page: int | None = None
    quote: str = Field(description="The exact words of the record it rests on, copied verbatim.")


class _Pending(Typed):
    record: Record = Field(description="The kind of report asked for but not received yet.")
    what: str
    requested_on: str | None = None
    quote: str


class _Card(Typed):
    record: Record
    title: str = Field(description="Short title: 'MLO of Naveed, 2 March 2023'.")
    background: bool = Field(default=False, description="True for a criminal record / earlier cases: background, "
                                                        "not evidence about this incident.")
    fields: list[_Field] = Field(default_factory=list)
    pending: list[_Pending] = Field(default_factory=list)
    not_in_record: list[str] = Field(default_factory=list, description="The things this kind of record normally "
                                     "gives (from the list for its kind) that THIS record does not contain.")


_SYSTEM = (
    "You file ONE police record into an evidence register. Say which kind of record it is, then fill its card - one "
    "field per fact, every field with the exact quote it rests on and its page when shown. What to read:\n"
    "- FIR: number, police station, district, date and time registered, sections, offence, investigating officer; "
    "incident date, time and place, distance from the station, the reason given for any delay; the complainant "
    "(name, father, CNIC, address, relation to the victim); each accused with their role in the occurrence and "
    "weapon; injured or deceased; witnesses named; the account in order, the motive as stated, property taken, "
    "vehicles; arrests, bail, recoveries, challan, court dates.\n"
    "- MLO: date and time examined, time since injury; each injury with its number, place on the body, size, kind "
    "(firearm entry or exit, incised, lacerated, blunt), the weapon opinion, grievous or simple; the doctor.\n"
    "- Postmortem: date and time, time between injury and death, time since death, cause of death, each injury, "
    "weapon opinion, samples taken.\n"
    "- DNA: each sample (what, from whom or where), reference samples, lab and report number, the result of each "
    "comparison (matched, excluded, inconclusive).\n"
    "- Chemical / toxicology: sample, what was tested for, result. - Forensic: firearm and ballistics, fingerprints, "
    "other tests, each with exhibit numbers and result.\n"
    "- Recovery memo: what was recovered, from whom, where, when, by whom, the witnesses, sealed or not.\n"
    "- Site plan: points marked, positions of victim, accused and witnesses, distances, light source.\n"
    "- CRO / past record: earlier FIRs and convictions - set background true.\n"
    "For every exhibit or sample also record each handling step the record states (collected, sealed, sent to the "
    "lab, received, result) with its date. List under `pending` any report the record says was requested and is "
    "still awaited. Under `not_in_record` name what a record of this kind normally gives and this one does not "
    "(e.g. 'time since injury', 'weapon opinion') - the report then says \"not in report\" instead of staying silent. "
    "Never add what the record does not say. At most 18 fields - the ones an investigator needs first - each with a "
    "short quote (the few words that carry it), and at most 6 things under `not_in_record`. "
    + DATA_NOT_INSTRUCTIONS)


def read_card(case: Any, doc_id: str, llm: Any) -> dict[str, Any] | None:
    """A2 fills the typed card of one record. Fields whose quote is not in the record
    are dropped. Returns the card, or None when it was not read (no model: next run)."""
    doc = case.documents[doc_id]
    if doc["kind"] not in READS or doc_id in (case.register.get("cards") or {}) or not (doc.get("text") or "").strip():
        return None
    out = ask(llm, _Card, f"Record: {doc['title']} ({doc['source']})\n\n{doc['text'][:9000]}",
              system=_SYSTEM, kind="register_card", version="v2", max_tokens=2400)
    if out is None:
        return None
    text = doc["text"]
    fields = [f.model_dump() for f in out.fields[:40] if quote_in(f.quote, text)]
    dropped = len(out.fields) - len(fields)
    card = {"record": out.record, "title": out.title[:160] or doc["title"], "background": bool(out.background),
            "fields": fields, "dropped": dropped,
            # What a typed record lacks; a bare statement or an unknown kind of file has no such list.
            "not_in_record": ([] if out.record in ("statement", "other") else
                              [x.strip()[:80] for x in out.not_in_record[:6] if x.strip()]),
            "pending": [p.model_dump() for p in out.pending[:8] if quote_in(p.quote, text)]}
    with case.acting("A2 Card reader"):
        case.set_card(doc_id, card)
    return card


def _when(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).strip().replace(" ", "T")[:16]) if value else None
    except ValueError:
        return None


def rows(case: Any) -> list[dict[str, Any]]:
    """Every field of every card, flat: the register."""
    out = []
    for doc_id, card in (case.register.get("cards") or {}).items():
        for i, f in enumerate(card.get("fields") or []):
            out.append({**f, "doc": doc_id, "record": card["record"], "card": card["title"],
                        "background": card.get("background", False), "n": i,
                        "ref": card.get("ref") or (f"{doc_id}" + (f" p.{f['page']}" if f.get("page") else ""))})
    return out


def custody(case: Any) -> list[dict[str, Any]]:
    """Each exhibit's handling, in order, with the days between steps."""
    by_exhibit: dict[str, list[dict[str, Any]]] = {}
    for r in rows(case):
        if r.get("exhibit") and r.get("step"):
            by_exhibit.setdefault(" ".join(r["exhibit"].lower().split()), []).append(r)
    chains = []
    for steps in by_exhibit.values():
        steps.sort(key=lambda r: (STEPS.index(r["step"]), r.get("when") or ""))
        chain = {"exhibit": steps[0]["exhibit"], "what": steps[0]["name"], "gaps": [],
                 "steps": [{"step": r["step"], "when": r.get("when"), "ref": r["ref"], "value": r["value"],
                            "quote": r["quote"]} for r in steps]}
        timed = [(r["step"], _when(r.get("when")), r) for r in steps if _when(r.get("when"))]
        for (a, ta, ra), (b, tb, rb) in itertools.pairwise(timed):
            days = (tb - ta).days
            if days >= CUSTODY_GAP_DAYS:
                chain["gaps"].append({"from": a, "to": b, "days": days, "refs": [ra["ref"], rb["ref"]],
                                      "docs": [ra["doc"], rb["doc"]]})
        chains.append(chain)
    return chains


def pending(case: Any) -> list[dict[str, Any]]:
    """Reports asked for and not on the board yet."""
    cards = case.register.get("cards") or {}
    have = {c["record"] for c in cards.values()}
    out = []
    for doc_id, card in cards.items():
        for p in card.get("pending") or []:
            if p["record"] not in have:
                out.append({**p, "doc": doc_id})
    return out


def system_cards(case: Any, net: Any, graph: dict[str, Any]) -> int:
    """Police system records as cards: for each person on the case, what every system
    returned - NADRA, SIMs, Excise, DLS, Hotel Eye, tenant registration, PSRMS... - with
    the call it came from and the day it was checked. "Checked, nothing found" is kept as
    a card too: it shows what was looked at."""
    from sherlocks.linkgraph.api_router import TOPIC_SYSTEMS, searched
    from sherlocks.linkgraph.systems import system_label

    topic_of = {s: SYSTEM_TOPIC.get(topic, "other") for topic, systems in reversed(TOPIC_SYSTEMS.items()) for s in systems}
    nodes = {n["id"]: n for n in graph.get("nodes") or [] if n.get("kind") == "system"}
    people = [p for p in dict.fromkeys([*net.seeds(), *case.roles]) if p in net.people][:30]
    cards = case.register.setdefault("cards", {})
    made = 0
    for pid in people:
        name, ids = net.name(pid), {str(net.data(pid).get("cnic") or ""), *map(str, net.data(pid).get("phones") or [])}
        last_call = {c["system"]: c for c in case.calls if str(c.get("identifier") or "") in ids and c.get("status") != "refused"}
        seen = set()
        for rec in net.data(pid).get("records") or []:
            system = str(rec.get("system") or "")
            data = (nodes.get(rec.get("node")) or {}).get("data") or {}
            if not system or system in seen:
                continue
            seen.add(system)
            topic, label = topic_of.get(system, "other"), system_label(system)
            call = last_call.get(system) or {}
            ref = label + (f", {call['id']}" if call.get("id") else "")
            fields = [{"name": str(f.get("label")), "value": str(f.get("value"))[:300], "topic": topic, "person": name,
                       "pid": pid, "quote": f"{f.get('label')}: {f.get('value')}"[:300], "page": None, "when": None,
                       "where": "", "exhibit": "", "step": ""}
                      for f in (data.get("fields") or [])[:30] if str(f.get("value") or "").strip()]
            for st in (data.get("stays") or [])[:20]:
                text = f"{st.get('hotel')} ({st.get('district') or '-'}), {st.get('check_in') or '-'} to {st.get('check_out') or '-'}"
                fields.append({"name": "hotel stay", "value": text, "topic": "stays", "person": name, "pid": pid, "quote": text,
                               "page": None, "when": str(st.get("check_in") or "")[:16] or None, "where": str(st.get("hotel") or ""),
                               "exhibit": "", "step": ""})
            for v in (data.get("vehicles") or [])[:10]:
                fields.append({"name": "vehicle", "value": str(v)[:200], "topic": "vehicle", "person": name, "pid": pid,
                               "quote": str(v)[:200], "page": None, "when": None, "where": "", "exhibit": "", "step": ""})
            card = {"record": "system", "system": system, "title": f"{label} record of {name}", "ref": ref, "person": pid,
                    "fields": fields, "summary": str(data.get("summary") or rec.get("summary") or "")[:300],
                    "trust": 6 if system in ("caller_id", "osint") else 1, "background": system in ("cro", "arms", "watchlist"),
                    "call": call.get("id"), "checked": str(call.get("at") or "")[:10] or None, "pending": [],
                    "nothing": False}
            made += _keep(case, cards, f"sys:{pid}:{system}", card)
        for system, status in searched(net, case, pid).items():
            if status == "nothing found" and system not in seen:
                call = last_call.get(system) or {}
                card = {"record": "system", "system": system, "title": f"{system_label(system)} record of {name}",
                        "ref": system_label(system) + (f", {call['id']}" if call.get("id") else ""), "person": pid,
                        "fields": [], "summary": "checked, nothing found", "trust": 1, "background": False,
                        "call": call.get("id"), "checked": str(call.get("at") or "")[:10] or None, "pending": [],
                        "nothing": True}
                made += _keep(case, cards, f"sys:{pid}:{system}", card)
    return made


def _keep(case: Any, cards: dict[str, Any], key: str, card: dict[str, Any]) -> int:
    """Store a card only when it changed: a run that finds nothing new writes nothing."""
    old = cards.get(key)
    if old is not None and {k: v for k, v in old.items() if k not in ("at", "doc")} == card:
        return 0
    with case.acting("S10 Fact collector"):
        case.set_card(key, card)
    return 1


def links(case: Any) -> list[dict[str, Any]]:
    """The same exhibit, sample or parcel in more than one record: the recovered weapon in
    the recovery memo and the forensic report, a sample in the memo and the DNA result."""
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows(case):
        if r.get("exhibit"):
            by.setdefault(" ".join(r["exhibit"].lower().split()), []).append(r)
    return [{"exhibit": group[0]["exhibit"], "records": sorted({r["doc"] for r in group}),
             "says": [f"{r['card']}: {r['name']} - {r['value']} [{r['ref']}]" for r in group][:12]}
            for group in by.values() if len({r["doc"] for r in group}) > 1]


def collect(case: Any, net: Any = None, graph: dict[str, Any] | None = None) -> dict[str, int]:
    """S10: the police systems' records as cards, custody chains with their long gaps as
    facts, and the pending reports."""
    systems = system_cards(case, net, graph) if net is not None and graph is not None else 0
    chains, waiting = custody(case), pending(case)
    with case.acting("S10 Fact collector"):
        for chain in chains:
            for gap in chain["gaps"]:
                case.add_fact(gap["docs"][-1], f"{chain['exhibit']}: {gap['to']} {gap['days']} days after {gap['from']} "
                                               f"[{', '.join(gap['refs'])}]", chain["exhibit"], kind="custody_gap",
                              by="derived", check_quote=False, topic=f"custody:{chain['exhibit'].lower()}",
                              rests_on=list(dict.fromkeys(gap["docs"])))
        case.set_register(pending=waiting, custody=chains)
    return {"cards": len(case.register.get("cards") or {}), "systems": systems, "custody": len(chains),
            "pending": len(waiting), "links": len(links(case))}


def lookup(case: Any, net: Any, *, person: str | None = None, topic: str | None = None, record: str | None = None,
           exhibit: str | None = None) -> dict[str, Any]:
    """The Evidence register tool: facts by record, person, topic or exhibit, in time
    order, with the custody chains and the pending reports that concern them."""
    from sherlocks.linkgraph.normalize import name_key
    from sherlocks.team.entities import lookup as who_is

    pid = net.resolve(person) if person else None
    key = name_key(net.name(pid) if pid else person or "")

    def about(r: dict[str, Any]) -> bool:
        if not person:
            return True
        if name_key(r.get("person") or "") == key:
            return True
        return pid is not None and (r.get("pid") == pid or who_is(case, net, r.get("person") or "").get("pid") == pid)

    found = [r for r in rows(case) if about(r) and (not topic or r["topic"] == topic)
             and (not record or r["record"] == record)
             and (not exhibit or " ".join(exhibit.lower().split()) in " ".join((r.get("exhibit") or "").lower().split()))]
    found.sort(key=lambda r: (r.get("when") or "9999", r["doc"], r["n"]))
    return {"count": len(found), "empty": not found,
            "facts": [{k: r.get(k) for k in ("record", "card", "name", "value", "topic", "person", "when", "where",
                                             "exhibit", "ref", "quote", "background")} for r in found[:80]],
            "custody": [c for c in custody(case) if not exhibit or exhibit.lower() in c["exhibit"].lower()][:12],
            "pending": pending(case), "linked_across_records": links(case)[:12],
            "checked_nothing_found": [c["title"] for c in (case.register.get("cards") or {}).values() if c.get("nothing")],
            "not_in_record": {d: c["not_in_record"] for d, c in (case.register.get("cards") or {}).items()
                              if c.get("not_in_record") and (not record or c["record"] == record)},
            "records": [{"doc": d, "record": c["record"], "title": c["title"], "fields": len(c.get("fields") or [])}
                        for d, c in (case.register.get("cards") or {}).items()]}
