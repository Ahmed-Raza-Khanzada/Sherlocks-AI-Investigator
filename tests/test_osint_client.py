"""Tests for the OpenOSINT bridge.

The classification tests use OpenOSINT's real message strings, copied from its source.
They are the contract this module depends on, and they are the thing most likely to
break silently on an upgrade - hence the coverage.
"""

from __future__ import annotations

import pytest

from sherlocks.osint.client import (
    OsintClient,
    OsintDisabled,
    ToolNotAllowed,
    classify_output,
)
from sherlocks.osint.models import FindingStatus, PlannedTool
from sherlocks.settings import Settings


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", FindingStatus.NO_RESULT),
        ("   \n ", FindingStatus.NO_RESULT),
        ("No data found for phone number '+923001234567'.", FindingStatus.NO_RESULT),
        ("No accounts found for username 'aliraza'.", FindingStatus.NO_RESULT),
        ("No registered services found for a@b.com.", FindingStatus.NO_RESULT),
        ("[Web Unlocker] URL: https://x\n\n(empty response body)", FindingStatus.NO_RESULT),
        ("Internal error: boom", FindingStatus.ERROR),
        ("Scan error: something broke", FindingStatus.ERROR),
        ("Invalid URL: must start with http:// or https://", FindingStatus.ERROR),
        ("Scan error: 'sherlock' scan timed out after 180s.", FindingStatus.ERROR),
        (
            "Scan error: BRIGHTDATA_API_KEY environment variable is not set.",
            FindingStatus.NOT_CONFIGURED,
        ),
        (
            "Scan error: BRIGHTDATA_SERP_ZONE environment variable is not set.",
            FindingStatus.NOT_CONFIGURED,
        ),
        (
            "Scan error: 'holehe' is not installed or not in PATH. Install it with: pip install holehe",
            FindingStatus.NOT_CONFIGURED,
        ),
        ("OSINT results for username 'x':\n\n[+] github", FindingStatus.OK),
    ],
)
def test_output_classification(text: str, expected: FindingStatus) -> None:
    status, _error = classify_output(text)
    assert status is expected


def test_a_missing_prerequisite_is_never_reported_as_an_error() -> None:
    """An unset key is an operator problem. Calling it an error would send an analyst
    looking for a fault in the subject's data."""
    status, error = classify_output(
        "Scan error: BRIGHTDATA_API_KEY environment variable is not set."
    )
    assert status is FindingStatus.NOT_CONFIGURED
    assert error is not None


def test_disabled_osint_refuses_to_run() -> None:
    settings = Settings()
    settings.osint.enabled = False
    with pytest.raises(OsintDisabled):
        OsintClient(settings).run_tool("generate_dorks", "Ali Raza")


def test_tool_outside_the_allowlist_is_refused(settings: Settings) -> None:
    settings.osint.allowed_tools = ["generate_dorks"]
    client = OsintClient(settings)
    with pytest.raises(ToolNotAllowed):
        client.run_tool("search_footprint", "Ali Raza")


def test_unknown_tool_is_refused(settings: Settings) -> None:
    with pytest.raises(ToolNotAllowed):
        OsintClient(settings).run_tool("search_shodan", "1.1.1.1")


def test_brightdata_tools_report_a_reason_rather_than_running(settings: Settings) -> None:
    settings.osint.allowed_tools = ["generate_dorks", "search_footprint"]
    settings.osint.brightdata_api_key = None
    client = OsintClient(settings)
    assert client.skip_reason("search_footprint") == (
        "no Bright Data API key and SERP zone are configured"
    )


def test_empty_query_is_skipped_not_dispatched(settings: Settings) -> None:
    result = OsintClient(settings).run_tool("generate_dorks", "   ")
    assert result.status is FindingStatus.SKIPPED
    assert result.duration_ms == 0


def test_run_budget_is_enforced(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.osint.max_tools_per_run = 2
    client = OsintClient(settings, isolate=False)
    calls: list[str] = []

    def fake_run_tool(tool: str, query: str):
        calls.append(tool)
        from sherlocks.osint.models import ToolResult

        return ToolResult(tool=tool, query=query, status=FindingStatus.OK)

    monkeypatch.setattr(client, "run_tool", fake_run_tool)
    planned = [PlannedTool(tool="generate_dorks", query=f"q{i}") for i in range(5)]
    results = client.run_planned(planned)

    assert len(calls) == 2
    assert [r.status for r in results[2:]] == [FindingStatus.SKIPPED] * 3
    assert "budget" in results[2].summary


def test_unrunnable_planned_tools_appear_in_the_results(settings: Settings) -> None:
    """A tool that could not run must never be silently dropped - an omitted scan reads
    exactly like a scan that found nothing."""
    client = OsintClient(settings, isolate=False)
    results = client.run_planned(
        [PlannedTool(tool="search_footprint", query="Ali", skip_reason="no key")]
    )
    assert len(results) == 1
    assert results[0].status is FindingStatus.NOT_CONFIGURED
    assert results[0].error == "no key"


def test_timeout_is_bounded_by_the_configured_ceiling(settings: Settings) -> None:
    from sherlocks.osint.tools import TOOL_CATALOG

    settings.osint.per_tool_timeout_seconds = 30
    client = OsintClient(settings)
    # search_username's own default is 180s; configuration must win.
    assert client._timeout_for(TOOL_CATALOG["search_username"]) == 30


def test_generate_dorks_is_not_given_a_timeout_kwarg(settings: Settings) -> None:
    """Its signature is ``run_dork_osint(target)``. Passing timeout_seconds is a
    TypeError, and it would surface as a mysterious 'Internal error'."""
    from sherlocks.osint.tools import TOOL_CATALOG

    kwargs = OsintClient(settings)._tool_kwargs(TOOL_CATALOG["generate_dorks"], 15)
    assert "timeout_seconds" not in kwargs


def test_brightdata_keys_are_passed_explicitly_not_via_environment(settings: Settings) -> None:
    settings.osint.brightdata_api_key = "key-123"
    settings.osint.brightdata_serp_zone = "serp1"
    from sherlocks.osint.tools import TOOL_CATALOG

    client = OsintClient(settings)
    kwargs = client._tool_kwargs(TOOL_CATALOG["search_footprint"], 60)
    assert kwargs["api_keys"]["BRIGHTDATA_API_KEY"] == "key-123"
    import os

    assert "BRIGHTDATA_API_KEY" not in os.environ


def test_generate_dorks_runs_for_real_in_process(settings: Settings) -> None:
    """The one catalogued tool that makes no network call, so it is safe in a test."""
    result = OsintClient(settings, isolate=False).run_tool("generate_dorks", "Ali Raza Karachi")
    assert result.status is FindingStatus.OK
    assert "google.com/search" in (result.data or {})["raw"]


def test_generate_dorks_runs_in_an_isolated_process(settings: Settings) -> None:
    result = OsintClient(settings, isolate=True).run_tool("generate_dorks", "Ali Raza Karachi")
    assert result.status is FindingStatus.OK
    assert "google.com/search" in (result.data or {})["raw"]
