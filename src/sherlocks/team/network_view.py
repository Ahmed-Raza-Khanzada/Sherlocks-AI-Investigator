"""P9 Network writer: the target's network, up to two steps, for the report.

The report's people section listed only the case's own records; the officer also needs
the picture around the target: everyone the records tie to them directly, and - one step
further - the people who matter. Code only, from the link graph:

* **first step** - every person directly connected to a target, with how the records
  connect them (stated by a record, or matched by comparing records), whether they are
  involved in this case and whether they have a criminal record. A police officer appears
  only when involved in the case (an investigating officer is on the FIR, not the circle).
* **second step** - people reached only through someone at the first step, kept only when
  they matter: involved in this case, with a criminal record, or tied to two or more of the
  first-step people. Each says through whom, and how.

The rest are counted, not drawn: a two-step picture of a busy graph is unreadable.
"""

from __future__ import annotations

import re
from typing import Any

MOST_FIRST = 30
MOST_SECOND = 24


def _how(net: Any, a: str, b: str) -> tuple[str, bool]:
    """How the records connect two people - the best link, in plain words - and whether a record states it."""
    from sherlocks.team.record_writer import plainly

    link = net.best_link(a, b)
    return plainly(link.sentence or link.label or ""), bool(net.G[a][b].get("stated"))


SHARED_HOLDERS = 4          # a number on this many people's records is a shared number, not a tie between them
_BY_NUMBER = re.compile(r"same identifier|same phone|same (?:phone/)?cnic|registered with the same|shares? (?:a )?phone|"
                        r"same number|registered owner of sim|sim registered", re.IGNORECASE)


def record_of(net: Any, pid: str) -> str:
    """Their criminal record in a line: each FIR they are accused in, with its offence - the serious ones marked."""
    from sherlocks.linkgraph.case_queries import role_kind
    from sherlocks.linkgraph.offences import classify, labels, severity

    rows = []
    for f in net.data(pid).get("firs") or []:
        if role_kind([str(f.get("role") or "")]) != "accused" or not f.get("label"):
            continue
        crimes = classify(str(f.get("offence") or ""))
        rows.append((severity(crimes), f"FIR {f['label']}" + (f" - {labels(crimes) or f.get('offence')}" if f.get("offence") else "")
                     + (" (serious)" if severity(crimes) >= 3 else "")))
    rows.sort(key=lambda r: -r[0])
    if not rows:
        # Flagged by another register (stolen vehicle, watch list): say which, in words.
        from sherlocks.linkgraph.dossier import criminal_flags

        flags = [f.replace("_", " ") for f in criminal_flags(net.data(pid)) if f != "accused_in_fir"]
        return f"on record: {', '.join(flags)}" if flags else ""
    return "; ".join(r[1] for r in rows[:4]) + (f"; and {len(rows) - 4} more" if len(rows) > 4 else "")


def shared_numbers(net: Any, pid: str) -> dict[str, list[str]]:
    """The target's numbers that are on the records of SHARED_HOLDERS or more people: number -> the others."""
    out = {}
    for number in net.data(pid).get("phones") or []:
        holders = [p for p in net.people if p != pid and number in (net.data(p).get("phones") or [])]
        if len(holders) + 1 >= SHARED_HOLDERS:
            out[number] = holders
    return out


def named_in(net: Any, pid: str, named: dict[str, str]) -> str:
    """The part the case's records give this person (their name as the records spell it, in either script, or a
    spelling that sounds the same: Maryam / Mariam / Marium), or ""."""
    from sherlocks.linkgraph.normalize import name_key, sounds_like

    for n in net.all_names(pid):
        for name, part in named.items():
            if name_key(n) == name_key(name) or (len(name.split()) >= 2 and sounds_like(n, name) and sounds_like(name, n)):
                return part or "named in this case"
    return ""


