"""S1's hypothesis matrix and S9, the Skeptic.

Every hypothesis against every piece of evidence (Analysis of Competing Hypotheses):
Sherlock sets each cell - for, against or neutral - with a one-line reason and the
evidence id; code counts and ranks, by how much evidence argues AGAINST each hypothesis,
not by how much seems to fit.

Rules that are code, so no model can bend them:

* strong marks (``++`` / ``--``) only from trust 1-2 sources (a system record, an
  official document) - anything weaker is cut to ``+`` / ``-``;
* a past record (CRO, earlier FIRs) is background: its cells count for nothing;
* leads ``K#`` never go in the matrix - only evidence does;
* a hypothesis that rests on one piece of evidence stays *open*, whatever its rank.

After every S1 run the Skeptic tries to break the leading hypothesis and keeps an
alternative open; S1 answers each challenge once, in the same run (accept it, or rebut it
with an id) - S1 -> S9 -> S1, never a loop. Unanswered challenges go in the report.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.team.runtime import Typed, ask, dumps

MARKS = {"++": 2, "+": 1, "0": 0, "-": -1, "--": -2}
STRONG_TRUST = 2
ROWS = 20
NO_TRAITS = ("A person's sect, caste, ethnicity, religion or family name is never evidence of anything. A past record "
             "(earlier FIRs, a CRO entry) can raise a question; it never counts for or against a hypothesis about "
             "this incident. ")

Mark = Literal["++", "+", "0", "-", "--"]


class _Cell(Typed):
    evidence: str = Field(description="The evidence id, exactly as given.")
    hypothesis: str = Field(description="The hypothesis id (H#).")
    mark: Mark = Field(description="++ strongly for, + for, 0 neutral, - against, -- strongly against.")
    reason: str = Field(description="One line: why this evidence bears on this hypothesis that way.")


class _Cells(Typed):
    cells: list[_Cell] = Field(default_factory=list, description="Only the cells that are not neutral.")


class _Challenge(Typed):
    kind: Literal["missed evidence", "other explanation", "single source", "inference on inference", "past record",
                  "protected trait"]
    hypothesis: str = Field(description="The hypothesis id (H#) challenged.")
    text: str = Field(description="The challenge, one or two sentences.")
    evidence: list[str] = Field(default_factory=list, description="The evidence ids it rests on.")


class _Skeptic(Typed):
    challenges: list[_Challenge] = Field(default_factory=list)
    alternative: str = Field(default="", description="One other explanation that the evidence does not rule out and "
                                                     "that should stay open - an innocent one if there is one.")


class _Reply(Typed):
    challenge: int = Field(description="The number of the challenge answered.")
    verdict: Literal["accept", "rebut"]
    note: str = Field(description="Why. A rebuttal must name the evidence id that answers the challenge.")
    evidence: list[str] = Field(default_factory=list)
    cells: list[_Cell] = Field(default_factory=list, description="When accepting: the cells that change.")


class _Replies(Typed):
    replies: list[_Reply] = Field(default_factory=list)


_S1_SYSTEM = ("You are Sherlock, the lead detective, filling a hypothesis matrix. For each piece of EVIDENCE say how it "
              "bears on each HYPOTHESIS: for, against or neutral, with a one-line reason. Judge each cell on its own: "
              "evidence that fits every hypothesis equally is neutral for all of them. Look hardest for evidence "
              "AGAINST a hypothesis - that is what separates them. A phone on a tower is where the phone was, not "
              "the person. " + NO_TRAITS + DATA_NOT_INSTRUCTIONS)
_S9_SYSTEM = ("You are the Skeptic of a police investigation team. Your only job is to try to break the LEADING "
              "hypothesis: evidence against it that was marked neutral or missed; another explanation for each key "
              "fact; a conclusion that rests on a single source, or on an inference built on another inference; any "
              "use of a past record or of who a person is (sect, caste, ethnicity, religion, family name) as "
              "evidence. Then name one alternative explanation that the evidence does not rule out - an innocent one "
              "if there is one. Challenges, not verdicts: you never decide the case. Use only the ids given. "
              + DATA_NOT_INSTRUCTIONS)
_REPLY_SYSTEM = ("You are Sherlock, the lead detective. The Skeptic challenged your leading hypothesis. Answer every "
                 "challenge once: ACCEPT it (and give the cells of the matrix that change), or REBUT it by naming the "
                 "evidence id that answers it. A rebuttal with no evidence id is not an answer. " + NO_TRAITS
                 + DATA_NOT_INSTRUCTIONS)


def evidence_rows(case: Any) -> list[dict[str, Any]]:
    """What may go in the matrix: facts, recorded statements and open contradictions - the
    entries the hypotheses already cite first, then the most trusted. Never a lead."""
    cited = [i for h in case.hypotheses.values() for i in (h.get("for") or []) + (h.get("against") or [])]
    lead_sources = {k["source"] for k in case.leads.values()}
    rows: dict[str, dict[str, Any]] = {}

    def add(ref: str, text: str, trust: int, background: bool = False, source: str = "") -> None:
        if ref not in rows and len(rows) < ROWS:
            rows[ref] = {"id": ref, "text": text[:260], "trust": trust, "background": background, "source": source or ref}

    def fact(f: dict[str, Any]) -> None:
        doc = case.documents.get(f["doc"]) or {}
        add(f["id"], f["statement"], int(f.get("trust") or 5), doc.get("kind") == "cro", f["doc"])

    for ref in dict.fromkeys(cited):
        if ref in case.facts and case.usable(ref):
            fact(case.facts[ref])
    for x in case.contradictions.values():
        if x["status"] == "open" and x["severity"] != "minor":
            add(x["id"], x["text"], min(x["a"]["trust"], x["b"]["trust"]))
    facts = [f for f in case.live_facts() if f.get("tier") == "fact" and f["id"] not in lead_sources
             and f.get("kind") not in ("telecom_habit",)]
    for f in sorted(facts, key=lambda f: (int(f.get("trust") or 5), f["id"])):
        fact(f)
    for w in case.statements.values():
        add(w["id"], f"{w['speaker']} ({w['role']}, {w['kind']}): {w['text']}", 4, source=w["doc"])
    return list(rows.values())


def rank(case: Any, hyps: list[dict[str, Any]], rows: list[dict[str, Any]], cells: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts and ranking, in code. Returns the matrix as stored on the board."""
    by_id = {r["id"]: r for r in rows}
    kept, seen = [], set()
    for c in cells:
        row = by_id.get(c["evidence"])
        if row is None or c["hypothesis"] not in {h["id"] for h in hyps} or (c["evidence"], c["hypothesis"]) in seen:
            continue
        seen.add((c["evidence"], c["hypothesis"]))
        mark, note = c["mark"], ""
        if row["background"] and mark != "0":
            mark, note = "0", "a past record is background, not evidence for this incident"
        elif row["trust"] > STRONG_TRUST and mark in ("++", "--"):
            mark, note = mark[0], "cut to a weak mark: the source is not a system record or an official document"
        kept.append({"evidence": c["evidence"], "hypothesis": c["hypothesis"], "mark": mark,
                     "reason": c.get("reason", "")[:240], "rule": note or c.get("rule", "")})
    score: dict[str, dict[str, Any]] = {}
    for h in hyps:
        mine = [c for c in kept if c["hypothesis"] == h["id"]]
        support = [c for c in mine if MARKS[c["mark"]] > 0]
        score[h["id"]] = {"against": sum(-MARKS[c["mark"]] for c in mine if MARKS[c["mark"]] < 0),
                          "for": sum(MARKS[c["mark"]] for c in support), "support_count": len(support),
                          "sources": sorted({by_id[c["evidence"]]["source"] for c in support})}
    ranking = sorted(score, key=lambda h: (score[h]["against"], -score[h]["for"], h))
    return {"hypotheses": [{"id": h["id"], "statement": h["statement"], "status": h["status"]} for h in hyps],
            "evidence": rows, "cells": kept, "score": score, "ranking": ranking,
            "leader": ranking[0] if ranking else None}


