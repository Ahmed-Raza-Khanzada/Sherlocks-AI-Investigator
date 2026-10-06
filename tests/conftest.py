"""Shared fixtures.

Nothing in this suite touches Postgres, Ollama or the public internet. Every test that
would otherwise need one of those uses a stub, so the suite runs on a laptop with no
services up - which is the only way it will actually get run.
"""

from __future__ import annotations

import pytest

from sherlocks.settings import Settings


@pytest.fixture
def settings() -> Settings:
    """OSINT on, but only the one tool that makes no network request.

    This is load-bearing. ``sherlock`` queries 300+ sites and ``holehe`` hundreds of
    signup endpoints; a test that reaches either is slow, flaky, and - since these are
    real requests about a real-looking person - not something a test suite should be
    doing at all. Tests needing another tool widen the allowlist themselves, and must
    stub the execution.
    """
    s = Settings()
    s.osint.enabled = True
    s.osint.allowed_tools = ["generate_dorks"]
    s.ollama.enabled = False  # deterministic paths only, unless a test opts in
    return s


@pytest.fixture
def offline_settings() -> Settings:
    s = Settings()
    s.osint.enabled = False
    s.ollama.enabled = False
    return s


@pytest.fixture(autouse=True)
def _fake_ems_endpoints(monkeypatch):
    """EMS endpoints and keys come from .env, which is not in the repository. Tests use
    these stand-ins - same URL paths the adapters are matched on, a fake host, dummy
    keys - so they run identically on a laptop with a real .env and on CI without one."""
    from sherlocks.linkgraph import ems

    host = "https://ems.test"
    fake = {
        "cro_url": f"{host}/crodashboard/api/SindhAPI/GetDataByCnic",
        "arms_url": f"{host}/api/external/get-profile-by-cnic",
        "psrms_url": f"{host}/restapi/Criminal_api/personsearch",
        "psrms_check_url": f"{host}/restapi/Criminal_api/checkperson",
        "cfms_url": f"{host}/service/person-status/test-key",
        "watchlist_url": f"{host}/api/suspect-info",
        "hotel_cnic_url": f"{host}/api/findGuest",
        "hotel_mobile_url": f"{host}/FNSCController/findGuestSimple",
        "sbvs_base": host, "prvs_base": host, "tenant_base": host,
        "evs_url": f"{host}/api/client/ipost-cnic-search",
        "hope_emp_url": f"{host}/api/client/hope-employee-cnic-search",
        "hope_empr_url": f"{host}/api/client/hope_employer_cnic_search",
        "dls_login_url": f"{host}/auth/login", "dls_api": f"{host}/api",
        "igp_url": f"{host}/api/complaint-details",
        "pfc_url": f"{host}/api/citizen/icop-complaints",
        "hrmis_url": f"{host}/api/fir/officer_data/",
        "milap_url": f"{host}/api/lost-records",
        "subscriber_url": f"{host}/apis/number_check.php",
        "nadra_url": f"{host}/api/admin/test",
        "excise_url": f"{host}/GetVehicleInquiry/getVehicleInfo",
        "tracs_url": f"{host}/api/v1/sindh-police/challans",
        "avlc_url": f"{host}/api/icop",
        "psrms_fir_url": f"{host}/restapi/Criminal_api/firfilereport",
        "labs_url": f"{host}/api/v1/show-reports",
        "safe_cro_url": f"{host}/api/cro-report-pdf",
        "hotel_person_url": "",   # the Hotel Eye person API is off unless a test turns it on
    }
    for key in ems.CONF:
        monkeypatch.setitem(ems.CONF, key, fake.get(key, f"test-{key}"))


@pytest.fixture(autouse=True)
def _no_real_web_search(monkeypatch):
    """The free name search talks to DuckDuckGo and Bing. No test may: a test that needs
    it swaps in a fake session or patches ``find_profiles``."""
    import requests

    from sherlocks.linkgraph import websearch

    real_get = requests.Session.get

    def guarded(self, url, *args, **kwargs):
        if any(host in str(url) for host in ("duckduckgo.com", "bing.com")):
            raise AssertionError(f"test tried to reach a real search engine: {url}")
        return real_get(self, url, *args, **kwargs)

    monkeypatch.setattr(requests.Session, "get", guarded)
    monkeypatch.setattr(websearch.FreeWebSearch, "_rested_until", {})
