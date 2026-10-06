"""The Conversation agent: the one voice that talks with the officer.

The other agents investigate; this one converses. Every turn it

1. hears which language the officer writes in - Urdu (script), Roman Urdu or English -
   and answers in that same language;
2. understands what kind of message it is - a question to investigate, information the
   officer is giving, an answer to Sherlock's open question, a greeting;
3. tells the officer what Sherlocks has learned since they last spoke (new documents,
   people found in them, CDR findings, the incident's nearest police station);
4. replies in natural sentences - acknowledging what was recorded, answering with its
   sources kept as [F3] / [D2], and asking at most one question back.

With no model the same steps run on templates in the three languages: plainer, never
wrong.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

Language = Literal["ur", "roman", "en"]
_ARABIC = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
_ROMAN = {"hai", "hain", "ha", "kya", "kia", "ka", "ki", "ke", "ko", "mein", "main", "mai", "nahi", "nahin", "kaun",
          "kon", "kahan", "kab", "kyun", "kyon", "aur", "se", "tha", "thi", "thay", "karo", "kro", "batao", "btao",
          "bataein", "ye", "yeh", "wo", "woh", "hum", "aap", "ap", "tum", "mujhe", "hamein", "liye", "wala", "wali",
          "raha", "rahi", "gaya", "gayi", "hua", "hui", "kar", "par", "pe", "bhi", "sirf", "abhi", "kuch", "koi",
          "jee", "ji", "haan", "han", "theek", "thik", "shukriya", "salam", "assalam", "walaikum", "dekho", "chahiye",
          "k", "batou", "btou", "batain", "bataen", "kitni", "kitne", "kitna", "ktne", "ktni", "ma", "uper", "upar",
          "kis", "kin", "konsi", "konsa", "kaunsi", "kaunsa", "jari", "rahe", "uske", "uski", "iske", "iski", "unke",
          "inke", "kaam", "walid", "rishtedar", "gawah", "muddai", "thana", "saathi", "taluq"}
_GREETING = re.compile(r"^\s*(hi|hello|hey|salam|salaam|as+alam(?:\s*[ou]\s*|\s*-?\s*)alaikum|aoa|السلام|سلام|good (morning|evening))\b",
                       re.IGNORECASE)
_QUESTION = re.compile(r"\?|؟|^\s*(who|what|where|when|why|how|which|is|are|was|were|did|does|do|can|could|show|tell|find|"
                       r"give|list|fetch|check|kya|kia|kaun|kon|kahan|kab|kyun|kaise|kitn|batao|btao|dikhao|"
                       r"کیا|کون|کہاں|کب|کیوں|کیسے|بتائیں|بتاؤ)\b", re.IGNORECASE)
# A question word anywhere ("kit ni fir in k uper ha", "fir status batou kamran ki").
_ASKING = re.compile(r"(?<![\w؀-ۿ])(kitn\w*|kit n[aeiy]|ktn[aeiy]|kaun|kon|konsa|konsi|kaunsa|kaunsi|kahan|kis|kin|"
                     r"kyun|kyon|kaise|batao|btao|batou|bataen|batain|bta|dikhao|how many|which|what|who|where|"
                     r"tafs[ei]+l|details?|ilza+m\w*|khulasa|summary|status|khatar?nak|sange?e?n|"
                     r"کتن[اےی]|کون|کہاں|کس|کن|کیوں|کیسے|بتائیں|بتاؤ)(?![\w؀-ۿ])", re.IGNORECASE)


def detect_language(text: str) -> Language:
    letters = [c for c in text if c.isalpha()]
    if letters and sum(1 for c in letters if _ARABIC.match(c)) >= 0.3 * len(letters):
        return "ur"
    words = re.findall(r"[a-z]+", text.lower())
    if words and sum(1 for w in words if w in _ROMAN) >= max(1, 0.15 * len(words)):
        return "roman"
    return "en"


Intent = Literal["question", "statement", "answer", "greeting"]


def route(text: str, open_questions: list[dict[str, Any]]) -> Intent:
    if _GREETING.match(text) and len(text.split()) <= 6:
        return "greeting"
    if _QUESTION.search(text) or _ASKING.search(text):
        return "question"
    if open_questions and len(text.split()) <= 25:
        return "answer"
    return "statement"


# --------------------------------------------------------------------------------------
# What Sherlocks knows that the officer has not heard yet
# --------------------------------------------------------------------------------------


def _num(ref: str) -> int:
    return int(ref[1:]) if ref[1:].isdigit() else 0


def briefing(case: Any, seen: dict[str, int]) -> tuple[list[str], dict[str, int]]:
    """New items on the case board since ``seen`` (highest D/F/L number told), as short
    English lines with their ids; and the new ``seen``."""
    lines: list[str] = []
    now = {"D": max((_num(d) for d in case.documents), default=0), "F": max((_num(f) for f in case.facts), default=0),
           "L": max((_num(x) for x in case.links), default=0)}
    for doc in case.documents.values():
        if _num(doc["id"]) > seen.get("D", 0) and doc["kind"] != "notes":
            lines.append(f"New {doc['kind'] if doc['kind'] != 'upload' else 'upload'}: {doc['title']} - "
                         f"{(doc.get('ai_summary') or doc.get('summary') or '')[:180]} [{doc['id']}]")
    for link in case.links.values():
        if _num(link["id"]) > seen.get("L", 0) and link.get("strength") == "identifier":
            lines.append(f"{link['name']} found in {case.documents[link['doc']]['title']}: {link['how']} [{link['id']}]")
    for fact in case.facts.values():
        if _num(fact["id"]) > seen.get("F", 0) and fact.get("by") == "cdr" and fact["statement"].startswith(
                ("On the graph", "Near the incident", "Subscriber")):
            lines.append(f"{fact['statement']} [{fact['id']}]")
    inc = case.incident or {}
    if inc.get("nearest_ps") and not seen.get("ps"):
        lines.append(f"Nearest police station to the incident: {inc['nearest_ps']}")
        now["ps"] = 1
    else:
        now["ps"] = seen.get("ps", 0)
    return lines[:8], now


# --------------------------------------------------------------------------------------
# What Sherlocks knows: the case memory the Conversation agent speaks from
# --------------------------------------------------------------------------------------

_FACT_ORDER = {"officer": 0, "ai": 1, "cdr": 2, "rule": 3}


def case_memory(case: Any, graph: dict[str, Any], net: Any = None, limit: int = 14000) -> dict[str, Any]:
    """Everything known about the case, compact and with ids: targets and roles, their
    connections, the rules' findings, every document's facts (FIR files and case diaries,
    lab / medical reports, CRO dossiers, uploads, CDR analyses), the officer's own
    statements, and the incident."""
    from sherlocks.linkgraph.network import PersonNetwork
    from sherlocks.linkgraph.scenarios import find_scenarios

    net = net or PersonNetwork(graph)
    targets = []
    for pid in net.seeds():
        d = net.data(pid)
        targets.append({
            "name": net.name(pid), "cnic": d.get("cnic"), "phones": (d.get("phones") or [])[:3],
            "role_stated_by_officer": case.roles.get(pid), "flags": d.get("flags") or [],
            "firs": [f"{f.get('label')} {f.get('role') or ''} {f.get('offence') or ''} [{(f.get('system') or '').upper()}]".strip()
                     for f in (d.get("firs") or [])[:8]],
            "hotel_stays": [f"{s.get('hotel')} {s.get('check_in') or ''}" for s in (d.get("stays") or [])[:4]],
            "connections": [f"{c['relation']}{'' if c['stated'] else ' (inferred)'} [{c['via']}]"
                            for c in (net.neighbours(pid)[:10] if pid in net.G else [])],
        })
    routes = []
    seeds = net.seeds()
    for i, a in enumerate(seeds):
        for b in seeds[i + 1:]:
            for r in net.paths(a, b, k=1):
                routes.append(f"{net.name(a)} → {net.name(b)}: " + "; ".join(h["relation"] for h in r["hops"]))
    try:
        findings = [f"{f['title']} ({f['tier']}): {f['summary']}" for f in find_scenarios(graph, net=net)[:8]]
    except Exception:  # noqa: BLE001
        findings = []
    docs = []
    for doc in case.documents.values():
        if doc["kind"] == "notes":
            continue
        facts = sorted((case.facts[f] for f in doc.get("facts") or [] if f in case.facts),
                       key=lambda f: _FACT_ORDER.get(f.get("by"), 9))
        docs.append({"id": doc["id"], "kind": doc["kind"], "title": doc["title"],
                     "summary": (doc.get("ai_summary") or doc.get("summary") or "")[:300],
                     "facts": [f"{f['id']}: {f['statement'][:220]}" for f in facts[:8]],
                     "people_found": [f"{link['name']} ({link['how']}) [{link['id']}]" for link in case.links.values()
                                      if link["doc"] == doc["id"]][:6]})
    notes = case.doc_for("notes:officer")
    memory = {
        "targets": targets, "routes_between_targets": routes, "linkage_findings": findings,
        "incident": case.incident, "officer_statements": (notes or {}).get("text", "")[-2000:],
        "officer_facts": [f"{f['id']}: {f['statement']}" for f in case.facts.values() if f.get("by") == "officer"][-12:],
        "documents": docs, "people_on_graph": len(net.people),
        "open_questions": [q["text"] for q in case.open_questions()],
    }
    text = json.dumps(memory, ensure_ascii=False, default=str)
    while len(text) > limit and any(d["facts"] for d in docs):
        for d in docs:       # trim evenly: every document keeps its title and best facts
            if len(d["facts"]) > 2:
                d["facts"].pop()
        if all(len(d["facts"]) <= 2 for d in docs):
            docs[:] = docs[: max(4, len(docs) - 4)]
        text = json.dumps(memory, ensure_ascii=False, default=str)
    return memory


# --------------------------------------------------------------------------------------
# Speaking
# --------------------------------------------------------------------------------------

# The Questioner's standard questions, in the three languages (the model translates any other).
QUESTIONS = {
    "incident:place": {
        "en": "Where did the incident happen? Pin it on the map and I will check whose phones were near the scene.",
        "roman": "Waqia kahan hua tha? Map par pin kar dein, main check karta hoon kis ke phone us waqt wahan qareeb thay.",
        "ur": "واقعہ کہاں پیش آیا؟ نقشے پر جگہ لگا دیں، میں دیکھوں گا کہ اس وقت کس کے فون وہاں قریب تھے۔"},
    "incident:when": {
        "en": "When did it happen - date and time?",
        "roman": "Waqia kis tareekh aur kis waqt hua?",
        "ur": "واقعہ کس تاریخ اور کس وقت ہوا؟"},
    "roles:main": {
        "en": "Who is the main suspect in this case?",
        "roman": "Is case mein main suspect kaun hai?",
        "ur": "اس مقدمے میں مرکزی ملزم کون ہے؟"},
}
QUESTIONS.update({
    "incident:pin": {
        "en": "You told me where the incident was. Can you pin it on the map? Then I can check which phones were near it.",
        "roman": "Aap ne jagah bata di thi - kya map par pin kar sakte hain? Phir main dekh sakta hoon kis ke phone wahan qareeb thay.",
        "ur": "آپ نے جگہ بتا دی تھی - کیا نقشے پر نشان لگا سکتے ہیں؟ پھر میں دیکھ سکتا ہوں کس کے فون وہاں قریب تھے۔"},
    "incident:what": {
        "en": "What happened in this case - which crime (robbery, murder, kidnapping, fraud...)?",
        "roman": "Is case mein hua kya tha - konsa jurm (dakaiti, qatl, aghwa, fraud...)?",
        "ur": "اس کیس میں ہوا کیا تھا - کون سا جرم (ڈکیتی، قتل، اغوا، فراڈ...)؟"},
    "incident:fir": {
        "en": "Is there an FIR for this incident? Tell me its number, year and police station and I will read its file.",
        "roman": "Is waqiye ka FIR hai? Number, saal aur thana bata dein, main us ki file parh leta hoon.",
        "ur": "کیا اس واقعے کی ایف آئی آر درج ہے؟ نمبر، سال اور تھانہ بتا دیں، میں اس کی فائل پڑھ لوں گا۔"},
    "roles:victim": {
        "en": "Who is the victim or the complainant in this incident?",
        "roman": "Is waqiye mein mutasir (victim) ya muddai kaun hai?",
        "ur": "اس واقعے میں متاثرہ یا مدعی کون ہے؟"},
    "incident:vehicle": {
        "en": "Was a vehicle or weapon used? A number plate or a description helps me search for it.",
        "roman": "Kya koi gaari ya hathiyar istemal hua? Number plate ya tafseel ho to main dhoond sakta hoon.",
        "ur": "کیا کوئی گاڑی یا ہتھیار استعمال ہوا؟ نمبر پلیٹ یا تفصیل ہو تو میں تلاش کر سکتا ہوں۔"},
    "suspects:other": {
        "en": "Is there anyone else you suspect who is not on the graph yet? Give me a name with a CNIC or number.",
        "roman": "Koi aur shakhs jis par shak ho aur graph par na ho? Naam ke saath CNIC ya number bata dein.",
        "ur": "کوئی اور شخص جس پر شک ہو اور گراف پر نہ ہو؟ نام کے ساتھ شناختی کارڈ یا نمبر بتا دیں۔"},
})
_REASK = {"en": "(Still open from earlier) ", "roman": "(Pehle wala sawal abhi baqi hai) ", "ur": "(پچھلا سوال ابھی باقی ہے) "}

_TEMPLATES = {
    "en": {"intro": "Here is what the records show:", "noted": "Noted: {x}.", "new": "Since we last spoke: {x}", "nothing": "I have nothing new since we last spoke.",
           "hello": "Hello. I am following this case: {x}. Ask me anything about it, or tell me what you know.",
           "answer_noted": "Thank you - recorded on the case board.", "ask": "{x}"},
    "roman": {"intro": "Records ke mutabiq yeh pata chala:", "noted": "Note kar liya: {x}.", "new": "Pichli baat ke baad naya yeh mila hai: {x}",
              "nothing": "Pichli baat ke baad koi nayi cheez nahi mili.",
              "hello": "Walaikum assalam. Main is case par kaam kar raha hoon: {x}. Jo poochna ho poochein, ya jo maloom hai batayein.",
              "answer_noted": "Shukriya - case board par note kar liya.", "ask": "{x}"},
    "ur": {"intro": "ریکارڈ کے مطابق یہ معلوم ہوا:", "noted": "نوٹ کر لیا: {x}۔", "new": "پچھلی بات کے بعد یہ نیا ملا ہے: {x}",
           "nothing": "پچھلی بات کے بعد کوئی نئی چیز نہیں ملی۔",
           "hello": "وعلیکم السلام۔ میں اس کیس پر کام کر رہا ہوں: {x}۔ جو پوچھنا ہو پوچھیں، یا جو معلوم ہے بتائیں۔",
           "answer_noted": "شکریہ - کیس بورڈ پر نوٹ کر لیا۔", "ask": "{x}"},
}
# How each Facts-agent answer opens, per language: (with results, when empty).
FACT_HEADS: dict[str, dict[str, tuple[str, str]]] = {
    "en": {
        "knowledge": ("Here is what the records hold on that ({s}):", "The records hold nothing on that about {s} - no source on the graph or in the documents read so far mentions it."),
        "not_understood": ("", "I did not quite get that. You can ask me, for example: \"which FIRs is Kamran in?\", \"status of his FIRs\", \"criminals among his connections\", \"how are Kamran and Sajid linked?\", \"his hotel stays\", \"what is new?\"."),
        "fir_status": ("Status of {s}'s {n} FIR(s):", "No FIR names {s} in the records searched."),
        "cases": ("{s} is named in {n} FIR(s):", "No FIR names {s} in the records searched."),
        "io_report": ("The IO reports ({s}):", "I have not read the FIR files of {s}'s cases yet, so I cannot say what the IO concluded - ask me to fetch them (e.g. \"fetch FIR 474/2022\")."),
        "research": ("I checked the systems for {s} - this is what came back:", "I checked the systems for {s}, but they returned nothing on that."),
        "serious_cases": ("Serious FIRs of {s}:", "No FIR names {s} in the records searched."),
        "fir_details": ("What the FIRs say ({s}):", "I have not read an FIR file for that yet - ask me to fetch it (e.g. \"fetch FIR 345/2026\")."),
        "criminals_near": ("{n} of {s}'s connections have a criminal record:",
                           "None of {s}'s stated connections has a criminal record."),
        "criminals_all": ("{n} of the {p} people on the graph have a criminal record:",
                          "No one on the graph has a criminal record."),
        "profile": ("Here is what the records say about {s}:", "I have nothing on {s} yet."),
        "associates": ("{s} has {n} direct link(s):", "{s} has no direct links on the graph yet."),
        "connection": ("How {s} are linked:", "I found no link between {s} in the records."),
        "hotels": ("{s} has {n} hotel stay(s) on record:", "No hotel stay is recorded for {s}."),
        "phones": ("{s}'s numbers ({n}):", "No number is recorded for {s}."),
        "vehicles": ("Vehicles linked to {s} ({n}):", "No vehicle is linked to {s}."),
        "documents": ("{n} case document(s) concern {s}:", "No case document concerns {s} yet."),
        "graph_stats": ("The graph so far:", "The graph is empty."),
        "summary": ("Where the case stands:", "Nothing gathered yet."),
        "news": ("New since we last spoke:", "Nothing new since we last spoke."),
        "inferred": "Through inferred links (leads, not facts), {m} more:",
    },
    "roman": {
        "knowledge": ("Records mein is bare mein yeh mila ({s}):", "Records mein {s} ke is bare mein kuch nahi mila - graph ya ab tak parhe gaye documents mein iska zikr nahi."),
        "not_understood": ("", "Main theek se samajh nahi saka. Aap aise pooch sakte hain: \"Kamran kin FIRs mein hai?\", \"us ke FIRs ka status\", \"us ke saathiyon mein kitne mujrim?\", \"Kamran aur Sajid ka kya taluq?\", \"us ke hotel stays\", \"kya naya hai?\"."),
        "fir_status": ("{s} ke {n} FIR(s) ka status:", "Records mein {s} ka koi FIR nahi mila."),
        "cases": ("{s} {n} FIR(s) mein naamzad hai:", "Records mein {s} ka koi FIR nahi mila."),
        "io_report": ("IO reports ({s}):", "{s} ke FIRs ki files abhi parhi nahi gayin, is liye IO ka nateeja nahi bata sakta - kahein to mangwa leta hoon (maslan \"fetch FIR 474/2022\")."),
        "research": ("Maine {s} ke liye systems check kiye - yeh mila:", "Maine {s} ke liye systems check kiye, lekin is bare mein kuch nahi mila."),
        "serious_cases": ("{s} ke sangeen FIRs:", "Records mein {s} ka koi FIR nahi mila."),
        "fir_details": ("FIRs mein yeh likha hai ({s}):", "Is FIR ki file abhi parhi nahi gayi - kahein to mangwa leta hoon (maslan \"fetch FIR 345/2026\")."),
        "criminals_near": ("{s} ke connections mein {n} log criminal record wale hain:",
                           "{s} ke kisi stated connection ka criminal record nahi."),
        "criminals_all": ("Graph ke {p} logon mein se {n} ka criminal record hai:", "Graph mein kisi ka criminal record nahi."),
        "profile": ("{s} ke bare mein records yeh batate hain:", "{s} ke bare mein abhi kuch nahi mila."),
        "associates": ("{s} ke {n} direct links hain:", "Graph par {s} ka abhi koi direct link nahi."),
        "connection": ("{s} ka taluq:", "Records mein {s} ka koi taluq nahi mila."),
        "hotels": ("{s} ke {n} hotel stays record mein hain:", "{s} ka koi hotel stay record nahi."),
        "phones": ("{s} ke numbers ({n}):", "{s} ka koi number record nahi."),
        "vehicles": ("{s} se juri gaariyan ({n}):", "{s} se koi gaari nahi juri."),
        "documents": ("{n} case documents mein {s} ka zikr hai:", "Abhi kisi case document mein {s} ka zikr nahi."),
        "graph_stats": ("Ab tak graph:", "Graph abhi khali hai."),
        "summary": ("Case ki surat-e-haal:", "Abhi kuch jama nahi hua."),
        "news": ("Pichli baat ke baad naya yeh mila:", "Pichli baat ke baad kuch naya nahi mila."),
        "inferred": "Inferred links se (yeh leads hain, facts nahi) {m} aur:",
    },
    "ur": {
        "knowledge": ("ریکارڈ میں اس بارے میں یہ ملا ({s}):", "ریکارڈ میں {s} کے بارے میں اس سلسلے میں کچھ نہیں ملا - گراف یا اب تک پڑھی گئی دستاویزات میں اس کا ذکر نہیں۔"),
        "not_understood": ("", "میں ٹھیک سے سمجھ نہیں سکا۔ آپ ایسے پوچھ سکتے ہیں: \"کامران کن ایف آئی آر میں ہے؟\"، \"اس کی ایف آئی آر کی صورتحال\"، \"اس کے ساتھیوں میں کتنے ملزم ہیں؟\"، \"کیا نیا ہے؟\"۔"),
        "fir_status": ("{s} کی {n} ایف آئی آر کی صورتحال:", "ریکارڈ میں {s} کی کوئی ایف آئی آر نہیں ملی۔"),
        "cases": ("{s} {n} ایف آئی آر میں نامزد ہیں:", "ریکارڈ میں {s} کی کوئی ایف آئی آر نہیں ملی۔"),
        "io_report": ("تفتیشی رپورٹیں ({s}):", "{s} کی ایف آئی آر کی فائلیں ابھی پڑھی نہیں گئیں، اس لیے تفتیشی افسر کا نتیجہ نہیں بتا سکتا۔"),
        "research": ("میں نے {s} کے لیے سسٹمز چیک کیے - یہ ملا:", "میں نے {s} کے لیے سسٹمز چیک کیے، لیکن اس بارے میں کچھ نہیں ملا۔"),
        "serious_cases": ("{s} کی سنگین ایف آئی آر:", "ریکارڈ میں {s} کی کوئی ایف آئی آر نہیں ملی۔"),
        "fir_details": ("ایف آئی آر میں یہ لکھا ہے ({s}):", "اس ایف آئی آر کی فائل ابھی پڑھی نہیں گئی - کہیں تو منگوا لوں۔"),
        "criminals_near": ("{s} کے روابط میں {n} افراد کا مجرمانہ ریکارڈ ہے:", "{s} کے کسی تصدیق شدہ رابطے کا مجرمانہ ریکارڈ نہیں۔"),
        "criminals_all": ("گراف کے {p} افراد میں سے {n} کا مجرمانہ ریکارڈ ہے:", "گراف میں کسی کا مجرمانہ ریکارڈ نہیں۔"),
        "profile": ("{s} کے بارے میں ریکارڈ یہ بتاتا ہے:", "{s} کے بارے میں ابھی کچھ نہیں ملا۔"),
        "associates": ("{s} کے {n} براہ راست روابط ہیں:", "گراف پر {s} کا ابھی کوئی براہ راست رابطہ نہیں۔"),
        "connection": ("{s} کا تعلق:", "ریکارڈ میں {s} کا کوئی تعلق نہیں ملا۔"),
        "hotels": ("{s} کے {n} ہوٹل قیام ریکارڈ میں ہیں:", "{s} کا کوئی ہوٹل قیام ریکارڈ میں نہیں۔"),
        "phones": ("{s} کے نمبر ({n}):", "{s} کا کوئی نمبر ریکارڈ میں نہیں۔"),
        "vehicles": ("{s} سے منسلک گاڑیاں ({n}):", "{s} سے کوئی گاڑی منسلک نہیں۔"),
        "documents": ("{n} کیس دستاویزات میں {s} کا ذکر ہے:", "ابھی کسی دستاویز میں {s} کا ذکر نہیں۔"),
        "graph_stats": ("اب تک گراف:", "گراف ابھی خالی ہے۔"),
        "summary": ("کیس کی صورتحال:", "ابھی کچھ جمع نہیں ہوا۔"),
        "news": ("پچھلی بات کے بعد یہ نیا ملا:", "پچھلی بات کے بعد کچھ نیا نہیں ملا۔"),
        "inferred": "قیاسی روابط سے (یہ سراغ ہیں، حقائق نہیں) {m} مزید:",
    },
}
FACT_HEADS["en"]["count_cases"] = FACT_HEADS["en"]["cases"]
FACT_HEADS["roman"]["count_cases"] = FACT_HEADS["roman"]["cases"]
FACT_HEADS["ur"]["count_cases"] = FACT_HEADS["ur"]["cases"]


def render_facts(facts: dict[str, Any], language: Language, limit: int = 15) -> str:
    """A Facts-agent answer as text, in the officer's language (items stay as recorded)."""
    from sherlocks.linkgraph.narrate import narrate

    told = narrate(facts, language)
    if told and not facts.get("inferred"):
        return told
    heads = FACT_HEADS[language]
    full, empty = heads.get(facts["type"], ("{s}:", "-"))
    fill = {"s": facts.get("subject") or "", "n": facts.get("count", 0), "p": facts.get("people", "")}
    if facts.get("empty") and not facts.get("inferred"):
        return empty.format(**fill)
    lines = [full.format(**fill) if not facts.get("empty") else empty.format(**fill)]
    for it in facts["items"][:limit]:
        lines.append(f"• {it['text']}" + (f" [{it['source']}]" if it.get("source") else ""))
    if len(facts["items"]) > limit:
        lines.append(f"… +{len(facts['items']) - limit}")
    inferred = facts.get("inferred") or []
    if inferred:
        lines.append(heads["inferred"].format(m=len(inferred)))
        lines += [f"• {it['text']}" for it in inferred[:6]]
    return "\n".join(lines)


