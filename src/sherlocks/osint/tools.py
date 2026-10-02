"""Catalogue of the OpenOSINT tools Sherlocks is allowed to use.

OpenOSINT ships 20 tools, but most of them investigate *infrastructure* - Shodan,
VirusTotal, Censys, subdomain enumeration, IP reputation. None of that helps identify
a Pakistani citizen. Only the seven tools below are people-facing, and this catalogue
is the closed set: a tool absent from here can never run, whatever an agent decides.

Each entry records what the tool needs, so a run can report *why* something was
skipped instead of silently returning nothing.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sherlocks.osint.models import IdentifierKind


def binary_on_path(binary: str) -> bool:
    """Mirror OpenOSINT's own binary lookup.

    ``openosint.utils.run_subprocess`` prepends the interpreter's ``bin`` directory to
    PATH so tools pip-installed into the venv resolve even when the venv is not
    activated. A plain ``shutil.which`` would report ``sherlock`` missing in exactly
    the setup we ship, so this must match.
    """
    venv_bin = str(Path(sys.executable).parent)
    search_path = os.pathsep.join([venv_bin, os.environ.get("PATH", "")])
    return shutil.which(binary, path=search_path) is not None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    module: str
    entrypoint: str
    # Which subject field feeds this tool.
    accepts: IdentifierKind
    description: str
    # External binary that must be on PATH (OpenOSINT shells out to these).
    requires_binary: str | None = None
    # Python package that must be importable (e.g. social-analyzer); skipped if missing.
    requires_package: str | None = None
    # Bright Data keys are billed per request and gate the highest-value tools.
    requires_brightdata: bool = False
    # An OsintSettings attribute holding an API key/token this tool passes as api_key=.
    # ``requires_key`` = the tool cannot run without it (e.g. HIBP for breach); when the
    # key is optional (GitHub token only raises the rate limit) leave requires_key False.
    api_key_setting: str | None = None
    requires_key: bool = False
    # Passed through to the tool's own timeout parameter.
    default_timeout: int = 60
    # False when the tool only reformulates its input (generate_dorks builds URLs and
    # makes no request). Extracting "findings" from such output just echoes the query
    # back as if it were discovered intelligence.
    yields_findings: bool = True
    extra_kwargs: dict[str, object] = field(default_factory=dict)

    @property
    def binary_available(self) -> bool:
        return self.requires_binary is None or binary_on_path(self.requires_binary)


TOOL_CATALOG: dict[str, ToolSpec] = {
    "search_phone": ToolSpec(
        name="search_phone",
        module="openosint.tools.search_phone",
        entrypoint="run_phone_osint",
        accepts=IdentifierKind.PHONE,
        description="Carrier, country and line type for a phone number (phoneinfoga).",
        requires_binary="phoneinfoga",
    ),
    "search_username": ToolSpec(
        name="search_username",
        module="openosint.tools.search_username",
        entrypoint="run_username_osint",
        accepts=IdentifierKind.USERNAME,
        description="Presence of a username across 300+ platforms (sherlock).",
        requires_binary="sherlock",
        default_timeout=180,
    ),
    "search_email": ToolSpec(
        name="search_email",
        module="openosint.tools.search_email",
        entrypoint="run_email_osint",
        accepts=IdentifierKind.EMAIL,
        description="Online accounts registered against an email address (holehe).",
        requires_binary="holehe",
        default_timeout=120,
    ),
    "generate_dorks": ToolSpec(
        name="generate_dorks",
        module="openosint.tools.generate_dorks",
        entrypoint="run_dork_osint",
        accepts=IdentifierKind.FULL_NAME,
        description="Targeted Google search URLs for manual follow-up. No network calls.",
        default_timeout=15,
        yields_findings=False,
    ),
    "search_footprint": ToolSpec(
        name="search_footprint",
        module="openosint.tools.search_footprint",
        entrypoint="run_footprint_osint",
        accepts=IdentifierKind.FULL_NAME,
        description="Public search-engine footprint, entity-type aware (Bright Data SERP).",
        requires_brightdata=True,
        default_timeout=180,
    ),
    "search_dorks_live": ToolSpec(
        name="search_dorks_live",
        module="openosint.tools.search_dorks_live",
        entrypoint="run_dorks_live_osint",
        accepts=IdentifierKind.FULL_NAME,
        description="Live Google results for dork queries (Bright Data SERP, billed per query).",
        requires_brightdata=True,
        default_timeout=180,
    ),
    "scrape_url": ToolSpec(
        name="scrape_url",
        module="openosint.tools.scrape_url",
        entrypoint="run_scrape_url_osint",
        accepts=IdentifierKind.URL,
        description="Fetch a page as clean Markdown, bypassing bot protection (Bright Data).",
        requires_brightdata=True,
        default_timeout=120,
    ),
    # -- person tools by username / email (no Bright Data) --------------------------
    "search_github": ToolSpec(
        name="search_github",
        module="openosint.tools.search_github",
        entrypoint="run_github_osint",
        accepts=IdentifierKind.USERNAME,
        description="GitHub account, repos and profile for a username or email (free; a "
                    "GITHUB_TOKEN only raises the rate limit).",
        api_key_setting="github_token",
        default_timeout=30,
    ),
    "search_paste": ToolSpec(
        name="search_paste",
        module="openosint.tools.search_paste",
        entrypoint="run_paste_osint",
        accepts=IdentifierKind.USERNAME,
        description="Pastebin dumps mentioning a username or email (psbdmp.ws, free).",
        default_timeout=20,
    ),
    "search_breach": ToolSpec(
        name="search_breach",
        module="openosint.tools.search_breach",
        entrypoint="run_breach_osint",
        accepts=IdentifierKind.EMAIL,
        description="Data-breach appearances for an email (Have I Been Pwned, PAID key).",
        api_key_setting="hibp_api_key",
        requires_key=True,
        default_timeout=20,
    ),
    # -- Sherlocks-added tools (not from OpenOSINT) ---------------------------------
    "search_social": ToolSpec(
        name="search_social",
        module="sherlocks.osint.custom_tools",
        entrypoint="run_social_crawl_osint",
        accepts=IdentifierKind.USERNAME,
        description="Crawl a username across many social sites and detect profiles "
                    "(Social-Analyzer, free; package must be installed).",
        requires_package="social_analyzer",
        default_timeout=120,
    ),
    "search_apify": ToolSpec(
        name="search_apify",
        module="sherlocks.osint.custom_tools",
        entrypoint="run_apify_osint",
        accepts=IdentifierKind.USERNAME,
        description="Run an Apify actor (Instagram/Facebook/TikTok/X scraper) for a "
                    "username or profile URL (PAID Apify token).",
        api_key_setting="apify_token",
        requires_key=True,
        default_timeout=180,
    ),
    # -- paid person-data APIs ------------------------------------------------------
    "search_socialcrawl": ToolSpec(
        name="search_socialcrawl",
        module="sherlocks.osint.custom_tools",
        entrypoint="run_socialcrawl_osint",
        accepts=IdentifierKind.USERNAME,
        description="Profile for a username on Instagram/Facebook/TikTok/X/YouTube/LinkedIn "
                    "(SocialCrawl, PAID credits per platform checked).",
        api_key_setting="socialcrawl_api_key",
        requires_key=True,
        default_timeout=120,
    ),
    "search_pipl": ToolSpec(
        name="search_pipl",
        module="sherlocks.osint.custom_tools",
        entrypoint="run_pipl_osint",
        accepts=IdentifierKind.PHONE,
        description="Person behind a phone number: other identifiers, profiles, jobs, relatives (Pipl, PAID).",
        api_key_setting="pipl_api_key",
        requires_key=True,
        extra_kwargs={"field": "phone"},
    ),
    "search_pipl_email": ToolSpec(
        name="search_pipl_email",
        module="sherlocks.osint.custom_tools",
        entrypoint="run_pipl_osint",
        accepts=IdentifierKind.EMAIL,
        description="Person behind an email: other identifiers, profiles, jobs, relatives (Pipl, PAID).",
        api_key_setting="pipl_api_key",
        requires_key=True,
        extra_kwargs={"field": "email"},
    ),
    "search_fullcontact": ToolSpec(
        name="search_fullcontact",
        module="sherlocks.osint.custom_tools",
        entrypoint="run_fullcontact_osint",
        accepts=IdentifierKind.EMAIL,
        description="Email -> name, location, employer, social profiles (FullContact, PAID).",
        api_key_setting="fullcontact_api_key",
        requires_key=True,
        extra_kwargs={"field": "email"},
    ),
    "search_fullcontact_phone": ToolSpec(
        name="search_fullcontact_phone",
        module="sherlocks.osint.custom_tools",
        entrypoint="run_fullcontact_osint",
        accepts=IdentifierKind.PHONE,
        description="Phone -> name, location, employer, social profiles (FullContact, PAID).",
        api_key_setting="fullcontact_api_key",
        requires_key=True,
        extra_kwargs={"field": "phone"},
    ),
    "search_hunter": ToolSpec(
        name="search_hunter",
        module="sherlocks.osint.custom_tools",
        entrypoint="run_hunter_osint",
        accepts=IdentifierKind.EMAIL,
        description="Is an email real, and which public pages carry it (Hunter.io, PAID).",
        api_key_setting="hunter_api_key",
        requires_key=True,
        default_timeout=30,
    ),
    "search_enformion": ToolSpec(
        name="search_enformion",
        module="sherlocks.osint.custom_tools",
        entrypoint="run_enformion_osint",
        accepts=IdentifierKind.PHONE,
        description="US public records for a phone number - US data only (EnformionGO, PAID).",
        api_key_setting="enformion_api_key",
        requires_key=True,
    ),
}


# Which tools can consume which subject field. Note there is no CNIC entry: no OSINT
# tool can search by CNIC, and pretending otherwise would be the single most
# misleading thing this module could do.
TOOLS_BY_KIND: dict[IdentifierKind, list[str]] = {
    IdentifierKind.PHONE: ["search_phone", "search_pipl", "search_fullcontact_phone", "search_enformion",
                           "search_footprint"],
    IdentifierKind.EMAIL: ["search_email", "search_breach", "search_pipl_email", "search_fullcontact", "search_hunter",
                           "search_github", "search_paste", "search_footprint"],
    IdentifierKind.USERNAME: ["search_username", "search_social", "search_socialcrawl", "search_apify", "search_github",
                              "search_paste", "search_footprint"],
    IdentifierKind.FULL_NAME: ["generate_dorks", "search_footprint", "search_dorks_live"],
    IdentifierKind.URL: ["scrape_url"],
}


def tools_for_kinds(kinds: set[IdentifierKind]) -> list[str]:
    """Every catalogued tool that any of ``kinds`` can drive, order preserved."""
    ordered: list[str] = []
    for kind in (
        IdentifierKind.PHONE,
        IdentifierKind.EMAIL,
        IdentifierKind.USERNAME,
        IdentifierKind.FULL_NAME,
        IdentifierKind.URL,
    ):
        if kind not in kinds:
            continue
        for tool in TOOLS_BY_KIND.get(kind, []):
            if tool not in ordered:
                ordered.append(tool)
    return ordered
