"""S8 Lead spotter: hints become leads ``K#``.

A hint is something odd that code found - a silence around the incident, a handset
changed after it, contact with one number jumping before it, the same pattern across
two CDRs or two cases, an exhibit that waited weeks before reaching the lab. S8 keeps each
as a lead: why it could matter, the hypothesis it bears on, the next step, and a status

    spotted -> open -> pursuing -> confirmed | dead end

A lead is not evidence: it never goes into the hypothesis matrix. The model writes the
one-line description and names the next step; the anomaly itself is found in code (the
CDR team, the graph analyst, the register). Dead ends stay on the board with the reason,
so nobody chases them twice.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.linkgraph.relevance import the_new_case
from sherlocks.team.runtime import NEW_CASE, PLAIN, Typed, ask, dumps

# The kinds of entry code writes when it finds something out of pattern.
HINTS = ("telecom_silence_incident", "telecom_imei_change", "telecom_spike", "cdr_cross", "custody_gap")
STATES = ("spotted", "open", "pursuing", "confirmed", "dead end")
# Patterns the graph notes that are too common to be a lead (a SIM registered in another person's name).
NOT_A_LEAD = ("graph_sim_front",)
PER_CALL = 10


class _Lead(Typed):
    hint: str = Field(description="The id of the hint (F#), exactly as given.")
    why: str = Field(description="One line: why this could matter to the case. A possibility, not a conclusion.")
    next_step: str = Field(description="The one next step that would test it: a lookup, a record to get, a question.")
    hypothesis: str | None = Field(default=None, description="The hypothesis id (H#) it bears on, if any.")
    bears: Literal["supports", "weakens", "unclear"] = "unclear"
    question: str = Field(default="", description="A neutral question for the officer that would move it; empty if none.")


class _Leads(Typed):
    leads: list[_Lead] = Field(default_factory=list)


class _Outcome(Typed):
    status: Literal["pursuing", "confirmed", "dead end"] = Field(
        description="confirmed: the answer bears the lead out; dead end: it rules the lead out or gives an innocent "
                    "reason; pursuing: still to be checked.")
    why: str = ""


_SYSTEM = ("You are the lead spotter of a police investigation team. Code found the HINTS below - things out of "
           "pattern in the call records, the records or the graph. For each, write one line on why it could matter, "
           "the hypothesis it bears on (only an id given), and the single next step that would test it. A lead is a "
           "possibility to check, never evidence and never an accusation. The next step must be something the "
           "police can do with records: get a CDR, look up an owner, record a statement, ask the officer. " + NEW_CASE + PLAIN
           + DATA_NOT_INSTRUCTIONS)
_OUTCOME_SYSTEM = ("A lead of an investigation was put to the investigating officer as a question. Read the answer "
                   "and say where the lead stands now. " + DATA_NOT_INSTRUCTIONS)


def key_of(fact: dict[str, Any]) -> str:
    """What a lead is about: the pattern a graph fact states (the same pattern stated again
    with more people is the same lead), else the entry itself."""
    from sherlocks.evidence.graph_analyst import pattern_of

    return pattern_of(fact) or fact["id"]


def hints(case: Any) -> list[dict[str, Any]]:
    """Out-of-pattern entries on the board that are not leads yet."""
    have = {k["key"] for k in case.leads.values()} | {k["source"] for k in case.leads.values()}
    return [f for f in case.live_facts() if f["id"] not in have and key_of(f) not in have
            and f.get("kind") not in NOT_A_LEAD and (
        f.get("kind") in HINTS or (str(f.get("kind") or "").startswith("graph_") and f.get("tier") == "inference"))]


def follow(case: Any) -> int:
    """A lead whose pattern was stated again follows the new statement; when another lead
    already holds that pattern, this one is closed quietly - it is the same lead."""
    moved = 0
    for k in list(case.leads.values()):
        fact = case.facts.get(k["source"])
        if fact is None or not fact.get("replaced_by") or k["status"] in ("confirmed", "dead end"):
            continue
        seen = set()
        while fact.get("replaced_by") in case.facts and fact["id"] not in seen:
            seen.add(fact["id"])
            fact = case.facts[fact["replaced_by"]]
        if fact.get("stale") or fact.get("replaced_by") or key_of(fact) != key_of(case.facts[k["source"]]):
            continue                                    # corrected away, not restated: handled below
        twin = next((o for o in case.leads.values() if o is not k and o["status"] not in ("dead end",)
                     and (o["source"] == fact["id"] or o["key"] == key_of(fact))), None)
        if twin is not None:
            k["silent"] = True
            case.move(k["id"], "dead end", f"the same finding as {twin['id']}, stated again as the graph grew")
        else:
            with case._lock:
                k.update({"key": key_of(fact), "source": fact["id"], "hint": fact["statement"],
                          "people": fact.get("pids") or [], "rests_on": [fact["id"]]})
                case._bump("lead", k["id"])
        moved += 1
    return moved


def run(case: Any, net: Any, llm: Any) -> dict[str, int]:
    made = 0
    with case.batch("S8 Lead spotter", "leads"):
        follow(case)
        new = hints(case)
        for start in range(0, len(new), PER_CALL):
            batch = new[start:start + PER_CALL]
            out = ask(llm, _Leads, dumps({
                "hints": [{"id": f["id"], "text": f["statement"], "tier": f.get("tier"), "rows": f.get("rows")} for f in batch],
                "hypotheses": [{"id": h["id"], "statement": h["statement"], "status": h["status"]}
                               for h in case.hypotheses.values() if not h.get("stale")],
                "the_new_case": the_new_case(case)}), system=_SYSTEM, kind="leads", version="v2", max_tokens=2000)
            written = {lead.hint: lead for lead in out.leads} if out is not None else {}
            for f in batch:
                lead = written.get(f["id"])
                hyp = lead.hypothesis if lead is not None and lead.hypothesis in case.hypotheses else None
                case.set_lead(key_of(f), {
                    "hint": f["statement"], "source": f["id"], "people": f.get("pids") or [],
                    "why": (lead.why if lead else "")[:300], "next_step": (lead.next_step if lead else "")[:300],
                    "hypothesis": hyp, "bears": lead.bears if lead and hyp else "unclear",
                    "question": (lead.question if lead else "")[:300],
                    # Described by the model: open. Only spotted in code: waiting for its description.
                    "status": "open" if lead else "spotted", "rests_on": [f["id"]]})
                made += 1
        # A lead whose hint was corrected away is a dead end, with the reason.
        for k in case.leads.values():
            if k["status"] not in ("confirmed", "dead end") and (case.facts.get(k["source"]) or {}).get("kind") in NOT_A_LEAD:
                k["silent"] = True                       # closed by rule: nothing to tell the officer
                case.move(k["id"], "dead end", "common, not a lead by itself")
            if k["status"] not in ("confirmed", "dead end") and not case.usable(k["source"]):
                case.move(k["id"], "dead end", "the entry it rested on was replaced by a correction")
        moved = outcomes(case, llm)
        described = redescribe(case, llm)
    return {"new": made, "moved": moved, "described": described,
            "open": sum(1 for k in case.leads.values() if k["status"] in ("spotted", "open", "pursuing"))}


def redescribe(case: Any, llm: Any) -> int:
    """Leads spotted while the model was away get their description on a later run, and
    leads found before Sherlock had any hypothesis are tied to one once he has."""
    hyps = sorted(h["id"] for h in case.hypotheses.values() if not h.get("stale"))
    waiting = [k for k in case.leads.values() if k["status"] == "spotted"
               or (k["status"] == "open" and not k.get("hypothesis") and hyps and k.get("weighed") != hyps)][:PER_CALL]
    if not waiting or llm is None:
        return 0
    out = ask(llm, _Leads, dumps({
        "hints": [{"id": k["source"], "text": k["hint"]} for k in waiting],
        "hypotheses": [{"id": h["id"], "statement": h["statement"], "status": h["status"]}
                       for h in case.hypotheses.values() if not h.get("stale")],
        "the_new_case": the_new_case(case)}), system=_SYSTEM, kind="leads", version="v2", max_tokens=2000)
    if out is None:
        return 0
    by = {lead.hint: lead for lead in out.leads}
    n = 0
    for k in waiting:
        lead = by.get(k["source"])
        k["weighed"] = hyps
        if lead is None:
            continue
        with case._lock:
            k.update({"why": lead.why[:300], "next_step": lead.next_step[:300], "question": lead.question[:300],
                      "hypothesis": lead.hypothesis if lead.hypothesis in case.hypotheses else None, "bears": lead.bears})
            case._bump("lead", k["id"])
        if k["status"] == "spotted":
            case.move(k["id"], "open", "described")
        n += 1
    return n


def outcomes(case: Any, llm: Any) -> int:
    """A lead whose question was asked is being pursued; an answer confirms it or ends it."""
    moved = 0
    for k in case.leads.values():
        q = next((q for q in case.questions if q["key"] == f"source:{k['id']}"), None)
        if q is None or k["status"] in ("confirmed", "dead end"):
            continue
        if q["status"] == "open" and q.get("times", 0) >= 1 and k["status"] != "pursuing":
            case.move(k["id"], "pursuing", f"asked the officer ({q['id']})")
            moved += 1
        elif q["status"] == "answered" and k.get("answer_read") != q.get("answered_at"):
            out = ask(llm, _Outcome, dumps({"lead": k["hint"], "why": k["why"], "question": q["text"],
                                            "officer_answer": q["answer"]}), system=_OUTCOME_SYSTEM, kind="lead_outcome")
            if out is None:
                continue
            k["answer_read"] = q.get("answered_at")
            case.move(k["id"], out.status, f"{q['id']}: {out.why or q['answer']}")
            moved += 1
        elif q["status"] == "dont_know" and k["status"] != "pursuing":
            case.move(k["id"], "pursuing", f"the officer does not know ({q['id']}): needs the record")
            moved += 1
    return moved