RESEARCH_LINE = {
    "en": "(This was not in what I had read, so I checked it live: {x}.)",
    "roman": "(Yeh pehle parhe gaye records mein nahi tha, is liye maine abhi check kiya: {x}.)",
    "ur": "(یہ پہلے پڑھے گئے ریکارڈ میں نہیں تھا، اس لیے میں نے ابھی چیک کیا: {x}۔)",
}

READING_LINE = {
    "en": "I had not read this graph's documents yet - I am now reading {n} FIR file(s) and dossier(s) (with their lab reports). Ask again in a minute and I will answer from them too.",
    "roman": "Is graph ke documents abhi parhe nahi gaye thay - ab main {n} FIR files aur dossiers (lab reports samait) parh raha hoon. Ek minute baad dobara poochein, phir un se bhi jawab doon ga.",
    "ur": "اس گراف کی دستاویزات ابھی پڑھی نہیں گئی تھیں - اب میں {n} ایف آئی آر فائلیں اور ڈوزیئر (لیب رپورٹس سمیت) پڑھ رہا ہوں۔ ایک منٹ بعد دوبارہ پوچھیں۔",
}

NEWS_LINE = {
    "en": "(Meanwhile I have read {n} new item(s) - ask \"what's new\" for the details.)",
    "roman": "(Is dauran {n} nayi cheezein mili hain - tafseel ke liye \"kya naya hai\" poochein.)",
    "ur": "(اس دوران {n} نئی چیزیں ملی ہیں - تفصیل کے لیے \"کیا نیا ہے\" پوچھیں۔)",
}

