"""Asking Sherlock in chat: a quick view, never a full run while the officer waits.

With the model, the Officer agent asks Sherlock on every question - a judgment (why, is
he involved, what next) or how the answer bears on the new case. He answers in **one model call,
with no tool rounds**, from his stored assessment and the context pack - within the view
budget. His view is kept on the board (``case.views``), so his next full assessment can
confirm or change it and the report can say which. In the reply it sits in its own
labelled block ("Sherlock ka andaza"), apart from the facts.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS

logger = logging.getLogger(__name__)


class _View(BaseModel):
    view: str = Field(description="Sherlock's view in 1-3 sentences, in the officer's language: what he thinks and "
                                  "why, citing the ids it rests on in [brackets]. An opinion, not a new fact.")
    people: list[str] = Field(default_factory=list, description="Names of the people the view is about.")
    supported: bool = Field(default=True, description="False when the board gives too little for a view.")


_SYSTEM = ("You are Sherlock, the lead detective. The officer asks for your judgment in the chat. Answer from your "
           "current assessment and the case board given - nothing else, no new facts, no tool calls. Say what you "
           "think and why, briefly, citing the ids your view rests on. If the board does not support a view, say what "
           "is missing. Reply in {lang}. " + DATA_NOT_INSTRUCTIONS)


def ask(llm: Any, message: str, language: str, pack: dict[str, Any]) -> dict[str, Any] | None:
    from sherlocks.linkgraph.conversation import LANG_NAME

    payload = {"question": message, "your_assessment": pack.get("sherlock_view"), "summary": pack.get("summary"),
               "facts": pack.get("matched_facts"), "officer_said": pack.get("statements"),
               "conflicts": pack.get("conflicts"), "missing": pack.get("missing"), "focus": pack.get("focus")}
    try:
        out, _ = llm.generate_structured(prompt=json.dumps(payload, ensure_ascii=False, default=str), schema=_View,
                                         system=_SYSTEM.format(lang=LANG_NAME.get(language, "English")),
                                         cache_kind="sherlock_view", prompt_version="v1", max_tokens=400)
    except Exception as exc:  # noqa: BLE001 - the reply carries on without his view
        logger.info("Sherlock's quick view failed: %s", exc)
        return None
    if not out.view.strip():
        return None
    return {"view": out.view.strip(), "people": out.people, "supported": out.supported}


_TAKE = {
    "head": ("On the new case: ", "Naye case ke hawale se: ", "نئے کیس کے حوالے سے: "),
    "fir": ("{who}'s FIR {fir} ({crimes}) bears on it - {why}.", "{who} ki FIR {fir} ({crimes}) is se juri hai - {why}.",
            "{who} کی ایف آئی آر {fir} ({crimes}) اس سے جڑی ہے - {why}۔"),
    "hyp": ("My hypothesis {id} ({status}): {statement}", "Mera andaza {id} ({status}): {statement}",
            "میرا اندازہ {id} ({status}): {statement}"),
    "none": ("Nothing on the board ties {who} to the new case yet.", "Board par abhi kuch {who} ko naye case se nahi jorta.",
             "بورڈ پر ابھی کچھ {who} کو نئے کیس سے نہیں جوڑتا۔"),
}


def take(case: Any, net: Any, query: dict[str, Any] | None, language: str) -> str | None:
    """Sherlock's own assessment for a question, from the board (no model): how the people
    asked about bear on the new case - their relevant previous FIRs, his hypotheses about
    them. None when the new case is not described yet (then the Questioner asks for it)."""
    from sherlocks.linkgraph.offences import labels
    from sherlocks.linkgraph.relevance import background, new_case, say_reasons

    if not new_case(case)["known"]:
        return None
    i = {"en": 0, "roman": 1, "ur": 2}.get(language, 0)
    people = [p for p in (query or {}).get("people") or [] if p in net.people] or list(net.seeds())
    parts: list[str] = []
    for row in [r for r in background(case, net, people) if r["score"] >= 2][:2]:
        parts.append(_TAKE["fir"][i].format(who=row["name"], fir=row["fir"], crimes=labels(row["crimes"], language) or "-",
                                            why=say_reasons(row["reasons"], language)) + f" [{row['doc'] or row['fir']}]"
                     if row.get("doc") else
                     _TAKE["fir"][i].format(who=row["name"], fir=row["fir"], crimes=labels(row["crimes"], language) or "-",
                                            why=say_reasons(row["reasons"], language)))
    for h in [h for h in case.hypotheses.values() if not h.get("stale") and set(h.get("people") or []) & set(people)][:1]:
        parts.append(_TAKE["hyp"][i].format(id=h["id"], status=h["status"], statement=h["statement"]) + f" [{h['id']}]")
    if not parts:
        parts.append(_TAKE["none"][i].format(who=", ".join(net.name(p) for p in people[:2]) or "-"))
    return _TAKE["head"][i] + " ".join(parts)
