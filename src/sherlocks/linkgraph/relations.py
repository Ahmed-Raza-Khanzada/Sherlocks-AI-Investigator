"""Turn a system's role label into a plain sentence with a direction.

A strong edge in the graph runs from a *record* to a *person*: "this Old Tenant record,
which belongs to Kamran, names Tariq as Landlord". The label is Tariq's role relative
to Kamran. For an analyst that should read as one directed statement:

    Tariq Hussain  —is landlord of→                      Kamran Ahmed
    Asif Ali       —was witness to the tenancy of→       Kamran Ahmed
    Waqas Javed    —filed FIR 45/2023 against→           Kamran Ahmed   (302 PPC)
    Sajid Mehmood  —is co-accused with→                  Kamran Ahmed   in FIR 45/2023

Some roles read the other way round ("Accused by the subject in FIR 45/2023" means the
record's owner filed the FIR), so each rule says which end the arrow starts from.
Symmetric relations (co-accused, room-mates, shared phone) carry no arrow.

Nothing here decides *whether* two people are related - only how a relation that a
system already stated is worded. Unknown labels fall back to "is <label> of".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_FIR = r"(?:FIR\s+)?(?P<fir>\d+\s*/\s*\d{2,4})"


@dataclass(frozen=True, slots=True)
class Relation:
    source: str          # person id the arrow starts at
    target: str          # person id it points to
    verb: str            # "is landlord of"
    symmetric: bool
    fir: str | None = None
    charges: str | None = None
    note: str | None = None      # "same employer · inferred 0.40"

    def sentence(self, names: dict[str, str]) -> str:
        a, b = names.get(self.source, self.source), names.get(self.target, self.target)
        text = f"{a} {self.verb} {b}"
        if self.fir and self.fir not in self.verb:
            text += f" in FIR {self.fir}"
        if self.charges:
            text += f" (charges: {self.charges})"
        if self.note:
            text += f" — {self.note}"
        return text

    def to_dict(self, names: dict[str, str]) -> dict:
        return {"from": self.source, "to": self.target, "verb": self.verb, "symmetric": self.symmetric,
                "fir": self.fir, "charges": self.charges, "note": self.note, "sentence": self.sentence(names)}


# (pattern, verb, direction, symmetric)
#   direction "named" : named person -> record owner   (Tariq is landlord of Kamran)
#   direction "owner" : record owner -> named person   (Kamran filed FIR against Imran)
_RULES: list[tuple[re.Pattern[str], str, str, bool]] = [
    # -- criminal / FIR -------------------------------------------------------------
    (re.compile(rf"^complainant against the subject in {_FIR}", re.IGNORECASE), "filed FIR {fir} against", "named", False),
    (re.compile(rf"^accused by the subject in {_FIR}", re.IGNORECASE), "filed FIR {fir} against", "owner", False),
    (re.compile(rf"^accused of harming the subject in {_FIR}", re.IGNORECASE), "is accused in FIR {fir} of harming",
     "named", False),
    (re.compile(rf"^co-accused(?: \(nominated\))? in {_FIR}", re.IGNORECASE), "is co-accused with", "named", True),
    (re.compile(r"^co-accused", re.IGNORECASE), "is co-accused with", "named", True),
    (re.compile(rf"^complainant in {_FIR}", re.IGNORECASE), "is complainant in FIR {fir} involving", "named", False),
    (re.compile(rf"^witness in {_FIR}", re.IGNORECASE), "is a witness in FIR {fir} involving", "named", False),
    (re.compile(rf"^investigating officer in {_FIR}", re.IGNORECASE), "investigated FIR {fir} involving", "named", False),
    (re.compile(rf"^accused(?: / suspect)? in {_FIR}", re.IGNORECASE), "is accused in FIR {fir} alongside", "named", True),
    (re.compile(rf"^named in {_FIR}", re.IGNORECASE), "is named in FIR {fir} with", "named", True),
    (re.compile(rf"^same identifier in {_FIR}(?: \((?P<role>[^)]+)\))?", re.IGNORECASE),
     "appears in FIR {fir} on the same phone/CNIC as", "named", False),
    (re.compile(rf"^same fir {_FIR}", re.IGNORECASE), "is named in the same FIR {fir} as", "named", True),
    (re.compile(rf"^mentioned in the file of {_FIR}", re.IGNORECASE), "is mentioned in the file of FIR {fir} involving",
     "named", False),
    (re.compile(rf"^guarantor in {_FIR}", re.IGNORECASE), "is a guarantor in FIR {fir} involving", "named", False),
    (re.compile(r"^complainant \(vehicle case\)", re.IGNORECASE), "is complainant in a vehicle case involving", "named", False),
    (re.compile(r"^complainant", re.IGNORECASE), "is complainant in a case involving", "named", False),
    (re.compile(r"^(?:accused|suspect)", re.IGNORECASE), "is accused in a case alongside", "named", True),
    (re.compile(r"^victim", re.IGNORECASE), "is victim in a case involving", "named", False),
    (re.compile(r"^witness", re.IGNORECASE), "is a witness in a case involving", "named", False),
    (re.compile(r"^investigating officer", re.IGNORECASE), "is investigating officer in a case of", "named", False),
    # -- property / tenancy -----------------------------------------------------------
    (re.compile(r"^landlord \(tenancy record\)", re.IGNORECASE),
     "is landlord on a tenancy record found on the phone/CNIC of", "named", False),
    (re.compile(r"^tenant \(tenancy record\)", re.IGNORECASE),
     "is tenant on a tenancy record found on the phone/CNIC of", "named", False),
    # The subject witnessed the named tenant's tenancy: the arrow runs subject -> tenant.
    (re.compile(r"^tenancy witnessed by the subject", re.IGNORECASE), "was witness to the tenancy of", "owner", False),
    (re.compile(r"^tenancy witness", re.IGNORECASE), "was witness to the tenancy of", "named", False),
    (re.compile(r"^verification witness", re.IGNORECASE), "vouched as a PRVS witness for", "named", False),
    (re.compile(r"^prvs profile found by this number", re.IGNORECASE), "was found in PRVS by the phone number of", "named", False),
    (re.compile(r"^landlord", re.IGNORECASE), "is landlord of", "named", False),
    (re.compile(r"^tenant of owned property", re.IGNORECASE), "is tenant of", "named", False),
    (re.compile(r"^co-tenant", re.IGNORECASE), "shares a tenancy with", "named", True),
    (re.compile(r"^in phone contact(?: \((?P<what>[^)]*)\))?", re.IGNORECASE),
     "was in phone contact with", "named", True),
    (re.compile(r"^tenant", re.IGNORECASE), "is tenant of", "named", False),
    (re.compile(r"^same number on a prvs tenancy record", re.IGNORECASE), "is on a tenancy record with the same number as", "named", False),
    (re.compile(r"^owner$", re.IGNORECASE), "is owner linked to", "named", False),
    # -- family ------------------------------------------------------------------------
    (re.compile(r"^family member \((?P<kin>[^)]+)\)", re.IGNORECASE), "is {kin} of", "named", False),
    (re.compile(r"^family member", re.IGNORECASE), "is a family member of", "named", True),
    (re.compile(r"^(?P<kin>husband|wife|father|mother|son|daughter|brother|sister)$", re.IGNORECASE), "is {kin} of", "named", False),
    (re.compile(r"^relative", re.IGNORECASE), "is a relative of", "named", True),
    (re.compile(r"^possible siblings|siblings", re.IGNORECASE), "is possibly a sibling of", "named", True),
    # -- phones / identity ---------------------------------------------------------------
    (re.compile(r"^registered owner of sim (?P<sim>\S+)", re.IGNORECASE), "is registered owner of SIM {sim} used by", "named", False),
    (re.compile(r"^registered owner of (?:a shared )?sim", re.IGNORECASE), "is registered owner of a SIM used by", "named", False),
    (re.compile(r"^sim (?P<sim>\S+) on another cnic", re.IGNORECASE), "holds SIM {sim}, also linked to", "named", False),
    (re.compile(r"^shared phone (?P<sim>\S+)", re.IGNORECASE), "shares phone {sim} with", "named", True),
    (re.compile(r"^driving licence registered with the same number", re.IGNORECASE), "holds a driving licence on the same number as", "named", False),
    (re.compile(r"^(?:verification record|named in verification)", re.IGNORECASE), "has a verification record on the same number as", "named", False),
    # -- travel ----------------------------------------------------------------------------
    (re.compile(r"^room-mate at hotel", re.IGNORECASE), "shared hotel with", "named", True),
    (re.compile(r"^shared hotel stay", re.IGNORECASE), "shared hotel with", "named", True),
    (re.compile(r"^co-guest", re.IGNORECASE), "stayed at the same hotel on the same day as", "named", True),
    (re.compile(r"^guest at the same hotel", re.IGNORECASE), "stayed at the same hotel as", "named", True),
    (re.compile(r"^used same phone/cnic at hotel", re.IGNORECASE), "checked into a hotel on the same phone/CNIC as", "named", False),
    # -- vehicles / work / complaints -----------------------------------------------------------
    (re.compile(r"^same vehicle (?P<sim>\S+)", re.IGNORECASE), "is linked to the same vehicle {sim} as", "named", True),
    (re.compile(r"^vehicle owner", re.IGNORECASE), "owns a vehicle linked to", "named", False),
    (re.compile(r"^driver", re.IGNORECASE), "is driver for", "named", False),
    (re.compile(r"^employer", re.IGNORECASE), "is employer of", "named", False),
    (re.compile(r"^employee", re.IGNORECASE), "is employee of", "named", False),
    (re.compile(r"^missing person", re.IGNORECASE), "was reported missing by", "named", False),
    (re.compile(r"^found person", re.IGNORECASE), "was reported found by", "named", False),
    (re.compile(r"^recovered from", re.IGNORECASE), "had property recovered in a report by", "named", False),
    (re.compile(r"^reported by", re.IGNORECASE), "filed a report naming", "named", False),
    (re.compile(r"^guarantor", re.IGNORECASE), "is guarantor for", "named", False),
    (re.compile(r"^reference", re.IGNORECASE), "is a reference for", "named", False),
    (re.compile(r"^nominee", re.IGNORECASE), "is nominee of", "named", False),
    (re.compile(r"^associate", re.IGNORECASE), "is an associate of", "named", True),
    (re.compile(r"^named in (?P<what>.+?) record", re.IGNORECASE), "is named in the {what} record of", "named", False),
    (re.compile(r"^named on traffic challan", re.IGNORECASE), "is named on a traffic challan of", "named", False),
]


def _fir_key(fir: str | None) -> str | None:
    return re.sub(r"\s+", "", fir) if fir else None


def _is_accused(role: str | None) -> bool:
    return bool(role) and any(t in role.lower() for t in ("accused", "suspect", "nominated"))


def _is_complainant(role: str | None) -> bool:
    return bool(role) and "complainant" in role.lower()


def describe(label: str, named: str, owner: str, *, charges_for: dict[str, str] | None = None,
             owner_roles: dict[str, str] | None = None) -> Relation:
    """``label`` is ``named``'s role relative to ``owner`` (the record's owner, or for a
    direct person-to-person edge, its source). ``owner_roles`` maps FIR number -> the
    owner's own role in it, which decides who filed against whom."""
    rel = _describe(label, named, owner, charges_for)
    role = (owner_roles or {}).get(rel.fir or "") if rel.fir else None
    text = (label or "").lower()
    if rel.fir and role:
        if text.startswith("complainant") and _is_accused(role):
            return Relation(named, owner, f"filed FIR {rel.fir} against", False, rel.fir, rel.charges)
        if (text.startswith(("accused", "co-accused")) or "suspect" in text) and _is_complainant(role):
            return Relation(owner, named, f"filed FIR {rel.fir} against", False, rel.fir, rel.charges)
        if text.startswith("witness") and _is_accused(role):
            return Relation(named, owner, f"is a witness in FIR {rel.fir} against", False, rel.fir, rel.charges)
    return rel


