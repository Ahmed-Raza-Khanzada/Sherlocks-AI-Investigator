"""Which case this is, again, when the records grow - and lab reports fetched again when the laboratory failed.

The Case matcher decides, from the officer's words, whether the case is an FIR already on file or a new matter. It
can only compare with the FIRs read so far. A FIR file read a minute later - the kidnapping FIR of the person now
found dead - was never compared, and the case stayed "a new matter" with that FIR treated as history. So, in the
background, whenever a FIR file arrives that the matcher has not seen and the case is not tied to an FIR, the
officer's own description is matched again; a match ties the case to that FIR (what it records about the offence,
the day and the place then describes the case) and the officer is told in the chat.

A laboratory system that answered with an error (HTTP 500, a timeout) is asked again on a later pass, a few times,
minutes apart - an error is not "no report".
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

LAB_TRIES = 3
LAB_WAIT_S = 300

TOLD = {"en": "FIR {fir} has now been read: {what}. It records the same event you described, so I am now working "
              "this case as FIR {fir} - its offence, date and place are taken from it. Tell me if it is a different case.",
        "roman": "FIR {fir} ab parh li gayi hai: {what}. Yeh wohi waqia hai jo aap ne bataya, is liye ab yeh case FIR "
                 "{fir} ke taur par dekh raha hoon. Agar yeh alag case hai to bata dein.",
        "ur": "ایف آئی آر {fir} اب پڑھ لی گئی ہے: {what}۔ یہ وہی واقعہ ہے جو آپ نے بتایا، اس لیے اب یہ کیس ایف آئی آر {fir} کے "
              "طور پر دیکھ رہا ہوں۔ اگر یہ الگ کیس ہے تو بتا دیں۔"}
LABS_IN = {"en": "The laboratory system answered this time: {n} report(s) of FIR {fir} are now on the case board - ask "
                 "me about them.",
           "roman": "Lab system ne ab jawab diya: FIR {fir} ki {n} report(s) case board par aa gayi hain - in ke baare mein "
                    "poochein.",
           "ur": "لیب سسٹم نے اب جواب دیا: ایف آئی آر {fir} کی {n} رپورٹیں کیس بورڈ پر آ گئی ہیں - ان کے بارے میں پوچھیں۔"}


MATCH_TRIES = 6        # matches of one case in all, while it is tied to no FIR
MORE_TURNS = 2         # the officer said this many more messages since the last match: matched again


def _fir_docs(case: Any) -> list[str]:
    return sorted(d["id"] for d in case.documents.values() if d.get("kind") == "fir")


def _uploads(case: Any) -> list[str]:
    """The officer's uploads read in full: what they are about can tie the case to an FIR."""
    return sorted(d["id"] for d in case.documents.values() if d.get("reading"))


def _now_seen(case: Any) -> dict[str, Any]:
    return {"firs": _fir_docs(case), "uploads": _uploads(case), "turns": len(case.conversation)}


def unmatched(case: Any) -> bool:
    """The case is tied to no FIR, FIR files are on the board, and since the last match something came that can
    tell which case it is: an FIR file, an upload read in full, or more of the officer's words."""
    inc, basis = case.incident or {}, (case.dialog or {}).get("case_basis")
    docs = _fir_docs(case)
    if not docs or inc.get("fir") or basis in ("fir_on_record", "record_check"):
        return False
    if not any(str(t.get("q") or "").strip() for t in case.conversation):
        return False
    if int(case.dialog.get("match_tries") or 0) >= MATCH_TRIES:
        return False
    last = case.dialog.get("matched_with") or {"firs": case.dialog.get("matched_with_firs"), "uploads": [], "turns": 0}
    now = _now_seen(case)
    return (now["firs"] != last.get("firs") or now["uploads"] != (last.get("uploads") or [])
            or now["turns"] - int(last.get("turns") or 0) >= MORE_TURNS)


def waiting(case: Any) -> bool:
    """Work for the background pass, known without the model: a re-match, or a laboratory fetch to ask again."""
    return unmatched(case) or bool(lab_retries(case, due_only=False))


