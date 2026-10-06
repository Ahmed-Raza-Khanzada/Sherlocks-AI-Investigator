"""What an FIR is about: its sections (PPC, CNSA, Arms Act, ATA...) as crime types with a
seriousness, named in English, Roman Urdu and Urdu.

``classify("324, 365, 375, 337A(i), 34")`` -> attempted murder, kidnapping, rape, hurt.
Seriousness: 3 = heinous (murder, rape, kidnapping, dacoity, extortion, terrorism),
2 = serious (robbery, vehicle theft, theft, arms, narcotics, hurt, rioting), 1 = other
(cheque, fraud, breach of trust, threats, gutka).
"""

from __future__ import annotations

import re
from typing import Any

# key: (seriousness, English, Roman Urdu, Urdu)
CRIMES: dict[str, tuple[int, str, str, str]] = {
    "murder": (3, "murder", "qatl", "قتل"),
    "attempted_murder": (3, "attempted murder", "iqdam-e-qatl", "اقدام قتل"),
    "rape": (3, "rape", "zina bil jabr", "زنا بالجبر"),
    "attempted_rape": (3, "attempted rape", "zina bil jabr ki koshish", "زنا بالجبر کی کوشش"),
    "sexual_assault": (3, "sexual assault / unnatural offence", "jinsi ziyadti", "جنسی زیادتی"),
    "kidnapping": (3, "kidnapping / abduction", "aghwa", "اغوا"),
    "kidnap_ransom": (3, "kidnapping for ransom", "aghwa bara-e-tawan", "اغوا برائے تاوان"),
    "dacoity": (3, "dacoity", "dakaiti", "ڈکیتی"),
    "extortion": (3, "extortion (bhatta)", "bhatta khori", "بھتہ خوری"),
    "terrorism": (3, "terrorism", "dehshatgardi", "دہشت گردی"),
    "robbery": (2, "robbery", "rahzani / chheena jhapti", "راہزنی"),
    "vehicle_theft": (2, "vehicle theft", "gaari chori", "گاڑی چوری"),
    "theft": (2, "theft / burglary", "chori / naqab zani", "چوری / نقب زنی"),
    "stolen_property": (2, "receiving stolen property", "chori ka maal rakhna", "مال مسروقہ"),
    "arms": (2, "illegal weapon", "ghair qanooni hathiyar", "غیر قانونی اسلحہ"),
    "narcotics": (2, "narcotics", "manshiyat", "منشیات"),
    "hurt": (2, "causing hurt / injury", "zakhmi karna", "زخمی کرنا"),
    "rioting": (2, "rioting", "hangama arai", "ہنگامہ آرائی"),
    "cheque": (1, "dishonoured cheque", "cheque bounce", "چیک باؤنس"),
    "fraud": (1, "fraud / cheating", "dhoka dahi", "دھوکہ دہی"),
    "breach_of_trust": (1, "criminal breach of trust", "amanat mein khayanat", "خیانت مجرمانہ"),
    "forgery": (1, "forgery", "jaal saazi", "جعل سازی"),
    "threat": (1, "criminal intimidation", "dhamkiyan", "دھمکیاں"),
    "gutka": (1, "gutka / mawa", "gutka / mawa", "گٹکا / ماوا"),
}

_SECTIONS: list[tuple[re.Pattern[str], str]] = [(re.compile(p, re.IGNORECASE), k) for p, k in [
    (r"^(302|303|311|316|304)$", "murder"),
    (r"^(324|307)$", "attempted_murder"),
    (r"^(365a|365-a)$", "kidnap_ransom"),
    (r"^(359|360|361|362|363|364|364a|365|365b|366|366a|366b|367|368|369|369a)$", "kidnapping"),
    (r"^(375|376|376a|376b)$", "rape"),
    (r"^(377|377a|377b|354a|354)$", "sexual_assault"),
    (r"^(395|396|397|398|399|400|401|402)$", "dacoity"),
    (r"^(384|385|386|387|388|389)$", "extortion"),
    (r"^(392|393|394)$", "robbery"),
    (r"^(381a|381-a)$", "vehicle_theft"),
    (r"^(378|379|380|381|382|454|455|456|457|458|459|460|461)$", "theft"),
    (r"^(411|412|413|414)$", "stolen_property"),
    (r"^337[a-z]?$|^(332|333|334|335|336|338)$", "hurt"),
    (r"^(147|148|149)$", "rioting"),
    (r"^489f$", "cheque"),
    (r"^(415|416|417|418|419|420)$", "fraud"),
    (r"^(405|406|407|408|409)$", "breach_of_trust"),
    (r"^(463|465|466|467|468|469|470|471|472|473|474)$", "forgery"),
    (r"^(503|504|506|507)$", "threat"),
]]
_WORDS: list[tuple[re.Pattern[str], str]] = [(re.compile(p, re.IGNORECASE), k) for p, k in [
    (r"narcotic|cnsa|منشیات|charas|heroin|\bice\b", "narcotics"),
    (r"arms? act|اسلحہ|13[-\s]?d\b|23\s*\(1\)", "arms"),
    (r"anti[-\s]?terror|\bata\b|7\s*ata|دہشت", "terrorism"),
    (r"گٹکا|gutka|mawa|ماوا", "gutka"),
]]
# Sections may be joined by "/" ("365/377/337A(i)/34"); offence fields never hold FIR numbers.
_SECTION = re.compile(r"(?<!\d)(\d{3})\s*-?\s*([A-Za-z])?(?![\dA-Za-z])")


