"""Is this online person the person in our records?

A name search on a person-data API (Pipl) returns everyone called "Kamran Ahmed" it
knows. Taking the first one would attach a stranger's email to the subject - and the
email pivot would then search the stranger. So each candidate is checked against what
the police/government records already say about the subject, and kept only when the
records corroborate it:

* the same phone or email                      - decisive on its own
* the same city, the same area                 - supporting
* a relative named as the subject's father     - strong
* a job at the subject's employer               - strong

A common name needs more corroboration than a rare one (``rarity.py``). The result is
still a lead - "corroborated" means the records agree, not that anyone has proved it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from sherlocks.linkgraph.normalize import name_key, parse_address
from sherlocks.linkgraph.rarity import commonness

# Matched on the subject's name at least this closely, or the candidate is not them.
NAME_MIN = 85
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def emails_in(value: Any) -> list[str]:
    """Every email address in any text / nested structure, lower-cased, in order."""
    found: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, str) and "@" in v:
            for e in _EMAIL_RE.findall(v):
                e = e.lower().rstrip(".")
                if e not in found and not e.endswith((".png", ".jpg", ".jpeg", ".gif")):
                    found.append(e)

    walk(value)
    return found


@dataclass
class KnownFacts:
    """What the records say about the subject - the yardstick for online candidates."""

    name: str
    father_name: str | None = None
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    organisations: list[str] = field(default_factory=list)

    @classmethod
    def from_person(cls, data: dict[str, Any], emails: list[str] | None = None) -> KnownFacts:
        return cls(name=str(data.get("name") or ""), father_name=data.get("father_name"),
                   phones=list(data.get("phones") or []), emails=list(emails or []),
                   addresses=list(data.get("addresses") or []), organisations=list(data.get("organisations") or []))

    def cities(self) -> set[str]:
        return {c.lower() for c in (parse_address(a).get("city") for a in self.addresses) if c}

    def area_tokens(self) -> set[str]:
        tokens: set[str] = set()
        for a in self.addresses:
            tokens |= {t for t in (parse_address(a).get("area_tokens") or "").split() if len(t) > 3}
        return tokens - self.cities()


@dataclass
class Match:
    name: str
    score: float
    status: str                  # corroborated | possible | rejected
    reasons: list[str]
    emails: list[str]
    usernames: list[str]
    urls: list[str]
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "score": round(self.score, 2), "status": self.status, "reasons": self.reasons,
                "emails": self.emails, "usernames": self.usernames, "urls": self.urls, "source": self.source}


def _values(person: dict, key: str, *fields: str) -> list[str]:
    out = []
    for item in person.get(key) or []:
        if isinstance(item, dict):
            value = next((item.get(f) for f in fields if item.get(f)), None)
            if value:
                out.append(str(value))
    return out


def _digits10(phone: str) -> str:
    return "".join(c for c in str(phone) if c.isdigit())[-10:]


def score_pipl_person(known: KnownFacts, person: dict[str, Any]) -> Match:
    """Score one Pipl person (the ``person`` or one of ``possible_persons``)."""
    names = _values(person, "names", "display") or [""]
    best_name = max(names, key=lambda n: fuzz.token_set_ratio(name_key(n), name_key(known.name)))
    emails = [e.lower() for e in _values(person, "emails", "address")]
    usernames = _values(person, "usernames", "content")
    urls = _values(person, "urls", "url")
    match = Match(best_name, 0.0, "rejected", [], emails, usernames, urls, "Pipl")
    if not known.name or fuzz.token_set_ratio(name_key(best_name), name_key(known.name)) < NAME_MIN:
        match.reasons.append("name does not match")
        return match

    decisive = False
    known_phones = {_digits10(p) for p in known.phones if _digits10(p)}
    phones = [p for p in _values(person, "phones", "display_international", "display", "number")
              if _digits10(p) in known_phones]
    if phones:
        match.score += 0.6
        decisive = True
        match.reasons.append(f"same phone {phones[0]}")
    shared_email = sorted(set(emails) & {e.lower() for e in known.emails})
    if shared_email:
        match.score += 0.6
        decisive = True
        match.reasons.append(f"same email {shared_email[0]}")

    addresses = " ".join(_values(person, "addresses", "display")).lower()
    city = next((c for c in known.cities() if c in addresses), None)
    if city:
        match.score += 0.2
        match.reasons.append(f"same city ({city.title()})")
    areas = sorted(t for t in known.area_tokens() if t in addresses)
    if areas:
        match.score += 0.15
        match.reasons.append(f"same area ({', '.join(areas[:2])})")

    if known.father_name:
        related = [n for r in person.get("relationships") or [] for n in _values(r, "names", "display")]
        father = next((n for n in related if fuzz.token_set_ratio(name_key(n), name_key(known.father_name)) >= 88), None)
        if father:
            match.score += 0.35
            match.reasons.append(f"relative named {father} (father in records: {known.father_name})")

    jobs = " ".join(_values(person, "jobs", "display")).lower()
    org = next((o for o in known.organisations if o and fuzz.partial_ratio(o.lower(), jobs) >= 85), None) if jobs else None
    if org:
        match.score += 0.3
        match.reasons.append(f"works at {org}")

    # A common name needs more than a rare one before it counts as the subject.
    need = 0.45 + 0.2 * commonness(known.name)
    support = len(match.reasons)
    if decisive or (match.score >= need and support >= 2):
        match.status = "corroborated"
    elif support >= 1:
        match.status = "possible"
    match.score = min(1.0, match.score)
    return match


def match_pipl(known: KnownFacts, body: dict[str, Any] | None) -> list[Match]:
    """Every candidate in a Pipl answer, best first."""
    if not body:
        return []
    people = ([body["person"]] if body.get("person") else []) + list(body.get("possible_persons") or [])
    matches = [score_pipl_person(known, p) for p in people[:10]]
    order = {"corroborated": 0, "possible": 1, "rejected": 2}
    return sorted(matches, key=lambda m: (order[m.status], -m.score))
