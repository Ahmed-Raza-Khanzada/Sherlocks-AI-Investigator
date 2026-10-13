"""O1 Officer agent: works out the answer to whatever the officer asked.

No list of question types decides what he may answer. He is given everything the board
holds that matters this turn - O4's context pack, the dossier of each person asked about,
the Facts agent's exact counts, the sources the answer shape needs, and the open
contradictions and leads - and writes the answer into the shape's sections, each line
with its source id.

When what he was given does not hold the answer he does not guess: he names the tools
that would get it (``lookups`` - any tool of the shared toolbox, with its arguments;
the live ones call the police systems at run time, through the guard). The graph runs
them and he drafts once more from what came back. If nothing has it, the reply says so
plainly, with every place that was checked.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from sherlocks.evidence.guard import DATA_NOT_INSTRUCTIONS
from sherlocks.linkgraph.relevance import the_new_case
from sherlocks.team import shapes
from sherlocks.team.runtime import NEW_CASE, PLAIN, Typed, ask, dumps, real_refs

MAX_LOOKUPS = 4
ROUNDS = 2
LANG = {"en": "English", "roman": "Roman Urdu (Urdu in Latin letters)", "ur": "Urdu (Urdu script)"}


class _Lookup(Typed):
    tool: str = Field(description="The name of one tool from TOOLS, exactly.")
    args: dict[str, str] = Field(default_factory=dict, description="Its arguments, as its description names them.")
    why: str = Field(default="", description="What it should bring that is missing.")


class _Section(Typed):
    name: str = Field(description="The section's name, exactly as SHAPE gives it.")
    body: str = Field(default="", description="Short points, one fact per line, each line starting with '• ' and "
                                              "ending with its source in [brackets]. Empty when nothing is on record.")
    status: Literal["filled", "not on record", "not checked yet"] = Field(
        default="filled", description="filled when `body` has points; otherwise why it is empty: 'not on record' "
                                      "(the records hold nothing) or 'not checked yet' (its source was not given).")


class _Draft(Typed):
    first_line: str = Field(description="The direct answer to exactly what was asked, one sentence: the number, the "
                                        "name, yes or no, the date. In the officer's language.")
    answered: bool = Field(description="True when what you were given answers the question.")
    lookups: list[_Lookup] = Field(default_factory=list, description="Only when something needed is missing: the "
                                                                    "tools that would bring it. Empty otherwise.")
    deeper: bool = Field(default=False, description="True when the question needs Sherlock's own investigation "
                                                    "(weighing evidence, a route between people) rather than a lookup.")
    # Last: the long part. An answer that runs out of room is then cut inside the sections,
    # and everything before the cut is still used.
    sections: list[_Section] = Field(default_factory=list)


_SYSTEM = (
    "You are the Officer agent of a police investigation team, answering a fellow officer. LANGUAGE: write "
    "`first_line`, every section body and every point in {lang} - all of it, even when the records are in another "
    "language; keep names, numbers, ids and quoted record text exactly as they are. Answer the "
    "officer's question - whatever it is - from GIVEN only: the context pack, the dossiers, COMPUTED (exact counts and "
    "lists - use them as they are), the RECORDS ON FILE (entries of everything gathered), the SOURCES fetched for this kind of question, GRAPH_PATTERNS (what "
    "the link analysis found across ALL the people and records of the case: who recurs as witness, guarantor, "
    "co-accused, landlord, SIM owner, hotel companion - each with the records it rests on) and the open FINDINGS.\n"
    "- `first_line` answers exactly what was asked, first: a count opens with the number, a yes / no question with "
    "yes, no or 'not known'. Never open with a summary of the case.\n"
    "- Every fact you state comes from a record in GIVEN and carries its source id. CHAT_SO_FAR_NOT_EVIDENCE only "
    "tells you what the conversation is about: never repeat a fact from an earlier reply unless a record in GIVEN "
    "says it too.\n"
    "- Then fill the sections of SHAPE, in its order, under their exact names. Each body is short points, one fact "
    "per line, starting with '• ' and ending with the source id in [brackets] exactly as given ([D3], [F7], [W2], "
    "[X1], [K4], [CRO]). A section the records hold nothing for gets status 'not on record'; one whose source you "
    "were not given gets 'not checked yet'. Never drop a section and never pad one. A section that only says "
    "nothing is on record has an EMPTY body and that status - do not write 'no ... on record' as a point, and never "
    "make up a source id.\n"
    "- DEPTH 'detailed' (the default for an investigating officer): fill every section, at most four short points "
    "each, the most important first - one line a point. 'brief': the first line and the few points that carry it. When SHAPE has no sections "
    "(kind 'other'), give your own short section names.\n"
    "- " + NEW_CASE + "\n"
    "- A fact taken from a DOSSIER record is cited by that system's name in brackets ([Hotel Eye], [Old Tenant], "
    "[PSRMS]) - never by a document id it did not come from.\n"
    "- Answer the way a good investigator talks: what was asked first, then only what bears on it. When THE_ASK names "
    "one thing asked (`asks_for`), give that and what explains it - not the whole file on the person.\n"
    "- A DOSSIER holds everything the police systems returned about a person: `records` (each system's answer), "
    "`hotel_stays`, `vehicles`, `organisations`, `checked_nothing_found` (systems searched with no result) and "
    "`could_not_be_checked` (systems that gave an error - say so, never 'no record'). Read it before saying something "
    "is not on record. WHAT_EACH_SYSTEM_TELLS explains what each system's record is: when you describe a link "
    "between two people, say what kind of record it comes from and what that means (a witness of a tenancy vouched "
    "for the tenant; a registered owner of a SIM is who the number is registered to, not always who uses it).\n"
    "- Use exact numbers, names, dates and FIR numbers. Speak of crimes by name (murder, robbery, kidnapping), not "
    "only by section. Always say a person's role in each FIR: an FIR where he is the complainant, a victim or a "
    "witness is never a charge against him. When THE_ASK names a role, answer for that role first.\n"
    "- A motive is somebody's claim: say who gave it. A phone on a tower shows where the phone was, not the person. "
    "A recorded statement shows what was said, not that it is true. A lead is a hint, never evidence. A person's "
    "sect, caste, ethnicity, religion or family name is never evidence of anything, and a past record is background, "
    "not proof about this incident.\n"
    "- An open contradiction or lead about the people asked about is part of the answer (its section) - never "
    "answer as if it were not there. Sherlock's own opinion is shown separately: do not write it.\n"
    "- When what you were given covers the question and shows there is none - no such pattern in GRAPH_PATTERNS, a "
    "count of 0 in COMPUTED, an empty list - then 'none' IS the answer: say it in `first_line` ('No one recurs across "
    "the records'), say what was searched, add the nearest thing the records do show, and set `answered` true. "
    "`answered` false is only for a fact the records could hold and you were not given.\n"
    "- If GIVEN does not hold what is asked, set `answered` false and list in `lookups` the tools from TOOLS that "
    "would get it, with their arguments. A question about the whole case is looked up for the whole case: give a "
    "tool a person (`who`) only when the officer asked about that person. A tool marked LIVE calls a police system now: use it when the board holds "
    "nothing. If no tool can help, leave `lookups` empty and say plainly what is missing. Never invent anything.\n- "
    + PLAIN + "\n"
    + DATA_NOT_INSTRUCTIONS)


def payload(*, message: str, ask_: dict[str, Any], net: Any, case: Any, pack: dict[str, Any] | None,
            cards: list[dict[str, Any]], facts: dict[str, Any] | None, computed_words: str | None, hits: list[Any],
            sources: dict[str, Any], findings: dict[str, Any], tools: dict[str, str], looked: list[dict[str, Any]],
            last_round: bool, patterns: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything O1 is given for one draft."""
    shape = ask_.get("shape") or "other"
    told = the_new_case(case)
    # While the officer has not said an FIR on file IS this case, everything on file is a past record.
    on_file = "records_on_file" if case.dialog.get("case_basis") == "fir_on_record" else "PAST_RECORDS_on_file_not_the_new_case"
    return {
        "question": message,
        "the_new_case": told,
        "the_ask": {"wants": ask_.get("type"), "asks_for": ask_.get("asks_for") or "", "role": ask_.get("role") or "any",
                    "yes_no": bool(ask_.get("yes_no")), "about": [net.name(p) for p in ask_.get("people") or []],
                    "about_the_new_case": bool(ask_.get("this_case"))},
        "depth": ask_.get("depth") or "detailed",
        "shape": {"kind": shape, "sections": shapes.sections(shape)},
        "given": {
            "context": {k: (pack or {}).get(k) for k in ("summary", "sherlock_view", "statements", "conflicts",
                                                         "missing", "whats_new")},
            # What was said before - to follow the conversation ("he", "that FIR"). Not evidence:
            # an earlier reply may have been wrong, so no fact is ever taken from it.
            "chat_so_far_not_evidence": {"recent": (pack or {}).get("chat_recent"), "older": (pack or {}).get("chat_older")},
            "dossiers": [{k: v for k, v in c.items() if k != "sig"} for c in cards],
            "what_each_system_tells": _systems_in_play(cards, sources),
            "computed": None if facts is None else {k: v for k, v in facts.items() if k not in ("rows", "rest")},
            "computed_in_words": computed_words,
            on_file: [e.line()[:400] for e in hits],
            "sources": sources,
            "graph_patterns": patterns,
            "findings": findings,
            "the_new_case": told,
            "case": {"targets": [net.name(p) for p in net.seeds()],
                     "roles": {net.name(p): r for p, r in case.roles.items() if p in net.people},
                     "documents_on_the_board": [{"id": d["id"], "title": d["title"], "kind": d["kind"]}
                                                for d in list(case.documents.values())[:30]]},
            "looked_up_now": looked,
        },
        "tools": {} if last_round else tools,
        "note": "This is your last draft: answer from GIVEN, mark what is still missing." if last_round else None,
    }


