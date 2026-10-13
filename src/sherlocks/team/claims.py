"""S5 Claim extractor: everything becomes a claim.

A contradiction can only be found between two claims that say the same kind of thing
about the same person and time. S5 turns the board into small comparable claims - who
said it, about whom, what, when, where - each keeping the exact quote it came from:

* a register card's fields are already claims by that record (code);
* a recorded statement ``W#`` and loose facts (the officer's words, CDR findings, graph
  inferences) are read by the model, a batch at a time.

Code checks every claim: the quote must be in its source, times are parsed, and the
person is resolved by A5 (a name-only *maybe* stays a maybe).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from sherlocks.evidence.case_file import TRUST_DOCUMENT, TRUST_RECORDED, quote_in
from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.team.runtime import Typed, ask, dumps

# The topics a claim can be compared on. A claim on another topic is stored, never compared.
COMPARED = ("presence", "time", "place", "weapon", "injury", "vehicle", "phone", "money", "relation", "role",
            "identity", "address")
# What a police system's record is compared on (its identity fields are how people are matched, not claims).
SYSTEM_COMPARED = ("address", "vehicle", "phone")
BATCH = 6
CALLS_PER_RUN = 12


class _Claim(Typed):
    source: str = Field(description="The id of the entry this claim comes from, exactly as given (W3, F12).")
    said_by: str = Field(description="Who asserts it: the speaker of a statement, or the record / analysis itself.")
    about: str = Field(default="", description="The person it is about, the name as written; empty for the incident.")
    about_father: str = Field(default="", description="That person's father's name, if the entry gives it.")
    topic: Literal["presence", "time", "place", "weapon", "injury", "vehicle", "phone", "money", "relation", "role",
                   "identity", "other"]
    event: str = Field(default="", description="The event it concerns, two or three words: 'the incident', 'arrest', "
                                               "'FIR registration', 'recovery'.")
    value: str = Field(description="What is claimed, short and exact: 'at home', 'pistol', 'incised wound, right thigh'.")
    when_from: str | None = Field(default=None, description="Start of the time it refers to, ISO YYYY-MM-DDTHH:MM "
                                                            "(or YYYY-MM-DD), resolved against the entry's own date.")
    when_to: str | None = None
    tolerance_min: int = Field(default=0, description="How loose the time is, in minutes: 30 for 'about 10 at night'.")
    where: str = ""
    certainty: Literal["stated", "approximate", "unsure"] = "stated"
    quote: str = Field(description="The exact words of the entry the claim rests on, copied verbatim.")


class _Claims(Typed):
    claims: list[_Claim] = Field(default_factory=list)


_SYSTEM = ("You break police case entries into small comparable claims, so that two sources can be checked against "
           "each other. One claim says ONE thing: where a person was, when something happened, which weapon, which "
           "injury, a vehicle, a phone, money, a relation, a role, an identity detail. Give each claim the id of the "
           "entry it comes from, who asserts it, whom it is about, the time it refers to (work out 'that night' or "
           "'next morning' from the entry's own date) and the exact words it rests on, copied verbatim in their own "
           "script. A phone on a tower is a claim about the PHONE's place, never the person's. Never add a claim the "
           "entry does not make. " + DATA_NOT_INSTRUCTIONS)


def when(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).strip().replace(" ", "T")[:16]) if value else None
    except ValueError:
        return None


def _iso(value: str | None) -> str | None:
    t = when(value)
    return t.isoformat(timespec="minutes") if t else None


def _who(case: Any, net: Any, name: str, source: str | None = None, father: str = "",
         cnic: str = "") -> tuple[str | None, str]:
    from sherlocks.team.entities import lookup

    got = lookup(case, net, name, source=source, father=father, cnic=cnic)
    return (got["pid"] if got["state"] == "same" else None), got["state"]


def from_cards(case: Any, net: Any) -> int:
    """Register fields as claims by their record (code)."""
    n = 0
    for doc_id, card in (case.register.get("cards") or {}).items():
        mark = f"card:{doc_id}"
        if case.claimed.get(mark) == len(card.get("fields") or []):
            continue
        system = card["record"] == "system"
        for f in card.get("fields") or []:
            if f["topic"] not in (SYSTEM_COMPARED if system else COMPARED):
                continue
            if system:                              # asked about this very person, by identifier
                pid, state = f.get("pid"), "same"
            else:
                pid, state = _who(case, net, f.get("person") or "", doc_id, f.get("father") or "", f.get("cnic") or "")
            case.add_claim(card.get("ref") or doc_id if system else doc_id, said_by=card["title"],
                           about=f.get("person") or "", topic=f["topic"],
                           value=f["value"], quote=f["quote"], when_from=_iso(f.get("when")), where=f.get("where") or "",
                           trust=int(card.get("trust") or TRUST_DOCUMENT), about_pid=pid, about_state=state,
                           page=f.get("page"), event=f["name"], background=bool(card.get("background")),
                           kind="system" if system else "record")
            n += 1
        case.claimed[mark] = len(card.get("fields") or [])
    return n


def _sources(case: Any) -> list[dict[str, Any]]:
    """Entries the model still has to read: recorded statements, then loose facts."""
    carded = set(case.register.get("cards") or {})
    out = []
    for w in case.statements.values():
        if w["id"] not in case.claimed:
            out.append({"id": w["id"], "kind": f"recorded statement ({w['kind']}) of {w['speaker']}, {w['role']}",
                        "date": w.get("date"), "text": w["text"][:3000], "trust": TRUST_RECORDED,
                        "speaker": w["speaker"], "page": w.get("page")})
    for f in case.live_facts():
        if f["id"] in case.claimed or f.get("kind") in ("custody_gap",):
            continue
        if f.get("by") in ("ai", "rule") and f["doc"] in carded:
            continue                                # the record's card already carries it
        out.append({"id": f["id"], "kind": f"{f.get('by')} entry ({f.get('tier')})", "date": f.get("at", "")[:10],
                    "text": f["statement"], "quote_text": f["statement"] + "\n" + (f.get("quote") or ""),
                    "trust": f.get("trust") or TRUST_DOCUMENT, "speaker": "the officer" if f.get("by") == "officer" else "",
                    "page": f.get("page")})
    return out


def run(case: Any, net: Any, llm: Any) -> dict[str, int]:
    """S5 on the board. Sources the model could not read wait for the next run."""
    with case.batch("S5 Claim extractor", "claims"):
        made = from_cards(case, net)
        waiting = _sources(case)
        dropped = calls = 0
        for start in range(0, len(waiting), BATCH):
            if calls >= CALLS_PER_RUN or llm is None:
                break
            batch = waiting[start:start + BATCH]
            calls += 1
            out = ask(llm, _Claims, dumps({"incident": case.incident, "entries": [
                {k: s[k] for k in ("id", "kind", "date", "text")} for s in batch]}),
                system=_SYSTEM, kind="claims", max_tokens=1800)
            if out is None:
                continue
            by_id = {s["id"]: s for s in batch}
            for c in out.claims[:80]:
                src = by_id.get(c.source)
                if src is None or not quote_in(c.quote, src.get("quote_text") or src["text"]):
                    dropped += 1
                    continue
                pid, state = _who(case, net, c.about, c.source, c.about_father)
                said_pid, _ = _who(case, net, c.said_by or src["speaker"], c.source)
                case.add_claim(c.source, said_by=c.said_by or src["speaker"], about=c.about, topic=c.topic, value=c.value,
                               quote=c.quote, when_from=_iso(c.when_from), when_to=_iso(c.when_to), where=c.where,
                               tolerance_min=max(0, min(c.tolerance_min, 720)), trust=src["trust"], said_pid=said_pid,
                               about_pid=pid, about_state=state, page=src.get("page"), certainty=c.certainty,
                               event=c.event)
                made += 1
            for s in batch:
                case.claimed[s["id"]] = case.version
    return {"claims": len(case.claims), "made": made, "dropped": dropped, "waiting": max(0, len(waiting) - calls * BATCH)}
