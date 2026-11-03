"""P12 Group writer: when the officer searches several targets together - a gang, a group of accused, a
family - how the records tie the targets to each other, and the people and contacts to investigate.

Code only, from the link graph, for any kind of case (robbery, snatching, murder, fraud):

* **the targets** - each one's CNIC, father, phones, addresses and criminal record, side by side;
* **ties between the targets** - stated by a record (accused together, family, one filed against the
  other) or found by comparing records (the same phone, numbers in sequence - SIMs issued together - the
  same address or village);
* **possibly the same person** - a target whose name sounds the same as a person a record names without a
  CNIC (an FIR lists "Muhammad Shahid Nasir" with no CNIC): if it is them, they are tied to that record;
* **common connections** - people tied to two or more targets, directly or one step on;
* **groups** - the targets the ties join together, each with what joins them, and the targets nothing on
  record ties to the others;
* **contacts to investigate** - each target's direct contacts with their CNIC, phones and address.

It never says anyone took part in anything: it says how the records connect people.
"""

from __future__ import annotations

import re
from typing import Any

MOST_CONTACTS = 15          # per target
MOST_COMMON = 20
SEQUENCE = 5                # two numbers this close (same network, last digits) look issued together
_ALIAS = re.compile(r"\s+(?:عرف|urf|alias|a\.k\.a\.?)\b.*$", re.IGNORECASE)
# One step on only through someone in the targets' circle - never through the complainant or a witness of
# their FIR (the complainant's family is not the targets' contact).
_NOT_THROUGH = re.compile(r"complainant|witness|filed|victim|investigat", re.IGNORECASE)
_VILLAGE = re.compile(r"\b(chak|goth|village|basti|mouza|deh|killi)\s*(?:no\.?|number|#)?\s*(\d{1,4})\b", re.IGNORECASE)


def _digits(number: Any) -> str:
    return re.sub(r"\D", "", str(number or ""))[-10:]


def _clean(name: str) -> str:
    return _ALIAS.sub("", str(name or "")).strip(" .")


def _people_ok(net: Any, involved: set[str]) -> Any:
    from sherlocks.team.case_scope import is_officer
    from sherlocks.team.record_writer import _kind

    def ok(a: str, b: str) -> bool:
        """A person worth listing next to ``a``: not an officer there on duty, not tied only by investigating an FIR."""
        if b not in net.people:
            return False
        if b in involved:
            return True
        if is_officer(net, b):
            return False
        return not all(_kind(link.label or "", link.sentence or "", link.system or "") is None for link in net.G[a][b]["links"])
    return ok


def _labels(net: Any, a: str, b: str) -> list[str]:
    """Every distinct way the records tie two people, in plain words - "Both on <system>" only when nothing says more."""
    from sherlocks.team.record_writer import plainly

    out: list[str] = []
    for link in sorted(net.G[a][b]["links"], key=lambda link: link.cost):
        text = plainly(re.sub(r"\s*[—-]\s*inferred(?: \d\.\d+)?", "", link.label or link.sentence or ""))
        if text and text.lower() not in {x.lower() for x in out}:
            out.append(text)
    real = [x for x in out if not x.lower().startswith("both on ")]
    out = real or out
    # "Accused in FIR 99/25" says nothing "Co-accused in FIR 99/25" does not.
    out = [x for x in out if not (re.match(r"accused in ", x, re.IGNORECASE)
                                  and any(y.lower() == "co-" + x.lower() for y in out))]
    return out[:4]


def _villages(addresses: list[str]) -> set[str]:
    return {f"{m.group(1).lower()} {m.group(2)}" for a in addresses for m in _VILLAGE.finditer(str(a))}


