"""Identifier, name and address normalisation.

Deliberately separate from ``cdr_report_app.utils.phones``: that module imports pandas
and accepts anything digit-shaped. Identity resolution needs the opposite - reject
anything that is not unambiguously a CNIC or a Pakistani mobile, because a false match
here merges two real people.
"""

from __future__ import annotations

import re
import unicodedata

_PLACEHOLDERS = {
    "",
    "-",
    "--",
    "no",
    "na",
    "n/a",
    "nil",
    "null",
    "none",
    "nan",
    "unknown",
    "not available",
    "new cust",
    "data not recieved from nadra",
    "data not received from nadra",
    "0",
}


def digits(value: object) -> str:
    return re.sub(r"\D", "", str(value if value is not None else ""))


def cnic13(value: object) -> str | None:
    """A CNIC as 13 bare digits, or ``None``.

    All-zero and repeated-digit strings appear in upstream data as "unknown" markers;
    treating them as a CNIC would merge every unknown person into one node.
    """
    d = digits(value)
    if len(d) != 13 or len(set(d)) == 1:
        return None
    return d


def dashed_cnic(value: object) -> str | None:
    d = cnic13(value)
    return f"{d[:5]}-{d[5:12]}-{d[12]}" if d else None


def dashed_mobile(value: object) -> str | None:
    m = mobile11(value)
    return f"{m[:4]}-{m[4:]}" if m else None


def mobile11(value: object) -> str | None:
    """A Pakistani mobile as ``03XXXXXXXXX``, or ``None``.

    Landlines are rejected on purpose. An office or PTCL number is shared by many
    people, so it must never become an identity key.
    """
    d = digits(value)
    if d.startswith("0092"):
        d = d[4:]
    elif d.startswith("92") and len(d) == 12:
        d = d[2:]
    if len(d) == 10 and d.startswith("3"):
        d = "0" + d
    if len(d) == 11 and d.startswith("03") and len(set(d[2:])) > 1:
        return d
    return None


def phone_variants(value: object) -> list[str]:
    """Every format a Pakistani mobile might be stored as, so a search that matches the
    stored string literally can be tried against all of them:
    03001234567, 0300-1234567, 3001234567, 923001234567, +923001234567,
    and the dashed international spellings 92300-1234567 / +92300-1234567.
    """
    m = mobile11(value)
    if not m:
        return []
    ten = m[1:]  # drop leading 0 -> 3001234567
    dashed = f"{ten[:3]}-{ten[3:]}"  # 300-1234567
    return [m, f"{m[:4]}-{m[4:]}", ten, f"92{ten}", f"+92{ten}",
            f"92{dashed}", f"+92{dashed}", dashed]


def detect_identifier(text: str) -> tuple[str, str] | None:
    """Classify free input from the search box as ``("cnic"|"phone", normalised)``."""
    text = (text or "").strip()
    if not text:
        return None
    if cnic := cnic13(text):
        return ("cnic", cnic)
    if phone := mobile11(text):
        return ("phone", phone)
    return None


def clean_text(value: object) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip().strip(",;")
    return None if text.lower() in _PLACEHOLDERS else text


def clean_name(value: object) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    # A "name" that is mostly digits is an identifier in the wrong field.
    if sum(ch.isdigit() for ch in text) > len(text) / 3:
        return None
    return text[:120]


_HONORIFICS = {
    "mr",
    "mrs",
    "ms",
    "miss",
    "dr",
    "haji",
    "hafiz",
    "mian",
    "ch",
    "chaudhry",
    "rana",
    "malik",
    "mst",
    "mst.",
    "sheikh",
}
_NAME_VARIANTS = {
    "mohammad": "muhammad",
    "mohammed": "muhammad",
    "muhammed": "muhammad",
    "mohamed": "muhammad",
    "mohd": "muhammad",
    "md": "muhammad",
    "m": "muhammad",
    "muhamad": "muhammad",
    "syed": "syed",
    "sayed": "syed",
    "sayyed": "syed",
    "shah": "shah",
    "ahmad": "ahmed",
    "hussain": "hussain",
    "husain": "hussain",
    "hussein": "hussain",
    "hassan": "hasan",
    "rehman": "rahman",
    "ur": "",
    "ul": "",
    "e": "",
}
# "Ali s/o Raza" - relationship markers that split a person from their father.
_RELATION_SPLIT = re.compile(r"\b(?:s/o|d/o|w/o|son of|daughter of|wife of)\b", re.IGNORECASE)


def split_relation(name: str | None) -> tuple[str | None, str | None]:
    """``"Ali Raza s/o Ghulam Nabi"`` -> ``("Ali Raza", "Ghulam Nabi")``."""
    if not name:
        return None, None
    parts = _RELATION_SPLIT.split(name, maxsplit=1)
    if len(parts) == 2:
        return clean_name(parts[0]), clean_name(parts[1])
    return clean_name(name), None


def name_key(value: object) -> str:
    """Comparison form of a name: lowercase, honorifics dropped, spellings unified."""
    text = unicodedata.normalize("NFKD", str(value or "")).lower()
    tokens = re.findall(r"[a-z]+|[؀-ۿ]+", text)
    out: list[str] = []
    for token in tokens:
        if token in _HONORIFICS:
            continue
        token = _NAME_VARIANTS.get(token, token)
        if token:
            out.append(token)
    return " ".join(out)


