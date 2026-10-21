"""P7 Story writer: the case under investigation told as a story, for the investigating officer.

An officer reading a case wants it in the order it happened: when the incident took place,
when and by whom the FIR was registered, who was named, who was arrested and when, what the
accused said, what was recovered, which reports were sent and what came back, and where the
case stands now. P7 reads ONLY the case's own material (``case_scope``: this FIR, the FIRs
that name it, their statements, case diaries and reports, the officer's own words) and
writes:

    in_short   three to five sentences - the case as a story
    events     the steps of the case in date order, each with the ids it rests on
    people     each person of the case: their part in it, their status, what they did or said

Every event must cite an id of the case's own material: one that cites nothing real is
dropped. Without the model the same story is laid out from the records with templates.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field, field_validator

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.team.runtime import PLAIN, Typed, ask, dumps, real_refs

TEXT = 1600          # characters of an FIR's own words given to the writer
SAID = 600           # of one statement


class _Event(Typed):
    when: str = Field(description="The date, and the time if recorded, as dd-mm-yyyy hh:mm. Empty when no record dates it.")
    what: str = Field(description="What happened, in one or two plain sentences: who did what, where. Name people by "
                                  "name and give their part (accused, complainant, investigating officer).")
    sources: list[str] = Field(description="The ids this rests on, exactly as given: D#, W#, F#.")


class _Person(Typed):
    name: str = Field(description="The person's name as the records give it.")
    part: str = Field(description="Their part in THIS case: victim, complainant, accused, co-accused, witness, "
                                  "investigating officer, or as the records say.")
    status: str = Field(description="Only as the records say: arrested (with the date), in custody, absconding, on "
                                    "bail, not arrested, deceased - or 'not on record'.")
    did: str = Field(description="One line: what they did or said in this case, per the records.")
    sources: list[str] = Field(description="The ids this rests on.")


class _Story(Typed):
    in_short: list[str] = Field(description="Three to five short sentences telling the case as a story, from the "
                                            "incident to where it stands now. Each ends with its ids in [brackets].")
    events: list[_Event] = Field(description="Every step of the case in the order it happened: the incident, the FIR, "
                                             "arrests, interrogations and disclosures, recoveries, statements recorded, "
                                             "reports sent and received with what they found, the case's position.")
    people: list[_Person] = Field(description="Each person of this case, the accused first.")

    @field_validator("in_short", mode="before")
    @classmethod
    def _lines(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value


_SYSTEM = ("You are a senior investigating officer writing up a case for a colleague who must take it over. From "
           "the case's own records below - its FIR, the FIRs that name it, the statements, case diaries and reports "
           "- tell the case as a STORY in plain English, in the order it happened: when and where the incident took "
           "place, when the FIR was registered and by whom, who was named, who was arrested and when, what an "
           "accused disclosed, what was recovered, which reports were sent and what they found, and where the case "
           "stands. Records are often in Urdu: write in English, names as spelt in the records. Use ONLY what the "
           "records say: no guess, no view on guilt, nothing filled in from another case. A step the records do not "
           "date has an empty `when`. Every event and every sentence of `in_short` cites the ids it rests on, only "
           "ids given below. Leave out what does not belong to this case. " + PLAIN + DATA_NOT_INSTRUCTIONS)


def _fir(doc: dict[str, Any]) -> dict[str, Any]:
    from sherlocks.team.case_scope import _person_names

    data = doc.get("data") or {}
    ref = doc.get("ref") or {}
    return {"id": doc["id"], "fir": f"{ref.get('fir_no')}/{str(ref.get('fir_year') or '')[-2:]}",
            "police_station": data.get("police_station") or "", "sections": data.get("sections") or "",
            "occurred": data.get("occurred") or "", "registered": data.get("reported") or "",
            "place": str(data.get("place") or "")[:240],
            "people": [{"name": n, "role": r} for n, r in _person_names(data)][:16],
            "result_so_far": str(data.get("investigation_result") or "")[:400],
            "positions": [{"date": p.get("date"), "position": p.get("position") or p.get("status")}
                          for p in data.get("case_positions") or [] if isinstance(p, dict)][:6],
            "text": (doc.get("text") or "")[:TEXT]}


def material(case: Any, scope: dict[str, Any]) -> dict[str, Any]:
    """The case's own records, short enough for one call."""
    from sherlocks.linkgraph.relevance import the_new_case
    from sherlocks.team.report_graph import _diary_steps

    docs = {d: case.documents[d] for d in scope["documents"] if d in case.documents}
    main = docs.get(scope.get("main") or "")
    linked = [docs[d] for d in scope["linked"] if d in docs]
    firs = [main, *linked] if main else linked
    cards = (case.register or {}).get("cards") or {}
    reports = []
    for d, doc in docs.items():
        card = cards.get(d) or {}
        if doc.get("kind") == "fir" or not (doc.get("kind") == "lab" or card.get("record")):
            continue
        received = re.search(r"case received ([\d\-: ]+)", doc.get("summary") or "")
        reports.append({"id": d, "title": card.get("title") or doc.get("title"), "record": card.get("record") or doc["kind"],
                        "received": received.group(1).strip() if received else "", "summary": (doc.get("summary") or "")[:300],
                        "found": [f"{x.get('name')}: {x.get('value')}" + (f" ({x['when']})" if x.get("when") else "")
                                  for x in card.get("fields") or [] if x.get("value")][:8]})
    steps = []
    for doc in firs:
        diaries = [x for x in (doc.get("data") or {}).get("case_diaries") or [] if isinstance(x, dict)]
        steps += [{**s, "source": doc["id"]} for s in _diary_steps(diaries)][:10]
    return {
        "this_case": the_new_case(case),
        "fir": _fir(main) if main else None,
        "firs_that_name_this_case": [_fir(d) for d in linked],
        "statements": [{"id": w["id"], "speaker": w["speaker"], "role": w.get("role") or "", "kind": w["kind"],
                        "date": w.get("date") or "", "recorded_by": w.get("recorded_by") or "", "text": w["text"][:SAID]}
                       for w in (case.statements[s] for s in scope["statements"] if s in case.statements)][:14],
        "investigation_steps": steps[:16],
        "reports": reports[:10],
        "awaited": [{"what": p["what"], "record": p["record"], "requested_on": p.get("requested_on"), "source": p["doc"]}
                    for p in (case.register or {}).get("pending") or [] if p.get("doc") in docs],
        "officer_said": [{"id": f["id"], "said": f["statement"].replace("Stated by the officer: ", "")}
                         for f in case.live_facts() if f.get("by") == "officer"][-12:],
    }