def rematch(case: Any, net: Any, llm: Any) -> str | None:
    """Match the officer's description of the case again when FIR files arrived since the last match. Returns the
    FIR adopted, or None."""
    from sherlocks.team import case_matcher
    from sherlocks.team.recorder import adopt_fir

    if not unmatched(case):
        return None
    said = [str(t.get("q") or "") for t in case.conversation if str(t.get("q") or "").strip()][-4:]
    seen = _now_seen(case)
    case.dialog["matched_with"] = seen
    case.dialog["matched_with_firs"] = seen["firs"]
    case.dialog["match_tries"] = int(case.dialog.get("match_tries") or 0) + 1
    if not said or llm is None:
        return None
    found = case_matcher.match(case, net, " / ".join(said)[-1500:], llm)
    if not found or found.get("says") != "same" or not found.get("fir"):
        return None
    fir = str(found["fir"]).strip()[:40]
    with case.batch("Case matcher", "statement"):
        case.dialog["case_basis"] = "fir_on_record"
        case.set_incident({"fir": fir})
        case.close_question("case:which", f"FIR {fir}")
        case.close_question("incident:fir", fir)
        adopt_fir(case, net, fir)
    doc = next((d for d in case.documents.values() if d.get("kind") == "fir"
                and case_matcher._number(d.get("title") or "") == case_matcher._number(fir)), {})
    what = re.sub(r"\s+", " ", str(doc.get("ai_summary") or doc.get("summary") or "")).strip()[:220] or doc.get("title") or ""
    language = case.dialog.get("language") or "en"
    case.add_followup(TOLD.get(language, TOLD["en"]).format(fir=fir, what=what), kind="case")
    logger.info("Case %s: re-matched to FIR %s", case.run_id, fir)
    return fir


def lab_retries(case: Any, *, due_only: bool = True) -> list[tuple[str, str, str]]:
    """The FIRs whose laboratory fetch failed with an error and is to be asked again - due now, or (``due_only``
    False) later: ``[(fir_no, fir_year, ps_id)]``."""
    tries: dict[str, list[dict[str, Any]]] = {}
    for a in case.attempts:
        if a.get("kind") == "lab" and str(a.get("key") or "").startswith("labs:"):
            tries.setdefault(a["key"], []).append(a)
    due = []
    now = datetime.now(UTC)
    for key, rows in tries.items():
        if any(r["status"] in ("hit", "none") for r in rows):
            continue
        errors = [r for r in rows if r["status"] == "error"]
        if not errors or len(errors) >= LAB_TRIES:
            continue
        try:
            last = datetime.fromisoformat(str(errors[-1]["at"]))
        except ValueError:
            continue
        if due_only and (now - last).total_seconds() < LAB_WAIT_S:
            continue
        parts = key[len("labs:"):].split("/")
        if len(parts) == 3:
            due.append((parts[0], parts[1], parts[2]))
    return due


def retry_labs(case: Any, agents: Any) -> int:
    """Ask the laboratory system again for the FIRs it failed on. Returns the reports brought in."""
    if agents is None or not hasattr(agents, "fetch_lab_reports"):
        return 0
    got = 0
    language = case.dialog.get("language") or "en"
    for no, year, ps_id in lab_retries(case):
        key = f"labs:{no}/{year}/{ps_id}"
        case.release(key)                       # the failed task's claim: let the fetch run again
        started = time.monotonic()
        try:
            found = agents.fetch_lab_reports(no, year, ps_id)
        except Exception as exc:  # noqa: BLE001 - tried again on a later pass
            logger.info("Lab retry %s failed: %s", key, exc)
            continue
        ids = found.get("documents") or []
        logger.info("Lab retry %s: %d report(s) in %.1fs", key, len(ids), time.monotonic() - started)
        if ids:
            got += len(ids)
            case.add_followup(LABS_IN.get(language, LABS_IN["en"]).format(n=len(ids), fir=f"{no}/{year}"), kind="labs")
    return got
