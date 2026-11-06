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
from sherlocks.team.runtime import PLAIN, TERMS, Typed, ask, dumps, real_refs

TEXT = 1600          # characters of an FIR's own words given to the writer
SAID = 600           # of one statement
DOC_STEPS = 70       # dated steps of the documents the officer uploaded (an inquiry's chronology)
MOST_EVENTS = 60


class _Event(Typed):
    when: str = Field(description="The date, and the time if recorded, as dd-mm-yyyy hh:mm. Empty when no record dates it.")
    what: str = Field(description="What happened, in one or two plain sentences: who did what, where. Name people by "
                                  "name and give their part (accused, complainant, investigating officer).")
    sources: list[str] = Field(description="The ids this rests on, exactly as given: D#, W#, F#.")


class _Person(Typed):
    name: str = Field(description="The person's name as the records give it.")
    part: str = Field(description="Their part in THIS case: victim, complainant, accused, co-accused, witness, "
                                  "investigating officer, or as the records say. Family of the victim or the accused "
                                  "with the relation the records give: 'mother of the victim', 'brother of the "
                                  "accused' - never only 'family'.")
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
    people: list[_Person] = Field(description="Each person of this case, the accused first - everyone its FIR and records "
                                              "name: the victim, the complainant, family members the records mention (who "
                                              "last saw or spoke to the victim), witnesses, people the documents raise "
                                              "questions about, the officers.")

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
           "ids given below. Documents the officer uploaded (an inquiry report, a medical report, an audit, a memo) give dated "
           "steps with their own ids (F#): use them - always the last known movements of the people at the centre of "
           "the case, when and where the key events happened and who discovered or reported them, examinations, "
           "recoveries, what happened to the property and the evidence - one event a step, the important ones, not "
           "every call. Copy each name letter for letter as the records spell it. Leave out what does not belong to "
           "this case. " + TERMS + PLAIN
           + DATA_NOT_INSTRUCTIONS)


class _Cast(Typed):
    people: list[_Person] = Field(description="Each person of this case: the victim first, then the accused, the "
                                              "complainant, family members the records mention, witnesses, people the "
                                              "documents raise questions about, and the officers who handled it.")


_CAST_SYSTEM = ("You are a senior investigating officer listing the people of a case for a colleague taking it over. "
                "From the case's own records below, list everyone who matters to the case - the victim, the accused, "
                "the complainant, family members (who last saw or spoke to the victim), witnesses, people the documents "
                "raise questions about, and the officers who registered, investigated or handled the evidence - each "
                "with their part, their status only as the records give it, and one line of what they did or said, "
                "citing the ids given. At most thirty, the most important first. Copy each name letter for letter "
                "as the records spell it - never respell or translate a name. A family tie to the victim or the accused "
                "only when a record says whose relative the person is. "
                + TERMS + PLAIN + DATA_NOT_INSTRUCTIONS)


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
        "documents_uploaded": documents(case, scope),
    }


def documents(case: Any, scope: dict[str, Any]) -> list[dict[str, Any]]:
    """The documents the officer uploaded, as read in full (R7): what each is, its dated steps and its people."""
    out = []
    for d in scope["documents"]:
        doc = case.documents.get(d) or {}
        r = doc.get("reading")
        if not r:
            continue
        out.append({"id": d, "title": doc.get("title"), "what_it_is": r.get("kind") or "", "summary": r.get("summary") or "",
                    "dated_steps": [{"id": e["fact"], "when": f"{e['when']} {e['time']}".strip(), "what": e["what"]}
                                    for e in r.get("events") or [] if case.usable(e["fact"])][:DOC_STEPS],
                    "people": [f"{p['name']} ({p['part']})" for p in r.get("people") or []][:40]})
    return out


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


def respelt(case: Any, scope: dict[str, Any], names: list[str]) -> dict[str, str]:
    """Name words written by the model that no record of the case holds, each with the one record's name word that
    sounds the same - or nothing, when there is none or more than one."""
    from difflib import SequenceMatcher

    from sherlocks.linkgraph.normalize import sounds_like
    from sherlocks.team.case_scope import _person_names

    texts = " ".join(str((case.documents.get(d) or {}).get("text") or "") for d in scope["documents"]).lower()
    have = set(re.findall(r"[a-z]{3,}", texts))
    known: set[str] = set()
    for d in scope["documents"]:
        doc = case.documents.get(d) or {}
        known |= {w for p in (doc.get("reading") or {}).get("people") or [] for w in re.findall(r"[A-Za-z]{3,}", p["name"])}
        known |= {w for n, _ in _person_names(doc.get("data") or {}) for w in re.findall(r"[A-Za-z]{3,}", n)}
    out: dict[str, str] = {}
    for word in {w for n in names for w in re.findall(r"[A-Za-z]{3,}", n)}:
        if word.lower() in have:
            continue
        like = [k for k in known if k.lower() != word.lower() and sounds_like(word, k) and sounds_like(k, word)]
        if like:
            best = max(like, key=lambda k: SequenceMatcher(None, word.lower(), k.lower()).ratio())
            if sum(1 for k in like if k.lower() == best.lower()) == len(like) or len({k.lower() for k in like}) == 1:
                out[word] = best
    return out


