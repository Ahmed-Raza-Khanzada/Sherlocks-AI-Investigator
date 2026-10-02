"""Social-media profile discovery and confirmation.

A username existing on Instagram is a *lead*, not proof it is the subject's. Half of
"aliraza" accounts belong to a different Ali Raza. So every discovered profile is
scored by how much of the subject's own information corroborates it, and nothing here
is ever treated as evidence - the strongest a profile reaches is a weak,
human-verify link.

Where profiles come from:

* **phone → Caller ID** (Truecaller-style, already integrated) - returns the name the
  number is saved as, and often a Facebook link. This is the one path that works from
  a phone number, and it is live in this deployment.
* **name → username guesses → sherlock** - presence of those usernames across 300+
  sites, filtered here to the social platforms. Tells us *where* a matching handle
  exists, not whose it is.
* **name (+ city) → Bright Data footprint / dorks** - search-engine hits that surface
  profile URLs directly. Needs a Bright Data key.
* **scrape_url (Bright Data)** - fetches a candidate profile page so its text can be
  checked for the subject's city / employer. Without it, confirmation rests on the
  name and the handle alone.

WhatsApp, Telegram and Signal cannot be enumerated - there is no lookup that lists who
holds an account. But the phone number *is* the account handle, so a known number is
reported as a reachable channel on that app, flagged as "not confirmed active".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from rapidfuzz import fuzz

from sherlocks.linkgraph.normalize import name_key, normalize_address, parse_address

# host substring -> platform label. Order does not matter; first containment wins.
_PLATFORMS: dict[str, str] = {
    "facebook.": "Facebook",
    "fb.com": "Facebook",
    "instagram.": "Instagram",
    "twitter.": "Twitter/X",
    "x.com": "Twitter/X",
    "snapchat.": "Snapchat",
    "tiktok.": "TikTok",
    "linkedin.": "LinkedIn",
    "youtube.": "YouTube",
    "t.me": "Telegram",
    "telegram.": "Telegram",
    "github.": "GitHub",
    "pinterest.": "Pinterest",
    "reddit.": "Reddit",
    "threads.": "Threads",
    "medium.": "Medium",
    "flickr.": "Flickr",
    "tumblr.": "Tumblr",
    "vk.com": "VK",
}
# The platforms this system MUST check first, in this order, before anything else. Every
# person hunt reports all of these - a "not found" is itself a result the analyst needs.
REQUIRED_PLATFORMS: tuple[str, ...] = (
    "Facebook", "WhatsApp", "Instagram", "Twitter/X", "YouTube", "Snapchat", "Telegram",
)
# Secondary platforms: classified and kept, but only after the required set.
SECONDARY_PLATFORMS: tuple[str, ...] = ("TikTok", "LinkedIn", "Reddit", "Threads", "Pinterest")
PRIORITY = set(REQUIRED_PLATFORMS) | set(SECONDARY_PLATFORMS)

# Messaging apps keyed by phone number - not searchable, but the number reaches them.
PHONE_CHANNELS = ("WhatsApp", "Telegram", "Signal")


@dataclass
class SocialProfile:
    platform: str
    url: str
    username: str | None
    source: str                       # caller_id | sherlock | footprint | dorks
    confidence: float = 0.0
    corroboration: list[str] = field(default_factory=list)  # why we think it is them
    status: str = "unconfirmed"       # unconfirmed | corroborated | channel

    def to_dict(self) -> dict:
        return {
            "platform": self.platform, "url": self.url, "username": self.username,
            "source": self.source, "confidence": round(self.confidence, 2),
            "corroboration": self.corroboration, "status": self.status,
        }


def platform_for(url: str) -> str | None:
    host = urlsplit(url if "//" in url else f"//{url}").netloc.lower()
    host = host.removeprefix("www.")
    for needle, label in _PLATFORMS.items():
        if needle in host or needle in url.lower():
            return label
    return None


def _username_from_url(url: str) -> str | None:
    path = urlsplit(url if "//" in url else f"//{url}").path.strip("/")
    if not path:
        return None
    seg = path.split("/")[-1] if not path.split("/")[0].startswith("@") else path.split("/")[0]
    seg = seg.lstrip("@").split("?")[0]
    return seg or None


@dataclass(frozen=True, slots=True)
class SubjectInfo:
    """The subject's own attributes, used to confirm a candidate profile."""

    name: str | None = None
    name_usernames: tuple[str, ...] = ()   # handles derived from the real name
    city: str | None = None
    area_tokens: tuple[str, ...] = ()
    employer: str | None = None

    @classmethod
    def build(cls, *, name: str | None, addresses: list[str], organisations: list[str],
              usernames: list[str]) -> SubjectInfo:
        city, tokens = None, set()
        for address in addresses:
            parts = parse_address(address)
            city = city or parts.get("city")
            tokens.update((parts.get("area_tokens") or "").split())
        return cls(
            name=name,
            name_usernames=tuple(u.lower() for u in usernames),
            city=city,
            area_tokens=tuple(t for t in tokens if len(t) > 2),
            employer=organisations[0] if organisations else None,
        )


