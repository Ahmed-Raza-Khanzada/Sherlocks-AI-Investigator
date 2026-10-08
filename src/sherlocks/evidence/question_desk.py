"""The Question desk: no question is asked twice, and every answer reaches Sherlock.

Sherlock's Questioner (and the board's gap rules) *propose* questions; the
**Gatekeeper** decides which ones the officer sees. The **Question ledger** is
``case.questions``: every question with its topic key, how often it was asked and in
which turns, its status (open, answered, dont_know, muted) and the answer with the turn
it came in.

The Gatekeeper's rules, each one a reason it can give back:

* **Same topic, same question** - a topic key (``incident:date``, ``roles:main``,
  ``cdr_owner:D3``) makes a reworded question the same question.
* **Answered anywhere closes it** - in the ledger, on the board (the gap rules no longer
  call for it), or anywhere in the chat: the officer may have said it in passing
  ("waqia wale din, yani 1 March ko..."); then the answer is taken from that turn.
* **"Don't know" counts** - never asked again.
* **Muted** - "ye mat poocho" / "don't ask that" stops a topic for good.
* **Max two asks, never twice in a row, one per reply, none while the officer is busy**
  asking about something else after an unanswered question.
* **Checked twice** - when proposed, and again just before it is asked.
* **Free-form questions** (no topic key) are compared with every earlier question and
  answer - by the model when there is one, by word overlap otherwise.

A rejection is never just "no": it carries the reason and the answer on file
(``case.desk``), so the Sherlock team uses the answer instead of asking again.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_ASKS = 2
_MUTE = re.compile(r"(mat|na)\s+(poocho|puchho|pucho|poochein|puchein|poochna)|don'?t\s+ask|stop\s+asking|"
                   r"no\s+more\s+questions|sawal\s+(mat|band)|مت\s+پوچھ|سوال\s+نہ", re.IGNORECASE)
_WORD = re.compile(r"[\w؀-ۿ]+")


def free_key(text: str) -> str:
    """A topic key for a free-form question (no rule gave it one)."""
    return "free:" + hashlib.sha1(" ".join(_WORD.findall(text.lower())).encode()).hexdigest()[:10]


def wants_mute(message: str) -> bool:
    return bool(_MUTE.search(message or ""))


class _Same(BaseModel):
    already_answered: bool = Field(description="True if the conversation already answers the question, even in "
                                               "other words or in passing.")
    turn: int | None = Field(default=None, description="The turn number where it was answered.")
    answer: str = Field(default="", description="The answer as the officer gave it.")


def _overlap(a: str, b: str) -> float:
    wa, wb = set(_WORD.findall(a.lower())), set(_WORD.findall(b.lower()))
    return len(wa & wb) / max(1, len(wa | wb))


class Gatekeeper:
    """Decides which proposed question the officer sees. Stateless: everything it knows
    is on the board (ledger, conversation, gaps, desk log)."""

    def __init__(self, case: Any, graph: dict[str, Any], llm: Any = None) -> None:
        from sherlocks.linkgraph.network import PersonNetwork

        self.case = case
        self.graph = graph
        self.net = PersonNetwork(graph)
        self.llm = llm

    # -- the checks -------------------------------------------------------------------

    def _in_chat(self, q: dict[str, Any]) -> tuple[int, str] | None:
        """The turn and words in which the officer already answered ``q``, if any."""
        from sherlocks.linkgraph.conversation import route
        from sherlocks.linkgraph.sherlock_team import _fit_score

        for i, turn in enumerate(self.case.conversation, 1):
            said = str(turn.get("q") or "")
            if not said or q["key"] == "suspects:other" or route(said, []) in ("question", "greeting"):
                continue           # a question the officer asked is not an answer they gave
            if _fit_score(q, said, self.net) >= 3:
                return turn.get("turn") or i, said
        return None

    def _free_form_known(self, q: dict[str, Any]) -> tuple[int | None, str] | None:
        earlier = [x for x in self.case.questions if x["id"] != q.get("id") and x["status"] != "open"]
        for x in earlier:
            if _overlap(x["text"], q["text"]) >= 0.6:
                return x.get("answer_turn"), str(x.get("answer") or "")
        if self.llm is None or not self.case.conversation:
            return None
        chat = [{"turn": t.get("turn") or i, "officer": t.get("q", "")[:300], "sherlock": t.get("a", "")[:300]}
                for i, t in enumerate(self.case.conversation[-12:], 1)]
        try:
            out, _ = self.llm.generate_structured(
                prompt=f"Question Sherlock wants to ask: {q['text']}\n\nConversation so far: {chat}",
                schema=_Same, system="You stop an assistant from asking a police officer something already answered. "
                                     "Compare the question with the whole conversation, in any language.",
                cache_kind="gatekeeper", prompt_version="v1")
            if out.already_answered:
                return out.turn, out.answer
        except Exception as exc:  # noqa: BLE001 - the lexical check above still ran
            logger.info("Gatekeeper model failed: %s", exc)
        return None

    def check(self, q: dict[str, Any], *, before_asking: bool = False) -> dict[str, Any]:
        """``{ok, reason, answer, turn}`` for one question (proposed or about to be asked)."""
        q = {"action": "text", **q}
        key = q["key"]
        muted = set(self.case.dialog.get("muted") or [])
        if key in muted or key.split(":", 1)[0] + ":*" in muted:
            return {"ok": False, "reason": "The officer asked not to be asked this.", "answer": None, "turn": None}
        same = [x for x in self.case.questions if x["key"] == key and x.get("id") != q.get("id")]
        mine = next((x for x in self.case.questions if x.get("id") == q.get("id")), None) or (same[0] if same else None)
        if mine is not None and mine["status"] in ("answered", "dont_know"):
            turn = mine.get("answer_turn")
            what = "said they don't know" if mine["status"] == "dont_know" or mine.get("answer") == "not known" \
                else f"answered: {mine.get('answer')}"
            return {"ok": False, "reason": f"Already {what}" + (f" (turn {turn})" if turn else ""),
                    "answer": mine.get("answer"), "turn": turn}
        if mine is not None and mine.get("times", 0) >= MAX_ASKS:
            return {"ok": False, "reason": f"Asked {MAX_ASKS} times without an answer.", "answer": None, "turn": None}
        if not key.startswith(("free:", "confirm_lookup:")):
            from sherlocks.evidence.questioner import gaps

            if key not in {g["key"] for g in gaps(self.case, self.graph)}:
                return {"ok": False, "reason": "The board already holds the answer.", "answer": None, "turn": None}
        found = self._in_chat(q) if not key.startswith("free:") else self._free_form_known(q)
        if found:
            turn, said = found
            return {"ok": False, "reason": f"Already answered in turn {turn}: \"{str(said)[:160]}\"" if turn
                    else f"Already answered: \"{str(said)[:160]}\"", "answer": said, "turn": turn, "take": True}
        return {"ok": True, "reason": "approved" + (" (checked again before asking)" if before_asking else ""),
                "answer": None, "turn": None}

    # -- proposing and asking ---------------------------------------------------------

    def admit(self, proposal: dict[str, Any], *, by: str = "S4 Questioner") -> dict[str, Any] | None:
        """A proposed question: on the ledger if approved, else logged with the reason (and
        an answer found in the chat is taken onto the board). Returns the ledger entry."""
        proposal = {"action": "text", **proposal, "key": proposal.get("key") or free_key(proposal["text"])}
        decision = self.check(proposal)
        self.case.log_desk({"key": proposal["key"], "text": proposal["text"][:200], "by": by,
                            "decision": "approved" if decision["ok"] else "rejected", "reason": decision["reason"],
                            "answer": decision.get("answer"), "turn": decision.get("turn")})
        if not decision["ok"]:
            self._take(proposal, decision)
            return None
        from sherlocks.evidence.questioner import priority_of

        return self.case.ask(proposal["key"], proposal["text"], action=proposal.get("action", "text"),
                             about=proposal.get("about"), options=proposal.get("options"), by=by,
                             priority=proposal.get("priority") or priority_of(proposal["key"]))

    def _take(self, q: dict[str, Any], decision: dict[str, Any]) -> None:
        """An answer found in the chat: record it on the ledger and the board."""
        if not decision.get("take") or not decision.get("answer"):
            return
        from sherlocks.linkgraph.sherlock_team import _take_answer

        entry = next((x for x in self.case.questions if x["key"] == q["key"]), None)
        if entry is None:
            entry = self.case.ask(q["key"], q["text"], action=q.get("action", "text"), about=q.get("about"),
                                  options=q.get("options"), by="Q1 Gatekeeper")
        if entry is not None and entry["status"] == "open":
            with self.case.acting("Q1 Gatekeeper"):
                _take_answer(self.case, self.net, str(decision["answer"]), entry)
            entry["answer_turn"] = decision.get("turn")

    def pick(self, *, intent: str) -> dict[str, Any] | None:
        """The one question to ask in this reply, or None. Re-checks each candidate just
        before asking - the officer may have answered it meanwhile."""
        case = self.case
        recent = {(t.get("checks") or {}).get("asked") for t in case.conversation[-3:]}
        last_asked = (case.conversation[-1].get("checks") or {}).get("asked") if case.conversation else None
        if last_asked in {q["key"] for q in case.open_questions()} and intent == "question":
            return None            # the officer is busy with something else: let them answer first
        order = {q["id"]: i for i, q in enumerate(case.questions)}
        candidates = sorted((q for q in case.open_questions() if q.get("times", 0) < MAX_ASKS and q["key"] not in recent),
                            key=lambda q: (q.get("times", 0), order[q["id"]]))
        for q in candidates:
            if intent == "question" and q.get("times", 0) >= 1:
                continue           # asked once: comes back only while the officer is telling, not asking
            decision = self.check(q, before_asking=True)
            if decision["ok"]:
                return q
            case.log_desk({"key": q["key"], "text": q["text"][:200], "by": "Q1 Gatekeeper", "decision": "withdrawn",
                           "reason": decision["reason"], "answer": decision.get("answer"), "turn": decision.get("turn")})
            if decision.get("take"):
                self._take(q, decision)
            elif q["status"] == "open" and "Asked" not in decision["reason"]:
                case.close_question(q["key"], decision.get("answer") or decision["reason"])
        return None

    def asked(self, q: dict[str, Any]) -> None:
        """The question went out in this reply: count it on the ledger."""
        q["times"] = q.get("times", 0) + 1
        q.setdefault("asked_turns", []).append(len(self.case.conversation) + 1)

    def mute(self, key: str) -> None:
        muted = list(dict.fromkeys([*(self.case.dialog.get("muted") or []), key]))
        self.case.dialog["muted"] = muted
        for q in self.case.questions:
            if q["key"] == key and q["status"] == "open":
                q["status"] = "muted"
        self.case.log_desk({"key": key, "decision": "muted", "reason": "The officer asked not to be asked this.",
                            "by": "Q1 Gatekeeper"})


def known_answers(case: Any) -> list[dict[str, Any]]:
    """What the desk told the Sherlock team: questions it need not ask, with the answer
    on file - read by Sherlock so he uses the answer instead of asking again."""
    out = []
    for d in case.desk[-40:]:
        if d.get("decision") in ("rejected", "withdrawn") and (d.get("answer") or "don't know" in d.get("reason", "")):
            out.append({"key": d["key"], "question": d.get("text"), "answer": d.get("answer"), "reason": d["reason"]})
    for q in case.questions:
        if q["status"] in ("answered", "dont_know"):
            out.append({"key": q["key"], "question": q["text"], "answer": q.get("answer"),
                        "reason": f"answered in turn {q.get('answer_turn')}" if q.get("answer_turn") else "answered"})
    return out[-40:]