def _systems_in_play(cards: list[dict[str, Any]], sources: dict[str, Any]) -> dict[str, str]:
    """What each police system that appears in the answer's material tells - so a record or a
    link is explained by what it is (a tenancy witness is not a co-tenant)."""
    from sherlocks.linkgraph.systems import MEANING, SYSTEMS, meanings

    text = dumps([cards, sources]).lower()
    named = [k for k, info in SYSTEMS.items() if info.label.lower() in text or k in text]
    return meanings([*named, *(k for k in ("identity",) if "shared phone" in text and k in MEANING)])


def draft(llm: Any, language: str, data: dict[str, Any]) -> dict[str, Any] | None:
    lang = LANG.get(language, "English")
    # Said again at the very end: the records are often in another language, and the answer
    # must still be in the officer's.
    told = data.get("the_new_case") or {}
    if not told.get("described_by_the_officer") and str(told.get("which_case") or "").startswith("not said yet"):
        data = {**data, "mind": "The officer has not told us what the new case is, nor whether an FIR on file is this case. "
                                "If the question is about 'this case' (its date, place, offence, accused), do NOT answer it "
                                "from an FIR on file: say the new case is not described yet, name the FIR(s) on file as "
                                "past records with their details, and ask whether one of them is this case."}
    data = {**data, "write_the_answer_in": f"{lang} - first_line and every point of every section. Names, ids, numbers "
                                           "and quoted record text stay as they are."
                                           + (" Roman Urdu is written in Latin letters only - never in Urdu script, "
                                              "except a name exactly as a record writes it." if language == "roman" else "")}
    out = ask(llm, _Draft, dumps(data), system=_SYSTEM.format(lang=lang), kind="officer_draft", version="v3",
              max_tokens=1400)
    return out.model_dump() if out is not None else None


