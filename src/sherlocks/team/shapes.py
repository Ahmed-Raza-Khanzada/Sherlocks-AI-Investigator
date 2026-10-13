"""Answer shapes: one shape for each kind of question.

An investigating officer who asks about an FIR wants who, when, where, what happened,
why, and every statement about it - not two lines. For each kind of question there is a
fixed list of sections the answer must cover:

* O0 picks the shape and the depth - full unless the officer asks for short;
* O4 fetches every source the shape needs (``SOURCES``), not only the top facts;
* O1 fills it section by section, with ids, writing only the summary in its own words;
* O2 checks that every section is filled or marked "not on record" / "not checked yet" -
  a section is never dropped silently;
* O5 lays it out under short headings, in the officer's language.

The shapes are data. Nothing here reads the officer's words: the model chose the shape.
"""

from __future__ import annotations

from typing import Any

HEAD_MARK = "## "
FINDING_MARK = "FINDING:"

SHAPES: dict[str, list[str]] = {
    "fir": ["What happened", "When", "Where", "Who", "Why", "Weapon and property", "Statements", "Evidence", "Status",
            "Contradictions and leads", "Not on record"],
    "person": ["Identity", "Role in this case", "FIRs", "Links to other people", "In the incident window",
               "Statements", "Hotel stays", "Contradictions and leads", "Not on record"],
    "incident": ["Timeline", "Place", "People and their roles", "How it happened, by each account", "Evidence",
                 "Where the accounts disagree"],
    "statement": ["Each version", "Key points", "What changed between versions", "Against the evidence",
                  "Relation to the parties"],
    "link": ["Every connection", "First and last contact", "How strong", "Still unknown"],
    "phone": ["Owner", "Period covered", "Top contacts", "Near the scene", "Silences", "IMEI changes",
              "Links to other CDRs"],
    "count": ["The items"],
    "yes_no": ["The basis", "Everything checked"],
    "what_next": ["Ranked steps"],
    "other": [],
}

# What O4 fetches for a shape before O1 writes: (tool, which argument takes the person asked about).
SOURCES: dict[str, list[tuple[str, str | None]]] = {
    "fir": [("evidence_register", None), ("statements", None), ("contradictions", None), ("case_leads", None),
            ("incident", None)],
    "person": [("dossier", "who"), ("statements", "who"), ("presence", "who"), ("contradictions", "who"),
               ("evidence_register", "who")],
    "incident": [("incident", None), ("presence", None), ("statements", None), ("contradictions", None),
                 ("evidence_register", None)],
    "statement": [("statements", "who"), ("contradictions", "who"), ("presence", "who")],
    "link": [("dossier", "who"), ("cdr_query", "who"), ("contradictions", None)],
    "phone": [("cdr_query", "who"), ("uploads", None), ("case_leads", None)],
    "count": [],
    "yes_no": [("presence", "who"), ("contradictions", "who")],
    "what_next": [("hypotheses", None), ("case_leads", None), ("contradictions", None)],
    "other": [],
}