def classify(offence: str | None) -> list[str]:
    """Crime types of an FIR's offence text, most serious first."""
    text = str(offence or "")
    found: list[str] = []
    sections = {f"{m.group(1)}{(m.group(2) or '').lower()}" for m in _SECTION.finditer(text)}
    attempt = "511" in sections
    for sec in sections:
        for rx, key in _SECTIONS:
            if rx.match(sec):
                if attempt and key == "rape":
                    key = "attempted_rape"
                found.append(key)
                break
    for rx, key in _WORDS:
        if rx.search(text):
            found.append(key)
    out = list(dict.fromkeys(found))
    return sorted(out, key=lambda k: (-CRIMES[k][0], list(CRIMES).index(k)))


def severity(keys: list[str]) -> int:
    return max((CRIMES[k][0] for k in keys), default=0)


def label(key: str, language: str = "en") -> str:
    _, en, roman, ur = CRIMES[key]
    return {"en": en, "roman": roman, "ur": ur}.get(language, en)


def labels(keys: list[str], language: str = "en") -> str:
    sep = "، " if language == "ur" else ", "
    return sep.join(label(k, language) for k in keys)


# What the officer may ask for: "qatl ke case", "gaari chori", "bhatta", "khatarnak".
ASKED: list[tuple[re.Pattern[str], set[str]]] = [(re.compile(p, re.IGNORECASE), set(k)) for p, k in [
    (r"qatl|katl|murder|kill|maar(?:a|na|ne)|قتل|خون", {"murder", "attempted_murder"}),
    (r"\brape|zina|ziyadti|zyadti|زیادتی|زنا|jinsi|sexual|بدفعلی|badfeli", {"rape", "attempted_rape", "sexual_assault"}),
    (r"aghwa|agwa|kidnap|abduct|اغوا|tawan|ransom", {"kidnapping", "kidnap_ransom"}),
    (r"dakaiti|dakait|dacoit|ڈکیتی|ڈاکہ|daka\b", {"dacoity"}),
    (r"robber|chheena|chhina|snatch|چھین|راہزن", {"robbery", "dacoity"}),
    (r"bhatta|bhata|extort|بھتہ", {"extortion"}),
    (r"gaa?r[iy] chor|gadi chor|car theft|vehicle theft|bike chor|motorcycle chor|گاڑی چوری", {"vehicle_theft"}),
    (r"\bchor[iy]?\b|theft|burglar|naqab|چوری|نقب", {"theft", "vehicle_theft", "stolen_property"}),
    (r"narcotic|drug|manshiyat|charas|heroin|\bice\b|منشیات|چرس", {"narcotics"}),
    (r"hathiyar|weapon|pistol|arms|اسلحہ|پستول", {"arms"}),
    (r"terror|dehshat|دہشت", {"terrorism"}),
    (r"fraud|dhoka|دھوکہ|cheque|chek|چیک", {"fraud", "cheque"}),
]]
SERIOUS_WORDS = re.compile(r"(?<![\w؀-ۿ])(khatar?nak|khatarnaak|sange?e?n|serious|dangerous|heinous|major|"
                           r"violent|bad[ae] (?:case|fir|jurm)|خطرناک|سنگین|بڑے)(?![\w؀-ۿ])", re.IGNORECASE)


def asked_crimes(text: str) -> set[str]:
    out: set[str] = set()
    for rx, keys in ASKED:
        if rx.search(text or ""):
            out |= keys
    if "vehicle_theft" in out and re.search(r"gaa?r[iy]|gadi|car|bike|motorcycle|vehicle|گاڑی", text or "", re.IGNORECASE):
        out -= {"theft", "stolen_property"}      # "gaari chori" is vehicle theft only
    return out


def summary_line(rows: list[dict[str, Any]], language: str = "en") -> str:
    """"murder 1, kidnapping 2, cheque 2" - how many FIRs of each crime type."""
    counts: dict[str, int] = {}
    for r in rows:
        for k in r.get("crimes") or []:
            counts[k] = counts.get(k, 0) + 1
    order = sorted(counts, key=lambda k: (-CRIMES[k][0], -counts[k]))
    sep = "، " if language == "ur" else ", "
    return sep.join(f"{label(k, language)} ({counts[k]})" for k in order)