LANG_NAME = {"en": "English", "roman": "Roman Urdu (Urdu written in English letters)", "ur": "Urdu in Urdu script"}


class _Reply(BaseModel):
    reply: str = Field(description="The message to the officer, in the officer's language.")


_SYSTEM = (
    "You are Sherlock, a seasoned police investigator talking with a fellow officer about a case, through a "
    "chat. Speak naturally and warmly, like a colleague: short paragraphs, plain words, no headings, no lists "
    "unless the officer asks. ALWAYS reply in {lang} - the language the officer wrote in. You know the whole case: "
    "`case_memory` holds the targets and their connections, the linkage findings, every document's facts (FIR files "
    "and case diaries, lab / medical reports, CRO dossiers, uploaded files and CDR analyses) and what the officer has "
    "told you. Use it: answer from it, and when the officer tells you something, relate it to what the records show "
    "(agrees, adds, or contradicts - say which). Use only the material given: the case memory, what was recorded "
    "from the officer, the investigation result, the new findings. "
    "Keep every source id exactly as given in square brackets, e.g. [F3] [D2] [PSRMS], next to the claim it "
    "supports; never invent ids, people, numbers or dates. Say plainly what is stated by records and what is only "
    "a lead. When `facts_answer` is given it is the exact answer computed from the records: give its number and "
    "its items faithfully (you may shorten a long list, never change a number, add or drop a person). If the "
    "officer gave information, acknowledge it briefly. Mention new findings only if given. End with at most ONE "
    "question back to the officer, only if one is given to ask - phrase it naturally in {lang}. Names of people "
    "stay as written."
)