def _templates(case: Any, scope: dict[str, Any], given: dict[str, Any]) -> dict[str, Any]:
    """The story without the model: one line per dated record of the case."""
    events: list[dict[str, Any]] = []
    for f in [given["fir"], *given["firs_that_name_this_case"]]:
        if not f:
            continue
        accused = ", ".join(p["name"] for p in f["people"] if p["role"].startswith("accused"))
        complainant = next((p["name"] for p in f["people"] if p["role"] == "complainant"), "")
        if f["occurred"]:
            events.append({"when": f["occurred"], "what": f"Incident of FIR {f['fir']} ({f['sections'] or 'offence not recorded'})"
                                                          + (f" at {f['place']}" if f["place"] else "") + ".",
                           "sources": [f["id"]]})
        events.append({"when": f["registered"], "what": f"FIR {f['fir']} registered" + (f" at PS {f['police_station']}" if f["police_station"] else "")
                       + (f"; complainant {complainant}" if complainant else "") + (f"; accused named: {accused}" if accused else "") + ".",
                       "sources": [f["id"]]})
        events += [{"when": p["date"] or "", "what": f"Case position: {p['position']}.", "sources": [f["id"]]}
                   for p in f["positions"][:3] if p.get("position")]
    events += [{"when": s["when"], "what": s["what"], "sources": [s["source"]]} for s in given["investigation_steps"]]
    events += [{"when": w["date"], "what": f"Statement of {w['speaker']}" + (f" ({w['role']})" if w["role"] else "")
                + " recorded" + (f" by {w['recorded_by']}" if w["recorded_by"] else "") + ".", "sources": [w["id"]]}
               for w in given["statements"]]
    for r in given["reports"]:
        when = r["received"] or next((x.split("(")[-1].rstrip(")") for x in r["found"] if "(" in x), "")
        events.append({"when": when, "what": f"{r['title']}" + (" received by the laboratory" if r["received"] else "")
                       + (f": {'; '.join(r['found'][:3])}" if r["found"] else "") + ".", "sources": [r["id"]]})
    people = [{"name": p["name"], "part": p["role"], "status": "arrested" if "arrested" in p["role"] else "not on record",
               "did": f"Named in FIR {p['fir']}.", "sources": []} for p in scope["people"]]
    return {"in_short": [], "events": events, "people": people}


def write(case: Any, scope: dict[str, Any], llm: Any = None) -> dict[str, Any]:
    """The story of the case: ``{in_short, events, people, by}``."""
    from sherlocks.evidence.case_report import _date_key
    from sherlocks.team.case_scope import source_doc

    given = material(case, scope)
    if not (given["fir"] or given["firs_that_name_this_case"] or given["statements"] or given["reports"]
            or given["officer_said"]):
        return {"in_short": [], "events": [], "people": [], "by": "nothing to tell"}
    out = ask(llm, _Story, dumps(given), system=_SYSTEM, kind="report_story", version="v1", max_tokens=2200)
    story, by = (out.model_dump(), "model") if out is not None else (_templates(case, scope, given), "templates")
    docs = set(scope["documents"])
    own = {i for i in case.known_ids() if source_doc(case, i) in docs} | set(scope["facts"])

    def real(ids: list[str]) -> list[str]:
        return [i for i in dict.fromkeys(str(x).strip("[] ") for x in ids or []) if i in own]

    events = []
    for e in story["events"]:
        ids = real(e.get("sources") or [])
        if ids and str(e.get("what") or "").strip():
            events.append({"when": str(e.get("when") or "").strip() or "date not recorded",
                           "what": real_refs(str(e["what"]).strip(), own), "sources": ids})
    # By day; within a day the writer's own order stands (a step is often recorded without its time).
    seen: set[tuple[str, str]] = set()
    events = [e for e in sorted(events, key=lambda e: _date_key(e["when"])[:10])
              if (e["when"], e["what"]) not in seen and not seen.add((e["when"], e["what"]))]
    people = [{"name": str(p.get("name") or "").strip(), "part": str(p.get("part") or "").strip() or "-",
               "status": str(p.get("status") or "").strip() or "not on record", "did": real_refs(str(p.get("did") or ""), own),
               "sources": real(p.get("sources") or [])} for p in story["people"] if str(p.get("name") or "").strip()]
    in_short = [real_refs(str(x), own) for x in story["in_short"] if str(x).strip()][:5]
    return {"in_short": in_short, "events": events[:40], "people": people[:16], "by": by}
