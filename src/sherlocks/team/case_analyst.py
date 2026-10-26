"""P10 Case analyst: what a senior detective needs from the records, beyond laying them out.

Three pieces of work, each general - for any case, not one kind of crime:

* **Forensic reading** - each medical, forensic or laboratory report of the case (and the
  reports on the target): its header (laboratory, report number, who submitted it, when it
  was received and issued), every exhibit with the test done and the RESULT, the findings in
  the report's own terms, what they indicate for the investigation and what they cannot
  show, and what the report does not state. A form the model fills from the report's text.
* **Statements explained** - each statement of the case's FIRs (and the target's earlier
  FIRs): what the person says, explained in plain English, kept beside their full words (the
  record is often in Urdu or Sindhi; it is never cut).
* **Weighing the evidence** - the question the case turns on (who did it; how it was done;
  whether it happened as reported), each explanation with what supports it, what goes
  against it and what would settle it, Sherlock's view and why, the questions still open, an
  action plan with priorities, the people the records tie to the case and why, and any
  section Sherlock judges the officer needs. The office model weighs in plain words and
  does it poorly inside a form (2026-10-21), so it reasons in text and code reads its lines.

Without a model the reports and statements are laid out from the records (findings read
earlier, the full words) and the weighing is left out - never guessed.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import Field

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.team.runtime import PLAIN, TERMS, Typed, ask, dumps, real_refs

logger = logging.getLogger(__name__)

REPORT_TEXT = 9000          # characters of a report's text given to the reader
MOST_REPORTS = 10
MOST_STATEMENT_FIRS = 6
REPORT_KINDS = ("lab", "medical", "mlo", "postmortem", "report")
_WORDS = re.compile(r"\b(?:inferred|corroborated|corroborating)\b", re.IGNORECASE)


def plain(text: Any) -> str:
    """No "inferred" or "corroborated": the officer is told what the records show, in those words."""
    return re.sub(r"\s{2,}", " ", _WORDS.sub(lambda m: {"inferred": "found by comparing records"}.get(
        m.group(0).lower(), "confirmed by more than one record"), str(text or ""))).strip()


# -- forensic reading ---------------------------------------------------------------------------


class _Exhibit(Typed):
    article: str = Field(description="The article / item number as the report gives it (e.g. 'Art. 10'), or ''.")
    item: str = Field(description="What the exhibit is, as the report names it (a sample, a garment, a document, a "
                                  "device, a substance).")
    test: str = Field(description="The test done on it, as the report names it.")
    result: str = Field(description="The RESULT for this exhibit, with numbers and units as written ('detected', "
                                   "'not detected', 'matches', 'does not match'). 'not stated' if absent.")


class _ReportRead(Typed):
    lab: str = Field(description="The laboratory or hospital that issued the report.")
    report_no: str = Field(description="The report's own number(s).")
    submitted_by: str = Field(description="Who sent the exhibits (rank and name), as written.")
    received: str = Field(description="When the laboratory received the case, as written.")
    issued: str = Field(description="The report's date of issue, as written.")
    exhibits: list[_Exhibit] = Field(description="Every exhibit examined, each with its test and its result.")
    findings: list[str] = Field(description="Each finding of the report in plain words, one per line, with the numbers, "
                                            "measurements and exhibit numbers as written. What the report says - not "
                                            "what it means.")
    indicates: list[str] = Field(description="What the findings indicate for the investigation, as a forensic expert "
                                             "would explain it to an investigating officer - and what they cannot show. "
                                             "Include what the exhibits themselves show about events (where they were "
                                             "taken from, when, what that says happened). Where a finding has more than one "
                                             "possible cause, name the causes - never pick one the report does not give. "
                                             "One point per line.")
    not_stated: list[str] = Field(description="What an investigator would need that this report does not give (the "
                                              "question it was sent with and does not answer, a cause, a time, a match).")


_REPORT_SYSTEM = ("You are a forensic analyst reading ONE medical, forensic or laboratory report for the investigating "
                  "officer. Its text is scanned (OCR) and may be noisy; what was read in it before is given too. Fill "
                  "the form from this report only: the header, every exhibit with the test and the RESULT, the "
                  "findings with every number and measurement as written, then - separately - what they indicate for "
                  "the investigation and what they cannot show, and what the report does not state. Something the report did "
                  "not test is not evidence that it is absent. Never write the words 'inferred' or 'corroborated'. "
                  + PLAIN + DATA_NOT_INSTRUCTIONS)


def _report_material(case: Any, doc: dict[str, Any]) -> dict[str, Any]:
    from sherlocks.team.officer_agent import page_line

    card = ((case.register or {}).get("cards") or {}).get(doc["id"]) or {}
    facts = [f for f in (case.facts.get(i) for i in doc.get("facts") or []) if f and not page_line(f, doc)]
    return {"title": doc.get("title"), "source": doc.get("source"), "summary": doc.get("summary") or "",
            "read_before": doc.get("ai_summary") or "",
            "fields": [f"{x.get('name')}: {x.get('value')}" + (f" ({x['when']})" if x.get("when") else "")
                       for x in card.get("fields") or [] if x.get("value")][:24],
            "facts_read_in_it": [f["statement"] for f in facts][:30], "text": (doc.get("text") or "")[:REPORT_TEXT]}


def _from_records(case: Any, doc: dict[str, Any]) -> dict[str, Any]:
    """The report without the model: its header fields and what was read in it."""
    given = _report_material(case, doc)
    fields = {f.split(":")[0].strip().lower(): f.split(":", 1)[1].strip() for f in given["fields"] if ":" in f}

    def pick(*keys: str) -> str:
        return next((fields[k] for k in fields for w in keys if w in k), "")

    received = re.search(r"case received ([\d\-: ]+)", doc.get("summary") or "")
    return {"lab": pick("lab") or doc.get("source") or "", "report_no": pick("report number", "report no"),
            "submitted_by": pick("submitted", "reference submitted"),
            "received": pick("received", "receipt") or (received.group(1).strip() if received else ""),
            "issued": pick("report date", "issued"), "exhibits": [],
            "findings": given["facts_read_in_it"][:14] or ([given["read_before"]] if given["read_before"] else []),
            "indicates": [], "not_stated": []}


def _body(doc: dict[str, Any]) -> str:
    """A report's own text, without the header line we write (its record number and dates)."""
    text = re.sub(r"^.*?\[page 1\]", "", doc.get("text") or "", count=1, flags=re.DOTALL)
    return " ".join(re.findall(r"\w+", text.lower()))


