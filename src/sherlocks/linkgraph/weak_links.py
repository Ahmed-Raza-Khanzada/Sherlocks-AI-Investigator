"""Weak links: relationships inferred, not stated.

Strong links are what a system says ("Tariq is Kamran's landlord"). Weak links are
what the data *suggests*: two addresses that are the same house spelled differently,
two men with the same father's name and surname, two hotel stays that overlap. They
are leads for an analyst, drawn dashed, scored, and always carry the reasons.

Two layers:

1. **Rules** - deterministic, cheap, run on every pair. Combined by noisy-OR so two
   moderate signals add up without ever exceeding 1 - but only the strongest signal of
   each *kind* counts (same address and neighbour are one fact, not two), a match on a
   common name counts for less than one on a rare name (``rarity.py``), and a value
   shared by a crowd (an employer of hundreds, a busy police station) barely counts.
2. **AI** - the LLM does the two things rules do badly, and nothing else:
   * split a free-text Pakistani address into house / street / block / area / city
     (kept only where every value appears in the original text), and
   * review *candidate* pairs the rules could not settle - people who share a surname,
     an area, an employer - and say whether the records suggest a connection.
     A judgement survives only if it quotes evidence that is really in the two
     profiles, and on its own it can never score above :data:`AI_PAIR_CAP`.

The model may be large now, but it still sees only what the records say, and it can
still be wrong. Neither layer ever merges two identities.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from sherlocks.linkgraph.graph import GraphBuilder
from sherlocks.linkgraph.normalize import (
    PK_CITIES,
    name_key,
    normalize_address,
    parse_address,
    surname,
)
from sherlocks.linkgraph.rarity import rarity_factor

logger = logging.getLogger(__name__)

AddressParser = Callable[[str], dict[str, str | None] | None]
Signal = tuple[float, str, str]

# An AI judgement alone is a lead to look at, not more. It can reach this, never above.
AI_PAIR_CAP = 0.6
# An employer or police station shared by more people than this in one graph is a crowd,
# not a connection: its signal is cut to HUB_SIGNAL.
HUB_SHARE = 5
HUB_SIGNAL = 0.12

# Which kind of fact each signal label reports. Only the best signal per kind counts.
_SIGNAL_KIND = {
    "Same address": "address", "Neighbour": "address", "Nearby address": "address",
    "Possible siblings": "family", "Possible parent / child": "family",
    "Co-stay": "stay", "Same employer": "employer", "Police colleagues": "police", "OSINT": "osint",
}


# --------------------------------------------------------------------------------------
# Address comparison
# --------------------------------------------------------------------------------------


class _AddressParts(BaseModel):
    house: str = ""
    street: str = ""
    block: str = ""
    sector: str = ""
    area: str = ""
    city: str = ""


_ADDRESS_SYSTEM = (
    "You split Pakistani street addresses into parts. Copy values exactly as they appear "
    "in the address - never translate, expand, guess or add anything. Use an empty string "
    "for any part that is not present. 'Khi' is Karachi, 'Hyd' is Hyderabad."
)


class LlmAddressParser:
    """LLM address splitter with a substring-consistency guard and a call budget."""

    def __init__(self, llm: Any, *, budget: int) -> None:
        self.llm = llm
        self.budget = budget
        self.calls = 0
        self._memo: dict[str, dict[str, str | None] | None] = {}

    def __call__(self, address: str) -> dict[str, str | None] | None:
        if address in self._memo:
            return self._memo[address]
        if self.calls >= self.budget:
            return None
        self.calls += 1
        try:
            parts, _ = self.llm.generate_structured(
                prompt=f"Address: {address}\nReturn the parts as JSON.", schema=_AddressParts,
                system=_ADDRESS_SYSTEM, cache_kind="address_parts", prompt_version="v1",
            )
        except Exception as exc:  # noqa: BLE001 - any model failure means "rules only", never a failed run
            logger.info("LLM address parse unavailable (%s); rules only", exc)
            self.budget = 0
            self._memo[address] = None
            return None
        self._memo[address] = _validated(address, parts)
        return self._memo[address]


def _validated(address: str, parts: _AddressParts) -> dict[str, str | None]:
    haystack = normalize_address(address)
    out: dict[str, str | None] = {}
    for key, value in parts.model_dump().items():
        norm = normalize_address(value)
        if key == "city":
            out[key] = norm if norm in PK_CITIES and norm in haystack else None
        elif norm and all(token in haystack for token in norm.split()):
            out[key] = norm.replace(" ", "") if key != "area" else norm
        else:
            out[key] = None
    return out


def _parts(address: str, cache: dict[str, dict], ai: AddressParser | None, want_ai: bool) -> dict:
    if address not in cache:
        cache[address] = parse_address(address)
    parts = cache[address]
    if want_ai and ai and not parts.get("_ai"):
        refined = ai(address)
        if refined:
            merged = dict(parts)
            for key in ("house", "street", "block", "sector", "city"):
                merged[key] = merged.get(key) or refined.get(key)
            if refined.get("area"):
                merged["area_tokens"] = refined["area"]
            merged["_ai"] = True
            cache[address] = parts = merged
    return parts


def address_score(a: str, b: str, pa: dict, pb: dict) -> tuple[float, str]:
    na, nb = normalize_address(a), normalize_address(b)
    if not na or not nb:
        return 0.0, ""
    if pa.get("city") and pb.get("city") and pa["city"] != pb["city"]:
        return 0.0, ""

    def differs(key: str) -> bool:
        return bool(pa.get(key) and pb.get(key) and pa[key] != pb[key])

    # Block 7 and Block 13-D are different places however similar the rest reads.
    if differs("block") or differs("sector"):
        return 0.0, ""
    area = fuzz.token_set_ratio(pa.get("area_tokens") or "", pb.get("area_tokens") or "") if (
        pa.get("area_tokens") and pb.get("area_tokens")) else 0
    same_house = bool(pa.get("house") and pa.get("house") == pb.get("house"))
    same_street = bool(pa.get("street") and pa.get("street") == pb.get("street"))
    same_block = bool(pa.get("block") and pa.get("block") == pb.get("block"))
    if same_house and (same_street or same_block) and area >= 60:
        return 0.85, "Same house, street/block and area"
    if not differs("house") and not differs("street"):
        if na == nb or fuzz.ratio(na, nb) >= 93:
            return 0.8, "Same address text"
        small, big = sorted((set(na.split()), set(nb.split())), key=len)
        # Every token of the shorter one, numbers included, is in the longer: "Flat 5,
        # House 45, Block 13-D" inside "House 45, Block 13-D". Needs a number to anchor it.
        if small <= big and len(small) >= 4 and any(t.isdigit() for t in small) and (same_house or same_block):
            return 0.6, "One address contains the other (same premises)"
        if same_house and area >= 75:
            return 0.6, "Same house number in the same area"
    # Not the same premises, but the same street or block: neighbours. Worth a lead -
    # people who live next door turn up in each other's records for real reasons.
    if same_street and differs("house") and (same_block or area >= 70):
        return 0.45, "Neighbours — same street, different house"
    if same_block and area >= 80:
        return 0.3, "Neighbours — same block"
    return 0.0, ""


# --------------------------------------------------------------------------------------
# Other rules
# --------------------------------------------------------------------------------------


def _parse_time(value: object) -> datetime | None:
    """Hotel check-in times are local wall-clock times with no zone; compared naive."""
    text = str(value or "").strip().replace("Z", "")
    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt, width in (("%d-%m-%Y %H:%M", 16), ("%d/%m/%Y %H:%M", 16), ("%d-%m-%Y", 10), ("%d/%m/%Y", 10)):
        try:
            return datetime.strptime(text[:width], fmt)  # noqa: DTZ007 - zone-less by design, see docstring
        except ValueError:
            continue
    return None


def _stay_overlap(x: dict, y: dict) -> tuple[float, str] | None:
    if fuzz.token_set_ratio(name_key(x.get("hotel")), name_key(y.get("hotel"))) < 90:
        return None
    if x.get("district") and y.get("district") and name_key(x["district"]) != name_key(y["district"]):
        return None
    xi, xo, yi, yo = (_parse_time(x.get("check_in")), _parse_time(x.get("check_out")),
                      _parse_time(y.get("check_in")), _parse_time(y.get("check_out")))
    if not (xi and yi):
        return None
    xo = xo or xi + timedelta(days=1)
    yo = yo or yi + timedelta(days=1)
    where = f"{x.get('hotel')} ({x.get('district') or '-'})"
    if xi <= yo and yi <= xo:
        return 0.7, f"Overlapping stays at {where}: {xi:%d %b %Y} and {yi:%d %b %Y}"
    if abs((xi - yi).days) <= 3:
        return 0.3, f"Stays at {where} within 3 days of each other"
    return None


def _same_org(a: str, b: str) -> bool:
    ka, kb = name_key(a), name_key(b)
    return bool(ka and kb) and (ka == kb or fuzz.token_sort_ratio(ka, kb) >= 92)


def _family(x: dict, y: dict, address: float) -> list[Signal]:
    out: list[Signal] = []
    fx, fy = name_key(x.get("father_name")), name_key(y.get("father_name"))
    nx, ny = name_key(x.get("name")), name_key(y.get("name"))
    same_surname = bool(surname(x.get("name")) and surname(x.get("name")) == surname(y.get("name")))
    if fx and fy and fuzz.token_sort_ratio(fx, fy) >= 92 and (same_surname or address >= 0.3):
        rarity = rarity_factor(x.get("father_name"))
        note = "" if rarity >= 0.9 else f" - a common name, weighted ×{rarity:.2f}"
        out.append((round((0.45 + (0.2 if address >= 0.6 else 0)) * rarity, 3), "Possible siblings",
                    f"Same father's name ({x.get('father_name')})" + (" and surname" if same_surname else "") + note))
    for child, parent, child_name, parent_raw in ((fx, ny, x.get("name"), y.get("name")),
                                                  (fy, nx, y.get("name"), x.get("name"))):
        if child and parent and fuzz.token_sort_ratio(child, parent) >= 92 and address >= 0.3:
            rarity = rarity_factor(parent_raw)
            out.append((round(0.5 * rarity, 3), "Possible parent / child",
                        f"{child_name}'s father's name matches the other person"
                        + ("" if rarity >= 0.9 else f" - a common name, weighted ×{rarity:.2f}")))
    return out


def _rule_signals(dx: dict, dy: dict, parts_cache: dict, ai: AddressParser | None,
                  crowd: dict[str, Counter[str]] | None = None) -> tuple[list[Signal], bool]:
    crowd = crowd or {"org": Counter(), "ps": Counter()}
    signals: list[Signal] = []
    best_address, best_reason, used_ai = 0.0, "", False
    for a, b in itertools.product(dx["addresses"][:6], dy["addresses"][:6]):
        pa, pb = _parts(a, parts_cache, ai, False), _parts(b, parts_cache, ai, False)
        score, reason = address_score(a, b, pa, pb)
        if score < 0.6 and ai and 55 <= fuzz.token_set_ratio(normalize_address(a), normalize_address(b)) < 93:
            pa, pb = _parts(a, parts_cache, ai, True), _parts(b, parts_cache, ai, True)
            ai_score, ai_reason = address_score(a, b, pa, pb)
            if ai_score > score:
                score, reason, used_ai = ai_score, ai_reason + " (AI-normalised)", True
        if score > best_address:
            best_address, best_reason = score, f"{reason}: “{a}” ~ “{b}”"
    if best_address:
        if "Neighbour" in best_reason:
            label = "Neighbour"
        elif best_address >= 0.6:
            label = "Same address"
        else:
            label = "Nearby address"
        signals.append((best_address, label, best_reason))

    signals += _family(dx, dy, best_address)

    for sx, sy in itertools.product(dx["stays"], dy["stays"]):
        if hit := _stay_overlap(sx, sy):
            signals.append((hit[0], "Co-stay", hit[1]))
            break

    for oa, ob in itertools.product(dx["organisations"], dy["organisations"]):
        if _same_org(oa, ob):
            size = crowd["org"][name_key(oa)]
            if size > HUB_SHARE:
                signals.append((HUB_SIGNAL, "Same employer", f"Both linked to “{oa}” - but so are {size} people in this graph"))
            else:
                signals.append((0.4, "Same employer", f"Both linked to organisation “{oa}”"))
            break

    for ps in set(map(name_key, dx["police_stations"])) & set(map(name_key, dy["police_stations"])):
        size = crowd["ps"][ps]
        if size > HUB_SHARE:
            signals.append((HUB_SIGNAL, "Police colleagues", f"Both posted at {ps.title()} - with {size - 2} others in this graph"))
        else:
            signals.append((0.3, "Police colleagues", f"Both posted at {ps.title()}"))
        break

    osint_x = {(i.get("kind"), str(i.get("value")).lower()) for i in dx.get("osint", [])}
    osint_y = {(i.get("kind"), str(i.get("value")).lower()) for i in dy.get("osint", [])}
    for kind, value in sorted(osint_x & osint_y)[:3]:
        signals.append((0.35, "OSINT", f"Both linked online to {kind} {value} (unverified)"))
    for person, other in ((dx, dy), (dy, dx)):
        for item in person.get("osint", []):
            if item.get("kind") == "phone" and any(p[-10:] in str(item.get("value")).replace(" ", "") for p in other["phones"]):
                signals.append((0.4, "OSINT", f"OSINT result for {person.get('name')} mentions a number of {other.get('name')} (unverified)"))
    return signals, used_ai


def _noisy_or(signals: list[Signal]) -> float:
    remaining = 1.0
    for score, _, _ in signals:
        remaining *= 1 - score
    return 1 - remaining


def _combine(signals: list[Signal]) -> float:
    """Noisy-OR over the best signal of each kind. Two signals of one kind are usually
    one fact seen twice (a house number and a street on the same address), so letting
    both count would inflate the score."""
    best: dict[str, Signal] = {}
    for signal in signals:
        kind = _SIGNAL_KIND.get(signal[1], signal[1])
        if kind not in best or signal[0] > best[kind][0]:
            best[kind] = signal
    return _noisy_or(list(best.values()))


# --------------------------------------------------------------------------------------
# AI pair review
# --------------------------------------------------------------------------------------


class _PairJudgement(BaseModel):
    relation: Literal["same_address", "same_household", "family", "same_workplace", "possibly_same_person", "none"]
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list, description="1-3 short fragments copied exactly from the profiles")
    explanation: str = Field(default="", description="One sentence.")


_PAIR_SYSTEM = (
    "You assist a police analyst. You are shown what official records say about two people, "
    "A and B. Decide whether the records themselves suggest a real-world connection: the same "
    "address written differently, the same household, a family tie (same father, parent and "
    "child, surname plus address), the same workplace, or that A and B may be one person "
    "recorded twice. Use only the text given. Copy 1-3 short evidence fragments exactly as they "
    "appear. A common name, a shared city, or a shared large area on its own is NOT a "
    "connection - answer none. Prefer none when unsure."
)
_AI_LABELS = {
    "same_address": "AI: same address",
    "same_household": "AI: same household",
    "family": "AI: possible family",
    "same_workplace": "AI: same workplace",
    "possibly_same_person": "AI: possibly same person",
}
_WORD = re.compile(r"[^\w]+")


def _flat(text: str) -> str:
    return _WORD.sub(" ", text.lower()).strip()


def _profile(d: dict) -> dict[str, Any]:
    """What the model sees: attributes only. CNICs and phone numbers stay out - they are
    identity keys the rules already compare, and the model has no business with them."""
    out = {
        "name": d.get("name"),
        "also_recorded_as": d["names"][1:4],
        "father_name": d.get("father_name"),
        "addresses": d["addresses"][:4],
        "organisations": d["organisations"][:4],
        "hotel_stays": [f"{s.get('hotel')} ({s.get('district') or '-'}) {s.get('check_in') or ''}".strip() for s in d["stays"][:3]],
        "police_stations": d["police_stations"][:3],
        "designation": d.get("extra", {}).get("designation"),
    }
    return {k: v for k, v in out.items() if v}


def _tokens(d: dict) -> set[str]:
    tokens: set[str] = set()
    for address in d["addresses"][:6]:
        area = parse_address(address).get("area_tokens") or ""
        tokens.update(t for t in area.split() if t not in PK_CITIES)
    return tokens


def _prior(dx: dict, dy: dict) -> float:
    """How promising a pair is for review. Cheap, and deliberately generous - the
    model is the filter; this only keeps the budget off obvious strangers."""
    score = min(0.6, 0.3 * len(_tokens(dx) & _tokens(dy)))
    if surname(dx.get("name")) and surname(dx.get("name")) == surname(dy.get("name")):
        score += 0.3
    fx = set(name_key(dx.get("father_name")).split()) - {"muhammad", "syed"}
    fy = set(name_key(dy.get("father_name")).split()) - {"muhammad", "syed"}
    if fx & fy:
        score += 0.2
    ox = {t for o in dx["organisations"] for t in name_key(o).split()}
    oy = {t for o in dy["organisations"] for t in name_key(o).split()}
    if ox & oy:
        score += 0.2
    if (not dx.get("cnic") or not dy.get("cnic")) and dx.get("name") and dy.get("name") and \
            fuzz.token_set_ratio(name_key(dx["name"]), name_key(dy["name"])) >= 85:
        score += 0.4
    return score


class LlmPairReviewer:
    """Ask the model about candidate pairs, keep only evidence-backed judgements."""

    def __init__(self, llm: Any, *, budget: int, workers: int = 4) -> None:
        self.llm = llm
        self.budget = budget
        self.workers = workers
        self.calls = 0
        self.kept = 0

    def review(self, pairs: list[tuple[dict, dict]]) -> list[Signal | None]:
        pairs = pairs[: max(0, self.budget)]
        self.calls += len(pairs)
        if not pairs:
            return []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(lambda p: self._one(*p), pairs))
        self.kept += sum(1 for r in results if r)
        return results

    def _one(self, dx: dict, dy: dict) -> Signal | None:
        text = json.dumps({"A": _profile(dx), "B": _profile(dy)}, ensure_ascii=False, indent=1)
        try:
            judgement, _ = self.llm.generate_structured(
                prompt=f"{text}\n\nReturn your judgement as JSON.", schema=_PairJudgement,
                system=_PAIR_SYSTEM, cache_kind="pair_review", prompt_version="v1",
            )
        except Exception as exc:  # noqa: BLE001 - a failed review is a missing lead, not a failed run
            logger.info("AI pair review failed: %s", exc)
            return None
        if judgement.relation == "none" or judgement.confidence < 0.3:
            return None
        haystack = _flat(text)
        evidence = [e.strip() for e in judgement.evidence if len(_flat(e)) >= 3 and _flat(e) in haystack]
        if not evidence:
            return None  # a conclusion with nothing in the records behind it
        score = round(min(AI_PAIR_CAP, AI_PAIR_CAP * judgement.confidence), 2)
        quoted = "; ".join(f"“{e}”" for e in evidence[:3])
        return score, _AI_LABELS[judgement.relation], f"{judgement.explanation.strip()[:220]} Evidence: {quoted}"


# --------------------------------------------------------------------------------------
# The pass
# --------------------------------------------------------------------------------------


def compute_weak_links(
    builder: GraphBuilder,
    *,
    min_score: float = 0.35,
    ai: AddressParser | None = None,
    reviewer: LlmPairReviewer | None = None,
) -> int:
    people = [n for n in builder.persons() if n.data.get("name") or n.data.get("addresses")]
    # The seed people (the targets). The AI review budget is limited, so pairs that
    # involve a target are reviewed first - the analyst cares most about "how is this
    # depth-5 person weakly tied to the target", so those get the model's attention before
    # peripheral pair-to-pair links.
    targets = {n.id for n in people if n.data.get("seed")}
    parts_cache: dict[str, dict] = {}
    # How many people in this graph share each employer / police station.
    crowd: dict[str, Counter[str]] = {"org": Counter(), "ps": Counter()}
    for n in people:
        crowd["org"].update({name_key(o) for o in n.data.get("organisations") or [] if name_key(o)})
        crowd["ps"].update({name_key(p) for p in n.data.get("police_stations") or [] if name_key(p)})
    found: dict[tuple[str, str], tuple[list[Signal], set[str]]] = {}
    candidates: list[tuple[float, tuple[str, str]]] = []

    for x, y in itertools.combinations(people, 2):
        dx, dy = x.data, y.data
        if dx.get("cnic") and dx.get("cnic") == dy.get("cnic"):
            continue
        signals, used_ai = _rule_signals(dx, dy, parts_cache, ai, crowd)
        found[(x.id, y.id)] = (signals, {"ai"} if used_ai else set())
        if reviewer is not None and _combine(signals) < AI_PAIR_CAP and not builder.connected_directly(x.id, y.id):
            prior = _prior(dx, dy)
            # A pair that ties someone to a target is worth spending the budget on even on
            # a thinner prior; bump it so target links win the ordering.
            involves_target = bool(targets & {x.id, y.id})
            if involves_target:
                prior += 0.5
            if prior >= 0.3:
                candidates.append((prior, (x.id, y.id)))

    if reviewer is not None and candidates:
        candidates.sort(key=lambda c: -c[0])
        keys = [key for _, key in candidates]
        results = reviewer.review([(builder.nodes[a].data, builder.nodes[b].data) for a, b in keys])
        for key, result in zip(keys, results, strict=False):
            if result:
                found[key][0].append(result)
                found[key][1].add("ai")

    added = 0
    for (a, b), (signals, sources) in found.items():
        if not signals:
            continue
        combined = _combine(signals)
        if combined < min_score:
            continue
        labels = list(dict.fromkeys(s[1] for s in sorted(signals, key=lambda s: -s[0])))
        top = max(signals, key=lambda s: s[0])
        source = "ai" if "ai" in sources else ("osint" if top[1] == "OSINT" else "rules")
        builder.add_weak(a, b, score=combined, label=" + ".join(labels[:2]),
                         reasons=[f"{s[1]} ({s[0]:.2f}): {s[2]}" for s in signals], source=source)
        added += 1
    return added
