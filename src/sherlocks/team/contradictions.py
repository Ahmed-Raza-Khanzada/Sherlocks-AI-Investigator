"""S6 Contradiction finder.

Code compares claims about the same person and topic from different sources, and flags
the pairs that may disagree; the model judges only those pairs - it never reads the whole
board. What code settles alone: two times for the same event that differ by more than
their tolerance (a contradiction), or by less (not one).

Each finding is an ``X#``: both sides quoted with their sources and trust, a severity, a
status (open, explained, resolved) and what would settle it. Only claims about people
A5 marks as the *same* person are compared - a name-only maybe never makes one.
"""

from __future__ import annotations

from itertools import combinations
from typing import Any, Literal

from pydantic import Field

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.team.claims import when
from sherlocks.team.runtime import Typed, ask, dumps

TOLERANCE_MIN = 30
PAIRS_PER_CALL = 8
CALLS_PER_RUN = 6
# Topics that are checked against each other as well as against themselves.
CROSS = {frozenset(("weapon", "injury")), frozenset(("presence", "place")), frozenset(("presence", "phone")),
         frozenset(("place", "phone")), frozenset(("address", "place"))}
# How bad a contradiction on a topic is when nobody judged it finer (the model may lower it).
SEVERITY = {"presence": "critical", "weapon": "critical", "injury": "critical", "identity": "critical",
            "time": "major", "role": "major", "place": "major"}


class _Verdict(Typed):
    pair: int = Field(description="The number of the pair judged.")
    contradiction: bool = Field(description="True only when both cannot be true together.")
    severity: Literal["critical", "major", "minor"] = Field(
        default="major", description="critical: goes to the heart of the case (presence, weapon, injury, identity); "
                                     "major: timeline or role; minor: spelling, rounding, wording.")
    text: str = Field(default="", description="One sentence in English stating both sides, no judgment of anyone.")
    settle: str = Field(default="", description="What would settle it: a record to get, a person to ask.")
    question: str = Field(default="", description="A neutral question for the officer that would settle it; it asks "
                                                  "for a fact and never assumes anyone's guilt. Empty if none.")


class _Verdicts(Typed):
    verdicts: list[_Verdict] = Field(default_factory=list)


class _Settled(Typed):
    status: Literal["open", "explained", "resolved"] = Field(
        description="explained: the answer gives a reason both can stand; resolved: the answer shows which side is "
                    "right; open: the answer does not settle it.")
    why: str = ""


_SYSTEM = ("You are the contradiction finder of a police investigation team. You are given numbered PAIRS of claims "
           "about the same person, each with who said it and its source. For each pair say whether both can be true "
           "together. Different wording of the same thing, a rounded time, a spelling variant, or two things that "
           "simply add to each other are NOT a contradiction. A phone on a tower shows where the phone was, not the "
           "person: it conflicts with an account only when the account cannot fit it. A past record is background "
           "and contradicts nothing about this incident. Never say anyone lied. " + DATA_NOT_INSTRUCTIONS)

_SETTLE_SYSTEM = ("A contradiction between two sources was put to the investigating officer as a question. Read the "
                  "officer's answer and say whether it settles the contradiction. " + DATA_NOT_INSTRUCTIONS)


def _side(c: dict[str, Any]) -> dict[str, Any]:
    return {"claim": c["id"], "said_by": c["said_by"], "topic": c["topic"], "value": c["value"],
            "when": c.get("when_from"), "where": c.get("where"), "quote": c["quote"], "source": c["source"],
            "trust": c["trust"], "page": c.get("page")}


def _pair_key(a: dict[str, Any], b: dict[str, Any]) -> str:
    return "|".join(sorted((a["key"], b["key"])))