def same_reports(docs: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Reports filed more than once - the same text under two record numbers: first id -> the others."""
    from difflib import SequenceMatcher

    out: dict[str, list[str]] = {}
    taken: set[str] = set()
    for i, a in enumerate(docs):
        if a["id"] in taken or len(_body(a)) < 400:
            continue
        for b in docs[i + 1:]:
            if b["id"] in taken or a.get("kind") != b.get("kind"):
                continue
            x, y = _body(a)[:8000], _body(b)[:8000]
            if x == y or (abs(len(x) - len(y)) < 0.05 * max(len(x), len(y)) and SequenceMatcher(None, x, y).quick_ratio() > 0.97
                          and SequenceMatcher(None, x, y).ratio() > 0.97):
                out.setdefault(a["id"], []).append(b["id"])
                taken.add(b["id"])
    return out


def forensics(case: Any, doc_ids: list[str], llm: Any = None) -> list[dict[str, Any]]:
    """Each report read in full: ``{doc, title, fir, lab, report_no, submitted_by, received, issued, exhibits,
    findings, indicates, not_stated, by, also_filed_as}``. A report filed twice is read once."""
    docs = [case.documents[d] for d in dict.fromkeys(doc_ids) if d in case.documents]
    twins = same_reports(docs)
    copies = {b for others in twins.values() for b in others}
    docs = [d for d in docs if d["id"] not in copies][:MOST_REPORTS]

    def read(doc: dict[str, Any]) -> dict[str, Any]:
        out = ask(llm, _ReportRead, dumps(_report_material(case, doc)), system=_REPORT_SYSTEM, kind="report_forensic",
                  version="v3", max_tokens=2200) if llm is not None else None
        got = out.model_dump() if out is not None else _from_records(case, doc)
        ref = doc.get("ref") or {}
        fir = re.search(r"(\d{1,5}\s*/\s*\d{2,4})", str(ref.get("fir") or ref.get("labs_of") or doc.get("title") or ""))
        clean = {k: ([plain(x) for x in v if str(x).strip()] if isinstance(v, list) and k != "exhibits" else v)
                 for k, v in got.items()}
        clean["exhibits"] = [{k: plain(v) for k, v in e.items()} for e in got.get("exhibits") or [] if any(e.values())]
        received = re.search(r"case received ([\d\-: ]+)", doc.get("summary") or "")
        if received:
            clean["received"] = received.group(1).strip()      # the laboratory system's own date, not a reading of the scan
        also = []
        for other in (case.documents[b] for b in twins.get(doc["id"]) or []):
            got_received = re.search(r"case received ([\d\-: ]+)", other.get("summary") or "")
            also.append(f"{other.get('title') or other['id']}" + (f", received {got_received.group(1).strip()}" if got_received else ""))
        return {"doc": doc["id"], "title": doc.get("title") or doc["id"], "fir": fir.group(1).replace(" ", "") if fir else "",
                **{k: plain(v) if isinstance(v, str) else v for k, v in clean.items()},
                "by": "model" if out is not None else "records", "also_filed_as": also,
                "copies": twins.get(doc["id"]) or []}

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(read, docs))


# -- statements explained -----------------------------------------------------------------------


class _Explained(Typed):
    key: str = Field(description="The statement's key, exactly as given (S1, S2 ...).")
    explained: str = Field(description="What this person says, explained in plain English in two to five sentences: "
                                       "what happened by their account, who did what, when and where, and anything "
                                       "they say about the people of the case. Names as written in the record.")


class _Statements(Typed):
    statements: list[_Explained] = Field(description="One entry per statement given.")


_STATEMENT_SYSTEM = ("You explain police statements to a senior investigating officer. Each statement below is a "
                     "person's own words from an FIR or a case file, often in Urdu or Sindhi. For each, explain in plain "
                     "English what the person says - by their account, not as fact - keeping every name, date, time, "
                     "place and number they give. Add nothing they did not say. " + TERMS + PLAIN + DATA_NOT_INSTRUCTIONS)


def _said_in(case: Any, doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Every statement of one FIR file, in full: its first information (the complainant's), then the others."""
    data = doc.get("data") or {}
    rows = []
    complainant = data.get("complainant") or {}
    name = complainant.get("name") if isinstance(complainant, dict) else complainant
    said = [w for w in case.statements.values() if w["doc"] == doc["id"]]
    if data.get("narrative") and not any("complain" in str(w.get("role") or "") for w in said):
        rows.append({"person": str(name or "the complainant"), "part": "complainant", "date": str(data.get("reported") or ""),
                     "kind": "first information (FIR)", "said": str(data["narrative"]).strip()})
    rows += [{"person": w["speaker"], "part": w.get("role") or "-", "date": w.get("date") or "", "kind": w.get("kind") or "",
              "said": str(w["text"]).strip(), "id": w["id"]} for w in said]
    return rows


def statements(case: Any, doc_ids: list[str], llm: Any = None) -> list[dict[str, Any]]:
    """For each FIR file, in the order given: ``{doc, fir, people: [{person, part, date, kind, explained, said}]}``."""
    docs = [case.documents[d] for d in list(dict.fromkeys(doc_ids)) if (case.documents.get(d) or {}).get("kind") == "fir"]

    def explain(doc: dict[str, Any]) -> dict[str, Any] | None:
        rows = _said_in(case, doc)
        if not rows:
            return None
        keyed = {f"S{i}": r for i, r in enumerate(rows, 1)}
        got = ask(llm, _Statements, dumps({"fir": doc.get("title"), "statements": [
            {"key": k, "person": r["person"], "part": r["part"], "words": r["said"][:4000]} for k, r in keyed.items()]}),
            system=_STATEMENT_SYSTEM, kind="report_statements", version="v2", max_tokens=2200) if llm is not None else None
        explained = {e.key.strip(): plain(e.explained) for e in (got.statements if got is not None else [])}
        return {"doc": doc["id"], "fir": doc.get("title") or doc["id"],
                "people": [{**r, "explained": explained.get(k, "")} for k, r in keyed.items()]}

    with ThreadPoolExecutor(max_workers=4) as pool:
        return [x for x in pool.map(explain, docs[:MOST_STATEMENT_FIRS]) if x]


# -- weighing the evidence ----------------------------------------------------------------------


_WEIGH_SYSTEM = (
    "You are Sherlock, a senior detective, writing the analysis section of a case report for the investigating "
    "officer. From the case material given - the officer's own words, the case's FIRs, the reports and what they "
    "found, the statements, the story of the case and the people - weigh the evidence as an experienced detective "
    "would. Weigh it properly: a finding that rules an explanation out (something that could not have happened that "
    "way) outweighs any number of findings that are merely consistent with it; say which finding does that. Use only "
    "the material; where a summary of a record and the record's own words (its first information, the statement) "
    "differ, the record's own words stand. Something a report did not test is not evidence that it is absent; a point "
    "about a report says only what THAT report states, and cites it. Where a finding has more than one possible "
    "cause, name the causes - never pick one the material does not give. Say only what a finding can show: a trace "
    "that fits more than one explanation decides none of them. Call no one a suspect unless a record does: say what "
    "the records tie them to and what to check. Work out what the records' dates and exhibits imply: when the event "
    "must have happened, the last time things were known to be as before, what was done afterwards. When a record "
    "measures something (a size, an amount, a weight, a time, a distance, a count), give the measurement in the "
    "point, and name every record that shows the same thing. "
    "Documents the officer uploaded (an inquiry report, a medical or forensic report, a statement, a memo, an audit, "
    "bank records) are "
    "records of the case too: use their dated steps, findings and leads; an inquiry's conclusion is that inquiry's "
    "finding - say so, and weigh the evidence it rests on. Cite the record ids in [brackets] where a point rests "
    "on one. Never use the words "
    "'inferred' or 'corroborated' - say what the record shows. No view on anyone's guilt as a fact: a view is a view.\n"
    "Write ONLY lines that start with these labels, in this order:\n"
    "QUESTION: the question this case turns on now (e.g. who did it; whether it happened as reported; how it was "
    "done; where the money or the property went)\n"
    "EXPLANATION: one possible answer, on the same line (two to four EXPLANATION lines), each followed by its lines:\n"
    "FOR: a point of the material that supports it\n"
    "AGAINST: a point that goes against it or that it cannot explain\n"
    "SETTLE: what would confirm or rule it out\n"
    "VIEW: your view in three to six sentences - which explanation fits the material best and why, what weakens it\n"
    "CLASH: what one source says | what another source (a report, a statement, the FIR, a document) says that "
    "cannot also be right | why it matters for the investigation - every such mismatch: between records, within one "
    "record (the FIR's place field and its own narrative), the section the FIR stands under and what the reports "
    "now show, two dates or times for one event. Facts only - never a guess about anyone's motive, never that "
    "someone lied or delayed on purpose (none if there is none)\n"
    "GAP: a question still open that matters and that the material does not already answer - three to six GAP lines\n"
    "ACTION: priority (high, medium or low) | what the officer should do, specifically (which record, which person, "
    "which place, which dates) | what it would settle - five to eight ACTION lines, the most urgent first\n"
    "PERSON: name | how the records tie them to THIS case | what to check about them - only people who could have "
    "been involved or know what happened, as the material ties them to this case; never a police officer doing his "
    "duty on the case (the IO, the officer who registered the FIR)\n"
    "SECTION: a heading for anything else the officer must see that the labels above do not cover (at most two)\n"
    "POINT: a point under the SECTION above it\n"
    "Plain English, short lines, concrete (names, dates, places, numbers). No other text. " + TERMS)
_LINE = re.compile(r"^\s*[-*•]?\s*\**(QUESTION|EXPLANATION|FOR|AGAINST|SETTLE|VIEW|CLASH|GAP|ACTION|PERSON|SECTION|POINT)\**\s*\d*\s*:\s*(.*)$")
_NUMBERED = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+(.+)$")


def read_weighing(text: str, known: set[str]) -> dict[str, Any]:
    """The analysis, from the labelled lines of the model's text."""
    out: dict[str, Any] = {"question": "", "explanations": [], "view": "", "clashes": [], "gaps": [], "actions": [],
                           "persons": [], "sections": []}
    listing = False                 # after an empty "EXPLANATION:" the model may number them on their own lines
    for line in (text or "").splitlines():
        m = _LINE.match(line)
        if not m:
            numbered = _NUMBERED.match(line)
            if listing and numbered:
                out["explanations"].append({"name": plain(real_refs(numbered.group(1).strip(), known)), "for": [],
                                            "against": [], "settle": []})
            continue
        label, value = m.group(1), plain(real_refs(m.group(2).strip(), known))
        listing = label in ("EXPLANATION", "FOR", "AGAINST", "SETTLE") and (listing or (label == "EXPLANATION" and not value))
        if not value:
            continue
        if label == "QUESTION":
            out["question"] = out["question"] or value
        elif label == "EXPLANATION":
            out["explanations"].append({"name": value, "for": [], "against": [], "settle": []})
        elif label in ("FOR", "AGAINST", "SETTLE") and out["explanations"]:
            out["explanations"][-1][label.lower()].append(value)
        elif label == "VIEW":
            out["view"] = f"{out['view']} {value}".strip()
        elif label == "CLASH":
            parts = [p.strip() for p in value.split("|")] + ["", ""]
            out["clashes"].append({"one": parts[0], "other": parts[1], "why": parts[2]})
        elif label == "GAP":
            out["gaps"].append(value)
        elif label == "ACTION":
            parts = [p.strip() for p in value.split("|")]
            priority = parts[0].lower() if parts[0].lower() in ("high", "medium", "low") else "medium"
            rest = parts[1:] if parts[0].lower() in ("high", "medium", "low") else parts
            settles = re.sub(r"^(?:to settle:?|to)\s+", "", rest[1], flags=re.IGNORECASE) if len(rest) > 1 else ""
            out["actions"].append({"priority": priority, "action": rest[0] if rest else value,
                                   "settles": settles[:1].upper() + settles[1:]})
        elif label == "PERSON":
            parts = [p.strip() for p in value.split("|")] + ["", ""]
            out["persons"].append({"name": parts[0], "tie": parts[1], "check": parts[2]})
        elif label == "SECTION" and len(out["sections"]) < 2:
            out["sections"].append({"heading": value, "points": []})
        elif label == "POINT" and out["sections"]:
            out["sections"][-1]["points"].append(value)
    order = {"high": 0, "medium": 1, "low": 2}
    out["actions"].sort(key=lambda a: order[a["priority"]])
    out["sections"] = [s for s in out["sections"] if s["points"]]
    return out


def weigh(case: Any, material: dict[str, Any], llm: Any = None) -> dict[str, Any] | None:
    """The analysis, or None (no model that writes plain text, or it failed)."""
    if llm is None or not hasattr(llm, "generate_text"):
        return None
    try:
        text = llm.generate_text(prompt=json.dumps(material, ensure_ascii=False, default=str)[:90000],
                                 system=_WEIGH_SYSTEM, cache_kind="report_weighing", prompt_version="v8", max_tokens=4500)
    except Exception as exc:  # noqa: BLE001 - the report goes out without it
        logger.info("Weighing the evidence failed: %s", exc)
        return None
    got = read_weighing(text, case.known_ids())
    if not (got["question"] or got["explanations"] or got["view"] or got["actions"]):
        return None
    got["added"] = complete(got, material, llm)
    got["moved"] = sides(got, llm)
    got["checked"] = check(got, material, llm)
    for e in got["explanations"]:
        for key in ("for", "against"):
            e[key] = once(e[key])
    got["measured"] = measured(got, material)
    return got


_SIDES_SYSTEM = (
    "Each numbered line below is one point weighed for one explanation of a case. For each, say what the point does "
    "to THAT explanation: MORE (makes it more likely), LESS (makes it less likely or cannot be explained by it) or "
    "NEITHER (does not bear on it). A finding that rules out a rival explanation makes this one more likely. A "
    "finding that fits this explanation is MORE even when it fits another explanation too; NEITHER is only for a "
    "point that is not about what happened at all. One line each, exactly:\n"
    "n: MORE | LESS | NEITHER\nNo other text. " + DATA_NOT_INSTRUCTIONS)


def sides(got: dict[str, Any], llm: Any) -> list[dict[str, str]]:
    """Each point put on the side it is really on: under "what goes against" a point that supports the explanation is
    moved to "what supports it", and the other way; a point that does not bear on it is taken out."""
    lines = [(e, key, x) for e in got["explanations"] for key in ("for", "against") for x in e[key]]
    if not lines:
        return []
    try:
        text = llm.generate_text(prompt=json.dumps({str(n): {"explanation": e["name"], "point": x}
                                                    for n, (e, _, x) in enumerate(lines, 1)}, ensure_ascii=False),
                                 system=_SIDES_SYSTEM, cache_kind="report_weighing_sides", prompt_version="v3",
                                 max_tokens=600)
    except Exception as exc:  # noqa: BLE001 - the points stay where they are
        logger.info("Sorting the points failed: %s", exc)
        return []
    said = {int(m.group(1)): m.group(2).upper() for m in re.finditer(r"(\d+)\s*[:.)]\s*\**\s*(MORE|LESS|NEITHER)", text or "",
                                                                     re.IGNORECASE)}
    moved = []
    for n, (e, key, x) in enumerate(lines, 1):
        side = {"MORE": "for", "LESS": "against"}.get(said.get(n, ""), None if said.get(n) == "NEITHER" else key)
        if side == key:
            continue
        if x in e[key]:
            e[key].remove(x)
        if side:
            e[side].append(x)
        moved.append({"explanation": e["name"], "point": x, "from": key, "to": side or "out (does not bear on it)"})
    return moved


# A measured finding: a number with its unit - a size, a weight, an amount of money, a time, a distance, a share.
_MEASURE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:mm|cm|km|m|kg|g|mg|ml|l|%|percent|rs\.?|pkr|usd|usdt|lakh|crore|"
                      r"millimet\w*|centimet\w*|met(?:er|re)s?|inch\w*|feet|foot|grams?|kilograms?|litres?|liters?|"
                      r"minutes?|hours?|seconds?|days?|rupees?)\b|(?:rs\.?|pkr|usd)\s*\d[\d,]*", re.IGNORECASE)
