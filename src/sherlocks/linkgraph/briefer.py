"""O4 Case briefer: the Officer agent never starts a reply cold.

Before every turn it builds a **context pack** from the case board, in two parts:

* **standing** - the same every turn: the chat (recent turns word for word, older ones as
  a running summary, so nothing the officer said is lost), the case summary (S2),
  Sherlock's latest view (S1, with its time), what is new since the last reply;
* **matched** - for this message: the person in focus and their dossier (A4), the facts
  that match (newest and strongest tier first, duplicates merged, corrected entries left
  out), what the officer told us (statements and answered questions from the ledger),
  sources that disagree on it.

It also says **what is missing** - topics asked about that the board does not hold ("no
employer on record") - so the Officer agent calls the API router or asks Sherlock instead
of guessing. Every line keeps its id (F#, D#, H#, Q#, rows, call id), and the pack is cut
at one board version, so the Answer checker checks the reply against exactly what the
Officer agent saw. Each part has a size cap; the whole pack has one too.
"""

from __future__ import annotations

import json
from typing import Any

_TIER_ORDER = {"fact": 0, "inference": 1, "speculation": 2}
# Share of the pack's characters each part may use.
_CAPS = {"chat_recent": 0.12, "chat_older": 0.06, "summary": 0.08, "sherlock_view": 0.10, "whats_new": 0.05,
         "focus": 0.12, "matched_facts": 0.20, "statements": 0.10, "conflicts": 0.04, "missing": 0.03}


def _cap(value: Any, chars: int) -> Any:
    """Trim a list from the end (or a string) until it fits ``chars`` as JSON."""
    if isinstance(value, str):
        return value[:chars]
    if isinstance(value, list):
        out = list(value)
        while out and len(json.dumps(out, ensure_ascii=False, default=str)) > chars:
            out.pop()
        return out
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, default=str)
        return value if len(text) <= chars else json.loads(json.dumps({k: value[k] for k in list(value)[:6]},
                                                                      default=str))
    return value


def older_summary(case: Any, recent: int) -> list[str]:
    """Older turns, one line each: what the officer said (never dropped) and what
    Sherlock answered, shortened."""
    older = case.conversation[:-recent] if recent else list(case.conversation)
    return [f"turn {t.get('turn') or i}: officer said \"{str(t.get('q') or '')[:140]}\" - Sherlock: "
            f"{str(t.get('a') or '').splitlines()[0][:100] if t.get('a') else ''}"
            for i, t in enumerate(older, 1)]


def build(case: Any, graph: dict[str, Any], net: Any, message: str, *, people: list[str] | None = None,
          kb: Any = None, chars: int = 14000, recent: int = 4) -> dict[str, Any]:
    """The context pack for one turn."""
    from sherlocks.evidence.dossiers import dossier
    from sherlocks.evidence.trust import conflicts
    from sherlocks.linkgraph.conversation import briefing, case_memory
    from sherlocks.linkgraph.knowledge import knowledge

    people = [p for p in people or case.dialog.get("focus") or [] if p in net.people]
    focus = [dossier(case, net, p) for p in people[:2]] if people else []   # may refresh a card first
    version = case.version
    memory = case_memory(case, graph, net, limit=int(chars * 0.35))
    kb = kb or knowledge(graph, case, net)
    hits = kb.search(message, people=people or None, k=40) if message else []
    seen: set[str] = set()
    matched = []
    facts = {f["id"]: f for f in case.facts.values()}
    for e in hits:
        f = facts.get(e.source)
        tier = (f or {}).get("tier", "fact")
        key = e.text.lower()[:120]
        if key in seen:
            continue                                   # duplicates merged
        seen.add(key)
        matched.append({"id": e.source, "about": e.title, "text": e.text[:300], "tier": tier,
                        "at": (f or {}).get("at") or ""})
    matched.sort(key=lambda m: (_TIER_ORDER.get(m["tier"], 3), "".join(chr(255 - ord(c)) for c in m["at"])))
    news, _ = briefing(case, case.seen)
    assessment = case.assessment or {}
    hyps = sorted((h for h in case.hypotheses.values() if not h.get("stale")),
                  key=lambda h: ({"supported": 0, "open": 1, "weakened": 2, "ruled out": 3}.get(h["status"], 4)))
    statements = [{"id": f["id"], "said": f["statement"], "topic": f.get("topic")}
                  for f in case.live_facts() if f.get("by") == "officer"][-15:]
    answered = [{"id": q["id"], "question": q["text"], "answer": q.get("answer"), "turn": q.get("answer_turn"),
                 "status": q["status"]} for q in case.questions if q["status"] in ("answered", "dont_know")]
    missing = []
    if message:
        holders = " ".join(m["text"].lower() for m in matched)
        for topic in kb.topics(message):
            if topic not in holders and not any(topic in c for e in hits for c in e.concepts):
                who = ", ".join(net.name(p) for p in people) or "the case"
                missing.append(f"no {topic} on record for {who}")
    pack = {
        **memory,
        "board_version": version,
        "chat_recent": [{"turn": t.get("turn"), "officer": str(t.get("q") or "")[:400], "sherlock": str(t.get("a") or "")[:400]}
                        for t in case.conversation[-recent:]],
        "chat_older": older_summary(case, recent),
        "summary": (case.summary or {}).get("text") or "",
        "sherlock_view": {"answer": assessment.get("answer"), "at": assessment.get("at"),
                          "hypotheses": [{"id": h["id"], "statement": h["statement"], "status": h["status"],
                                          "confidence": h["confidence"], "tier": h["tier"]} for h in hyps[:6]]},
        "whats_new": news,
        "focus": [{k: d.get(k) for k in ("name", "role", "cnic", "phones", "firs", "links", "statements", "cdrs",
                                         "hypotheses")} for d in focus],
        "matched_facts": matched,
        "statements": {"said": statements, "answered_questions": answered},
        "conflicts": [{"topic": c["topic"], "values": c["values"], "settle": c["settle"]}
                      for c in conflicts(case, net)],
        "missing": missing,
    }
    for part, share in _CAPS.items():
        pack[part] = _cap(pack[part], int(chars * share))
    return pack


def pack_ids(pack: dict[str, Any]) -> set[str]:
    """Every id in the pack - what the Answer checker accepts as a source for this turn."""
    import re

    return set(re.findall(r"\b([DFLHQ]\d{1,4})\b", json.dumps(pack, ensure_ascii=False, default=str)))
