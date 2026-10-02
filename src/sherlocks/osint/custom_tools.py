"""OSINT tools Sherlocks adds on top of OpenOSINT: Apify actors, a social-profile crawl
(Social-Analyzer), and the paid person-data APIs - SocialCrawl, Pipl, FullContact,
Hunter.io and EnformionGO.

Each entrypoint matches the OpenOSINT contract the client expects: an ``async def`` that
takes the query first, accepts ``timeout_seconds`` (and ``api_key`` where relevant), and
returns a **string** (never raises for an empty result - it returns text the classifier
reads). A transport/config failure raises, and the client turns it into an ERROR result.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import requests

APIFY_BASE = "https://api.apify.com/v2"


async def run_apify_osint(query: str, timeout_seconds: int = 120, *, api_key: str | None = None,
                          actor: str | None = None, input_field: str | None = None) -> str:
    """Run an Apify actor for a username / profile URL and return its dataset as text.

    Apify hosts scrapers ("actors") for Instagram, Facebook, TikTok, Twitter/X, LinkedIn
    etc. Which actor runs, and which input field carries the query, are configurable
    (``SHERLOCKS_OSINT_APIFY_ACTOR`` / ``_APIFY_INPUT_FIELD``) because each actor differs.
    Needs a paid ``APIFY_TOKEN``.
    """
    if not api_key:
        return "Apify skipped: no APIFY_TOKEN configured."
    actor = (actor or "apify/instagram-scraper").replace("/", "~")   # Apify path form
    field = input_field or "usernames"
    payload: dict[str, Any] = {field: [query], "resultsLimit": 5, "maxItems": 5}
    url = f"{APIFY_BASE}/acts/{actor}/run-sync-get-dataset-items?token={api_key}"

    def _call() -> str:
        resp = requests.post(url, json=payload, timeout=timeout_seconds)
        if resp.status_code >= 400:
            raise RuntimeError(f"Apify HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            items = resp.json()
        except ValueError:
            return f"Apify returned non-JSON: {resp.text[:300]}"
        if not items:
            return f"Apify actor {actor} found no items for {query!r}."
        lines = [f"[Apify {actor}] {len(items)} item(s) for {query!r}:"]
        for item in items[:5]:
            keep = {k: v for k, v in item.items()
                    if k in ("username", "fullName", "full_name", "name", "url", "profileUrl",
                             "biography", "bio", "followersCount", "email", "city", "location")}
            lines.append(json.dumps(keep or item, ensure_ascii=False, default=str)[:400])
        return "\n".join(lines)

    return await asyncio.to_thread(_call)


async def run_social_crawl_osint(query: str, timeout_seconds: int = 120) -> str:
    """Crawl a username across many social sites with Social-Analyzer, returning the
    profiles it detects (name, link, per-site). Free; the ``social-analyzer`` package
    must be installed (the tool is skipped otherwise)."""
    def _call() -> str:
        from social_analyzer import SocialAnalyzer  # imported here so a missing package = skip

        results = SocialAnalyzer().run_as_object(
            username=query, silent=True, output="json", metadata=False,
            top=200, timeout=max(5, min(timeout_seconds, 120)),
            method="find", filter="good",
        )
        detected = (results or {}).get("detected") or []
        if not detected:
            return f"Social crawl: no profiles detected for {query!r}."
        lines = [f"[Social-Analyzer] {len(detected)} profile(s) for {query!r}:"]
        for d in detected[:40]:
            lines.append(f"  {d.get('title') or d.get('name') or '?'}: {d.get('link') or d.get('url') or ''}")
        return "\n".join(lines)

    return await asyncio.to_thread(_call)


# --------------------------------------------------------------------------------------
# Paid person-data APIs. Each needs its key in .env; without it the tool is skipped by
# the client before it is ever called (and, called directly, says so instead of failing).
# A "no match" answer (HTTP 404) is a result, not an error.
# --------------------------------------------------------------------------------------

SOCIALCRAWL_BASE = "https://www.socialcrawl.dev/v1"
PIPL_URL = "https://api.pipl.com/search/"
FULLCONTACT_URL = "https://api.fullcontact.com/v3/person.enrich"
HUNTER_VERIFY_URL = "https://api.hunter.io/v2/email-verifier"
ENFORMION_URL = "https://api.galaxysearchapi.com/PersonSearch"

# SocialCrawl paths: most platforms answer a handle at /profile, YouTube at /channel.
_SOCIALCRAWL_PATHS = {"youtube": "channel"}
# The profile page itself. SocialCrawl's ``url`` is sometimes the account's own website
# (Instagram), so the platform link is built from the username the API confirmed.
_PROFILE_PAGES = {
    "instagram": "https://www.instagram.com/{}", "facebook": "https://www.facebook.com/{}",
    "tiktok": "https://www.tiktok.com/@{}", "twitter": "https://x.com/{}",
    "youtube": "https://www.youtube.com/@{}", "linkedin": "https://www.linkedin.com/in/{}",
    "threads": "https://www.threads.net/@{}", "snapchat": "https://www.snapchat.com/add/{}",
}
_PROFILE_KEYS = ("username", "handle", "display_name", "name", "full_name", "fullName", "nickname", "url",
                 "profile_url", "profileUrl", "bio", "biography", "description", "location", "city", "followers",
                 "followers_count", "followersCount", "following", "posts_count", "verified", "private",
                 "joined_at", "website", "email", "phone")


def _e164_pk(phone: str) -> str:
    """03001234567 -> +923001234567. Person-data APIs expect international numbers."""
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("0") and len(digits) == 11:
        return "+92" + digits[1:]
    if digits.startswith("92") and len(digits) == 12:
        return "+" + digits
    return phone


def _request(method: str, url: str, timeout: int, **kwargs: Any) -> Any | None:
    """JSON body of a successful call, ``None`` for "no match" (404). Anything else raises,
    so the client records an ERROR - an expired key must never look like "not found"."""
    resp = requests.request(method, url, timeout=timeout, **kwargs)
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(f"non-JSON answer: {resp.text[:200]}") from exc


def _pick(obj: Any, keys: tuple[str, ...] = _PROFILE_KEYS) -> dict[str, Any]:
    return {k: obj[k] for k in keys if isinstance(obj, dict) and obj.get(k) not in (None, "", [], {})}


async def run_socialcrawl_osint(query: str, timeout_seconds: int = 120, *, api_key: str | None = None,
                                platforms: list[str] | None = None) -> str:
    """Look a username up on several social platforms through SocialCrawl (one key, one
    response shape for all of them). Each platform checked costs credits; which ones are
    checked is ``SHERLOCKS_OSINT_SOCIALCRAWL_PLATFORMS``."""
    if not api_key:
        return "SocialCrawl skipped: no SOCIALCRAWL_API_KEY configured."
    platforms = platforms or ["instagram", "facebook", "tiktok", "twitter", "youtube", "linkedin"]
    handle = query.lstrip("@").strip()
    per_call = max(5, timeout_seconds // max(1, len(platforms)))

    def _call() -> str:
        lines = [f"[SocialCrawl] handle {handle!r} on {len(platforms)} platform(s):"]
        failures = []
        for platform in platforms:
            path = _SOCIALCRAWL_PATHS.get(platform, "profile")
            try:
                body = _request("GET", f"{SOCIALCRAWL_BASE}/{platform}/{path}", per_call,
                                params={"handle": handle}, headers={"x-api-key": api_key})
            except Exception as exc:  # noqa: BLE001 - one platform down must not hide the rest
                failures.append(f"{platform}: {exc}")
                continue
            if not body or not body.get("success"):
                lines.append(f"  {platform}: not found")
                continue
            data = body.get("data") or {}
            profile = _pick(data.get("author") or data.get("user") or data.get("profile") or data)
            page = _PROFILE_PAGES.get(platform, "").format(profile.get("username") or handle) if platform in _PROFILE_PAGES else ""
            lines.append(f"  {platform}: FOUND {page} " + json.dumps(profile or data, ensure_ascii=False, default=str)[:400])
        if failures and len(failures) == len(platforms):
            raise RuntimeError("SocialCrawl failed on every platform - " + "; ".join(failures)[:300])
        lines += [f"  {f} (error)" for f in failures]
        return "\n".join(lines)

    return await asyncio.to_thread(_call)


def _pipl_person(p: dict) -> list[str]:
    def shown(key: str, field: str = "display") -> list[str]:
        return [str(x.get(field) or x.get("content") or x.get("url")) for x in p.get(key) or [] if isinstance(x, dict)]

    out = []
    for label, key, field in (("names", "names", "display"), ("usernames", "usernames", "content"),
                              ("emails", "emails", "address"), ("phones", "phones", "display_international"),
                              ("addresses", "addresses", "display"), ("jobs", "jobs", "display"),
                              ("education", "educations", "display"), ("profiles", "urls", "url")):
        values = [v for v in shown(key, field) if v and v != "None"]
        if values:
            out.append(f"    {label}: {', '.join(values[:6])}")
    related = [r.get("names", [{}])[0].get("display") for r in p.get("relationships") or [] if r.get("names")]
    if related:
        out.append(f"    related people: {', '.join(x for x in related[:8] if x)}")
    return out


async def run_pipl_osint(query: str, timeout_seconds: int = 60, *, api_key: str | None = None,
                         field: str = "phone") -> str:
    """Pipl: a phone / email / username -> the person behind it, with their other
    identifiers, online profiles, jobs and related people."""
    if not api_key:
        return "Pipl skipped: no PIPL_API_KEY configured."
    value = _e164_pk(query) if field == "phone" else query

    def _call() -> str:
        body = _request("GET", PIPL_URL, timeout_seconds, params={"key": api_key, field: value})
        if not body:
            return f"Pipl: no person found for {field} {value!r}."
        lines = [f"[Pipl] {field} {value!r}: {body.get('@persons_count', '?')} match(es)"]
        if body.get("person"):
            lines.append("  best match:")
            lines += _pipl_person(body["person"])
        for i, p in enumerate((body.get("possible_persons") or [])[:3], start=1):
            lines.append(f"  possible person {i} (match {p.get('@match', '?')}):")
            lines += _pipl_person(p)
        return "\n".join(lines) if len(lines) > 1 else f"Pipl: no person found for {field} {value!r}."

    return await asyncio.to_thread(_call)


async def run_fullcontact_osint(query: str, timeout_seconds: int = 60, *, api_key: str | None = None,
                                field: str = "email") -> str:
    """FullContact person enrich: an email or phone -> name, location, employer, bio and
    linked social profiles."""
    if not api_key:
        return "FullContact skipped: no FULLCONTACT_API_KEY configured."
    value = _e164_pk(query) if field == "phone" else query

    def _call() -> str:
        body = _request("POST", FULLCONTACT_URL, timeout_seconds, json={field: value},
                        headers={"Authorization": f"Bearer {api_key}"})
        if not body:
            return f"FullContact: no person found for {field} {value!r}."
        lines = [f"[FullContact] {field} {value!r}:"]
        top = _pick(body, ("fullName", "location", "title", "organization", "twitter", "linkedin",
                           "facebook", "bio", "website", "ageRange", "gender"))
        lines.append("  " + json.dumps(top, ensure_ascii=False))
        details = body.get("details") or {}
        for p in (details.get("profiles") or {}).values() if isinstance(details.get("profiles"), dict) else []:
            if isinstance(p, dict) and p.get("url"):
                lines.append(f"  profile: {p.get('service', '')} {p['url']}")
        for key in ("emails", "phones"):
            for item in details.get(key) or []:
                if isinstance(item, dict) and item.get("value"):
                    lines.append(f"  {key[:-1]}: {item['value']}")
        return "\n".join(lines)

    return await asyncio.to_thread(_call)


async def run_hunter_osint(query: str, timeout_seconds: int = 30, *, api_key: str | None = None) -> str:
    """Hunter.io email verifier: is the address real, and on which public pages it
    appears (useful for an employer or a business the subject runs)."""
    if not api_key:
        return "Hunter skipped: no HUNTER_API_KEY configured."

    def _call() -> str:
        body = _request("GET", HUNTER_VERIFY_URL, timeout_seconds, params={"email": query, "api_key": api_key})
        data = (body or {}).get("data") or {}
        if not data:
            return f"Hunter: nothing known about {query!r}."
        lines = [f"[Hunter] {query}: status {data.get('status')}, score {data.get('score')}"]
        for source in (data.get("sources") or [])[:10]:
            lines.append(f"  seen at {source.get('uri') or source.get('domain')} ({source.get('extracted_on') or ''})")
        return "\n".join(lines)

    return await asyncio.to_thread(_call)


async def run_enformion_osint(query: str, timeout_seconds: int = 60, *, api_key: str | None = None,
                              profile: str | None = None) -> str:
    """EnformionGO person search by phone. US public records only - for a subject with a
    US number or US ties; it finds nothing for a Pakistani number."""
    if not (api_key and profile):
        return "Enformion skipped: ENFORMION_API_PROFILE and ENFORMION_API_KEY are both needed."

    def _call() -> str:
        body = _request("POST", ENFORMION_URL, timeout_seconds,
                        json={"Phone": query, "Page": 1, "ResultsPerPage": 5},
                        headers={"galaxy-ap-name": profile, "galaxy-ap-password": api_key,
                                 "galaxy-search-type": "PersonSearch", "Content-Type": "application/json"})
        persons = (body or {}).get("persons") or []
        if not persons:
            return f"Enformion: no US person found for {query!r}."
        lines = [f"[Enformion] {len(persons)} US person(s) for {query!r}:"]
        for p in persons[:5]:
            lines.append("  " + json.dumps({k: p.get(k) for k in ("name", "age", "addresses", "phoneNumbers",
                                                                   "emailAddresses", "associates", "relativesSummary")
                                            if p.get(k)}, ensure_ascii=False, default=str)[:600])
        return "\n".join(lines)

    return await asyncio.to_thread(_call)


# -- direct calls for the link graph's own pivots (structured answers, not text) --------

HUNTER_FINDER_URL = "https://api.hunter.io/v2/email-finder"


def pipl_name_search(name: str, api_key: str, *, city: str | None = None, country: str = "PK",
                     timeout: int = 60) -> dict[str, Any] | None:
    """Pipl search by name (+ city, country). The raw answer, for ``match.match_pipl`` to
    check against the records - a name alone matches many people."""
    params = {"key": api_key, "raw_name": name, "country": country}
    if city:
        params["city"] = city
    return _request("GET", PIPL_URL, timeout, params=params)


def pipl_lookup(api_key: str, *, timeout: int = 60, **query: str) -> dict[str, Any] | None:
    """Pipl search by any identifier (``email=``, ``phone=``…): the raw answer."""
    return _request("GET", PIPL_URL, timeout, params={"key": api_key, **query})


def fullcontact_lookup(api_key: str, *, timeout: int = 60, **query: str) -> dict[str, Any] | None:
    """FullContact person enrich by ``email=`` / ``phone=``: the raw answer."""
    return _request("POST", FULLCONTACT_URL, timeout, json=query, headers={"Authorization": f"Bearer {api_key}"})


def hunter_find_email(full_name: str, company: str, api_key: str, *, timeout: int = 30) -> dict[str, Any] | None:
    """Hunter email-finder: a person's name + their employer -> their likely work email
    (with Hunter's confidence score), or ``None``."""
    body = _request("GET", HUNTER_FINDER_URL, timeout,
                    params={"full_name": full_name, "company": company, "api_key": api_key})
    data = (body or {}).get("data") or {}
    return data if data.get("email") else None