HEADINGS: dict[str, dict[str, str]] = {
    "What happened": {"roman": "Kya hua", "ur": "کیا ہوا"},
    "When": {"roman": "Kab", "ur": "کب"},
    "Where": {"roman": "Kahan", "ur": "کہاں"},
    "Who": {"roman": "Kaun", "ur": "کون"},
    "Why": {"roman": "Kyun (jaisa bataya gaya)", "ur": "کیوں (جیسا بتایا گیا)"},
    "Weapon and property": {"roman": "Hathiyar aur maal", "ur": "ہتھیار اور مال"},
    "Statements": {"roman": "Bayanaat", "ur": "بیانات"},
    "Evidence": {"roman": "Saboot", "ur": "ثبوت"},
    "Status": {"roman": "Maujooda soorat-e-haal", "ur": "موجودہ صورتحال"},
    "Contradictions and leads": {"roman": "Tazadaat aur suraagh", "ur": "تضادات اور سراغ"},
    "Not on record": {"roman": "Record mein nahi", "ur": "ریکارڈ میں نہیں"},
    "Identity": {"roman": "Shanakht", "ur": "شناخت"},
    "Role in this case": {"roman": "Is case mein kirdar", "ur": "اس کیس میں کردار"},
    "FIRs": {"roman": "FIRs", "ur": "ایف آئی آرز"},
    "Links to other people": {"roman": "Doosron se taluq", "ur": "دوسروں سے تعلق"},
    "In the incident window": {"roman": "Waqia ke waqt kahan", "ur": "وقوعہ کے وقت کہاں"},
    "Hotel stays": {"roman": "Hotel mein qayam", "ur": "ہوٹل میں قیام"},
    "Timeline": {"roman": "Tarteeb-e-waqiat", "ur": "ترتیبِ واقعات"},
    "Place": {"roman": "Jagah", "ur": "جگہ"},
    "People and their roles": {"roman": "Log aur un ke kirdar", "ur": "لوگ اور ان کے کردار"},
    "How it happened, by each account": {"roman": "Har bayan ke mutabiq kaise hua", "ur": "ہر بیان کے مطابق کیسے ہوا"},
    "Where the accounts disagree": {"roman": "Bayanaat kahan mukhtalif hain", "ur": "بیانات کہاں مختلف ہیں"},
    "Each version": {"roman": "Har bayan", "ur": "ہر بیان"},
    "Key points": {"roman": "Aham nukaat", "ur": "اہم نکات"},
    "What changed between versions": {"roman": "Bayanaat mein kya badla", "ur": "بیانات میں کیا بدلا"},
    "Against the evidence": {"roman": "Saboot se farq", "ur": "ثبوت سے فرق"},
    "Relation to the parties": {"roman": "Fareeqain se rishta", "ur": "فریقین سے رشتہ"},
    "Every connection": {"roman": "Har taluq", "ur": "ہر تعلق"},
    "First and last contact": {"roman": "Pehla aur aakhri rabta", "ur": "پہلا اور آخری رابطہ"},
    "How strong": {"roman": "Kitna mazboot", "ur": "کتنا مضبوط"},
    "Still unknown": {"roman": "Abhi maloom nahi", "ur": "ابھی معلوم نہیں"},
    "Owner": {"roman": "Malik", "ur": "مالک"},
    "Period covered": {"roman": "Muddat", "ur": "مدت"},
    "Top contacts": {"roman": "Zyada rabtay", "ur": "زیادہ رابطے"},
    "Near the scene": {"roman": "Jaye waqia ke qareeb", "ur": "جائے وقوعہ کے قریب"},
    "Silences": {"roman": "Khamoshi ke auqaat", "ur": "خاموشی کے اوقات"},
    "IMEI changes": {"roman": "IMEI ki tabdeeli", "ur": "آئی ایم ای آئی کی تبدیلی"},
    "Links to other CDRs": {"roman": "Doosre CDRs se taluq", "ur": "دوسرے سی ڈی آرز سے تعلق"},
    "The items": {"roman": "Tafseel", "ur": "تفصیل"},
    "The basis": {"roman": "Bunyad", "ur": "بنیاد"},
    "Everything checked": {"roman": "Jo kuch check kiya", "ur": "جو کچھ چیک کیا"},
    "Ranked steps": {"roman": "Agle iqdamaat", "ur": "اگلے اقدامات"},
}
MARKED = {"not on record": {"en": "Not on record.", "roman": "Record mein nahi.", "ur": "ریکارڈ میں نہیں۔"},
          "not checked yet": {"en": "Not checked yet.", "roman": "Abhi check nahi hua.", "ur": "ابھی چیک نہیں ہوا۔"}}
STATUSES = ("filled", "not on record", "not checked yet")


def sections(shape: str) -> list[str]:
    return SHAPES.get(shape) or []


def heading(name: str, language: str) -> str:
    return (HEADINGS.get(name) or {}).get(language) or name


def complete(draft: dict[str, Any], shape: str) -> list[str]:
    """O2's completeness check: the sections of the shape the draft left out. Each must be
    filled with sources, or marked - never dropped."""
    have = {s["name"] for s in draft.get("sections") or [] if (s.get("body") or "").strip() or s.get("status") in STATUSES[1:]}
    return [name for name in sections(shape) if name not in have]


def fill_missing(draft: dict[str, Any], shape: str) -> dict[str, Any]:
    """A section the draft dropped is put back, marked "not checked yet" - never silently gone."""
    by_name = {s["name"]: s for s in draft.get("sections") or []}
    ordered = [by_name.get(name) or {"name": name, "body": "", "status": "not checked yet"} for name in sections(shape)]
    extra = [s for s in draft.get("sections") or [] if s["name"] not in sections(shape) and (s.get("body") or "").strip()]
    return {**draft, "sections": ordered + extra}


def render(draft: dict[str, Any], shape: str, language: str, *, depth: str = "detailed") -> str:
    """The checked draft as text: the answer first, then the sections under headings.
    ``depth`` brief: the first line and the key points only."""
    lines = [f"**{draft['first_line'].strip().strip('*')}**"] if (draft.get("first_line") or "").strip() else []
    filled = [s for s in draft.get("sections") or [] if (s.get("body") or "").strip() and s.get("status", "filled") == "filled"]
    if depth == "brief" or not sections(shape):
        for s in filled[:3] if depth == "brief" else filled:
            lines.append(s["body"].strip())
        return "\n".join(lines)
    for s in draft.get("sections") or []:
        body = (s.get("body") or "").strip()
        mark = MARKED.get(s.get("status") or "", {}).get(language) or MARKED.get(s.get("status") or "", {}).get("en")
        if not body and not mark:
            continue
        lines.append(f"{HEAD_MARK}{heading(s['name'], language)}")
        lines.append(body if body and s.get("status", "filled") == "filled" else (mark or body))
    return "\n".join(lines)
