"""Which source wins: the order of trust, and the conflicts on the board.

Tiers say how sure a claim is (fact, inference, speculation). Trust says who it comes
from. When two sources disagree, the answer leads with the more trusted one, shows the
other next to it and names both - nothing is deleted, and the officer decides. Sherlock,
the Answer checker, the Summarizer and the report all use this one order, so they never
pick different winners.

====  ======================  ==============================================
rank  source                  examples
====  ======================  ==============================================
1     official system record  NADRA, SIMs, Excise, PSRMS record, CDR rows
2     official document       FIR text, MLO / DNA report, CRO dossier, CDR server report
3     the officer's statement what the officer told us; a later statement replaces an earlier one
4     derived by us           CDR and graph inferences, Sherlock's assessment
5     unverified              OSINT, Caller ID, web search
====  ======================  ==============================================

Exceptions: for the incident's **time and place** and for **people's roles** the
officer's statement leads over the FIR - investigators often know more than the FIR
shows (the FIR is still shown next to it). Between two sources of the same rank the
newer leads; between two documents the one with an exact page leads. Trust never
upgrades a tier: an inference from CDR rows stays an inference.
"""

from __future__ import annotations

import re
from typing import Any

RANKS = {1: "official system record", 2: "official document", 3: "the officer's statement", 4: "derived by us",
         5: "unverified"}
OFFICER_LEADS = ("incident:date", "incident:time", "incident:place", "role")
_FIR_ROLE = {"nominated_suspects": "accused", "accused": "accused", "complainant": "complainant",
             "witnesses": "witness", "victims": "victim", "victim": "victim"}


def leader(values: list[dict[str, Any]], topic: str) -> list[dict[str, Any]]:
    """``values`` ({value, trust, source, at?, page?}) ordered: the one the answer leads
    with first."""
    officer_first = topic.startswith(OFFICER_LEADS)

    def key(v: dict[str, Any]) -> tuple:
        trust = int(v.get("trust") or 4)
        if officer_first and trust == 3:
            trust = 0
        return (trust, 0 if v.get("page") else 1, "".join(chr(255 - ord(c)) for c in str(v.get("at") or "")))

    return sorted(values, key=key)


def _day(text: str) -> str | None:
    from sherlocks.linkgraph.sherlock_team import _iso_date

    return _iso_date(text or "")


def _fir_label(text: str) -> str | None:
    m = re.search(r"(\d{1,5})\s*/\s*(\d{2,4})", text or "")
    return f"{m.group(1).lstrip('0')}/{m.group(2)[-2:]}" if m else None


def _side(role: str) -> str:
    """Which side of a case a role is on: a (main) suspect and the accused are one side."""
    role = (role or "").lower()
    if re.search(r"suspect|accus|mulzim|nominat", role):
        return "accused"
    if re.search(r"victim|complain|muddai|affected", role):
        return "victim"
    return role.split()[-1] if role else ""


def conflicts(case: Any, net: Any = None) -> list[dict[str, Any]]:
    """Places where sources on the board disagree: the incident's date (officer vs the
    incident's FIR), people's roles (officer vs the FIR roster). Each with the values,
    the one that leads, and what would settle it."""
    out: list[dict[str, Any]] = []
    inc = case.incident or {}
    stated = case.statement_for("incident:date")
    fir = _fir_label(str(inc.get("fir") or ""))
    if inc.get("date") and fir:
        for doc in case.documents.values():
            ref = doc.get("ref") or {}
            if doc["kind"] != "fir" or _fir_label(f"{ref.get('fir_no')}/{ref.get('fir_year')}") != fir:
                continue
            occurred = str((doc.get("data") or {}).get("occurred") or "")
            day = _day(occurred)
            if day and day != str(inc["date"])[:10]:
                values = [{"value": str(inc["date"])[:10], "trust": 3, "source": "officer",
                           "ref": stated["id"] if stated else None, "at": inc.get("at")},
                          {"value": day, "trust": 2, "source": doc["title"], "ref": doc["id"]}]
                out.append({"topic": "incident:date", "values": leader(values, "incident:date"),
                            "settle": f"Check the FIR's date of occurrence against the officer's account ({doc['id']})."})
    if net is not None:
        for pid, role in case.roles.items():
            if pid not in net.people:
                continue
            d = net.data(pid)
            ids = {x for x in [d.get("cnic"), *(d.get("phones") or [])] if x}
            for doc in case.documents.values():
                if doc["kind"] != "fir":
                    continue
                for p in doc.get("people") or []:
                    if ids & {p.get("cnic"), p.get("phone")} - {None}:
                        fir_role = _FIR_ROLE.get(str(p.get("role") or ""), str(p.get("role") or ""))
                        if fir_role and _side(role) != _side(fir_role):
                            values = [{"value": role, "trust": 3, "source": "officer"},
                                      {"value": fir_role, "trust": 2, "source": doc["title"], "ref": doc["id"]}]
                            out.append({"topic": f"role:{pid}", "person": net.name(pid),
                                        "values": leader(values, "role"),
                                        "settle": f"The FIR names {net.name(pid)} as {fir_role}; the officer says {role}."})
    return out


CONFLICT_LINE = {
    "en": "Note: {a} {says} {x}; {b} says {y}. Both are kept - {lead} leads.",
    "roman": "Note: {a} ke mutabiq {x}; {b} ke mutabiq {y}. Dono record mein hain - {lead} ko tarjeeh.",
    "ur": "نوٹ: {a} کے مطابق {x}؛ {b} کے مطابق {y}۔ دونوں ریکارڈ میں ہیں - {lead} کو ترجیح۔",
}


def conflict_lines(found: list[dict[str, Any]], language: str) -> list[str]:
    lines = []
    for c in found:
        first, second = c["values"][0], c["values"][1]

        def name(v: dict[str, Any]) -> str:
            who = "you" if v["source"] == "officer" and language == "en" else (
                "aap" if v["source"] == "officer" and language == "roman" else
                ("آپ" if v["source"] == "officer" else v["source"]))
            return who + (f" [{v['ref']}]" if v.get("ref") and v["source"] != "officer" else "")

        lines.append(CONFLICT_LINE.get(language, CONFLICT_LINE["en"]).format(
            a=name(first), says="say" if first["source"] == "officer" else "says", x=first["value"], b=name(second),
            y=second["value"], lead=name(first)))
    return lines