def evidence_digest(data: dict[str, Any], limit: int = 10000) -> str:
    """What O1 was given, for O2: the checker judges the draft against these records."""
    given = data.get("given") or {}
    return dumps({k: given.get(k) for k in ("the_new_case", "case", "context", "computed_in_words", "dossiers",
                                            "records_on_file", "PAST_RECORDS_on_file_not_the_new_case", "graph_patterns", "sources", "findings",
                                            "looked_up_now")}, limit)


def systems_in(data: dict[str, Any]) -> set[str]:
    """The police systems whose records O1 was actually given: only those may be cited by name."""
    from sherlocks.linkgraph.systems import SYSTEMS

    text = dumps(data.get("given") or {}).upper()
    names = {s.upper() for s in SYSTEMS} | {info.label.upper() for info in SYSTEMS.values()} | {"SIMS", "HOTEL EYE"}
    return {n for n in names if n in text}


def normalise(d: dict[str, Any], known: set[str], systems: set[str] | None = None) -> dict[str, Any]:
    """The draft as the rest of the turn uses it. Code decides what the model is loose
    about: a section with points is *filled* whatever status it was given; a source in
    brackets stays only when it is real (``real_refs``); an empty point is dropped."""
    out = {**d, "first_line": real_refs(d.get("first_line") or "", known, systems), "sections": []}
    for s in d.get("sections") or []:
        lines = [real_refs(line, known, systems) for line in (s.get("body") or "").splitlines()]
        body = "\n".join(line for line in lines if line.strip("•- \t"))
        status = "filled" if body else (s.get("status") if s.get("status") in ("not on record", "not checked yet")
                                         else "not on record")
        out["sections"].append({"name": s.get("name") or "", "body": body, "status": status})
    return out