_WORD = re.compile(r"[a-z]{4,}")
_COMMON = {"report", "found", "shows", "with", "from", "that", "this", "which", "were", "have", "been", "approximately",
           "measuring", "about", "article", "item", "records", "record"}


def measured(got: dict[str, Any], material: dict[str, Any]) -> list[str]:
    """A record's own measurements, written into the point that rests on that record and speaks of the same thing,
    when the analysis gave the conclusion without them - for any case: the size of a mark, the weight of a seized
    substance, the amount of a transfer, the time between two calls. The officer sees the measured fact, not only what
    it was taken to mean."""
    text = json.dumps(got, ensure_ascii=False)
    done = []
    for r in material.get("reports_of_this_case") or []:
        rid = r.get("id") or ""
        facts = [f for f in r.get("findings") or [] if _MEASURE.search(f)
                 and not any(n in text for n in re.findall(r"\d+(?:\.\d+)?", _MEASURE.search(f).group(0)))]
        for e in got["explanations"]:
            for key in ("for", "against"):
                for i, point in enumerate(e[key]):
                    if rid not in point:
                        continue
                    words = set(_WORD.findall(point.lower())) - _COMMON
                    near = [f for f in facts if len(words & (set(_WORD.findall(f.lower())) - _COMMON)) >= 2][:2]
                    if not near:
                        continue
                    # ... with the measurements beside them about the same thing (the front of what was measured at
                    # the back, the second transfer of the same account): compared, they say what one cannot.
                    about = set().union(*(set(_WORD.findall(f.lower())) - _COMMON for f in near))
                    near += [f for f in facts if f not in near and len(about & (set(_WORD.findall(f.lower())) - _COMMON)) >= 2]
                    near = near[:3]
                    said = "measured in the record: " + "; ".join(f.rstrip(". ") for f in near) + f" [{rid}]"
                    point = re.sub(rf"\s*\[{re.escape(rid)}\]\s*\.?\s*$", "", point.rstrip(". "))     # the id once
                    e[key][i] = point + f" - {said}."
                    done.append(said)
                    facts = [f for f in facts if f not in near]
                    text = json.dumps(got, ensure_ascii=False)
    return done


