from __future__ import annotations

import pytest

from sherlocks.osint.models import IdentifierKind
from sherlocks.osint.tools import TOOL_CATALOG, TOOLS_BY_KIND, tools_for_kinds


def test_cnic_drives_no_tool() -> None:
    """The single most important assertion in this file.

    If a CNIC ever maps to a tool, the product starts claiming it searched for someone
    when it searched for nothing.
    """
    assert IdentifierKind.CNIC not in TOOLS_BY_KIND
    assert tools_for_kinds({IdentifierKind.CNIC}) == []


def test_every_mapped_tool_exists_in_the_catalogue() -> None:
    for kind, names in TOOLS_BY_KIND.items():
        for name in names:
            assert name in TOOL_CATALOG, f"{kind} maps to unknown tool {name}"


def test_tools_for_kinds_deduplicates_and_keeps_order() -> None:
    result = tools_for_kinds({IdentifierKind.PHONE, IdentifierKind.EMAIL})
    # phone tools first (set order is unstable, so assert membership + no duplicates)
    phone = TOOLS_BY_KIND[IdentifierKind.PHONE]
    assert result[: len(phone)] == phone
    assert {"search_email", "search_breach", "search_github", "search_paste"} <= set(result)
    assert len(result) == len(set(result))


def test_generate_dorks_is_marked_as_producing_no_findings() -> None:
    # It reformulates its input into URLs; mining it would report the query as a result.
    assert TOOL_CATALOG["generate_dorks"].yields_findings is False
    assert all(
        spec.yields_findings for name, spec in TOOL_CATALOG.items() if name != "generate_dorks"
    )


def test_social_and_apify_tools_are_registered():
    from sherlocks.osint.models import IdentifierKind
    from sherlocks.osint.tools import TOOL_CATALOG, TOOLS_BY_KIND

    assert TOOL_CATALOG["search_social"].requires_package == "social_analyzer"
    assert TOOL_CATALOG["search_social"].module == "sherlocks.osint.custom_tools"
    assert TOOL_CATALOG["search_apify"].api_key_setting == "apify_token"
    assert TOOL_CATALOG["search_apify"].requires_key is True
    assert {"search_social", "search_apify"} <= set(TOOLS_BY_KIND[IdentifierKind.USERNAME])


def test_apify_skips_without_token_and_social_skips_without_package():
    from sherlocks.osint.client import OsintClient
    from sherlocks.settings import Settings

    s = Settings()
    s.osint.enabled = True
    s.osint.allowed_tools = ["search_apify", "search_social"]
    c = OsintClient(s.osint)
    assert "apify_token" in (c.skip_reason("search_apify") or "")
    # social skips only when the package is absent; assert the reason names it either way
    reason = c.skip_reason("search_social")
    assert reason is None or "social_analyzer" in reason


def test_apify_tool_needs_no_token_returns_message():
    import asyncio

    from sherlocks.osint.custom_tools import run_apify_osint

    out = asyncio.run(run_apify_osint("someuser", api_key=None))
    assert "no APIFY_TOKEN" in out


# -- paid person-data APIs --------------------------------------------------------------

PAID = {
    "search_socialcrawl": ("socialcrawl_api_key", IdentifierKind.USERNAME),
    "search_pipl": ("pipl_api_key", IdentifierKind.PHONE),
    "search_pipl_email": ("pipl_api_key", IdentifierKind.EMAIL),
    "search_fullcontact": ("fullcontact_api_key", IdentifierKind.EMAIL),
    "search_fullcontact_phone": ("fullcontact_api_key", IdentifierKind.PHONE),
    "search_hunter": ("hunter_api_key", IdentifierKind.EMAIL),
    "search_enformion": ("enformion_api_key", IdentifierKind.PHONE),
}


def test_paid_person_apis_are_registered_allowed_and_skipped_without_keys():
    from sherlocks.osint.client import OsintClient
    from sherlocks.settings import load_settings

    s = load_settings()
    for name, (setting, kind) in PAID.items():
        spec = TOOL_CATALOG[name]
        assert spec.api_key_setting == setting and spec.requires_key and spec.accepts == kind
        assert name in TOOLS_BY_KIND[kind] and name in s.osint.allowed_tools
        setattr(s.osint, setting, None)
    c = OsintClient(s.osint)
    for name, (setting, _) in PAID.items():
        assert setting in (c.skip_reason(name) or ""), name