def compose(*, language: Language, intent: Intent, message: str, recorded: list[str], result: dict[str, Any] | None,
            news: list[str], question: dict[str, Any] | None, case_summary: str, history: list[dict[str, Any]],
            llm: Any = None, memory: dict[str, Any] | None = None, facts: dict[str, Any] | None = None,
            news_line: str | None = None) -> tuple[str, str]:
    """The reply and who wrote it ("model" or "templates")."""
    ask = None
    if question:
        ask = QUESTIONS.get(question["key"], {}).get(language) or question["text"]
        if question.get("times", 0) >= 1:
            ask = _REASK[language] + ask
    if llm is not None:
        payload = {
            "officer_message": message, "message_kind": intent,
            "recorded_from_officer": recorded,
            "investigation_result": None if not result else {
                "answer": result.get("answer"), "confident": result.get("confident"),
                "hypotheses": [{"statement": h.get("statement"), "tier": h.get("tier"), "evidence": h.get("evidence")}
                               for h in (result.get("hypotheses") or [])[:4]],
                "next_steps": (result.get("next_steps") or [])[:3]},
            "facts_answer": facts, "new_findings_since_last_message": news, "also_mention": news_line,
            "question_to_ask": ask,
            "case_summary": case_summary, "case_memory": memory,
            "recent_conversation": [{"officer": t.get("q", "")[:300], "sherlock": t.get("a", "")[:400]} for t in history[-4:]],
        }
        try:
            out, _ = llm.generate_structured(prompt=json.dumps(payload, ensure_ascii=False, default=str), schema=_Reply,
                                             system=_SYSTEM.format(lang=LANG_NAME[language]), cache_kind="converse",
                                             prompt_version="v1", max_tokens=1200)
            if out.reply.strip():
                return out.reply.strip(), "model"
        except Exception as exc:  # noqa: BLE001 - the templates below always work
            logger.info("Conversation agent model failed: %s", exc)
    t = _TEMPLATES[language]
    parts: list[str] = []
    if intent == "greeting":
        parts.append(t["hello"].format(x=case_summary))
    if intent == "answer":
        parts.append(t["answer_noted"])
    elif recorded:
        # "role: X = main suspect", "incident date: ..." say it best; the raw quote only when alone.
        items = [r.replace("role: ", "").replace("incident date: ", "incident date ") for r in recorded
                 if not r.startswith(("recorded ", "answer to"))] or [r.split(": ", 1)[-1] for r in recorded]
        parts.append(t["noted"].format(x="; ".join(dict.fromkeys(items))))
    if facts is not None:
        parts.append(render_facts(facts, language))
    elif result and intent == "question":
        parts.append(t["intro"] + "\n" + (result.get("answer") or ""))
    if news:
        parts.append(t["new"].format(x="\n• " + "\n• ".join(news)))
    if news_line:
        parts.append(news_line)
    if ask:
        parts.append(t["ask"].format(x=ask))
    return "\n\n".join(p for p in parts if p.strip()), "templates"