def _handle_from_name(username: str | None, subject: SubjectInfo) -> bool:
    if not username:
        return False
    u = re.sub(r"[^a-z0-9]", "", username.lower())
    if any(u == re.sub(r"[^a-z0-9]", "", g) for g in subject.name_usernames):
        return True
    tokens = [t for t in re.split(r"[^a-z]+", name_key(subject.name)) if len(t) > 2]
    return len(tokens) >= 2 and all(t in u for t in tokens)


def score_profile(profile: SocialProfile, subject: SubjectInfo, *, page_text: str | None = None,
                  caller_name: str | None = None) -> SocialProfile:
    """Corroborate a candidate profile against what we know about the subject.

    The score is confidence that the profile *belongs to the subject*, never that the
    account is genuine. It is capped below 1: a profile is a lead to verify by eye.
    """
    reasons: list[str] = []
    score = 0.15  # a matching handle exists on the platform

    if _handle_from_name(profile.username, subject):
        score += 0.3
        reasons.append(f"handle “{profile.username}” is built from the subject's name")

    if caller_name and subject.name and fuzz.token_set_ratio(name_key(caller_name), name_key(subject.name)) >= 80:
        score += 0.3
        reasons.append(f"phone is saved as “{caller_name}”, matching the subject's name")

    strong = len(reasons)   # handle / caller-name evidence counted so far
    if page_text:
        flat = normalize_address(page_text)
        name_tokens = [t for t in name_key(subject.name).split() if len(t) > 2 and t != "muhammad"]
        if len(name_tokens) >= 2 and all(t in name_key(page_text).split() for t in name_tokens):
            score += 0.1
            reasons.append("result carries the subject's full name")
        if subject.city and subject.city in flat:
            score += 0.2
            reasons.append(f"profile page mentions {subject.city.title()}")
        hit = next((t for t in subject.area_tokens if t in flat), None)
        if hit:
            score += 0.15
            reasons.append(f"profile page mentions “{hit}” from the subject's address")
        if subject.employer and all(t in flat for t in name_key(subject.employer).split()[:2] if len(t) > 2):
            score += 0.2
            reasons.append(f"profile page mentions employer “{subject.employer}”")
            strong += 1
        if hit:
            strong += 1

    profile.confidence = min(0.85, score)
    profile.corroboration = reasons
    # Name and city are shared by thousands of people in one city. A profile is only
    # corroborated by something specific to this person: their area, employer, a handle
    # built from their name, or the name their phone is saved under.
    profile.status = "corroborated" if len(reasons) >= 2 and strong >= 1 else "unconfirmed"
    return profile