def build(case: Any, net: Any, scope: dict[str, Any], named: dict[str, str] | None = None) -> dict[str, Any]:
    """``named``: the people the case's records name (the story's people, the uploaded documents') - name -> part."""
    from sherlocks.team.case_scope import _NOT_CIRCLE, father_of, is_officer

    targets = [p for p in scope.get("targets") or [] if p in net.G]
    involved = set(scope.get("pids") or [])
    named_as = {p["pid"]: p["role"] for p in scope.get("people") or [] if p.get("pid")}
    for t in targets:
        for other in net.G.neighbors(t):
            part = named_in(net, other, named or {}) if other in net.people and other not in targets else ""
            if part:
                involved.add(other)             # named by the case's records: never folded into a shared number
                named_as.setdefault(other, part)

    def part(pid: str) -> str:
        return case.roles.get(pid) or named_as.get(pid) or ("in this case" if pid in involved else "")

    def officer(pid: str) -> bool:
        return is_officer(net, pid)

    def only_investigated(a: str, b: str) -> bool:
        """Every tie between the two is an officer investigating an FIR - the same rule as the record writer:
        an investigating officer is on the FIR, not in anyone's circle."""
        from sherlocks.team.record_writer import _kind

        return all(_kind(link.label or "", link.sentence or "", link.system or "") is None for link in net.G[a][b]["links"])

    first: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[str]] = {}
    for t in targets:
        shared = shared_numbers(net, t)
        for other in net.G.neighbors(t):
            if other in targets or other not in net.people or (
                    (officer(other) or only_investigated(t, other)) and other not in involved):
                continue
            number = next((n for n, holders in shared.items() if other in holders), None)
            if number and other not in involved and all(_BY_NUMBER.search(f"{link.label or ''} {link.sentence or ''}")
                                                         for link in net.G[t][other]["links"]):
                # Tied only by a number on many people's records (a family, shop or agent's number written on
                # their forms): one entry for all of them, and nobody is reached through them.
                grouped.setdefault(number, []).append(net.name(other))
                continue
            how, stated = _how(net, t, other)
            how = father_of(net, t, other) or how
            row = first.setdefault(other, {"pid": other, "name": net.name(other), "to": [], "how": how, "stated": stated,
                                           "involved": other in involved, "part": part(other),
                                           "criminal": bool(net.criminal(other)), "record": record_of(net, other), "links": 0})
            row["to"].append(net.name(t))
            row["stated"] = row["stated"] or stated
            row["links"] += len(net.G[t][other]["links"])
    firsts = sorted(first.values(), key=lambda r: (not r["involved"], not r["stated"], not r["criminal"], -r["links"]))
    shown = firsts[:MOST_FIRST]
    second: dict[str, dict[str, Any]] = {}
    in_group = {n for names in grouped.values() for n in names}
    hub = {n for t in targets for n in shared_numbers(net, t)}
    for f in shown:
        for other in net.G.neighbors(f["pid"]):
            if other in targets or other in first or other not in net.people or net.name(other) in in_group or (
                    (officer(other) or only_investigated(f["pid"], other)) and other not in involved):
                continue
            if hub & set(net.data(other).get("phones") or []) and other not in involved and all(
                    _BY_NUMBER.search(f"{link.label or ''} {link.sentence or ''}") for link in net.G[f["pid"]][other]["links"]):
                continue            # one step on through the same shared number is no tie either
            # A tenant on someone's tenancy form, a SIM owner, a guarantor: counted, never worth a step on.
            weak = other not in involved and not net.criminal(other) and all(
                _NOT_CIRCLE.search(f"{link.label or ''} {link.sentence or ''}") for link in net.G[f["pid"]][other]["links"])
            how, stated = _how(net, f["pid"], other)
            row = second.setdefault(other, {"pid": other, "name": net.name(other), "through": [], "how": how,
                                            "stated": stated, "involved": other in involved, "part": part(other),
                                            "criminal": bool(net.criminal(other)), "record": record_of(net, other)})
            if f["name"] not in row["through"]:
                row["through"].append(f["name"])
            row["weak"] = row.get("weak", True) and weak
    matter = [r for r in second.values() if r["involved"] or r["criminal"] or (len(r["through"]) >= 2 and not r["weak"])]
    seconds = sorted(matter, key=lambda r: (not r["involved"], not r["criminal"], -len(r["through"]), not r["stated"]))
    kept = {r["pid"] for r in shown} | {r["pid"] for r in seconds[:MOST_SECOND]}
    by_name = {**{r["name"]: r["pid"] for r in shown}}
    edges = [{"a": t, "b": r["pid"], "stated": net.G[t][r["pid"]].get("stated", False)}
             for r in shown for t in targets if net.G.has_edge(t, r["pid"])]
    edges += [{"a": by_name[f], "b": r["pid"], "stated": bool(net.G[by_name[f]][r["pid"]].get("stated"))}
              for r in seconds[:MOST_SECOND] for f in r["through"] if f in by_name and net.G.has_edge(by_name[f], r["pid"])]
    return {"targets": [{"pid": t, "name": net.name(t)} for t in targets], "first": shown,
            "shared_numbers": [{"number": n, "people": names, "holders": len(shared_numbers(net, targets[0]).get(n) or []) + 1,
                                "elsewhere": len(shared_numbers(net, targets[0]).get(n) or []) - len(names)}
                               for n, names in grouped.items()],
            "second": seconds[:MOST_SECOND], "edges": [e for e in edges if e["b"] in kept],
            "not_shown": {"first": max(0, len(firsts) - len(shown)), "second": max(0, len(second) - len(seconds[:MOST_SECOND]))}}
