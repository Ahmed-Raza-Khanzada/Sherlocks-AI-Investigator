"""R8 CDR analyst: answers any question about the case's call records by computing it - never by reading them.

A CDR runs to thousands of rows; put in a model's context, they are skimmed and miscounted. The CDR analyst
gives the model only what the records ARE - the files on the board (their own number, period, size), the
fields of a record, the numbers of the case's people, and the fixed analytics of ``evidence/cdr_tools.py`` -
and asks it to write a few lines of Python that compute the answer. The code runs in the sandbox
(``evidence/cdr_sandbox.py``) over every record of every CDR on the board; a failed run goes back once with its
error. What the code computed is what the officer's answer is written from, and the code is kept with the
answer, so anyone can see how a number was reached.

General: no question type is hard-coded - top contacts, a call between two numbers, who links two suspects,
where a phone was on a night, a handset change, silences around an incident are all the same path.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from sherlocks.evidence import cdr_sandbox, cdr_tools
from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS

logger = logging.getLogger(__name__)

_CACHE: dict[tuple[str, float], list[dict[str, Any]]] = {}
KEEP_CODE = 30                      # questions whose code stays on the board

_SYSTEM = (
    "You are the CDR analyst of a police investigation. You never see the call records: you write Python that "
    "computes the answer from them, and it runs over every record of every CDR on the case board. "
    "`calls` is a list of dicts, one a record (fields in RECORD). The functions in FUNCTIONS are already defined - use "
    "them where they fit, and plain Python (loops, collections, datetime, itertools, statistics, re) for the rest. "
    "Numbers: pass them as the question gives them; norm() writes any number the way the records do. Each function "
    "returns exactly what FUNCTIONS says - read it before using the result; prefer one call to a function that already "
    "answers the question. Only these modules can be imported: collections, datetime, itertools, math, statistics, re, "
    "json, functools, operator, string. "
    "Put what answers the question in a variable named `result`: a short list or dict that names, for every "
    "finding, the file and rows it comes from (most functions already return them). Keep it small - at most 50 "
    "items; count instead of listing when there are more. Do not import anything else, do not open files, do not "
    "print the records. A CDR's own number is its `subject`; `other` is the party at the other end. A tower is not "
    "a place: a record on a tower shows a connection near it. 'Calls' means records of kind call; SMS are counted "
    "only when the question asks for them (say how many SMS there were besides). "
    "Write ONLY the code, in one ```python block. " + DATA_NOT_INSTRUCTIONS)

RECORD = {"file": "board document id of the CDR (D12)", "title": "the file's title", "subject": "the CDR's own number",
          "row": "the record's row in the sheet", "a": "A party", "b": "B party", "other": "the party that is not the subject",
          "when": "YYYY-MM-DD HH:MM:SS or null", "date": "YYYY-MM-DD", "hour": "0-23",
          "kind": "call | sms | data", "way": "in | out", "duration": "seconds (calls)", "site": "cell / site id",
          "addr": "tower address", "lat": "tower latitude or null", "lon": "tower longitude or null",
          "imei": "handset", "imsi": "SIM", "dir": "the call type as the operator wrote it"}


def records(case: Any, llm: Any = None) -> list[dict[str, Any]]:
    """Every record of every CDR on the case board (the files the officer uploaded and the CDR archive brought),
    read from the stored files - not from the text of the board."""
    from sherlocks.evidence import cdr

    out: list[dict[str, Any]] = []
    for doc in case.documents.values():
        path = Path(str((doc.get("data") or {}).get("path") or ""))
        if doc.get("kind") != "cdr" or not path.is_file():
            continue
        key = (str(path), path.stat().st_mtime)
        if key not in _CACHE:
            read = cdr.read_events(path.read_bytes(), llm)
            if read["kind"] not in ("cdr", "tower_dump"):
                _CACHE[key] = []
                continue
            subject = ((doc.get("data") or {}).get("analysis") or {}).get("subject")
            rows = []
            for e in read["events"]:
                own = subject if read["kind"] == "cdr" else e["a"]
                other = e["b"] if e["a"] == own or not e["a"] else e["a"]
                when = e["when"]
                rows.append({"file": doc["id"], "title": doc.get("title"), "subject": own, "row": e["row"],
                             "a": e["a"], "b": e["b"], "other": other if other != own else None,
                             "when": when.strftime("%Y-%m-%d %H:%M:%S") if when else None,
                             "date": when.strftime("%Y-%m-%d") if when else None, "hour": when.hour if when else None,
                             "kind": e.get("kind"), "way": e.get("way"), "duration": e.get("duration"),
                             "site": e.get("site"), "addr": e.get("addr"), "lat": e.get("lat"), "lon": e.get("lon"),
                             "imei": e.get("imei"), "imsi": e.get("imsi"), "dir": e.get("dir")})
            _CACHE[key] = rows
        out += [{**r, "file": doc["id"], "title": doc.get("title")} for r in _CACHE[key]]
    return out


def catalog() -> dict[str, str]:
    """The fixed analytics, as the model sees them: signature, what each does and exactly what it returns."""
    return {f.__name__: f"{f.__name__}{inspect.signature(f)} - " + " ".join((inspect.getdoc(f) or "").split())
            for f in cdr_tools.FUNCTIONS}


def _people(case: Any, net: Any) -> list[dict[str, Any]]:
    if net is None:
        return []
    out = []
    for pid in list(net.seeds())[:12]:
        phones = [cdr_tools.norm(p) for p in net.data(pid).get("phones") or []]
        out.append({"name": net.name(pid), "phones": [p for p in phones if p][:10], "target": True})
    return out


def _code(text: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text or "", re.DOTALL)
    return (m.group(1) if m else text or "").strip()


def answer(case: Any, question: str, llm: Any, *, net: Any = None, workdir: str | Path | None = None) -> dict[str, Any]:
    """``{question, files, code, result, ok, error, ms}`` - the answer computed over the CDRs on the board."""
    started = time.monotonic()
    calls = records(case, llm)
    files = cdr_tools.files(calls)
    if not calls:
        return {"question": question, "ok": False, "files": [],
                "error": "No CDR file is on the case board - upload one, or search the CDR archive for the number."}
    if llm is None or not hasattr(llm, "generate_text"):
        return {"question": question, "ok": False, "files": files, "error": "No model to write the analysis."}
    brief = {"QUESTION": question, "FILES": files, "RECORD": RECORD, "FUNCTIONS": catalog(),
             "PEOPLE_OF_THE_CASE": _people(case, net),
             "INCIDENT": {k: v for k, v in (case.incident or {}).items() if k in ("date", "time", "place", "lat", "lon")}}
    code, out, tries = "", {}, []
    for attempt in range(2):
        prompt = json.dumps(brief, ensure_ascii=False, default=str)
        if attempt:
            prompt += ("\n\nYour code failed. Fix it and write it again.\nCODE:\n" + code + "\nERROR:\n"
                       + str(out.get("error"))[:600])
        try:
            text = llm.generate_text(prompt=prompt, system=_SYSTEM, cache_kind="cdr_analyst", prompt_version="v4",
                                     max_tokens=1500)
        except Exception as exc:  # noqa: BLE001 - the officer is told it could not be computed
            out = {"ok": False, "error": f"the model did not answer: {type(exc).__name__}"}
            break
        code = _code(text)
        out = cdr_sandbox.run(code, calls, workdir=workdir)
        tries.append({"code": code, "ok": out.get("ok"), "error": out.get("error"), "ms": out.get("ms")})
        if out.get("ok") and out.get("result") not in (None, [], {}, ""):
            break
        if out.get("ok"):
            out = {**out, "ok": True, "note": "the code ran and found nothing"}
            break
    done = {"question": question, "files": files, "code": code, "result": out.get("result"), "ok": bool(out.get("ok")),
            "error": out.get("error"), "note": out.get("note"), "ms": int((time.monotonic() - started) * 1000),
            "tries": len(tries)}
    kept = case.dialog.setdefault("cdr_code", [])
    kept.append({k: done[k] for k in ("question", "code", "ok", "error", "ms", "tries")})
    del kept[:-KEEP_CODE]
    return done
