from __future__ import annotations

from sherlocks.settings import Settings


def test_osint_is_off_by_default() -> None:
    """Reaching the public internet on behalf of an investigation is a policy decision.
    It must never become on-by-default through a refactor."""
    assert Settings().osint.enabled is False


def test_brightdata_readiness_needs_both_a_key_and_a_zone() -> None:
    settings = Settings()
    settings.osint.brightdata_api_key = "key"
    assert settings.osint.brightdata_ready is False
    settings.osint.brightdata_serp_zone = "serp1"
    assert settings.osint.brightdata_ready is True


def test_the_allowlist_holds_only_people_facing_tools() -> None:
    from sherlocks.osint.tools import TOOL_CATALOG

    for tool in Settings().osint.allowed_tools:
        assert tool in TOOL_CATALOG


def test_ollama_readiness_tracks_the_enabled_flag() -> None:
    settings = Settings()
    assert settings.ollama.ready is True
    settings.ollama.enabled = False
    assert settings.ollama.ready is False