def write(case: Any, scope: dict[str, Any], llm: Any = None, net: Any = None) -> dict[str, Any]:
    """The story of the case: ``{in_short, events, people, by}``."""
    from sherlocks.evidence.case_report import _date_key
    from sherlocks.team.case_scope import source_doc

    given = material(case, scope)
    if not (given["fir"] or given["firs_that_name_this_case"] or given["statements"] or given["reports"]
            or given["officer_said"] or given["documents_uploaded"]):
        return {"in_short": [], "events": [], "people": [], "by": "nothing to tell"}
    from concurrent.futures import ThreadPoolExecutor

    # The steps and the people side by side: one answer holding sixty steps had no room left for the people.
    with ThreadPoolExecutor(max_workers=2) as pool:
        told = pool.submit(ask, llm, _Story, dumps(given), system=_SYSTEM, kind="report_story", version="v5",
                           max_tokens=6000)
        cast = pool.submit(ask, llm, _Cast, dumps(given), system=_CAST_SYSTEM, kind="report_cast", version="v3",
                           max_tokens=4000)
        out, cast = told.result(), cast.result()
    story, by = (out.model_dump(), "model") if out is not None else (_templates(case, scope, given), "templates")
    if cast is not None and len(cast.people) >= len(story["people"]):
        story["people"] = [p.model_dump() for p in cast.people]
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
    def status(text: Any) -> str:
        """Only what the records say about the person: the system's "not said yet" wording is not a status."""
        text = str(text or "").strip()
        return "not on record" if not text or re.search(r"not said|target of the search|not known to be", text, re.IGNORECASE) \
            else text

    people = [{"name": re.sub(r"\s*\([^)]*\)", "", str(p.get("name") or "")).strip(),       # "X (deceased)": a name only
               "part": str(p.get("part") or "").strip() or "-",
               "status": status(p.get("status")), "did": real_refs(str(p.get("did") or ""), own),
               "sources": real(p.get("sources") or [])} for p in story["people"] if str(p.get("name") or "").strip()]
    in_short = [real_refs(str(x), own) for x in story["in_short"] if str(x).strip()][:5]
    # A name the model respelt ("Kasim" for the records' "Qasim"): a name word found in no record of the case is
    # replaced by the one name word of the records that sounds the same.
    for wrong, right in respelt(case, scope, [p["name"] for p in people]).items():
        swap = re.compile(rf"\b{re.escape(wrong)}\b")
        for p in people:
            p["name"], p["did"], p["part"] = swap.sub(right, p["name"]), swap.sub(right, p["did"]), swap.sub(right, p["part"])
        for e in events:
            e["what"] = swap.sub(right, e["what"])
        in_short = [swap.sub(right, x) for x in in_short]
    if net is not None:
        # One spelling per person: a name the records give another way ("Kamraan Aslam") takes the graph's.
        from sherlocks.linkgraph.normalize import sounds_like

        for p in people:
            pid = net.resolve(p["name"])
            shown = net.name(pid) if pid in net.people else ""
            if shown and shown != p["name"] and sounds_like(shown, p["name"]) and sounds_like(p["name"], shown):
                old, p["name"] = p["name"], shown
                for e in events:
                    e["what"] = e["what"].replace(old, shown)
                in_short = [x.replace(old, shown) for x in in_short]
                for q in people:
                    q["part"], q["did"] = q["part"].replace(old, shown), q["did"].replace(old, shown)
    return {"in_short": in_short, "events": events[:MOST_EVENTS], "people": one_each(people)[:30], "by": by}


_HONORIFIC = re.compile(r"^(?:mian|mr|ms|mrs|haji|hafiz|syed|sayed|chaudhry|ch|malik|rana|میاں|حاجی|حافظ|سید|چوہدری|ملک|رانا)\.?\s+",
                        re.IGNORECASE)


def one_each(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row a person: the same name with or without a title ("Mian Muhammad Ashfaq" and "Muhammad Ashfaq"), or
    with the same words, is one person - their parts joined, the first row kept."""
    from sherlocks.linkgraph.normalize import name_key

    def key(name: str) -> str:
        return name_key(_HONORIFIC.sub("", str(name or "").strip())) or str(name or "").strip().lower()

    out: list[dict[str, Any]] = []
    for p in people:
        same = next((q for q in out if key(q["name"]) == key(p["name"])), None)
        if same is None:
            out.append(p)
            continue
        if p["part"] not in ("-", "") and p["part"].lower() not in same["part"].lower():
            same["part"] = f"{same['part']}; {p['part']}"
        if same["status"] == "not on record" and p["status"] != "not on record":
            same["status"] = p["status"]
        same["sources"] = list(dict.fromkeys([*same["sources"], *p["sources"]]))
    return out
