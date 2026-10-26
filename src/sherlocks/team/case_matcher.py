"""O3b Case matcher: which case the officer's message is about.

One decision, asked in plain words rather than as a field of the statement recorder's form:
does the officer say there is no case (they are only looking at someone's record), describe
no event at all (a request, a question), describe a new event, or describe the very event
that an FIR on file records?

Inside the recorder's JSON form the office model could not weigh this: it gave "a request
to see his FIRs" as "no case", an event of last night as a years-old FIR, the same event as
a new matter - and a dedicated JSON form called everything the same event. Asked
to reason in plain text and then answer, it got them right. So it reasons in words, and code
reads only its last line - ``ANSWER: no case | no event | new | same <FIR>`` - and checks
that a named FIR is one on file. No answer (no model, a failed call, an unreadable line)
leaves the recorder's own reading standing.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are an experienced investigating officer. A colleague has written you a message about the person they are "
    "looking at. Decide which of these it is:\n"
    "- no case: the colleague SAYS there is no case - nothing happened, the person is not involved in anything, they "
    "are only looking at his past record or profile;\n"
    "- no event: the message describes no event - it asks a question or asks to see records ('who is he', 'show me "
    "his FIRs', 'record batao'), or only names a suspect or a role;\n"
    "- new: it describes an event that no FIR on file records - a different victim or complainant, a different act, "
    "or a time after the FIR's own date ('last night', when the FIR is years old);\n"
    "- same <FIR number>: it describes the very event an FIR on file records - the same victim or complainant, the "
    "same act, the same place - or says the case IS that FIR. A case grows over time: the same victim's case can "
    "move from one offence to a graver one (a missing or hurt person later found dead, a theft that turns out a "
    "robbery), so compare the people, the place and the time, and read each FIR's reports (its_reports) as well as "
    "its first information - not only the section it was first registered under.\n"
    "The messages may be in Urdu, Roman Urdu or English. Think it through in a few short sentences, then end with one "
    "last line exactly: ANSWER: no case | no event | new | same <FIR number as on file>. " + DATA_NOT_INSTRUCTIONS)

_ANSWER = re.compile(r"ANSWER:\s*\**\s*(no case|no event|new|same)\b\s*(?:FIR\s*)?([0-9]{1,5}\s*/\s*[0-9]{2,4})?",
                     re.IGNORECASE)


def _number(text: str) -> tuple[str, str]:
    m = re.search(r"(\d{1,5})\s*/\s*(\d{2,4})", text or "")
    return (m.group(1).lstrip("0"), m.group(2)[-2:]) if m else ("", "")


def read_answer(text: str, firs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The answer on the model's last ANSWER line; a named FIR must be one on file."""
    found = list(_ANSWER.finditer(text or ""))
    if not found:
        return None
    m = found[-1]
    says = m.group(1).lower()
    why = text[: m.start()].strip()[-400:]
    if says != "same":
        return {"says": says, "fir": "", "why": why}
    want = _number(m.group(2) or "")
    on_file = next((f["fir"] for f in firs if want[0] and _number(str(f.get("fir") or "")) == want), None)
    return {"says": "same", "fir": on_file, "why": why} if on_file else None


def match(case: Any, net: Any, message: str, llm: Any) -> dict[str, Any] | None:
    """``{says: no case | no event | new | same, fir, why}``, or None when there is no answer."""
    from sherlocks.team.recorder import firs_on_file

    if llm is None or not hasattr(llm, "generate_text") or not (message or "").strip():
        return None
    firs = firs_on_file(case, net)
    given = {"message": message, "today": datetime.now(UTC).astimezone().date().isoformat(),
             "earlier_from_the_colleague": [t.get("q", "")[:200] for t in case.conversation[-2:]],
             "firs_on_file": firs}
    try:
        text = llm.generate_text(prompt=json.dumps(given, ensure_ascii=False, default=str), system=_SYSTEM,
                                 cache_kind="case_matcher", prompt_version="v3", max_tokens=900)
    except Exception as exc:  # noqa: BLE001 - the recorder's own reading stands
        logger.info("Case matcher failed: %s", exc)
        return None
    return read_answer(text, firs)
