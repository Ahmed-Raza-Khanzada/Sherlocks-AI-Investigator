"""S4 Questioner: where Sherlock's questions come from.

Five sources, each giving a different kind of question, and each question carries the id
it came from, so the officer's answer goes straight back to that entry:

* a contradiction ``X#`` - "the FIR says 1 March, you said 2 March: which is right?";
* a lead ``K#`` - "whose number is 0345-xxxx12?";
* a statement change (S7) - "why did the time move between his two statements?";
* a missing fact - a report asked for and not received (a *task*, asked once, then tracked);
* an identity ``E#`` (A5) - "is the Imran of FIR 391/26 this Imran?".

Ranked by impact: a critical contradiction first, then major ones, then leads that bear
on a hypothesis, statement changes, identity checks, and tasks last (what the officer
can answer from memory before what needs new paperwork). The model words each question
in the officer's language, neutrally - a question asks for a fact and never assumes
guilt. Every question still passes the Gatekeeper.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.team.runtime import NEW_CASE, PLAIN, Typed, ask, dumps

PER_RUN = 3
OPEN_AT_ONCE = 4        # questions from the casework waiting for the officer at any one time
LANG = {"en": "English", "roman": "Roman Urdu (Urdu in Latin letters)", "ur": "Urdu (Urdu script)"}


class _Worded(Typed):
    source: str = Field(description="The id of the entry the question comes from, exactly as given.")
    text: str = Field(description="The question, one sentence, in the officer's language.")
    task: bool = Field(default=False, description="True when it cannot be answered from memory: a record to fetch, "
                                                  "a person to examine.")


class _Wording(Typed):
    questions: list[_Worded] = Field(default_factory=list)


class _Identity(Typed):
    same: bool | None = Field(description="True: the officer says it is the same person; False: a different person; "
                                          "null: the answer does not say.")


_SYSTEM = ("You word the questions an investigation team puts to the investigating officer. For each ITEM write one "
           "short question in {lang} that would settle it. Ask for a FACT. Never assume anyone's guilt, never suggest "
           "the answer: 'Why was Imran's phone off that night?' is right; 'Why did Imran hide his phone?' is wrong. "
           "Name the people, the dates and the record numbers the item gives. Mark as a task anything the officer "
           "cannot answer from memory. " + NEW_CASE + PLAIN + DATA_NOT_INSTRUCTIONS)
_IDENTITY_SYSTEM = ("The investigating officer was asked whether two records name the same person. Read the answer. "
                    + DATA_NOT_INSTRUCTIONS)


def sources(case: Any, net: Any) -> list[dict[str, Any]]:
    """Every entry that calls for a question, most important first."""
    from sherlocks.team.entities import open_maybes

    asked = {q["key"] for q in case.questions}
    out: list[dict[str, Any]] = []

    def add(ref: str, rank: int, kind: str, about: str, draft: str, priority: str, task: bool = False) -> None:
        if f"source:{ref}" not in asked:
            out.append({"source": ref, "rank": rank, "kind": kind, "about": about[:400], "draft": draft[:300],
                        "priority": priority, "task": task})

    for x in case.contradictions.values():
        if x["status"] == "open" and x["severity"] != "minor":
            crit = x["severity"] == "critical"
            add(x["id"], 0 if crit else 1, "contradiction", x["text"] + (f" To settle: {x['settle']}" if x.get("settle") else ""),
                x.get("question") or "", "red" if crit else "orange")
    for k in case.leads.values():
        if k["status"] == "open" and not k.get("silent") and (k.get("question") or k.get("next_step")):
            add(k["id"], 2 if k.get("hypothesis") else 4, "lead", f"{k['hint']} {k.get('why') or ''} Next step: {k.get('next_step')}",
                k.get("question") or "", "orange" if k.get("hypothesis") else "green")
    for a in case.analysis.values():
        for i, ch in enumerate(a.get("changes") or []):
            add(f"{ch['later']}~{i}", 3, "statement change", f"{a['name']}: between {ch['earlier']} and {ch['later']}: {ch['what']}",
                ch.get("question") or "", "orange")
    for e in open_maybes(case):
        add(e["id"], 3, "identity", f"'{e['name']}'" + (f" s/o {e['father']}" if e.get("father") else "")
            + f" in {', '.join(e['sources'][:3])}: the same person as {e.get('name_on_graph')} on the case?", "", "orange")
    for p in case.register.get("pending") or []:
        add(f"pending:{p['record']}", 5, "pending report", f"{p['what']} ({p['record']}) was requested"
            + (f" on {p['requested_on']}" if p.get("requested_on") else "") + " and is not on record.", "", "green", task=True)
    return sorted(out, key=lambda s: (s["rank"], s["task"]))


def run(case: Any, graph: dict[str, Any], net: Any, llm: Any) -> dict[str, int]:
    """S4: the board's own gaps, Sherlock's questions, then the five sources - all through Q1."""
    from sherlocks.evidence.question_desk import Gatekeeper
    from sherlocks.linkgraph.casework import rank_gaps

    ranked = rank_gaps(case, graph)
    read = answers(case, net, llm)
    withdraw(case)
    waiting = sum(1 for q in case.open_questions() if q["key"].startswith("source:"))
    room = max(0, min(PER_RUN, OPEN_AT_ONCE - waiting))
    todo = sources(case, net)[:room * 2]
    admitted = 0
    if todo and llm is not None:
        language = case.dialog.get("language") or "en"
        out = ask(llm, _Wording, dumps({"items": [{k: s[k] for k in ("source", "kind", "about", "draft")} for s in todo]}),
                  system=_SYSTEM.format(lang=LANG.get(language, "English")), kind="questioner_wording", max_tokens=1500)
        worded = {w.source: w for w in out.questions} if out is not None else {}
        desk = Gatekeeper(case, graph, llm)
        with case.acting("S4 Questioner"):
            for s in todo:
                w = worded.get(s["source"])
                if w is None or not w.text.strip() or admitted >= room:
                    continue
                q = desk.admit({"key": f"source:{s['source']}", "text": w.text.strip(), "priority": s["priority"]},
                               by="S4 Questioner")
                if q is not None:
                    q["source"], q["task"], q["kind"] = s["source"], bool(s["task"] or w.task), s["kind"]
                    admitted += 1
                    ranked.append({"key": q["key"], "text": q["text"], "why": f"{s['kind']} {s['source']}",
                                   "source": s["source"], "task": q["task"]})
            case.set_gaps(sorted(ranked, key=lambda g: 1 if g.get("task") else 0))
    return {"questions": admitted, "waiting": max(0, len(todo) - admitted), "answers_read": read}


def withdraw(case: Any) -> int:
    """A question outlives its reason when the entry it came from is settled (the lead was a
    restatement, the identity is now known, the contradiction resolved): it is withdrawn, not
    left waiting for the officer. Of the rest, only the most important stay open."""
    from sherlocks.evidence.question_desk import source_open

    rank = {"red": 0, "orange": 1, "green": 2}
    gone = 0
    with case._lock:
        mine = [q for q in case.questions if q["status"] == "open" and q["key"].startswith("source:")]
        keep = sorted((q for q in mine if source_open(case, q.get("source") or q["key"].split(":", 1)[1])),
                      key=lambda q: (-q.get("times", 0), rank.get(q.get("priority"), 1)))[:OPEN_AT_ONCE]
        for q in mine:
            if not any(q is k for k in keep):
                q["status"], q["withdrawn"] = "withdrawn", "no longer needed"
                case._bump("question", q["id"])
                gone += 1
    return gone


def answers(case: Any, net: Any, llm: Any) -> int:
    """Answers that go back to the entry the question came from. Contradictions and leads
    read theirs themselves (S6, S8); identity checks and statement changes are read here."""
    from sherlocks.team.entities import decide

    done = 0
    for q in case.questions:
        ref = q.get("source") or ""
        if q["status"] != "answered" or q.get("answer_used"):
            continue
        if ref in case.entities:
            out = ask(llm, _Identity, dumps({"question": q["text"], "officer_answer": q["answer"]}),
                      system=_IDENTITY_SYSTEM, kind="identity_answer")
            if out is None:
                continue
            if out.same is not None:
                decide(case, ref, out.same)
            q["answer_used"] = True
            done += 1
        elif "~" in ref:                                  # a statement change: the officer's reason is kept with it
            w = ref.split("~", 1)[0]
            for a in case.analysis.values():
                if w in (a.get("statements") or []):
                    a.setdefault("notes", []).append({"question": q["text"], "answer": q["answer"], "ref": q["id"]})
            q["answer_used"] = True
            done += 1
    return done


def open_tasks(case: Any) -> list[dict[str, Any]]:
    """Things asked for that only paperwork can answer: asked once, then tracked here."""
    return [q for q in case.questions if q.get("task") and q["status"] == "open"]
