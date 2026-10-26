"""Hint keeper: what the chat brings up, kept on the case board as leads - only on evidence.

An answer or Sherlock's view often notices something worth following ("the cheque was cashed
the day before the complaint", "the phone went off minutes after he left home") - and it
was lost when the chat moved on. After each answered question the Hint keeper reads the
reply in plain words and proposes hints: what was noticed, why it could matter, the next
step, and the records it rests on. Code keeps a hint only when it rests on a record of the
case - a report's or an FIR's finding, a statement, a link proven by CNIC or phone - and
never on the chat alone: the officer's own words, Sherlock's earlier replies, a pattern of
the link graph or a common pattern (a SIM in another's name, a tenancy witness) are not
evidence. A hint already on the board is not kept twice. Each becomes a lead ``K#`` that
says it came from the chat and on which turn.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS

logger = logging.getLogger(__name__)

REF = re.compile(r"\b([DFWL]\d{1,4})\b")
_LINE = re.compile(r"^\s*[-*•]?\s*\**HINT\**\s*:\s*(.+)$", re.IGNORECASE)
MOST = 3

_SYSTEM = ("You keep the case board of a police investigation. Below is the officer's question, the reply they were "
           "given, Sherlock's view, and the records the reply cites (with their ids). Write the hints worth keeping on "
           "the board: something the records show that is worth following up - a finding, a mismatch, an "
           "unexplained detail - each resting on the cited records, not on the conversation. A hint is a thing to "
           "check, never a conclusion or an accusation. At most three; none when nothing is worth keeping. One line "
           "each, exactly:\nHINT: what the records show | why it could matter | the next step that would test it | "
           "the ids it rests on\nNo other text. Never use the words 'inferred' or 'corroborated'. " + DATA_NOT_INSTRUCTIONS)


def evidence(case: Any, ref: str) -> bool:
    """Is this id a record of the case - not the officer's words, our own analysis or a common pattern?"""
    from sherlocks.team.case_scope import NOT_EVIDENCE

    if ref.startswith("D"):
        return (case.documents.get(ref) or {}).get("kind") not in (None, "notes", "graph")
    if ref.startswith("F"):
        f = case.facts.get(ref) or {}
        return bool(f) and f.get("by") not in ("officer", "graph") and f.get("kind") not in NOT_EVIDENCE \
            and case.usable(ref) and (case.documents.get(f.get("doc") or "") or {}).get("kind") not in ("notes", "graph")
    if ref.startswith("W"):
        return ref in case.statements
    if ref.startswith("L"):
        return (case.links.get(ref) or {}).get("strength") == "identifier"
    return False


def _key(text: str) -> str:
    return "chat:" + " ".join(re.findall(r"[a-z0-9]+", text.lower()))[:80]


def keep(case: Any, question: str, answer: str, view: str, llm: Any, *, turn: int | None = None) -> list[str]:
    """The leads kept from one reply (their ids) - none without a model that writes plain text."""
    if llm is None or not hasattr(llm, "generate_text"):
        return []
    cited = [r for r in dict.fromkeys(REF.findall(f"{answer} {view}")) if evidence(case, r)]
    if not cited:
        return []                                   # a reply that rests on no record gives no hint
    records = {r: (case.cite(r) or {}).get("title", "")[:400] for r in cited[:20]}
    try:
        text = llm.generate_text(prompt=json.dumps({"question": question[:600], "reply": answer[:5000], "sherlocks_view": view[:1500],
                                                    "records": records}, ensure_ascii=False),
                                 system=_SYSTEM, cache_kind="hint_keeper", prompt_version="v1", max_tokens=700)
    except Exception as exc:  # noqa: BLE001 - the reply stands without its hints
        logger.info("Hint keeper failed: %s", exc)
        return []
    have = {k.get("key") for k in case.leads.values()} | {
        " ".join(re.findall(r"[a-z0-9]+", str(k.get("hint") or "").lower()))[:80] for k in case.leads.values()}
    kept: list[str] = []
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if not m or len(kept) >= MOST:
            continue
        parts = [p.strip() for p in m.group(1).split("|")]
        hint = parts[0]
        rests = [r for r in dict.fromkeys(REF.findall(" ".join(parts))) if r in cited and evidence(case, r)]
        if not hint or not rests or _key(hint) in have or _key(hint)[5:] in have:
            continue                                # resting on no record of the case, or already on the board
        people = sorted({p for r in rests for p in (case.facts.get(r) or {}).get("pids") or []})
        with case.acting("Hint keeper"):
            lead = case.set_lead(_key(hint), {
                "hint": re.sub(r"\b(?:inferred|corroborated)\b", "", hint, flags=re.IGNORECASE).strip()[:300],
                "why": (parts[1] if len(parts) > 1 else "")[:300], "next_step": (parts[2] if len(parts) > 2 else "")[:300],
                "source": rests[0], "rests_on": rests, "people": people, "status": "open", "found_in": "chat",
                "turn": turn})
        have.add(_key(hint))
        kept.append(lead)
    return kept
