"""Social-profile classification and confirmation. No network; tool output is stubbed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sherlocks.linkgraph.social import (
    REQUIRED_PLATFORMS,
    SubjectInfo,
    phone_channels,
    platform_for,
    platform_report,
    profiles_from_tool_results,
)


@dataclass
class _Result:
    tool: str
    data: dict[str, Any]
    is_hit: bool = True


def _subject() -> SubjectInfo:
    return SubjectInfo.build(
        name="Ali Raza",
        addresses=["House 12, Block 13-D, Gulshan-e-Iqbal, Karachi"],
        organisations=["Qureshi Motors"],
        usernames=["aliraza", "ali.raza"],
    )


def test_platform_for():
    assert platform_for("https://instagram.com/ali.raza") == "Instagram"
    assert platform_for("https://x.com/aliraza") == "Twitter/X"
    assert platform_for("https://www.facebook.com/ali.raza") == "Facebook"
    assert platform_for("https://example.com/ali") is None


def test_profiles_classified_and_name_derived_handle_scores():
    raw = "Found: https://instagram.com/ali.raza https://x.com/aliraza https://github.com/aliraza"
    profiles = profiles_from_tool_results([_Result("search_username", {"raw": raw})], _subject())
    plats = {p.platform: p for p in profiles}
    assert "Instagram" in plats and "Twitter/X" in plats
    assert "GitHub" not in plats  # not a priority social platform
    assert plats["Instagram"].confidence >= 0.4
    assert any("built from the subject's name" in r for r in plats["Instagram"].corroboration)


def test_caller_id_facebook_and_name_corroborate():
    results = [_Result("caller_id", {"raw": {"primary_name": "Ali Raza", "facebook_link": "https://facebook.com/ali.raza.99"}})]
    profiles = profiles_from_tool_results(results, _subject())
    fb = next(p for p in profiles if p.platform == "Facebook")
    assert fb.source == "caller_id"
    assert any("saved as" in r for r in fb.corroboration)


def test_scraped_page_confirms_by_city_and_employer():
    raw = "https://instagram.com/ali.raza"
    scrape = _Result("scrape_url", {"query": "https://instagram.com/ali.raza",
                                     "raw": "Ali Raza | Karachi | works at Qureshi Motors"})
    profiles = profiles_from_tool_results([_Result("search_username", {"raw": raw}), scrape], _subject())
    insta = next(p for p in profiles if p.platform == "Instagram")
    assert insta.status == "corroborated"
    assert insta.confidence >= 0.6
    assert any("Karachi" in r for r in insta.corroboration)
    assert any("Qureshi Motors" in r for r in insta.corroboration)


def test_unrelated_handle_stays_low():
    # A profile whose handle is nothing like the name, with no page and no caller match.
    raw = "https://instagram.com/xxcooldude99"
    profiles = profiles_from_tool_results([_Result("search_footprint", {"raw": raw})], _subject())
    insta = next(p for p in profiles if p.platform == "Instagram")
    assert insta.confidence <= 0.2 and insta.status == "unconfirmed"


def test_required_platforms_always_reported_even_when_not_found():
    raw = "Found: https://instagram.com/ali.raza"
    profiles = profiles_from_tool_results([_Result("search_username", {"raw": raw})], _subject())
    report = platform_report(profiles, ["03001234567"], brightdata_ready=False)
    required = [r for r in report if r.required]
    # all 7 core platforms present, in fixed order, whether found or not
    assert [r.platform for r in required] == list(REQUIRED_PLATFORMS)
    insta = next(r for r in report if r.platform == "Instagram")
    assert insta.found
    twitter = next(r for r in report if r.platform == "Twitter/X")
    assert not twitter.found and "Bright Data" in twitter.note
    wa = next(r for r in report if r.platform == "WhatsApp")
    assert wa.found and "reachable" in wa.note  # from the number


def test_phone_channels_are_reach_not_accounts():
    chans = phone_channels(["03001234567"])
    apps = {c.platform for c in chans}
    assert {"WhatsApp", "Telegram", "Signal"} <= apps
    wa = next(c for c in chans if c.platform == "WhatsApp")
    assert wa.status == "channel" and wa.url == "https://wa.me/923001234567"
    assert wa.confidence <= 0.2