def once(points: list[str]) -> list[str]:
    """The same point said twice under one heading is kept once (the fuller wording)."""
    out: list[str] = []
    for p in points:
        words = set(re.findall(r"[a-z0-9]{4,}", p.lower()))
        twin = next((i for i, q in enumerate(out) if words and (theirs := set(re.findall(r"[a-z0-9]{4,}", q.lower())))
                     and len(words & theirs) / min(len(words), len(theirs)) >= 0.5), None)
        if twin is None:
            out.append(p)
        elif len(p) > len(out[twin]):
            out[twin] = p
    return out


_COMPLETE_SYSTEM = (
    "You review a detective's analysis before it goes to the investigating officer. Below are the explanations it "
    "weighs, numbered, each with its points, and the findings of the case's reports and documents with their ids. "
    "Every finding that bears on an explanation must be weighed in it - above all a finding that rules an explanation "
    "out or makes it unlikely (something that could not have happened that way, a record that contradicts it, a "
    "measurement that shows how or when it happened). Only findings about what happened - the people, the acts, the "
    "places, the times, the money or property, the evidence itself - never about how the investigation was handled "
    "(custody of evidence, delays, leaks), which supports no explanation. A finding a record measures (a size, an "
    "amount, a time, a count) is missing when a point gives the same conclusion only from another record without "
    "the measurement: add the measured finding with its own record's id. For each finding that bears on an "
    "explanation but is in none of its "
    "points, write one line exactly:\n"
    "ADD: explanation number | FOR or AGAINST | the point in one plain sentence, with the record id in [brackets]\n"
    "At most six lines, the most decisive first. Write NONE when nothing that matters is missing. "
    + DATA_NOT_INSTRUCTIONS)


