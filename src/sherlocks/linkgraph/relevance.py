"""The new case, and how the previous record bears on it.

The search starts from a target to learn who he is involved with - but the case being
worked is a **new** one: the incident the officer pinned and described (what happened,
where, when). The FIRs already on record - his and his associates' - are background:
they matter as far as they bear on the new case. For each previous FIR this says how:

* **same kind of crime** as the new case (robbery and dacoity, murder and attempted murder);
* **same area** - the new case's place or police station appears in the old FIR;
* **the same people** - someone in the old FIR (co-accused, complainant) is linked to a
  target, or is a target;
* **recent** - within two years before the new incident.

Code only, so the chat, Sherlock and the report all say the same; the model gets these
as facts and explains them.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

# Crimes of one family count as "the same kind of crime".
_FAMILY = [{"murder", "attempted_murder", "hurt"}, {"robbery", "dacoity", "vehicle_theft", "theft", "stolen_property"},
           {"kidnapping", "kidnap_ransom"}, {"rape", "attempted_rape", "sexual_assault"}, {"extortion"},
           {"narcotics"}, {"arms"}, {"fraud", "cheque", "breach_of_trust"}, {"terrorism"}]
_YEAR = re.compile(r"(?:19|20)\d{2}")


def new_case(case: Any) -> dict[str, Any]:
    """The case under investigation, from the board: ``{known, crimes, place, date, ps, fir}``."""
    from sherlocks.linkgraph.offences import classify

    inc = case.incident or {}
    crimes = classify(" ".join(str(inc.get(k) or "") for k in ("offence", "sections", "vehicle_or_weapon")))
    if not crimes and inc.get("offence"):
        from sherlocks.linkgraph.offences import asked_crimes

        crimes = sorted(asked_crimes(str(inc["offence"])))
    place = " ".join(str(inc.get(k) or "") for k in ("place", "nearest_ps", "police_station")).strip()
    return {"known": bool(inc.get("offence") or inc.get("place") or inc.get("date")), "crimes": crimes,
            "place": place, "date": str(inc.get("date") or "")[:10], "fir": inc.get("fir"),
            "offence": inc.get("offence")}


def _family(keys: list[str]) -> set[int]:
    return {i for i, fam in enumerate(_FAMILY) for k in keys if k in fam}


def _year(text: str) -> int | None:
    m = _YEAR.search(text or "")
    return int(m.group(0)) if m else None


def fir_relevance(row: dict[str, Any], new: dict[str, Any], *, linked: set[str] | None = None,
                  people_in: list[str] | None = None) -> dict[str, Any]:
    """How one previous FIR (a ``case_queries._firs`` row) bears on the new case:
    ``{score, reasons}`` - reasons in plain English, the strongest first."""
    reasons: list[tuple[int, str]] = []
    if new.get("fir") and str(new["fir"]).split("/")[0].lstrip("0") == str(row.get("label", "")).split("/")[0].lstrip("0"):
        reasons.append((5, "it is the new case's own FIR"))
    same = _family(row.get("crimes") or []) & _family(new.get("crimes") or [])
    if same:
        reasons.append((3, "same kind of crime as the new case"))
    place = (new.get("place") or "").lower()
    where = f"{row.get('ps') or ''} {row.get('place') or ''}".lower()
    words = {w for w in re.findall(r"[a-z؀-ۿ]{4,}", place)} - {"road", "street", "block", "near", "town", "police",
                                                                    "station", "karachi"}
    if words and any(w in where for w in words):
        reasons.append((2, "same area as the new case"))
    names = [n for n in people_in or [] if n in (linked or set())]
    if names:
        reasons.append((2, "involves " + ", ".join(names[:3]) + ", linked to the target"))
    y_new, y_old = _year(new.get("date") or ""), _year(str(row.get("occurred") or row.get("label") or ""))
    if y_new and y_old and 0 <= y_new - y_old <= 2:
        reasons.append((1, "recent - within two years before the new case"))
    reasons.sort(key=lambda r: -r[0])
    return {"score": sum(r[0] for r in reasons), "reasons": [r[1] for r in reasons]}


def background(case: Any, net: Any, people: list[str] | None = None) -> list[dict[str, Any]]:
    """The previous FIRs of the people (default: the targets and their stated associates),
    each with how it bears on the new case, most relevant first."""
    from sherlocks.linkgraph.case_queries import _firs, accused_of

    new = new_case(case)
    targets = list(net.seeds())
    if people is None:
        people = list(dict.fromkeys([*targets, *[c["id"] for t in targets if t in net.G
                                                 for c in net.neighbours(t) if c["stated"]][:12]]))
    linked = {net.name(p) for t in targets if t in net.G for p in [t, *[c["id"] for c in net.neighbours(t)]]}
    out, seen = [], set()
    for pid in people:
        if pid not in net.people:
            continue
        for row in _firs(net, pid, case):
            key = (pid, row["label"])
            if key in seen:
                continue
            seen.add(key)
            others = [n for n in accused_of(net, row["label"], case, exclude=pid) if n.lower() != net.name(pid).lower()]
            rel = fir_relevance(row, new, linked=linked, people_in=others)
            out.append({"pid": pid, "name": net.name(pid), "fir": row["label"], "ps": row.get("ps"),
                        "crimes": row.get("crimes") or [], "role": row["role_kind"], "doc": row.get("doc"),
                        "sources": row.get("sources") or [], **rel})
    return sorted(out, key=lambda r: -r["score"])


REL_WORDS = {
    "same kind of crime as the new case": ("same kind of crime as the new case", "naye case jaisa jurm",
                                           "نئے کیس جیسا جرم"),
    "same area as the new case": ("same area as the new case", "naye case wala ilaqa", "نئے کیس والا علاقہ"),
    "recent - within two years before the new case": ("recent (within two years before the new case)",
                                                      "haal ka (naye case se do saal pehle tak)",
                                                      "حالیہ (نئے کیس سے دو سال پہلے تک)"),
    "it is the new case's own FIR": ("the new case's own FIR", "naye case ki apni FIR", "نئے کیس کی اپنی ایف آئی آر"),
}


def say_reasons(reasons: list[str], language: str) -> str:
    i = {"en": 0, "roman": 1, "ur": 2}.get(language, 0)
    out = []
    for r in reasons:
        if r in REL_WORDS:
            out.append(REL_WORDS[r][i])
        elif r.startswith("involves "):
            names = r[len("involves "):].replace(", linked to the target", "")
            out.append((f"involves {names}, linked to the target", f"is mein {names} shamil (target se juray)",
                        f"اس میں {names} شامل (ٹارگٹ سے جڑے)")[i])
    sep = "، " if language == "ur" else "; "
    return sep.join(out)
