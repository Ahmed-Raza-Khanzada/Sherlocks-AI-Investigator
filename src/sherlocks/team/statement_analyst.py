"""S7 Statement analyst: each person's recorded statements, in order.

For every person with a recorded statement: the versions in date order, what changed
between them, where an account does not fit the other evidence, and how the speaker is
related to the parties (from the graph). The model compares the versions; code keeps only
findings that point at real entries.

S7 says a statement *changed*, never that someone lied: a change can have an innocent
cause, so it becomes a question for the officer.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.linkgraph.normalize import name_key
from sherlocks.team.runtime import Typed, ask, dumps


class _Version(Typed):
    statement: str = Field(description="The statement id (W#).")
    key_points: list[str] = Field(default_factory=list, description="What it says, 2-5 short points.")


class _Change(Typed):
    earlier: str = Field(description="The earlier statement id (W#).")
    later: str = Field(description="The later statement id (W#).")
    what: str = Field(description="What changed, plainly: a name added or dropped, a time moved, a detail gone.")
    question: str = Field(default="", description="A neutral question for the officer about the change; empty if none.")


class _Clash(Typed):
    statement: str = Field(description="The statement id (W#).")
    against: str = Field(description="The id of the other entry it does not fit (a claim's source: D#, F#, W#).")
    what: str


class _Analysis(Typed):
    summary: str = Field(description="Two sentences: how many versions, what moved, what does not fit. An inference.")
    versions: list[_Version] = Field(default_factory=list)
    changes: list[_Change] = Field(default_factory=list)
    clashes: list[_Clash] = Field(default_factory=list)


_SYSTEM = ("You are the statement analyst of a police investigation team. You are given ONE person's recorded "
           "statements in date order, and what the other evidence says about them. Report what changed between the "
           "versions and where an account does not fit the other evidence. Say that a statement CHANGED or DOES NOT "
           "FIT - never that the person lied or is guilty: a change can have an innocent cause. Use only the ids "
           "given. " + DATA_NOT_INSTRUCTIONS)


def _speakers(case: Any) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for w in case.statements.values():
        out.setdefault(w.get("pid") or name_key(w["speaker"]), []).append(w)
    return out


def _relations(case: Any, net: Any, pid: str | None) -> list[str]:
    """How the speaker is linked to the people with a role on the case (from the graph)."""
    if pid is None or pid not in net.G:
        return []
    out = []
    for other, role in case.roles.items():
        if other != pid and other in net.G:
            for link in net.link_between(pid, other)[:2]:
                out.append(f"{net.name(pid)} - {net.name(other)} ({role}): {link.get('relation') or link.get('kind')}")
    return out[:6]


def run(case: Any, net: Any, llm: Any) -> dict[str, int]:
    done = changes = 0
    for who, rows in _speakers(case).items():
        rows.sort(key=lambda w: (w.get("date") or "9999", w["id"]))
        sig = "|".join(w["id"] for w in rows) + f"|{sum(1 for c in case.claims.values() if c.get('about_pid') == who)}"
        if (case.analysis.get(who) or {}).get("sig") == sig:
            continue
        pid = who if who in net.people else None
        others = [{"source": c["source"], "said_by": c["said_by"], "topic": c["topic"], "value": c["value"],
                   "when": c.get("when_from"), "where": c.get("where")}
                  for c in case.claims.values() if not c["source"].startswith("W")
                  and (c.get("about_pid") == pid if pid else name_key(c["about"]) == who)][:40]
        out = ask(llm, _Analysis, dumps({
            "person": rows[0]["speaker"], "role": rows[0]["role"],
            "statements": [{"id": w["id"], "kind": w["kind"], "date": w.get("date"), "text": w["text"][:2500]} for w in rows],
            "other_evidence_about_them": others}), system=_SYSTEM, kind="statement_analysis", max_tokens=1600)
        if out is None:
            continue
        ids = {w["id"] for w in rows}
        known = case.known_ids()
        kept = [c.model_dump() for c in out.changes if c.earlier in ids and c.later in ids]
        with case.acting("S7 Statement analyst"):
            case.set_analysis(who, {
                "sig": sig, "pid": pid, "name": rows[0]["speaker"], "role": rows[0]["role"], "summary": out.summary[:600],
                "tier": "inference", "statements": [w["id"] for w in rows],
                "versions": [v.model_dump() for v in out.versions if v.statement in ids],
                "changes": kept,
                "clashes": [c.model_dump() for c in out.clashes if c.statement in ids and c.against in known],
                "relations": _relations(case, net, pid)})
        done += 1
        changes += len(kept)
    return {"people": done, "changes": changes}
