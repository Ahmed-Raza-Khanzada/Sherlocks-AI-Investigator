"""Q3 Insight desk: which of Sherlock's findings the officer hears, and when.

It does for findings what the Gatekeeper does for questions:

* **what can be passed on** - a critical or major contradiction, a lead that was
  confirmed or ruled out (or that bears on a hypothesis), a hypothesis whose status
  changed, a Skeptic's challenge S1 accepted against the leader, the result of a
  background investigation the officer was waiting for;
* **ranked** - critical first, then findings about the person being discussed, then newest;
* **told once** - the ledger (``case.insights``) keeps every finding told, by its id and
  state; a finding that changes (X3 resolved) can be told once more;
* **one extra item per reply**, after the answer, in its own labelled block; a critical
  finding goes before the Questioner's question, which waits a turn;
* the officer can pause it ("Sherlock ki baatein baad mein") - the report holds all of
  them either way.

Code only: every finding was already written and judged by the agent that made it.
"""

from __future__ import annotations

from typing import Any

LABEL = {"en": "Sherlock's new point (not confirmed)", "roman": "Sherlock ki nayi baat (tasdeeq nahi)",
         "ur": "شرلاک کی نئی بات (تصدیق شدہ نہیں)"}
SEVERITY = {"critical": 0, "major": 1, "minor": 2}


def candidates(case: Any) -> list[dict[str, Any]]:
    """Everything worth telling, with the state it is in now."""
    out: list[dict[str, Any]] = []
    for x in case.contradictions.values():
        if x["severity"] in ("critical", "major"):
            out.append({"ref": x["id"], "state": x["status"], "severity": x["severity"], "people": [x.get("person")],
                        "at": x.get("updated_at") or x["at"], "kind": "contradiction",
                        "text": x["text"] + (f" What would settle it: {x['settle']}" if x.get("settle") and x["status"] == "open" else "")
                                + (f" ({x['status']}: {x.get('reason')})" if x["status"] != "open" else "")})
    for k in case.leads.values():
        if k.get("silent"):
            continue
        if k["status"] in ("confirmed", "dead end") or (k["status"] in ("open", "pursuing") and k.get("hypothesis")):
            out.append({"ref": k["id"], "state": k["status"], "severity": "major", "people": k.get("people") or [],
                        "at": k.get("updated_at") or k["at"], "kind": "lead",
                        "text": f"{k['hint']} {k.get('why') or ''}".strip()
                                + (f" ({k['status']}: {k.get('reason')})" if k["status"] in ("confirmed", "dead end") else "")})
    for h in case.history[-30:]:
        hyp = case.hypotheses.get(h["id"])
        moved = str(h.get("before") or "").split(" (")[0] != str(h.get("after") or "").split(" (")[0]
        if hyp is not None and h["change"] == "updated" and moved and not hyp.get("stale"):
            out.append({"ref": h["id"], "state": str(h["after"]), "severity": "major", "people": hyp.get("people") or [],
                        "at": h["at"], "kind": "hypothesis",
                        "text": f"{hyp['statement']}: {h['before']} -> {h['after']}" + (f" ({h['cause']})" if h.get("cause") else "")})
    for a in case.dialog.get("asked_of_sherlock") or []:
        if a.get("answer"):                      # a background investigation the officer was waiting for
            out.append({"ref": f"ASK{a.get('turn')}", "state": "answered", "severity": "major", "people": [],
                        "at": "", "kind": "investigation",
                        "text": f"On your question \"{a.get('question')}\": {a['answer']}"})
    leader = (case.matrix or {}).get("leader")
    for c in case.challenges:
        if c.get("verdict") == "accepted" and c.get("hypothesis") == leader:
            out.append({"ref": f"S9:{c['kind']}:{c['hypothesis']}", "state": c["text"][:60], "severity": "major",
                        "people": (case.hypotheses.get(leader) or {}).get("people") or [], "at": c.get("at") or "",
                        "kind": "challenge", "text": f"Against {leader}: {c['text']} Sherlock accepts it: {c.get('note') or ''}"})
    return out


def waiting(case: Any, focus: list[str] | None = None) -> list[dict[str, Any]]:
    """Findings not told yet in their current state, the one to tell first."""
    told = {(t["ref"], t.get("state") or "") for t in case.insights}
    focus = focus or []
    fresh = [c for c in candidates(case) if (c["ref"], c["state"]) not in told]
    newest = sorted(fresh, key=lambda c: c["at"], reverse=True)
    return sorted(newest, key=lambda c: (SEVERITY.get(c["severity"], 3), 0 if set(c["people"]) & set(focus) else 1))


def paused(case: Any) -> bool:
    return bool(case.dialog.get("insights_paused"))


def pause(case: Any, on: bool) -> None:
    case.dialog["insights_paused"] = bool(on)


def pick(case: Any, focus: list[str] | None = None) -> dict[str, Any] | None:
    """The one finding this reply may carry, or None (paused, or nothing new)."""
    if paused(case):
        return None
    queue = waiting(case, focus)
    return queue[0] if queue else None


def told(case: Any, finding: dict[str, Any], turn: int) -> None:
    with case.acting("Q3 Insight desk"):
        case.tell(finding["ref"], finding["text"], turn=turn, state=finding["state"])


def critical_waiting(case: Any) -> list[dict[str, Any]]:
    """For the case board panel's badge: critical findings the officer has not seen."""
    return [c for c in waiting(case) if c["severity"] == "critical"]
