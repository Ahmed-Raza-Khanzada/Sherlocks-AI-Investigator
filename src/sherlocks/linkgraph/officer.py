"""O1 Officer agent: who to ask, in order, so a question is never left unanswered.

The Question reader says what is asked; the Officer agent then asks his agents one after
another and stops at the first that answers:

1. **Dossier agent** (A4) - when the question is about a person, that person's card:
   identity, role, FIRs, links, documents, what the officer said, hypotheses.
2. **Case board and graph** - the Facts agent, the knowledge base (every fact, document
   and statement on the board) and the connections on the graph.
3. **API agent** (the Research agent) - when 1 and 2 hold no answer, the police systems
   and the FIR files are called at run time, through the guard, and what comes back goes
   onto the board and into the answer.
4. When none of them has it, the officer is told so plainly - "I did not find an answer"
   - with what was checked, never a guess.

Code only: it decides and records; the model (when there is one) reads the question and
writes the answer from what the agents brought.
"""

from __future__ import annotations

from typing import Any

# Facts-agent answers that are "nothing found" rather than an answer. An empty list of a
# person's FIRs is an answer ("no FIR names him"); an empty search is not.
_SEARCHES = {"knowledge", "not_understood", "research", "fact", "open"}

NOT_FOUND = {   # (no answer, where it looked, what the officer can do)
    "en": ("I did not find an answer to that.", "I checked: {c}.",
           "Tell me where it may be recorded (an FIR number, a document, a system) and I will look there."),
    "roman": ("Mujhe is ka jawab nahi mila.", "Maine yeh check kiya: {c}.",
              "Batayein yeh kahan record ho sakta hai (FIR number, document ya system), main wahan dekh leta hoon."),
    "ur": ("مجھے اس کا جواب نہیں ملا۔", "میں نے یہ چیک کیا: {c}۔",
           "بتائیں یہ کہاں ریکارڈ ہو سکتا ہے (ایف آئی آر نمبر، دستاویز یا سسٹم)، میں وہاں دیکھ لیتا ہوں۔"),
}


def about_person(query: dict[str, Any]) -> bool:
    """The question is about a specific person (the reader's scope; people named)."""
    return bool(query.get("people")) and query.get("scope", "person") in ("person", "connection") or (
        bool(query.get("people")) and query.get("type") in ("profile", "hotels", "phones", "vehicles", "associates"))


def dossier_step(case: Any, net: Any, query: dict[str, Any]) -> list[dict[str, Any]]:
    """1. The Dossier agent: the card of each person asked about."""
    from sherlocks.evidence.dossiers import dossier

    if not about_person(query):
        return []
    return [dossier(case, net, pid) for pid in query["people"][:2] if pid in net.people]


def card_items(card: dict[str, Any]) -> list[dict[str, Any]]:
    """A dossier card as Facts-agent items (what the case holds on the person)."""
    pid, items = card["pid"], []
    if card.get("role"):
        items.append({"text": f"{card['name']}: {card['role']} in the new case (officer)", "source": "officer", "pid": pid})
    for f in card.get("firs") or []:
        items.append({"text": f"FIR {f.get('label')}: {f.get('offence') or '-'} ({f.get('role') or '-'}"
                              + (f", {f['status']}" if f.get("status") else "") + ")",
                      "source": f.get("system") or "", "pid": pid})
    for f in (card.get("statements") or [])[-3:]:
        items.append({"text": f["statement"], "source": f["id"], "pid": pid})
    for f in (card.get("facts") or [])[:6]:
        items.append({"text": f["statement"], "source": f["id"], "pid": pid})
    for link in [x for x in card.get("links") or [] if x.get("stated")][:6]:
        items.append({"text": f"linked to {link['to']} ({link['relation']})", "source": link.get("via") or "graph",
                      "pid": pid})
    for h in (card.get("hypotheses") or [])[:2]:
        items.append({"text": f"Sherlock's hypothesis ({h['status']}): {h['statement']}", "source": h["id"], "pid": pid})
    return items


def found_nothing(facts: dict[str, Any] | None, hits: list[Any]) -> bool:
    """Neither the board nor the graph answered: no result, or an empty search."""
    if facts is None:
        return not hits
    return bool(facts.get("empty")) and facts.get("type") in _SEARCHES and not hits


def research_query(query: dict[str, Any], net: Any, unanswered: bool) -> dict[str, Any]:
    """What the API agent is asked for: the question's people - or, for a question about
    the case that nothing answered, the targets."""
    people = [p for p in query.get("people") or [] if p in net.people]
    if not people and unanswered:
        people = [p for p in net.seeds() if p in net.people][:1]
    return {**query, "people": people, "unanswered": unanswered}


_CHECKED = {
    "dossier": ("the dossier of {x}", "{x} ka dossier", "{x} کا ڈوزیئر"),
    "board": ("the case board ({f} facts, {d} documents)", "case board ({f} facts, {d} documents)",
              "کیس بورڈ ({f} حقائق، {d} دستاویزات)"),
    "graph": ("the graph connections ({n} people)", "graph ke rabtay ({n} log)", "گراف کے روابط ({n} افراد)"),
    "no_call": ("the police systems (no lookup fits this question)", "police systems (is sawal ke liye koi lookup nahi)",
                "پولیس سسٹمز (اس سوال کے لیے کوئی تلاش نہیں)"),
    "offline": ("live police systems (not connected in this session)", "live police systems (is session mein connected nahi)",
                "لائیو پولیس سسٹمز (اس سیشن میں منسلک نہیں)"),
}