def surname(value: object) -> str | None:
    tokens = name_key(value).split()
    return tokens[-1] if len(tokens) >= 2 else None


# --------------------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------------------

_ADDRESS_ABBREVIATIONS = [
    (r"\bh\s*#|\bh\.?\s*no\.?|\bhouse\s*no\.?|\bhno\.?|\bhouse\s*#|\bbungalow\s*(?:no\.?|#)?", " house "),
    (r"\bflat\s*no\.?|\bflat\s*#|\bapt\.?|\bapartment\s*no\.?", " flat "),
    (r"\bplot\s*no\.?|\bplot\s*#", " plot "),
    (r"\bst\.?(?=\s|\d|$)|\bstr\.?(?=\s|\d|$)|\bstreet\s*no\.?|\bgali\b", " street "),
    (r"\bblk\.?|\bblock\s*no\.?", " block "),
    (r"\bsec\.?(?=\s|\d|$)|\bsector\s*no\.?", " sector "),
    (r"\bmohalla\b|\bmuhalla\b|\bmohallah\b|\bmahalla\b", " mohalla "),
    (r"\bgulshan[\s\-]*e[\s\-]*iqbal\b|\bgulshan\s+iqbal\b", " gulshaniqbal "),
    (r"\bgulistan[\s\-]*e[\s\-]*jauhar\b|\bgulistan\s+jauhar\b|\bjohar\b", " gulistanjauhar "),
    (r"\bn\.?\s*nazimabad\b|\bnorth\s+nazimabad\b", " northnazimabad "),
    (r"\bdha\b|\bdefen[cs]e(\s+housing\s+authority)?\b", " dha "),
    (r"\bp\.?e\.?c\.?h\.?s\.?\b", " pechs "),
    (r"\bkhi\b", " karachi "),
    (r"\bhyd\b", " hyderabad "),
    (r"\blhr\b", " lahore "),
    (r"\bisb\b|\bisl\b", " islamabad "),
    (r"\brwp\b|\bpindi\b", " rawalpindi "),
    (r"\bgawader\b", " gwadar "),
    (r"\bnawabshah\b", " shaheedbenazirabad "),
]

# Enough of a gazetteer to recognise a city and to reject one a model invents.
PK_CITIES = {
    "karachi", "hyderabad", "sukkur", "larkana", "shaheedbenazirabad", "mirpurkhas",
    "jamshoro", "kotri", "thatta", "badin", "dadu", "khairpur", "ghotki", "jacobabad",
    "shikarpur", "kashmore", "kandhkot", "sanghar", "naushahro", "matiari", "tando",
    "umerkot", "tharparkar", "mithi", "sujawal", "qambar", "shahdadkot", "sehwan",
    "lahore", "islamabad", "rawalpindi", "quetta", "peshawar", "multan", "faisalabad",
    "gujranwala", "sialkot", "sargodha", "bahawalpur", "gwadar", "turbat", "hub",
    "mardan", "abbottabad", "swat", "dera", "rahim", "sahiwal", "okara", "jhelum",
}

_ADDRESS_STOPWORDS = {
    "no", "of", "the", "near", "opp", "opposite", "behind", "main", "road", "rd",
    "town", "colony", "city", "district", "distt", "dist", "tehsil", "taluka", "pakistan",
    "sindh", "punjab", "area", "phase", "house", "flat", "plot", "street", "block",
    "sector", "mohalla", "village", "goth", "and", "at", "p", "o", "po",
}


def normalize_address(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).lower()
    for pattern, replacement in _ADDRESS_ABBREVIATIONS:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"[^a-z0-9؀-ۿ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _number_after(text: str, word: str) -> str | None:
    match = re.search(rf"\b{word}\s+([a-z]?\d+[a-z]?(?:\s*[a-z]\b)?)", text)
    return match.group(1).replace(" ", "") if match else None


def parse_address(value: object) -> dict[str, str | None]:
    """Rule-based decomposition. Good enough to compare; the LLM refines the rest."""
    text = normalize_address(value)
    house = _number_after(text, "house") or _number_after(text, "flat") or _number_after(text, "plot")
    if not house:
        lead = re.match(r"^([a-z]?\d+[a-z]?)\b", text)
        house = lead.group(1) if lead else None
    city = next((token for token in reversed(text.split()) if token in PK_CITIES), None)
    # Area = the place names left once numbers, single letters and filler are gone.
    tokens = [t for t in text.split() if t not in _ADDRESS_STOPWORDS and len(t) > 1 and not any(c.isdigit() for c in t)]
    return {
        "house": house,
        "street": _number_after(text, "street"),
        "block": _number_after(text, "block"),
        "sector": _number_after(text, "sector"),
        "city": city,
        "area_tokens": " ".join(t for t in tokens if t != city) or None,
    }


def clean_address(value: object) -> str | None:
    text = clean_text(value)
    if not text or len(text) < 4 or not re.search(r"[A-Za-z؀-ۿ]", text):
        return None
    return text[:300]


_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def valid_email(value: object) -> str | None:
    """Lower-cased email address, or ``None``."""
    text = str(value or "").strip().lower()
    return text if _EMAIL.match(text) else None