def build(case: Any, llm: Any) -> dict[str, Any] | None:
    """S1 sets the cells; code ranks. None when there is nothing to weigh yet."""
    hyps = [h for h in case.hypotheses.values() if not h.get("stale") and h["status"] != "ruled out"]
    rows = evidence_rows(case)
    if not hyps or not rows:
        return None
    out = ask(llm, _Cells, dumps({
        "hypotheses": [{"id": h["id"], "statement": h["statement"]} for h in hyps],
        "evidence": [{"id": r["id"], "text": r["text"], "trust": r["trust"],
                      "kind": "past record (background)" if r["background"] else "evidence"} for r in rows]}),
        system=_S1_SYSTEM, kind="hypothesis_matrix", max_tokens=1800)
    if out is None:
        return None                                  # no model: the matrix waits; nothing is guessed
    matrix = rank(case, hyps, rows, [c.model_dump() for c in out.cells])
    _store(case, matrix)
    return matrix


def _store(case: Any, matrix: dict[str, Any]) -> None:
    with case.acting("S1 Sherlock"):
        case.set_matrix(matrix)
        for hid, s in matrix["score"].items():
            h = case.hypotheses.get(hid)
            # One piece of evidence is not enough to call a hypothesis supported.
            if h is not None and h["status"] == "supported" and s["support_count"] <= 1:
                case.set_hypothesis(h["statement"], status="open", confidence="low", tier=h["tier"], hid=hid,
                                    cause="rests on one piece of evidence")


