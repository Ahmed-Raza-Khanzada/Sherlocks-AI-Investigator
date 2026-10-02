"""How much a shared value says.

Two men whose fathers are both "Muhammad Ali" share almost nothing - half of Karachi
could say the same. Two whose fathers are both "Ghulam Sarwar Jatoi" probably share a
family. A rule that scores both the same floods the graph with false family links.

This module holds the one judgement the link rules need about names: how common each
token is. It is a fixed list (the commonest given names, honorific name-parts and
surnames/castes in Sindh), not a model - an analyst can read it and argue with it.
"""

from __future__ import annotations

from sherlocks.linkgraph.normalize import name_key

# Tokens so frequent in Pakistani names that sharing them is close to meaningless.
COMMON_NAME_TOKENS: frozenset[str] = frozenset({
    # name-parts and the commonest given names
    "muhammad", "ahmed", "ali", "hasan", "hussain", "abdul", "allah", "ullah", "din", "deen",
    "ghulam", "bux", "bakhsh", "khan", "shah", "syed", "raza", "abbas", "akbar", "aslam",
    "iqbal", "akhtar", "mehmood", "mahmood", "javed", "rafiq", "rahman", "nawaz", "sharif",
    "bibi", "begum", "khatoon", "noor", "nabi", "rasool", "karim", "rehman", "imran", "usman",
    "umar", "bilal", "asif", "kamran", "tariq", "zahid", "khalid", "arshad", "ashraf", "anwar",
    "aziz", "jamil", "jameel", "hameed", "hamid", "majeed", "rashid", "saeed", "shahid",
    "fatima", "ayesha", "amir", "aamir", "nadeem", "naveed", "waqas", "faisal", "farooq",
    # surnames / castes / tribes common across Sindh
    "shaikh", "sheikh", "malik", "baig", "mirza", "qureshi", "siddiqui", "ansari", "memon",
    "baloch", "rajput", "butt", "awan", "jatoi", "soomro", "brohi", "khoso", "chandio", "abro",
    "magsi", "mangi", "channa", "laghari", "leghari", "bhutto", "shar", "junejo", "pathan",
    "khattak", "yousafzai", "afridi", "abbasi", "arain", "jutt", "jat", "chaudhry", "rana",
})


def commonness(value: object) -> float:
    """Share of a name's tokens that are common, 0 (all rare) .. 1 (all common).
    An empty name counts as fully common: it distinguishes nobody."""
    tokens = name_key(value).split()
    if not tokens:
        return 1.0
    return sum(1 for t in tokens if t in COMMON_NAME_TOKENS) / len(tokens)


def rarity_factor(value: object, floor: float = 0.45) -> float:
    """Multiplier for a signal that rests on this name matching: 1.0 for a rare name,
    down to ``floor`` for one made only of common tokens."""
    return round(1.0 - (1.0 - floor) * commonness(value), 3)