def _same_place(a: list[str], b: list[str]) -> str:
    """How two people's addresses are the same place, or ""."""
    from sherlocks.linkgraph.normalize import parse_address
    from sherlocks.linkgraph.weak_links import address_score

    for x in a:
        for y in b:
            score, why = address_score(x, y, parse_address(x), parse_address(y))
            if score >= 0.5:
                return f"{why.split(' (')[0].lower()}: {x} / {y}"
    shared = _villages(a) & _villages(b)
    if shared:
        return f"the same village on both addresses ({', '.join(sorted(shared))})"
    return ""


def _contact(net: Any, pid: str, how: str, stated: bool, others: list[str]) -> dict[str, Any]:
    from sherlocks.linkgraph.normalize import dashed_cnic
    from sherlocks.team.network_view import record_of

    d = net.data(pid)
    return {"pid": pid, "name": net.name(pid), "cnic": dashed_cnic(d.get("cnic")) or "-",
            "phones": [str(p) for p in (d.get("phones") or [])][:3], "address": str((d.get("addresses") or [""])[0])[:90],
            "how": how, "stated": stated, "record": record_of(net, pid), "criminal": bool(net.criminal(pid)),
            "also_tied_to": others}


def build(case: Any, net: Any, scope: dict[str, Any]) -> dict[str, Any] | None:
    """The targets side by side and how they connect - ``None`` when there are fewer than two targets."""
    from sherlocks.linkgraph.normalize import dashed_cnic, sounds_like
    from sherlocks.team.case_scope import _NOT_CIRCLE, father_of
    from sherlocks.team.network_view import record_of

    targets = [p for p in scope.get("targets") or [] if p in net.G]
    if len(targets) < 2:
        return None
    tset = set(targets)
    involved = set(scope.get("pids") or [])
    ok = _people_ok(net, involved)
    name = {t: _clean(net.name(t)) or net.name(t) for t in targets}

    # Ties between two targets: pair -> reasons, and whether a record states one of them.
    ties: dict[tuple[str, str], dict[str, Any]] = {}

    def tie(a: str, b: str, how: str, kind: str) -> None:
        key = tuple(sorted((a, b)))
        row = ties.setdefault(key, {"a": name[key[0]], "b": name[key[1]], "how": [], "kinds": []})
        if how not in row["how"]:
            row["how"].append(how)
        if kind not in row["kinds"]:
            row["kinds"].append(kind)

    for i, a in enumerate(targets):
        for b in targets[i + 1:]:
            if net.G.has_edge(a, b):
                stated = bool(net.G[a][b].get("stated"))
                for how in _labels(net, a, b):
                    tie(a, b, how, "stated" if stated else "matched")
                father = father_of(net, a, b) or father_of(net, b, a)
                if father:
                    tie(a, b, father, "matched")
            pa = {_digits(p): p for p in net.data(a).get("phones") or [] if len(_digits(p)) == 10}
            pb = {_digits(p): p for p in net.data(b).get("phones") or [] if len(_digits(p)) == 10}
            for x in sorted(pa.keys() & pb.keys()):
                tie(a, b, f"the same phone {pa[x]} on both records", "matched")
            for x, shown_a in pa.items():
                for y, shown_b in pb.items():
                    if x != y and x[:7] == y[:7] and abs(int(x) - int(y)) <= SEQUENCE:
                        tie(a, b, f"phone numbers in sequence: {shown_a} and {shown_b} - SIMs often bought or issued "
                            "together", "matched")
            place = _same_place(net.data(a).get("addresses") or [], net.data(b).get("addresses") or [])
            if place:
                row = ties.get(tuple(sorted((a, b))))
                if row:                              # the addresses themselves, not the bare "Same locality"
                    row["how"] = [h for h in row["how"] if not re.fullmatch(r"same (?:locality|address)", h, re.IGNORECASE)]
                tie(a, b, place, "matched")

    # Possibly the same person: a target's name, sounding the same as a person a record names without a CNIC
    # (or with the same CNIC) - and that person tied to another target.
    same: list[dict[str, Any]] = []
    for t in targets:
        mine = [_clean(n) for n in net.all_names(t) if len(_clean(n).split()) >= 2]
        cnic = re.sub(r"\D", "", str(net.data(t).get("cnic") or ""))
        for p in net.people:
            if p in tset or p not in net.G:
                continue
            theirs = re.sub(r"\D", "", str(net.data(p).get("cnic") or ""))
            if theirs and theirs != cnic:
                continue
            names = [_clean(n) for n in net.all_names(p) if len(_clean(n).split()) >= 2]
            if not any(sounds_like(x, y) and sounds_like(y, x) for x in mine for y in names):
                continue
            joined = [o for o in net.G.neighbors(p) if o in tset and o != t]
            if not joined:
                continue
            where = sorted({how for o in joined for how in _labels(net, o, p)})[:3]
            same.append({"target": name[t], "target_cnic": dashed_cnic(cnic) or "-", "name_in_record": net.name(p),
                         "cnic_in_record": dashed_cnic(theirs) or "none",
                         "tied_to": [name[o] for o in joined], "where": where})
            for o in joined:
                tie(t, o, f"if {net.name(p)} on the record is {name[t]}: " + "; ".join(where[:2]), "same person?")

    # Common connections: tied to two or more targets - directly, or one step on.
    reach: dict[str, dict[str, Any]] = {}
    for t in targets:
        for o in net.G.neighbors(t):
            if o in tset or not ok(t, o):
                continue
            r = reach.setdefault(o, {"direct": {}, "through": {}})
            r["direct"][t] = _labels(net, t, o)[0] if _labels(net, t, o) else "-"
    complainants = {o for t in targets for o in net.G.neighbors(t)
                    if o not in tset and any(_NOT_THROUGH.search(h) for h in _labels(net, t, o))}
    for t in targets:
        for f in [o for o in net.G.neighbors(t) if o not in tset and o not in complainants and ok(t, o)]:
            for o in net.G.neighbors(f):
                if o in tset or o == f or not ok(f, o) or t in (reach.get(o) or {}).get("direct", {}):
                    continue
                # One step on through a SIM owner, a tenancy witness or a guarantor is no tie.
                if all(_NOT_CIRCLE.search(f"{link.label or ''} {link.sentence or ''}") for link in net.G[f][o]["links"]):
                    continue
                r = reach.setdefault(o, {"direct": {}, "through": {}})
                r["through"].setdefault(t, net.name(f))
    common = []
    for o, r in reach.items():
        tied = {**{t: f"through {via}" for t, via in r["through"].items()}, **r["direct"]}
        if len(tied) < 2:
            continue
        row = _contact(net, o, "", True, [])
        row["tied_to"] = [{"target": name[t], "how": how} for t, how in tied.items()]
        row["direct"] = len(r["direct"])
        common.append(row)
        if len(r["direct"]) >= 2:
            ts = list(r["direct"])
            for i, a in enumerate(ts):
                for b in ts[i + 1:]:
                    tie(a, b, f"the same complainant against both: {net.name(o)}" if o in complainants
                        else f"both tied to {net.name(o)}", "common contact")
    common.sort(key=lambda r: (-r["direct"], -len(r["tied_to"]), not r["criminal"]))

    # Groups: the targets the ties join.
    parent = {t: t for t in targets}

    def root(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_name = {v: k for k, v in name.items()}
    for row in ties.values():
        if set(row["kinds"]) - {"same person?"}:       # a sound-alike name alone joins no group: it is a question
            parent[root(by_name[row["a"]])] = root(by_name[row["b"]])
    groups: dict[str, list[str]] = {}
    for t in targets:
        groups.setdefault(root(t), []).append(t)
    joined = []
    for members in sorted(groups.values(), key=len, reverse=True):
        if len(members) < 2:
            continue
        names_ = {name[m] for m in members}
        why = []
        for row in ties.values():
            if row["a"] in names_ and set(row["kinds"]) - {"same person?"}:
                for how in row["how"]:
                    short = re.sub(r"\s*\(charges:.*?\)", "", how)
                    if short not in why and not short.startswith("if "):
                        why.append(short)
        joined.append({"members": [name[m] for m in members], "why": why[:6]})
    alone = [name[t] for t in targets if len(groups[root(t)]) < 2]

    # Each target, and the contacts to investigate.
    rows, contacts = [], []
    for t in targets:
        d = net.data(t)
        mine = []
        for o in net.G.neighbors(t):
            if o in tset or not ok(t, o):
                continue
            labels = _labels(net, t, o)
            others = [name[x] for x in targets if x != t and net.G.has_edge(x, o)]
            mine.append(_contact(net, o, father_of(net, t, o) or (labels[0] if labels else "-"),
                                 bool(net.G[t][o].get("stated")), others))
        weak = re.compile(_NOT_CIRCLE.pattern, re.IGNORECASE)
        mine.sort(key=lambda r: (not r["also_tied_to"], not r["criminal"], bool(weak.search(r["how"])), not r["stated"]))
        contacts.append({"target": name[t], "rows": mine[:MOST_CONTACTS], "more": max(0, len(mine) - MOST_CONTACTS)})
        rows.append({"pid": t, "name": name[t], "cnic": dashed_cnic(d.get("cnic")) or "-", "father": d.get("father_name") or "-",
                     "phones": [str(p) for p in d.get("phones") or []], "addresses": [str(a) for a in d.get("addresses") or []][:3],
                     "record": record_of(net, t) or "-", "contacts": len(mine),
                     "group": next((i + 1 for i, g in enumerate(joined) if name[t] in g["members"]), None)})
    return {"targets": rows, "ties": sorted(ties.values(), key=lambda r: ("stated" not in r["kinds"], r["a"], r["b"])),
            "same_person": same, "common": common[:MOST_COMMON], "groups": joined, "alone": alone, "contacts": contacts,
            "bridges": _bridges(same, joined, alone)}


def _bridges(same: list[dict[str, Any]], groups: list[dict[str, Any]], alone: list[str]) -> list[str]:
    """What each "possibly the same person" would join, if confirmed: a lone target to a group, or two groups."""
    def where(n: str) -> str:
        i = next((i for i, g in enumerate(groups) if n in g["members"]), None)
        return f"group {i + 1}" if i is not None else n

    out = []
    for s in same:
        a, others = where(s["target"]), sorted({where(o) for o in s["tied_to"]} - {where(s["target"])})
        if others:
            out.append(f"If {s['name_in_record']} on the record is target {s['target']}, "
                       f"{' and '.join(sorted({a, *others}))} are one group.")
    return out


def lines(group: dict[str, Any] | None, ref: str | None = None) -> list[str]:
    """The group in a few plain lines, for the brief writer and the weighing - each ending with ``ref`` (the board's
    link-analysis document), so a point made from them cites the records, never the officer."""
    if not group:
        return []
    out = ["From the police and telecom records of the search, not the officer's words:"]
    out += [f"Group {i + 1}: {', '.join(g['members'])} - " + "; ".join(g["why"][:4]) for i, g in enumerate(group["groups"])]
    if group["alone"]:
        out.append(f"No tie on record to the other targets: {', '.join(group['alone'])}")
    out += group.get("bridges") or []
    out += [f"A record ({'; '.join(s['where'][:2])}, with {', '.join(s['tied_to'])}) names {s['name_in_record']} and gives no "
            f"CNIC for that person; the name sounds the same as target {s['target']} (CNIC {s['target_cnic']}) - confirm "
            "from that record's particulars (father's name, address, photo) whether it is the target"
            if s["cnic_in_record"] == "none" else
            f"A record names {s['name_in_record']} with the target's own CNIC: it is target {s['target']}"
            for s in group["same_person"]]
    out += [f"{c['name']} is tied to " + "; ".join(f"{x['target']} ({x['how']})" for x in c["tied_to"]) for c in group["common"][:8]]
    return [out[0], *(f"{x} [{ref}]" for x in out[1:])] if ref else out
