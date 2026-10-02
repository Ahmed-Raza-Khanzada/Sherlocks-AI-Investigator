"""Free name search: find a person's social profiles without a paid search API.

Bright Data is the deep search, and without its key the footprint tools are skipped - so
a person known only by name got nothing online. This fills that gap with the plain-HTML
pages of two public search engines, which need no key:

    "Muhammad Asif Khan" Karachi (site:facebook.com OR site:instagram.com OR ...)

Each result's title and snippet travel with its URL, so the profile matcher can confirm
it the way it confirms a scraped page - does it mention the person's area or employer?
A result is only ever a lead.

Free engines throttle automated use, so this is deliberately frugal: one combined query
per person, a couple of per-platform follow-ups only when that finds nothing, a pause
between every query, and DuckDuckGo first with Bing as the fallback. An engine that
throttles is rested for a while instead of being retried harder. Every query is written
to ``logs/osint_websearch_<date>.txt`` so what was sent out is on record.
"""

from __future__ import annotations

import base64
import html
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, unquote, urlsplit

import requests

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}

# Required platforms first, in the order the analyst asked for. WhatsApp has no public
# profiles to search; it is reported from the phone number.
PLATFORM_SITES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Facebook", ("facebook.com",)),
    ("Instagram", ("instagram.com",)),
    ("Twitter/X", ("x.com", "twitter.com")),
    ("YouTube", ("youtube.com",)),
    ("Snapchat", ("snapchat.com",)),
    ("Telegram", ("t.me",)),
    ("TikTok", ("tiktok.com",)),
    ("LinkedIn", ("linkedin.com",)),
)
# Where a Pakistani citizen most often has a findable profile: follow-up targets.
FOLLOW_UP = ("Facebook", "Instagram")
ENGINE_REST_SECONDS = 600

_TAG = re.compile(r"<[^>]+>")