def _describe(label: str, named: str, owner: str, charges_for: dict[str, str] | None) -> Relation:
    text = (label or "").strip()
    for pattern, verb, direction, symmetric in _RULES:
        m = pattern.search(text)
        if not m:
            continue
        groups = {k: (v or "").strip() for k, v in m.groupdict().items()}
        fir = _fir_key(groups.get("fir"))
        wording = verb.format(fir=fir or "", kin=(groups.get("kin") or "").lower(), sim=groups.get("sim") or "",
                              what=groups.get("what") or "")
        wording = re.sub(r"\s+", " ", wording).strip()
        source, target = (named, owner) if direction == "named" else (owner, named)
        charges = (charges_for or {}).get(fir) if fir else None
        return Relation(source, target, wording, symmetric, fir, charges)
    # Unknown wording: keep the system's own words, still directed.
    fir_match = re.search(_FIR, text)
    fir = _fir_key(fir_match.group("fir")) if fir_match else None
    return Relation(named, owner, f"is {text.lower() or 'linked to'} of" if text else "is linked to",
                    False, fir, (charges_for or {}).get(fir) if fir else None)


# What each inferred signal says, as a verb. The first (strongest) signal names the
# relation; the rest go in the note. "May be linked" said nothing the dashed line did not.
_WEAK_VERBS = (
    ("same address", "shares an address with"),
    ("nearby address", "lives near"),
    ("neighbour", "is a neighbour of"),
    ("possible siblings", "may be a sibling of"),
    ("parent / child", "may be a parent or child of"),
    ("co-stay", "stayed at the same hotel at the same time as"),
    ("same employer", "works at the same place as"),
    ("police colleagues", "is posted at the same police station as"),
    ("osint", "is linked online to"),
    ("possibly same person", "may be the same person as"),
    ("same household", "may share a household with"),
    ("same workplace", "may work at the same place as"),
    ("family", "may be family of"),
)


def weak_relation(label: str, a: str, b: str, score: float | None = None) -> Relation:
    """An inferred link: a lead, worded as the fact it rests on."""
    parts = [p.strip() for p in (label or "").split("+") if p.strip()]
    first = parts[0].lower().removeprefix("ai:").strip() if parts else ""
    verb = next((v for key, v in _WEAK_VERBS if key in first), "has an inferred link to")
    extra = [p for p in parts[1:]]
    note = "inferred" + (f" {score:.2f}" if score is not None else "") + (f" · also {', '.join(extra).lower()}" if extra else "")
    return Relation(a, b, verb, True, note=note)
