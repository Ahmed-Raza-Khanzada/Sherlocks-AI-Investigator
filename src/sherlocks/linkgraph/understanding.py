"""The Understanding agent: what is the officer asking, and about whom?

Turns a message (English, Roman Urdu or Urdu) into a structured query for the Facts
agent - ``{type, people, hops}`` - or marks it ``open`` for the Investigator.

* **People**: names in the message (full name, a distinctive word of it, or by sound
  across scripts); otherwise a pronoun ("ye", "wo", "us", "his", "یہ", "اس") means the
  person being discussed - the dialogue's focus.
* **Type**: by keywords in the three languages; the model decides when it is available
  and the rules cannot.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from sherlocks.linkgraph.network import PersonNetwork

logger = logging.getLogger(__name__)

QueryType = Literal["cases", "count_cases", "fir_status", "serious_cases", "fir_details", "io_report", "criminals_near", "criminals_all", "profile", "associates", "connection",
                    "hotels", "phones", "vehicles", "documents", "graph_stats", "news", "summary", "open"]


def _words(*items: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w؀-ۿ])(?:" + "|".join(items) + r")(?![\w؀-ۿ])", re.IGNORECASE)


KW = {
    "count": _words(r"how many", r"how much", r"kitn[aei]y?", r"kitne", r"ktn[aei]", r"kitny", r"total", r"count", r"number of", r"tadad",
                    r"تعداد", r"کتنی", r"کتنے", r"کتنا"),
    "cases": _words(r"firs?", r"cases?", r"mul[aw]?wi?s", r"mulawwis", r"involved?", r"shamil", r"ملوث", r"شامل", r"muqadm[aey]", r"mukadm[aey]", r"muqadmat", r"parchay?", r"parche",
                    r"مقدم[ہے]", r"مقدمات", r"ایف آئی آر", r"پرچ[ہے]", r"crimes?", r"offen[cs]es?"),
    "status": _words(r"status(?:es)?", r"jari", r"chal rah[aei]", r"zer ?e? ?sama?at", r"zair ?e? ?sama?at", r"position", r"halat", r"haalat", r"surat ?e? ?hal", r"outcome", r"result", r"faisla",
                     r"faisl[ae]", r"decision", r"verdict", r"ac?qu?i+t+\w*", r"bari", r"conv[ic]\w*", r"saza", r"sazaa",
                     r"sentenc\w*", r"trial", r"challan", r"bail", r"zamanat", r"dispos\w*", r"pending", r"court",
                     r"adalat", r"حیثیت", r"سٹیٹس", r"بری", r"سزا", r"فیصلہ", r"چالان", r"ضمانت", r"کیفیت", r"عدالت"),
    "criminal": _words(r"criminals?", r"mujrim", r"mujrimon", r"mulzim", r"mulzimon", r"accused", r"ملزم", r"ملزمان",
                       r"مجرم", r"criminal record", r"record wal[ae]"),
    "connections": _words(r"connections?", r"connected", r"lag(?:t[aei]?|ta|ti|te)", r"rishta", r"relation(?:ship)?",
                          r"relat(?:ed|e)", r"لگتا", r"لگتی", r"رشتہ", r"links?", r"linked", r"rabt[ae]", r"taluq(?:at)?",
                          r"tauluq", r"associates?", r"saathi", r"sathi", r"contacts?", r"network", r"ساتھی", r"تعلق",
                          r"رابط[ہے]", r"around", r"near him", r"his people"),
    "whole": _words(r"whole", r"entire", r"all", r"graph", r"overall", r"sab", r"saare?", r"tamam", r"poor[ae]",
                    r"پورے", r"تمام", r"سب"),
    "hotel": _words(r"hotels?", r"stay(?:ed|s)?", r"thehr[aei]", r"ٹھہر[اے]", r"ہوٹل"),
    "phone": _words(r"phones?", r"numbers?", r"sims?", r"mobiles?", r"فون", r"نمبر"),
    "vehicle": _words(r"vehicles?", r"cars?", r"bikes?", r"gaa?ri", r"motorcycle", r"گاڑی"),
    "documents": _words(r"documents?", r"reports?", r"lab", r"dna", r"medical", r"dossier", r"files?", r"دستاویز",
                        r"رپورٹ"),
    "news": _words(r"new", r"naya", r"nayi", r"update", r"latest", r"نیا", r"نئی"),
    "summary": _words(r"summary", r"summari[sz]e", r"khulasa", r"brief", r"overview", r"خلاصہ"),
    "who": _words(r"who is", r"kaun hai", r"kon hai", r"about", r"bare mein", r"baare mein", r"کون ہے", r"بارے"),
    "people_count": _words(r"people", r"persons?", r"log", r"afraad", r"لوگ", r"افراد"),
}
# "not just his", "the whole graph", "na sirf": widen the question to everyone.
WIDEN = re.compile(r"not just|not only|na sirf|sirf\s+\S+(?:\s+\S+)?\s+(?:ke\s+)?nahi|besides|other than|apart from|"
                   r"instead of|whole|entire|overall|all of|in the graph|on the graph|graph mein|graph main|sab\s|saar[ae]|"
                   r"tamam|poor[ae]|پورے|تمام|سب|صرف .* نہیں", re.IGNORECASE)
POSSESSIVE = _words(r"his", r"her", r"their", r"us ke", r"uske", r"us ki", r"uski", r"is ke", r"iske", r"is ki", r"iski",
                    r"in ke", r"inke", r"un ke", r"unke", r"اس کے", r"اسکے", r"ان کے")
PRONOUNS = _words(r"ye", r"yeh", r"is", r"iska", r"iski", r"iske", r"isne", r"wo", r"woh", r"us", r"uska", r"uski",
                  r"uske", r"usne", r"he", r"him", r"his", r"she", r"her", r"this person", r"that person",
                  r"یہ", r"وہ", r"اس", r"اسکا", r"اس کا", r"اس کی", r"اس کے", r"ان", r"انہوں")


class _Query(BaseModel):
    type: QueryType = Field(description="What is asked. 'open' for why/how/what-is-going-on questions.")
    people: list[str] = Field(default_factory=list, description="Names of the people asked about, as written.")
    uses_pronoun: bool = Field(default=False, description="True if the question refers to someone as he/ye/wo/us.")


_SYSTEM = ("Classify a police officer's question about a link graph (English, Urdu or Roman Urdu). Types: cases "
           "(which FIRs/cases a person is in), count_cases (how many FIRs), fir_status (status / outcome of a person's FIRs: "
           "convicted, acquitted, under trial, challan, bail), serious_cases (dangerous / serious FIRs, or FIRs of a crime "
           "type: murder, rape, kidnapping, robbery, vehicle theft, extortion...), fir_details (what each FIR alleges, "
           "what happened, details or summary of FIRs), io_report (what the investigating officer's report concluded: "
           "guilty or not, challan, cancelled, IO's findings), criminals_near (criminals among a person's "
           "connections), criminals_all (criminals in the whole graph), profile (who is X), associates (X's "
           "connections), connection (how A and B are linked), hotels, phones, vehicles, documents (reports/files "
           "about X), graph_stats (how many people etc. in the graph), news (what is new), summary (the case), open "
           "(anything needing reasoning). List the people named.")


# Details no fixed answer covers: these questions are answered from the knowledge base.
DETAIL = re.compile(r"diary|diaries|zimni|ضمنی|gawah|witness|گواہ|muddai|mudai|complainant|مدعی|مستغیث|address|"
                    r"kahan rehta|kahan rehti|rehaish|ghar|پتہ|سکونت|lab\b|dna|chemical|medical|report|dossier|statement|"
                    r"bayan|بیان|narrative|weapon|pistol|hathiyar|property|maal|stolen|chori ka|investigat|tafteesh|"
                    r"\bio\b|officer|thana|police station|تھانہ|sections?|dafa|دفعہ|occur|kab hua|kahan hua|kis ne|"
                    r"why|kyun|kyon|how did|kaise", re.IGNORECASE)
FIR_NO = re.compile(r"\b\d{1,5}\s*/\s*\d{2,4}\b")
# "har FIR mein kya ilzaam hai?", "FIR 345/26 ki tafseel", "kya hua tha?"
ALLEGATION = re.compile(r"(?<![\w؀-ۿ])(ilza+m\w*|ilzam\w*|allegations?|alleg\w*|charges?|charged|accused of|"
                        r"kis (?:cheez|chiz|baat|jurm) k[aei]|kya jurm|jurm|tafs[ei]+l|details?|kahani|story|"
                        r"kya hua tha|what happened|waqia|waqiya|واقعہ|تفصیل|الزام\w*|جرم)(?![\w؀-ۿ])", re.IGNORECASE)
# "IO ki report", "investigation officer report", "guilty or not", "challan hua?", "بے گناہ"
IO_REPORT = re.compile(r"(?<![\w؀-ۿ])(i\.?o\b|investigat\w*\s+(?:officer|report)|tafteeshi|tafteesh\w*\s+(?:officer|afsar|report)|"
                       r"guilty|innocent|be ?gunah|begunah|qasoor ?war|gunah ?gar|declared?|found (?:him|her)|"
                       r"challan (?:hua|huwa|kia|kiya|pesh)|charge ?sheet\w*|169|final report|a class|b class|c class|"
                       r"تفتیشی|بے گناہ|قصوروار|گناہگار|چالان ہوا)(?![\w؀-ۿ])", re.IGNORECASE)
FIR_SUMMARY = re.compile(r"(?<![\w؀-ۿ])(summary|summari[sz]e|khulasa|brief|خلاصہ)(?![\w؀-ۿ])", re.IGNORECASE)
FIR_WORD = re.compile(r"(?<![\w؀-ۿ])(firs?|ایف آئی آر|muqadm\w*|mukadm\w*|parch\w*)(?![\w؀-ۿ])", re.IGNORECASE)


def normalise(text: str) -> str:
    """Typing slips that change the meaning: "kit ni" -> "kitni", "kit ne" -> "kitne"."""
    return re.sub(r"\bkit\s+n([aeiy])\b", r"kitn\1", text, flags=re.IGNORECASE)


def rule_type(text: str, has_people: int) -> QueryType:
    t = normalise(text)
    has = {k: bool(p.search(t)) for k, p in KW.items()}
    from sherlocks.linkgraph.offences import SERIOUS_WORDS, asked_crimes

    # What the investigating officer concluded ("IO ki report", "guilty or not").
    if IO_REPORT.search(t) and (has_people or has["cases"] or FIR_NO.search(t)):
        return "io_report"
    # What the FIRs allege / what happened in them.
    asks_details = (ALLEGATION.search(t) and (has["cases"] or FIR_NO.search(t) or has_people)) or (
        FIR_SUMMARY.search(t) and FIR_WORD.search(t))
    if asks_details and (has_people or FIR_NO.search(t)):
        return "fir_details"
    # "koi khatarnak FIR?", "qatl ke case?", "gaari chori mein?"
    crimes = asked_crimes(t) - ({"arms", "narcotics"} if not has["cases"] else set())
    if (SERIOUS_WORDS.search(t) or crimes) and (has_people or has["cases"]) and not has["criminal"]:
        return "serious_cases"
    if FIR_NO.search(t) or (DETAIL.search(t) and not has["count"]):
        return "open"
    if has["news"] and not has["cases"]:
        return "news"
    if has["summary"]:
        return "summary"
    if has["criminal"]:
        if WIDEN.search(t):
            return "criminals_all"
        if has["connections"] or POSSESSIVE.search(t) or (has_people and not has["whole"]):
            return "criminals_near"
        return "criminals_all"
    if has_people >= 2 and (has["connections"] or not any(has[k] for k in ("cases", "hotel", "phone", "vehicle"))):
        return "connection"
    if has["status"] and (has["cases"] or has_people):
        return "fir_status"
    if has["cases"]:
        return "count_cases" if has["count"] else "cases"
    if has["hotel"]:
        return "hotels"
    if has["vehicle"]:            # "gaari ka number" is a vehicle, not a phone
        return "vehicles"
    if has["phone"]:
        return "phones"
    if has["documents"]:
        return "documents"
    if has["connections"]:
        return "associates"
    if has["count"] and (has["people_count"] or has["whole"]) and not has_people:
        return "graph_stats"
    if has_people and (has["who"] or len(t.split()) <= 6):
        from sherlocks.linkgraph.knowledge import _CONCEPT_OF, tokens

        # "kamran ka kaam?", "us ke rishtedar?": a topic is asked - the knowledge base answers it.
        if any(w in _CONCEPT_OF for w in tokens(t)):
            return "open"
        return "profile"
    return "open"


def understand(message: str, net: PersonNetwork, focus: list[str], llm: Any = None) -> dict[str, Any]:
    """``{type, people (ids), pronoun, by}``."""
    from sherlocks.linkgraph.investigator import _mentioned

    message = normalise(message)
    pronoun = bool(PRONOUNS.search(message))
    people = _mentioned(net, message, sound=not (pronoun and focus))
    kind: str | None = None
    by = "rules"
    if llm is not None:
        try:
            q, _ = llm.generate_structured(prompt=f"Question: {message}", schema=_Query, system=_SYSTEM,
                                           cache_kind="understand", prompt_version="v1")
            kind, by = q.type, "model"
            for name in q.people:
                pid = net.resolve(name)
                if pid and pid not in people:
                    people.append(pid)
            pronoun = pronoun or q.uses_pronoun
        except Exception as exc:  # noqa: BLE001 - rules below
            logger.info("Understanding model failed: %s", exc)
    whole_graph = ("criminals_all", "graph_stats", "news", "summary")
    if by == "model":
        # Clear keyword rules win over the model's guess ("kahan kahan FIR hui" is a list of
        # FIRs, not their full stories); the model decides what the rules leave open.
        ruled = rule_type(message, len(people) or (1 if focus else 0))
        if ruled not in ("open", "profile") and ruled != kind:
            kind, by = ruled, "rules"
    if kind is None:
        # Rules: a follow-up with no name ("kis FIR mein acquitted hai?") is about the
        # person being discussed, unless it is about the whole graph.
        kind = rule_type(message, len(people) or (1 if focus else 0))
    if not people and focus and not WIDEN.search(message) and (pronoun or kind not in whole_graph):
        people = [p for p in focus if p in net.people][:1]
    if not people and by == "rules":
        kind = rule_type(message, 0)
        # A person-scoped type with nobody named and no focus cannot be answered as such.
        if kind in ("cases", "count_cases", "fir_status", "serious_cases", "fir_details", "io_report", "profile", "associates", "hotels", "phones", "vehicles", "documents") and not people:
            kind = "open"
    from sherlocks.linkgraph.offences import asked_crimes

    firs = [f"{m.group(0)}" for m in FIR_NO.finditer(message)]
    if kind in ("open", "fir_details", "io_report") and firs and not people and by == "rules":
        ruled = rule_type(message, 1)
        kind = ruled if ruled in ("fir_details", "io_report") else kind
    return {"type": kind, "people": people, "pronoun": pronoun, "by": by, "crimes": sorted(asked_crimes(message)),
            "firs": firs}