def complete(got: dict[str, Any], material: dict[str, Any], llm: Any) -> list[dict[str, str]]:
    """The decisive findings the analysis left out, put under the explanations they bear on (a report's finding that
    rules an explanation out can be crowded out when a long uploaded document fills the material)."""
    if not got["explanations"]:
        return []
    findings = [{"report": r.get("report"), "id": r.get("id"), "findings": r.get("findings"), "indicates": r.get("indicates")}
                for r in material.get("reports_of_this_case") or []]
    findings += [{"document": d.get("title"), "id": d.get("id"), "most_important": d.get("most_important"),
                  "findings": d.get("findings")} for d in material.get("other_documents") or []]
    given = {"explanations": {str(i): {"explanation": e["name"], "for": e["for"], "against": e["against"]}
                              for i, e in enumerate(got["explanations"], 1)}, "what_the_records_found": findings}
    try:
        text = llm.generate_text(prompt=json.dumps(given, ensure_ascii=False)[:50000], system=_COMPLETE_SYSTEM,
                                 cache_kind="report_weighing_complete", prompt_version="v4", max_tokens=1000)
    except Exception as exc:  # noqa: BLE001 - the analysis stands as written
        logger.info("Completing the analysis failed: %s", exc)
        return []
    added = []
    for m in re.finditer(r"ADD:\s*\**\s*(\d+)\s*\|\s*(FOR|AGAINST)\s*\|\s*(.+)", text or "", re.IGNORECASE):
        n, side, point = int(m.group(1)), m.group(2).lower(), plain(m.group(3).strip())
        if 1 <= n <= len(got["explanations"]) and len(added) < 6 and point:
            got["explanations"][n - 1][side].insert(0, point)        # the decisive point first
            added.append({"explanation": got["explanations"][n - 1]["name"], "side": side, "point": point})
    return added