def _clean(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", fragment))).strip()


def _blocks(page: str, marker: re.Pattern[str]) -> list[str]:
    starts = [m.start() for m in marker.finditer(page)]
    return [page[a:b] for a, b in zip(starts, [*starts[1:], len(page)], strict=True)]


# -- DuckDuckGo (html.duckduckgo.com) ------------------------------------------------

_DDG_START = re.compile(r'<div class="result [^"]*results_links')
_DDG_LINK = re.compile(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_DDG_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)


def _ddg_url(href: str) -> str:
    """DuckDuckGo wraps results as //duckduckgo.com/l/?uddg=<encoded target>."""
    href = html.unescape(href)
    if "uddg=" in href:
        return unquote(parse_qs(urlsplit(href if "//" in href else f"https:{href}").query).get("uddg", [""])[0])
    return href if href.startswith("http") else f"https:{href}"


def parse_duckduckgo(page: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for block in _blocks(page, _DDG_START):
        link = _DDG_LINK.search(block)
        if not link:
            continue
        url = _ddg_url(link.group(1))
        if "duckduckgo.com" in urlsplit(url).netloc:
            continue  # sponsored / internal
        snippet = _DDG_SNIPPET.search(block, link.end())
        out.append((url, _clean(link.group(2)), _clean(snippet.group(1)) if snippet else ""))
    return out


# -- Bing (www.bing.com) --------------------------------------------------------------

_BING_START = re.compile(r'<li class="b_algo"')
_BING_LINK = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
_BING_SNIPPET = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL)


def _bing_url(href: str) -> str:
    """Bing wraps results as /ck/a?...&u=a1<base64url of the target>."""
    href = html.unescape(href)
    if "/ck/a" in href:
        token = parse_qs(urlsplit(href).query).get("u", [""])[0]
        if token.startswith("a1"):
            token = token[2:]
            try:
                return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8", "replace")
            except (ValueError, TypeError):
                return href
    return href


def parse_bing(page: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for block in _blocks(page, _BING_START):
        link = _BING_LINK.search(block)
        if not link:
            continue
        snippet = _BING_SNIPPET.search(block, link.end())
        out.append((_bing_url(link.group(1)), _clean(link.group(2)), _clean(snippet.group(1)) if snippet else ""))
    return out


# -- search --------------------------------------------------------------------------


@dataclass
class WebHit:
    platform: str
    url: str
    title: str
    snippet: str
    query: str
    engine: str

    @property
    def text(self) -> str:
        return f"{self.title} {self.snippet}".strip()


@dataclass
class WebSearchRun:
    hits: list[WebHit] = field(default_factory=list)
    queries: int = 0
    engines: list[str] = field(default_factory=list)
    stopped: str | None = None   # why it stopped early (every engine throttling, network down)


def combined_query(name: str, city: str | None, platforms: tuple[str, ...] | None = None) -> str:
    sites = [s for p, group in PLATFORM_SITES if platforms is None or p in platforms for s in group]
    place = f" {city.title()}" if city else ""
    return f'"{name.strip()}"{place} (' + " OR ".join(f"site:{s}" for s in sites) + ")"


class FreeWebSearch:
    """Sequential, throttle-aware search over DuckDuckGo, then Bing."""

    _lock = threading.Lock()                 # one query at a time, process-wide
    _rested_until: ClassVar[dict[str, float]] = {}   # engine -> time it may be used again

    ENGINES = (
        ("duckduckgo", "https://html.duckduckgo.com/html/", parse_duckduckgo),
        ("bing", "https://www.bing.com/search", parse_bing),
    )

    def __init__(self, *, delay_seconds: float = 3.0, timeout: int = 15, max_results: int = 10,
                 log_dir: Path | None = None, session: requests.Session | None = None) -> None:
        self.delay = max(1.0, delay_seconds)
        self.timeout = timeout
        self.max_results = max_results
        self.session = session or requests.Session()
        self.log_dir = log_dir

    def _log(self, engine: str, query: str, status: int | str, count: int) -> None:
        if not self.log_dir:
            return
        now = datetime.now(UTC).astimezone()
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with (self.log_dir / f"osint_websearch_{now:%Y-%m-%d}.txt").open("a", encoding="utf-8") as fh:
                fh.write(f"{now:%Y-%m-%dT%H:%M:%S}  {engine:<10}  {status}  results={count}  q={query}\n")
        except OSError:
            pass

    def search(self, query: str) -> tuple[list[tuple[str, str, str]], str | None, str | None]:
        """``(results, engine used, reason every engine refused)``."""
        refusals: list[str] = []
        for engine, url, parse in self.ENGINES:
            if self._rested_until.get(engine, 0) > time.time():
                refusals.append(f"{engine} resting after throttling")
                continue
            params = {"q": query} if engine == "duckduckgo" else {"q": query, "count": "20", "setlang": "en"}
            with self._lock:
                try:
                    resp = self.session.get(url, params=params, headers=_HEADERS, timeout=self.timeout)
                except requests.RequestException as exc:
                    self._log(engine, query, "ERR", 0)
                    refusals.append(f"{engine}: {type(exc).__name__}")
                    continue
                finally:
                    time.sleep(self.delay)
            body = resp.text or ""
            throttled = (resp.status_code in (202, 403, 429) or "anomaly-modal" in body
                         or "b_captcha" in body or "/challenge" in resp.url)
            if throttled:
                self._rested_until[engine] = time.time() + ENGINE_REST_SECONDS
                self._log(engine, query, f"THROTTLED {resp.status_code}", 0)
                refusals.append(f"{engine} is throttling (HTTP {resp.status_code})")
                continue
            results = parse(body)[: self.max_results]
            self._log(engine, query, resp.status_code, len(results))
            return results, engine, None
        return [], None, "; ".join(refusals) or "no search engine available"

    def find_profiles(self, name: str, city: str | None = None, *, max_queries: int = 3) -> WebSearchRun:
        from sherlocks.linkgraph.social import PRIORITY, platform_for

        run = WebSearchRun()
        if not name or len(name.strip()) < 4 or max_queries < 1:
            return run
        seen: set[str] = set()

        def collect(query: str) -> bool:
            results, engine, refused = self.search(query)
            run.queries += 1
            if refused:
                run.stopped = refused
                return False
            if engine and engine not in run.engines:
                run.engines.append(engine)
            for url, title, snippet in results:
                platform = platform_for(url)
                key = url.split("?")[0].rstrip("/").lower()
                if platform in PRIORITY and key not in seen:
                    seen.add(key)
                    run.hits.append(WebHit(platform, url, title, snippet, query, engine or "?"))
            return True

        if not collect(combined_query(name, city)):
            return run
        found = {h.platform for h in run.hits}
        for platform in FOLLOW_UP:
            if run.queries >= max_queries:
                break
            if platform not in found and not collect(combined_query(name, city, (platform,))):
                break
        return run