def _comparable(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a["source"] == b["source"] or a.get("background") or b.get("background"):
        return False
    if a.get("kind") == b.get("kind") == "system":
        return False                                  # two police systems: not for S6 to weigh against each other
    if a["topic"] != b["topic"] and frozenset((a["topic"], b["topic"])) not in CROSS:
        return False
    if {a["topic"], b["topic"]} == {"weapon", "injury"}:
        # The weapon one source names against the injury another found: about the incident,
        # whoever each sentence names (the model sees both names when it judges the pair).
        return True
    if a.get("about_pid") or b.get("about_pid"):
        # Only the same person, by identifier - a maybe never makes a contradiction.
        return a.get("about_pid") == b.get("about_pid") and a["about_state"] == b["about_state"] == "same"
    return not a["about"] and not b["about"]          # both about the incident itself


def _time_gap(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    """Minutes between two time claims on the same event; None when not comparable in code."""
    ta, tb = when(a.get("when_from")), when(b.get("when_from"))
    if a["topic"] != "time" or b["topic"] != "time" or ta is None or tb is None:
        return None
    if " ".join(a["occasion"].lower().split()) != " ".join(b["occasion"].lower().split()) or not a["occasion"]:
        return None
    if len(str(a["when_from"])) <= 10 or len(str(b["when_from"])) <= 10:      # a day only on one side
        return 0.0 if ta.date() == tb.date() else abs((ta.date() - tb.date()).days) * 1440.0
    return abs((ta - tb).total_seconds()) / 60.0


def candidates(case: Any) -> tuple[list[tuple[dict, dict, str]], list[tuple[dict, dict]]]:
    """``(settled in code, to judge)``: pairs that code alone calls a contradiction, and
    pairs the model must look at. Pairs already judged are skipped."""
    from sherlocks.team.claims import COMPARED

    claims = [c for c in case.claims.values() if c["topic"] in COMPARED]
    sure: list[tuple[dict, dict, str]] = []
    maybe: list[tuple[dict, dict]] = []
    # Only claims about the same person (or the incident itself) are ever paired.
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in claims:
        if c.get("about_pid") or not c["about"]:
            groups.setdefault(c.get("about_pid") or "", []).append(c)
    pairs = [pair for group in groups.values() for pair in combinations(group, 2)]
    seen = {_pair_key(a, b) for a, b in pairs}
    pairs += [(w, i) for w in claims if w["topic"] == "weapon" for i in claims if i["topic"] == "injury"
              and _pair_key(w, i) not in seen]
    for a, b in pairs:
        if not _comparable(a, b) or f"pair:{_pair_key(a, b)}" in case.claimed:
            continue
        gap = _time_gap(a, b)
        slack = max(TOLERANCE_MIN, int(a.get("tolerance_min") or 0), int(b.get("tolerance_min") or 0))
        if gap is not None:
            if gap > slack:
                sure.append((a, b, (f"{a['event'] or 'the time'}: {a['said_by']} gives {a['when_from']} [{a['source']}]; "
                                   f"{b['said_by']} gives {b['when_from']} [{b['source']}]")))
            else:
                case.claimed[f"pair:{_pair_key(a, b)}"] = case.version       # inside the tolerance: not one
            continue
        if " ".join(a["value"].lower().split()) == " ".join(b["value"].lower().split()) and a["topic"] == b["topic"]:
            continue                                                          # the same thing said twice
        maybe.append((a, b))
    # The most trusted and most serious first: the run's model calls are limited.
    maybe.sort(key=lambda p: (0 if SEVERITY.get(p[0]["topic"]) == "critical" else 1, p[0]["trust"] + p[1]["trust"]))
    return sure, maybe


def _write(case: Any, net: Any, a: dict[str, Any], b: dict[str, Any], *, severity: str, text: str, settle: str = "",
           question: str = "", by: str) -> str:
    lead, other = sorted((a, b), key=lambda c: c["trust"])
    pid = a.get("about_pid") or b.get("about_pid")
    return case.set_contradiction(_pair_key(a, b), {
        "topic": a["topic"] if a["topic"] == b["topic"] else f"{a['topic']} / {b['topic']}",
        "person": pid, "person_name": net.name(pid) if pid else (a["about"] or b["about"] or None),
        "a": _side(lead), "b": _side(other), "severity": severity, "text": text[:500], "settle": settle[:300],
        "question": question[:300], "by": by, "rests_on": [lead["source"], other["source"]]})


def travel(case: Any, net: Any) -> int:
    """Impossible travel the CDR team measured (distance / time between two towers): one
    phone cannot be at both, so it is a contradiction inside the record - settled in code."""
    made = 0
    for f in case.live_facts():
        if f.get("kind") != "telecom_impossible_travel" or any(x["key"] == f"travel:{f['id']}" for x in case.contradictions.values()):
            continue
        doc = case.documents.get(f["doc"]) or {}
        pid = next((p for p in doc.get("owners") or [] if p in net.people), None)
        first, _, second = f["statement"].removeprefix("Impossible travel: ").partition(", then ")
        side = {"claim": None, "said_by": doc.get("title") or "CDR", "topic": "presence", "when": None, "where": "",
                "source": f["id"], "trust": f.get("trust") or 1, "page": None, "quote": f["statement"]}
        case.set_contradiction(f"travel:{f['id']}", {
            "topic": "presence", "person": pid, "person_name": net.name(pid) if pid else None, "severity": "major",
            "a": {**side, "value": first}, "b": {**side, "value": second.split(" - ")[0]},
            "text": f["statement"] + f" [{f['id']}" + (f", rows {f['rows']}" if f.get("rows") else "") + "]",
            "settle": "Check both tower locations with the operator: a wrong site record or a second SIM would explain it.",
            "question": "", "by": "code", "rests_on": [f["id"]]})
        made += 1
    return made


def run(case: Any, net: Any, llm: Any) -> dict[str, int]:
    sure, maybe = candidates(case)
    found = 0
    with case.batch("S6 Contradiction finder", "contradictions"):
        found += travel(case, net)
        for a, b, text in sure:
            _write(case, net, a, b, severity=SEVERITY.get(a["topic"], "major"), text=text, by="code")
            case.claimed[f"pair:{_pair_key(a, b)}"] = case.version
            found += 1
        judged = 0
        for start in range(0, len(maybe), PAIRS_PER_CALL):
            if llm is None or start // PAIRS_PER_CALL >= CALLS_PER_RUN:
                break
            batch = maybe[start:start + PAIRS_PER_CALL]
            out = ask(llm, _Verdicts, dumps({"pairs": [
                {"pair": i + 1, "about": a["about"] or "the incident",
                 "first": {k: a.get(k) for k in ("said_by", "topic", "occasion", "value", "when_from", "where", "quote", "source")},
                 "second": {k: b.get(k) for k in ("said_by", "topic", "occasion", "value", "when_from", "where", "quote", "source")}}
                for i, (a, b) in enumerate(batch)]}), system=_SYSTEM, kind="contradictions", max_tokens=1600)
            if out is None:
                continue
            verdicts = {v.pair: v for v in out.verdicts}
            for i, (a, b) in enumerate(batch, 1):
                v = verdicts.get(i)
                case.claimed[f"pair:{_pair_key(a, b)}"] = case.version
                judged += 1
                if v is not None and v.contradiction:
                    _write(case, net, a, b, severity=v.severity, text=v.text or f"{a['value']} [{a['source']}] against "
                                                                                 f"{b['value']} [{b['source']}]",
                           settle=v.settle, question=v.question, by="model")
                    found += 1
        settled = settle(case, llm)
    return {"found": found, "judged": judged, "waiting": max(0, len(maybe) - judged), "settled": settled,
            "open": sum(1 for x in case.contradictions.values() if x["status"] == "open")}


def settle(case: Any, llm: Any) -> int:
    """An open contradiction whose question the officer answered: does the answer settle it?"""
    done = 0
    for x in case.contradictions.values():
        q = next((q for q in case.questions if q["key"] == f"source:{x['id']}" and q["status"] == "answered"), None)
        if x["status"] != "open" or q is None or x.get("answer_read") == q.get("answered_at"):
            continue
        out = ask(llm, _Settled, dumps({"contradiction": x["text"], "first": x["a"], "second": x["b"],
                                        "question": q["text"], "officer_answer": q["answer"]}),
                  system=_SETTLE_SYSTEM, kind="contradiction_settled")
        if out is None:
            continue
        x["answer_read"] = q.get("answered_at")
        if out.status != "open":
            case.move(x["id"], out.status, f"{q['id']}: {out.why or q['answer']}")
            done += 1
    return done


def about(case: Any, pids: list[str] | None = None, *, only_open: bool = True) -> list[dict[str, Any]]:
    order = {"critical": 0, "major": 1, "minor": 2}
    found = [x for x in case.contradictions.values() if (not only_open or x["status"] == "open")
             and (not pids or x.get("person") in pids or x.get("person") is None)]
    return sorted(found, key=lambda x: (order.get(x["severity"], 3), x["id"]))