def _structural(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    """Challenges code can raise without reading anything: one source, or none."""
    leader = matrix.get("leader")
    s = (matrix.get("score") or {}).get(leader) or {}
    out = []
    if leader and s.get("support_count", 0) >= 1 and len(s.get("sources") or []) == 1:
        out.append({"kind": "single source", "hypothesis": leader, "by": "code",
                    "text": f"Everything for {leader} comes from one source ({s['sources'][0]}).",
                    "evidence": [c["evidence"] for c in matrix["cells"] if c["hypothesis"] == leader and MARKS[c["mark"]] > 0]})
    for c in matrix["cells"]:
        if c.get("rule", "").startswith("a past record"):
            out.append({"kind": "past record", "hypothesis": c["hypothesis"], "by": "code", "evidence": [c["evidence"]],
                        "text": f"{c['evidence']} is a past record: it was marked for or against {c['hypothesis']} and "
                                "counts for nothing."})
    return out


def challenge(case: Any, llm: Any) -> dict[str, int]:
    """S9, then S1's replies - once."""
    matrix = case.matrix
    if not matrix or not matrix.get("leader"):
        return {"challenges": 0, "accepted": 0, "unanswered": 0}
    found = _structural(matrix)
    ids = {r["id"] for r in matrix["evidence"]}
    hyp_ids = {h["id"] for h in matrix["hypotheses"]}
    out = ask(llm, _Skeptic, dumps({"leading": matrix["leader"], "hypotheses": matrix["hypotheses"],
                                    "evidence": [{k: r[k] for k in ("id", "text", "trust")} for r in matrix["evidence"]],
                                    "cells": matrix["cells"], "score": matrix["score"]}),
              system=_S9_SYSTEM, kind="skeptic", max_tokens=1300)
    alternative = ""
    if out is not None:
        alternative = out.alternative.strip()
        for c in out.challenges[:8]:
            if c.hypothesis in hyp_ids:
                found.append({**c.model_dump(), "evidence": [e for e in c.evidence if e in ids], "by": "S9 Skeptic"})
    for i, c in enumerate(found, 1):
        c.update({"n": i, "verdict": "unanswered", "note": ""})
    cells = list(matrix["cells"])
    # Hard rules first: a trait or a past record never counts, whatever S1 would reply.
    barred = {e for c in found if c["kind"] in ("protected trait", "past record") for e in c["evidence"]}
    cells = [{**c, "mark": "0", "rule": "not evidence (the Skeptic's rule)"} if c["evidence"] in barred
             and c["mark"] != "0" else c for c in cells]
    replies = ask(llm, _Replies, dumps({"challenges": [{k: c[k] for k in ("n", "kind", "hypothesis", "text", "evidence")}
                                                       for c in found],
                                        "hypotheses": matrix["hypotheses"], "cells": cells,
                                        "evidence": [{k: r[k] for k in ("id", "text", "trust")} for r in matrix["evidence"]]}),
                  system=_REPLY_SYSTEM, kind="skeptic_reply", max_tokens=1500) if found else None
    known = case.known_ids()
    for r in (replies.replies if replies is not None else []):
        c = next((c for c in found if c["n"] == r.challenge), None)
        if c is None:
            continue
        cited = [e for e in r.evidence if e in known]
        if r.verdict == "rebut" and not cited:
            continue                                 # no id: not an answer - stays unanswered
        c.update({"verdict": "accepted" if r.verdict == "accept" else "rebutted", "note": r.note[:400], "cited": cited})
        if r.verdict == "accept":
            changed = {(x.evidence, x.hypothesis): x for x in r.cells}
            cells = [{**cell, "mark": changed[(cell["evidence"], cell["hypothesis"])].mark,
                      "reason": changed[(cell["evidence"], cell["hypothesis"])].reason[:240], "rule": "changed after S9"}
                     if (cell["evidence"], cell["hypothesis"]) in changed else cell for cell in cells]
            cells += [x.model_dump() for key, x in changed.items()
                      if key not in {(cell["evidence"], cell["hypothesis"]) for cell in cells}]
    hyps = [h for h in case.hypotheses.values() if h["id"] in hyp_ids]
    with case.acting("S9 Skeptic"):
        case.set_challenges(found)
        if alternative and sum(1 for h in case.hypotheses.values() if h["status"] == "open" and not h.get("stale")) < 1:
            # Keep an alternative open until the evidence rules it out.
            case.set_hypothesis(alternative, status="open", tier="speculation", confidence="low",
                                cause="kept open by the Skeptic")
    _store(case, rank(case, hyps, matrix["evidence"], cells))
    return {"challenges": len(found), "accepted": sum(1 for c in found if c["verdict"] == "accepted"),
            "unanswered": sum(1 for c in found if c["verdict"] == "unanswered")}


def table(case: Any) -> dict[str, Any] | None:
    """The matrix for the board panel, the report and the toolbox: one row per evidence."""
    m = case.matrix
    if not m:
        return None
    cell = {(c["evidence"], c["hypothesis"]): c for c in m["cells"]}
    return {"hypotheses": m["hypotheses"], "ranking": m["ranking"], "leader": m["leader"], "score": m["score"],
            "rows": [{"id": r["id"], "text": r["text"], "trust": r["trust"],
                      "marks": {h["id"]: (cell.get((r["id"], h["id"])) or {}).get("mark", "0") for h in m["hypotheses"]},
                      "reasons": {h["id"]: (cell.get((r["id"], h["id"])) or {}).get("reason", "") for h in m["hypotheses"]
                                  if (r["id"], h["id"]) in cell}}
                     for r in m["evidence"] if any((r["id"], h["id"]) in cell for h in m["hypotheses"])],
            "challenges": case.challenges, "at": m.get("at"), "board_version": m.get("board_version")}