_CHECK_SYSTEM = (
    "You check a detective's analysis against the records before it goes to the investigating officer. Below are "
    "numbered lines of the analysis, and what the case's FIR, reports and documents found. Be sparing: mark a line "
    "only when you are sure - a fair summary, other words for the same thing, "
    "or a record you were not shown is never a reason. A line is WRONG when it "
    "says a record found something it does not say, cites a record for what another record says, contradicts what a "
    "record found, treats something a report did not test as absent, claims a finding rules something out or "
    "proves something it cannot (a trace that fits several explanations proves none), or calls a person a suspect or "
    "guilty when no record does, or - a line marked [supports: X] or [goes against: X] - does not bear on X or argues "
    "the other way. A line that is an opinion, a question or a plan is "
    "not wrong for that. A line that "
    "gives only part of what a record says, paraphrases it, or cites a neighbouring id of the same document is NOT "
    "wrong - mark it only when the records say something different or nothing like it. Write one line for each "
    "wrong line, exactly:\n"
    "WRONG: line number | what the record actually says\n"
    "and nothing else; write NONE when every line is right. " + DATA_NOT_INSTRUCTIONS)


# What the checker must say for a line to go: that the record says otherwise, or says nothing of it.
_CONTRARY = re.compile(r"\b(?:does not|doesn't|did not|do not|never|no record|not (?:state|say|mention|show|find|support)|"
                       r"contradict\w*|opposite|instead|rather than|wrong(?:ly)?|misattribut\w*|attributes? (?:it|this) to)\b",
                       re.IGNORECASE)


