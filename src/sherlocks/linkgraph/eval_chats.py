"""The chat test set: fixed conversations in English, Roman Urdu and Urdu, scored.

Every change to the agents is measured on the same chats (``scripts/eval_chats.py``
prints the scorecard; the test suite holds it to its thresholds):

* **answer first** - a count question opens with the count;
* **sources real** - every id a reply cites is on the board (or a known system);
* **no repeats** - no question asked twice in a row, none more than twice, none already
  answered;
* **corrections** - a corrected value replaces the old one;
* **reply time** - each turn's time (templates: well under a second).

Runs on the demo world, without the model, so it is the same every time.
"""

from __future__ import annotations

import itertools
import re
import time
from typing import Any

CHATS: dict[str, list[str]] = {
    "roman": ["Kamran Ahmed", "ye kitni firs ma involve ha ?", "uske saathiyon mein kitne mujrim hain?",
              "Kamran main suspect hai. Waqia 01-03-2023 ko hua", "nahi, waqia 02-03-2023 ko hua tha",
              "pata nahi", "Kamran aur Sajid ka kya taluq hai?"],
    "en": ["How many FIRs is Kamran Ahmed in?", "total criminals in the whole graph?",
           "The incident happened near University Road Gulshan", "what is new?", "ok"],
    "ur": ["کامران کتنی ایف آئی آر میں ہے؟", "کامران مرکزی ملزم ہے", "نہیں معلوم"],
}
_SYSTEMS = {"CRO", "PSRMS", "NADRA", "SIMs", "Labs", "SAFE", "PRVS", "officer"}


def _demo() -> tuple[dict[str, Any], Any]:
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.settings import load_settings

    s = load_settings()
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = False
    s.evidence.auto_report = False
    if hasattr(s, "chat"):
        s.chat.reply_budget_s = 60.0
    handle = memory_manager(s).start(GraphRunParams(cnic="9999900000011", depth=2, max_persons=15, backend="demo"),
                                     wait=True)
    return handle.graph(), handle.case


def run_chat(messages: list[str]) -> dict[str, Any]:
    from sherlocks.linkgraph.sherlock_team import run_turn

    graph, case = _demo()
    turns = []
    for message in messages:
        started = time.monotonic()
        final = list(run_turn(graph, message, case=case))[-1]
        turns.append({"message": message, "final": final, "ms": int((time.monotonic() - started) * 1000)})
    return {"turns": turns, "case": case}


def score(chat: dict[str, Any]) -> dict[str, Any]:
    case = chat["case"]
    known = case.known_ids() if hasattr(case, "known_ids") else set(case.documents) | set(case.facts) | set(case.links)
    counting, opened = 0, 0
    bad_ids: list[str] = []
    for t in chat["turns"]:
        f = t["final"]
        if (f.get("query") or {}).get("type") in ("count_cases", "criminals_near", "criminals_all") and f.get("facts") \
                and not f["facts"].get("empty"):
            counting += 1
            first = re.split(r"(?<=[.!?:؟۔])\s|\n", f["answer"].replace("**", "").strip(), maxsplit=1)[0]
            opened += str(f["facts"]["count"]) in first
        for ref in re.findall(r"\[([A-Za-z]{1,8}\d{0,4})\]", f["answer"]):
            if re.fullmatch(r"[DFLHQ]\d+", ref) and ref not in known:
                bad_ids.append(ref)
    asked = [t["final"]["checks"].get("asked") for t in chat["turns"]]
    real = [a for a in asked if a]
    repeats = sum(1 for a, b in itertools.pairwise(asked) if a and a == b)
    over = sum(1 for k in set(real) if real.count(k) > 2)
    answered_again = sum(1 for i, a in enumerate(asked) if a and any(
        q["key"] == a and q["status"] in ("answered", "dont_know") and (q.get("answer_turn") or 99) <= i
        for q in case.questions))
    times = [t["ms"] for t in chat["turns"]]
    replaced = sum(1 for f in case.facts.values() if f.get("replaced_by"))     # corrections kept, not overwritten
    return {"turns": len(times), "answer_first": f"{opened}/{counting}", "answer_first_ok": opened == counting,
            "bad_ids": bad_ids, "repeat_in_a_row": repeats, "asked_over_twice": over,
            "asked_after_answered": answered_again, "corrections_kept": replaced,
            "ms_max": max(times), "ms_avg": int(sum(times) / len(times))}


def scorecard() -> dict[str, dict[str, Any]]:
    return {lang: score(run_chat(messages)) for lang, messages in CHATS.items()}