def test_enformion_also_needs_its_profile_name():
    from sherlocks.osint.client import OsintClient
    from sherlocks.settings import Settings

    s = Settings()
    s.osint.allowed_tools = ["search_enformion"]
    s.osint.enformion_api_key = "secret"
    assert "enformion_api_profile" in (OsintClient(s.osint).skip_reason("search_enformion") or "")
    s.osint.enformion_api_profile = "profile"
    assert OsintClient(s.osint).skip_reason("search_enformion") is None


def test_settings_read_the_keys_from_env(monkeypatch):
    from sherlocks.settings import load_settings

    monkeypatch.setenv("SOCIALCRAWL_API_KEY", "sc-key")
    monkeypatch.setenv("PIPL_API_KEY", "pipl-key")
    monkeypatch.setenv("SHERLOCKS_OSINT_SOCIALCRAWL_PLATFORMS", "instagram,tiktok")
    o = load_settings().osint
    assert o.socialcrawl_api_key == "sc-key" and o.pipl_api_key == "pipl-key"
    assert o.socialcrawl_platforms == ["instagram", "tiktok"]


class _Resp:
    def __init__(self, status: int, body: object) -> None:
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self) -> object:
        return self._body


def _fake_http(monkeypatch, answers: dict[str, _Resp]) -> list[dict]:
    """requests.request replaced: answers by URL substring, records every call."""
    import sherlocks.osint.custom_tools as ct

    calls: list[dict] = []

    def request(method, url, timeout=None, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return next((r for k, r in answers.items() if k in url), _Resp(404, {}))

    monkeypatch.setattr(ct.requests, "request", request)
    return calls


def test_socialcrawl_checks_each_platform_with_the_key_header(monkeypatch):
    import asyncio

    from sherlocks.osint.custom_tools import run_socialcrawl_osint

    calls = _fake_http(monkeypatch, {"/instagram/profile": _Resp(200, {"success": True, "data": {"author": {
        "username": "kamran.khi", "full_name": "Kamran Ahmed", "location": "Karachi"}}})})
    out = asyncio.run(run_socialcrawl_osint("@kamran.khi", api_key="k", platforms=["instagram", "youtube"]))
    assert "instagram: FOUND https://www.instagram.com/kamran.khi" in out and "Kamran Ahmed" in out
    assert "youtube: not found" in out
    assert calls[0]["headers"] == {"x-api-key": "k"} and calls[0]["params"] == {"handle": "kamran.khi"}
    assert calls[1]["url"].endswith("/youtube/channel")


def test_pipl_sends_pakistani_numbers_internationally_and_lists_what_it_found(monkeypatch):
    import asyncio

    from sherlocks.osint.custom_tools import run_pipl_osint

    calls = _fake_http(monkeypatch, {"api.pipl.com": _Resp(200, {"@persons_count": 1, "person": {
        "names": [{"display": "Kamran Ahmed"}], "urls": [{"url": "https://facebook.com/kamran.khi"}],
        "relationships": [{"names": [{"display": "Imran Ahmed"}]}]}})})
    out = asyncio.run(run_pipl_osint("0300-1234567", api_key="k", field="phone"))
    assert calls[0]["params"] == {"key": "k", "phone": "+923001234567"}
    assert "Kamran Ahmed" in out and "facebook.com/kamran.khi" in out and "Imran Ahmed" in out


def test_no_match_is_a_result_but_a_bad_key_is_an_error(monkeypatch):
    import asyncio

    from sherlocks.osint.custom_tools import run_fullcontact_osint, run_hunter_osint

    _fake_http(monkeypatch, {"fullcontact": _Resp(404, {})})
    assert "no person found" in asyncio.run(run_fullcontact_osint("a@b.pk", api_key="k"))
    _fake_http(monkeypatch, {"hunter.io": _Resp(401, {"errors": "bad key"})})
    with pytest.raises(RuntimeError, match="HTTP 401"):
        asyncio.run(run_hunter_osint("a@b.pk", api_key="bad"))


def test_enformion_sends_its_profile_headers(monkeypatch):
    import asyncio

    from sherlocks.osint.custom_tools import run_enformion_osint

    calls = _fake_http(monkeypatch, {"galaxysearchapi": _Resp(200, {"persons": [{"name": {"firstName": "John"}}]})})
    out = asyncio.run(run_enformion_osint("2125550100", api_key="pw", profile="prof"))
    headers = calls[0]["headers"]
    assert headers["galaxy-ap-name"] == "prof" and headers["galaxy-ap-password"] == "pw"
    assert headers["galaxy-search-type"] == "PersonSearch" and "John" in out