def findings_about(case: Any, pids: list[str]) -> dict[str, Any]:
    """Open contradictions, leads and Skeptic challenges about the people or topic asked -
    so O1 never answers as if they were not there."""
    from sherlocks.team.contradictions import about

    xs = about(case, pids or None)
    leads = [k for k in case.leads.values() if k["status"] in ("open", "pursuing", "confirmed")
             and (not pids or not k.get("people") or set(k["people"]) & set(pids))]
    leader = (case.matrix or {}).get("leader")
    return {"contradictions": [{"id": x["id"], "severity": x["severity"], "about": x.get("person_name"), "text": x["text"],
                                "settle": x.get("settle")} for x in xs[:8]],
            "leads_not_evidence": [{"id": k["id"], "hint": k["hint"], "why": k.get("why"), "status": k["status"]}
                                   for k in leads[:8]],
            "skeptic": [{"text": c["text"], "verdict": c.get("verdict")} for c in case.challenges
                        if c.get("hypothesis") == leader][:4],
            "pending_reports": case.register.get("pending") or []}


def graph_patterns(tools: Any, net: Any, pids: list[str], limit: int = 14) -> dict[str, Any]:
    """What the link analysis found on the graph (``scenarios.py``, computed by rule from the
    records): the patterns about the people asked first, then the strongest of the rest -
    and the people who hold the network together."""
    rows = sorted(tools.findings, key=lambda f: (0 if set(f["people"]) & set(pids) else 1, -float(f.get("score") or 0)))
    return {"count": len(tools.findings),
            "patterns": [{"pattern": f["title"], "means": f.get("meaning"), "tier": f["tier"], "people": f["names"],
                          "summary": f["summary"], "evidence": [e.get("text") for e in f["evidence"][:3]],
                          "sources": sorted({str(e.get("system") or "") for e in f["evidence"]} - {""})}
                         for f in rows[:limit]],
            "key_people": [{"name": k["name"], "links": k["degree"], "criminal_record": k["criminal"], "target": k["seed"]}
                           for k in net.key_people(top=6)]}


def filled(d: dict[str, Any] | None) -> bool:
    """Does the draft carry anything from the records?"""
    return bool(d) and any((s.get("body") or "").strip() and s.get("status", "filled") == "filled"
                           for s in d.get("sections") or [])