# ... and a reason that approves the line, or only quibbles over its wording, never takes it out.
_STANDS = re.compile(r"\bconsistent with\b|rule[sd]? out|absolute|sufficient|wording|phras\w+|too strong|overstat\w+|"
                     r"certain(?:ty)?\b|\bcorrect\b|reasonable|accurate(?:ly)?|is supported|fair\b|specifically|"
                     r"explicit(?:ly)?|exactly|as a distinct", re.IGNORECASE)


def check(got: dict[str, Any], material: dict[str, Any], llm: Any) -> list[dict[str, str]]:
    """Each claim of the analysis checked against what the records found; a wrong one is taken out. Returns what was
    taken out and why (kept on the analysis for the trace)."""
    lines: list[tuple[str, Any, int | None]] = []          # (text, list it sits in, index)
    for e in got["explanations"]:
        for key in ("for", "against"):
            for i, x in enumerate(e[key]):
                lines.append((f"[{'supports' if key == 'for' else 'goes against'}: {e['name']}] {x}", e[key], i))
    for i, c in enumerate(got["clashes"]):
        lines.append((f"{c['one']} | {c['other']}", got["clashes"], i))
    # Sherlock's view is his labelled opinion, kept whole: what it rests on is checked as the points above.
    if not lines:
        return []
    records = {"reports": material.get("reports_of_this_case") or [], "documents": [
        {k: d.get(k) for k in ("id", "title", "findings", "evidence")} for d in material.get("other_documents") or []],
        "the_firs": [{"fir": f.get("fir"), "id": f.get("file"), "first_information": f.get("first_information"),
                      "what_happened": f.get("what_happened")}
                     for t in (material.get("everything_on_the_target") or {}).get("targets") or []
                     for f in t.get("firs") or [] if f.get("is_this_case")],
        "statements": material.get("statements") or [], "story": material.get("story_of_the_case") or []}
    try:
        text = llm.generate_text(prompt=json.dumps({"analysis_lines": {str(n): x for n, (x, _, _) in enumerate(lines, 1)},
                                                    "what_the_records_found": records}, ensure_ascii=False, default=str)[:70000],
                                 system=_CHECK_SYSTEM, cache_kind="report_weighing_check", prompt_version="v6",
                                 max_tokens=1200)
    except Exception as exc:  # noqa: BLE001 - unchecked, the analysis stands as written
        logger.info("Checking the analysis failed: %s", exc)
        return []
    wrong: dict[int, str] = {}
    for m in re.finditer(r"WRONG:\s*\**\s*(\d+)\s*\|?\s*(.*)", text or ""):
        n, why = int(m.group(1)), m.group(2).strip()
        if re.search(r"not (?:in|among) the (?:provided|given)|not provided|was not (?:shown|given)|is not an? "
                     r"(?:wrong|error)|not wrong", why, re.IGNORECASE):
            continue                    # the checker was not shown that record, or itself says the line stands
        if not _CONTRARY.search(why) or _STANDS.search(why):
            continue                    # a quibble, or the checker's own reason says the line is right: it stands
        if 1 <= n <= len(lines):
            wrong[n] = why
    removed = []
    for n in sorted(wrong, reverse=True):                    # from the end, so the indexes stay right
        line, store, i = lines[n - 1]
        removed.append({"line": line, "record_says": wrong[n]})
        if store is not None:
            store.pop(i)
    return removed