def profiles_from_tool_results(results: list, subject: SubjectInfo) -> list[SocialProfile]:
    """Turn discovered URLs (sherlock/footprint/dorks) + Caller ID into scored profiles.

    ``results`` are :class:`ToolResult`-like objects (``tool``, ``status``, ``data``).
    Caller ID is passed through the same list as a synthetic result with
    ``tool='caller_id'`` and ``data={'raw': {...caller-id data...}}``.
    """
    profiles: dict[tuple[str, str], SocialProfile] = {}
    caller_name: str | None = None
    scraped: dict[str, str] = {}  # url -> page text, from scrape_url results

    for result in results:
        data = getattr(result, "data", None) or {}
        raw = data.get("raw") if isinstance(data, dict) else None
        if getattr(result, "tool", "") == "scrape_url" and isinstance(raw, str):
            scraped[data.get("query", "")] = raw
        if getattr(result, "tool", "") == "caller_id" and isinstance(raw, dict):
            caller_name = raw.get("primary_name") or caller_name
            link = raw.get("facebook_link")
            if link and platform_for(link):
                key = ("Facebook", link)
                profiles.setdefault(key, SocialProfile("Facebook", link, _username_from_url(link), "caller_id"))

    text_blob = " ".join(
        (r.data or {}).get("raw", "") for r in results
        if getattr(r, "tool", "") in ("search_username", "search_footprint", "search_dorks_live")
        and isinstance((r.data or {}).get("raw"), str)
    )
    for url in re.findall(r"https?://[^\s<>\"')\]]+", text_blob):
        platform = platform_for(url)
        if not platform or platform not in PRIORITY:
            continue
        key = (platform, url.rstrip("/.,"))
        profiles.setdefault(key, SocialProfile(platform, key[1], _username_from_url(key[1]),
                                               "sherlock" if "search_username" in text_blob else "footprint"))

    out = [score_profile(p, subject, page_text=scraped.get(p.url), caller_name=caller_name) for p in profiles.values()]
    order = {name: i for i, name in enumerate(REQUIRED_PLATFORMS + SECONDARY_PLATFORMS)}
    # Required platforms first (in the fixed order), then by confidence.
    out.sort(key=lambda p: (order.get(p.platform, 99), -p.confidence))
    return out


@dataclass
class PlatformStatus:
    platform: str
    required: bool
    found: bool
    best: SocialProfile | None
    note: str

    def to_dict(self) -> dict:
        return {
            "platform": self.platform, "required": self.required, "found": self.found,
            "note": self.note, "best": self.best.to_dict() if self.best else None,
        }


def platform_report(profiles: list[SocialProfile], phones: list[str], *,
                    brightdata_ready: bool = False, free_search: bool = False) -> list[PlatformStatus]:
    """One row per REQUIRED platform (then secondaries that were found), always.

    A required platform with nothing found still gets a row - "not found" is the answer
    the analyst asked for. WhatsApp/Telegram/Signal are reported as reachable channels
    from the number, since no lookup can confirm the account itself.
    """
    by_platform: dict[str, list[SocialProfile]] = {}
    for p in profiles:
        by_platform.setdefault(p.platform, []).append(p)
    channels = {c.platform: c for c in phone_channels(phones)}
    rows: list[PlatformStatus] = []

    for name in REQUIRED_PLATFORMS:
        hits = by_platform.get(name, [])
        if name in PHONE_CHANNELS and channels.get(name):
            best = max(hits, key=lambda p: p.confidence) if hits else channels[name]
            rows.append(PlatformStatus(name, True, bool(phones), best,
                                       "reachable via number (account not confirmed)" if not hits else best.corroboration[0] if best.corroboration else "candidate"))
        elif hits:
            best = max(hits, key=lambda p: p.confidence)
            rows.append(PlatformStatus(name, True, True, best,
                                       best.corroboration[0] if best.corroboration else "candidate, unconfirmed"))
        else:
            if brightdata_ready:
                note = "not found"
            elif free_search:
                note = "not found by free name search (Bright Data would search deeper)"
            else:
                note = "not searched online - no Bright Data key and free name search is off"
            rows.append(PlatformStatus(name, True, False, None, note))

    for name in SECONDARY_PLATFORMS:
        hits = by_platform.get(name, [])
        if hits:
            best = max(hits, key=lambda p: p.confidence)
            rows.append(PlatformStatus(name, False, True, best,
                                       best.corroboration[0] if best.corroboration else "candidate"))
    return rows


def phone_channels(phones: list[str]) -> list[SocialProfile]:
    """A known number reaches these apps; not a confirmed active account."""
    out: list[SocialProfile] = []
    for phone in phones[:3]:
        intl = "+92" + phone[1:] if phone.startswith("0") else phone
        for app in PHONE_CHANNELS:
            url = f"https://wa.me/{intl.lstrip('+')}" if app == "WhatsApp" else ""
            out.append(SocialProfile(app, url, phone, "phone", confidence=0.2, status="channel",
                                     corroboration=[f"number {phone} is reachable on {app} (account not confirmed)"]))
    return out