# What a toolbox lookup is called when the officer is told it was checked.
_TOOL = {
    "findings": ("linkage patterns", "rabton ke patterns", "روابط کے پیٹرن"),
    "network": ("the network as a whole", "poora network", "پورا نیٹ ورک"),
    "person": ("the records of", "records", "ریکارڈ"),
    "neighbours": ("direct links of", "seedhay rabtay", "براہِ راست روابط"),
    "paths": ("routes between", "darmiyan ke raaste", "درمیانی راستے"),
    "compare": ("what they share", "mushtarik cheezein", "مشترک چیزیں"),
    "hidden_associates": ("hidden associates", "chhupay saathi", "چھپے ساتھی"),
    "criminal_proximity": ("nearness to known criminals", "mujrimon se qurbat", "مجرموں سے قربت"),
    "leads": ("people not searched yet", "jin ki talash baqi hai", "جن کی تلاش باقی ہے"),
    "timeline": ("the timeline", "timeline", "ٹائم لائن"),
    "evidence": ("the case documents", "case ke documents", "کیس کی دستاویزات"),
    "read_document": ("document", "document", "دستاویز"),
    "search_evidence": ("a search of the documents", "documents mein talash", "دستاویزات میں تلاش"),
    "fetch_fir": ("the FIR file", "FIR file", "ایف آئی آر فائل"),
    "fetch_lab_reports": ("the lab reports", "lab reports", "لیب رپورٹس"),
    "fetch_cro": ("the CRO dossier", "CRO dossier", "سی آر او ڈوزیئر"),
    "lookup": ("the police system", "police system", "پولیس سسٹم"),
    "api_router": ("the police systems", "police systems", "پولیس سسٹمز"),
    "cdr_lookup": ("the CDR server", "CDR server", "سی ڈی آر سرور"),
    "cdr_query": ("the CDR analysis", "CDR ka tajzia", "سی ڈی آر کا تجزیہ"),
    "statements": ("the recorded statements", "record shuda bayanaat", "ریکارڈ شدہ بیانات"),
    "presence": ("who was where at the time", "us waqt kaun kahan tha", "اس وقت کون کہاں تھا"),
    "evidence_register": ("the evidence register", "saboot ka register", "ثبوت کا رجسٹر"),
    "contradictions": ("the contradictions found", "paye gaye tazadaat", "پائے گئے تضادات"),
    "case_leads": ("the leads being followed", "zer-e-ghaur suraagh", "زیرِ غور سراغ"),
    "hypotheses": ("Sherlock's hypotheses", "Sherlock ke mafroozay", "شرلاک کے مفروضے"),
    "dossier": ("the dossier of", "dossier", "ڈوزیئر"),
    "board": ("the case board", "case board", "کیس بورڈ"),
    "chat_memory": ("what you told me", "jo aap ne bataya", "جو آپ نے بتایا"),
    "incident": ("the incident as recorded", "darj shuda waqia", "درج شدہ وقوعہ"),
    "uploads": ("the uploaded files", "upload shuda files", "اپ لوڈ شدہ فائلیں"),
}


def checked_tool(tool: str, on: str, language: str) -> str:
    """One lookup the Officer agent made, as the officer reads it: "linkage patterns
    (recurring witness)" - never the tool's own syntax."""
    i = {"en": 0, "roman": 1, "ur": 2}.get(language, 0)
    name = (_TOOL.get(tool) or (tool.replace("_", " "),) * 3)[i]
    about = ", ".join(part.replace("_", " ").strip(" .") for part in (on or "").split(" · ") if part.strip(" ."))
    return f"{name} ({about})" if about else name


def checked_item(kind: str, language: str, **fill: Any) -> str:
    """One place the Officer agent looked, in the officer's language."""
    i = {"en": 0, "roman": 1, "ur": 2}.get(language, 0)
    return _CHECKED[kind][i].format(**fill)


def checked_line(checked: list[str]) -> str:
    return ", ".join(dict.fromkeys(c for c in checked if c))


def not_found(checked: list[str], subject: str | None, said: str = "") -> dict[str, Any]:
    """4. A Facts-agent answer that says plainly nothing was found, and where it looked.
    ``said``: what the search itself said about it ("no record mentions his job"), kept."""
    return {"type": "not_found", "subject": subject, "count": 0, "items": [], "empty": True,
            "checked": list(dict.fromkeys(c for c in checked if c)), "said": said}


def say_not_found(facts: dict[str, Any], language: str) -> str:
    head, where, tail = NOT_FOUND.get(language, NOT_FOUND["en"])
    first = (facts.get("said") or head).strip()      # the search's own words, when it said what is missing
    if first[-1:] not in ".!?۔؟":
        first += "۔" if language == "ur" else "."
    return "\n".join([f"{first} {where.format(c=checked_line(facts.get('checked') or []) or '-')}", tail])
