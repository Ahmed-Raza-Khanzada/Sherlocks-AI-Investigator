"""Free name search: engine parsing, throttling fallback, and OSINT for everyone found."""

from __future__ import annotations

import base64

import pytest

from sherlocks.linkgraph import websearch
from sherlocks.linkgraph.websearch import (
    FreeWebSearch,
    combined_query,
    parse_bing,
    parse_duckduckgo,
)

DDG_PAGE = """
<div class="result results_links results_links_deep web-result ">
  <div class="links_main"><h2 class="result__title">
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.facebook.com%2Fasif.khan.4421%2F&amp;rut=x">Asif Khan - Facebook</a>
  </h2><div class="result__extras"><div></div></div>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x"><b>Asif Khan</b>. Lives in Karachi. Works at Gulshan Traders.</a>
  </div>
</div>
<div class="result results_links results_links_deep web-result ">
  <h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fnews">News</a></h2>
  <a class="result__snippet" href="#">not a profile</a>
</div>
"""


def _bing_href(url: str) -> str:
    token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&amp;&amp;p=abc&amp;u=a1{token}&amp;ntb=1"


BING_PAGE = f"""
<li class="b_algo"><h2 class=""><a target="_blank" href="{_bing_href('https://www.instagram.com/asifkhan_khi/')}">Asif Khan (@asifkhan_khi)</a></h2>
<div class="b_caption"><p class="b_lineclamp2">Asif Khan, Block 13-D Gulshan-e-Iqbal, Karachi</p></div></li>
"""


def test_duckduckgo_results_are_unwrapped_with_snippets():
    rows = parse_duckduckgo(DDG_PAGE)
    assert rows[0][0] == "https://www.facebook.com/asif.khan.4421/"
    assert "Gulshan Traders" in rows[0][2]
    assert rows[1][0] == "https://example.com/news"


def test_bing_results_are_unwrapped_with_snippets():
    rows = parse_bing(BING_PAGE)
    assert rows == [("https://www.instagram.com/asifkhan_khi/", "Asif Khan (@asifkhan_khi)",
                     "Asif Khan, Block 13-D Gulshan-e-Iqbal, Karachi")]


def test_combined_query_covers_the_required_platforms():
    q = combined_query("Asif Khan", "karachi")
    assert q.startswith('"Asif Khan" Karachi (')
    for site in ("facebook.com", "instagram.com", "x.com", "youtube.com", "snapchat.com", "t.me"):
        assert f"site:{site}" in q


class _Resp:
    def __init__(self, status: int, text: str, url: str = "https://engine/") -> None:
        self.status_code, self.text, self.url = status, text, url


class _Session:
    """DuckDuckGo throttles (HTTP 202); Bing answers."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        if "duckduckgo" in url:
            return _Resp(202, "<html>anomaly-modal</html>")
        return _Resp(200, BING_PAGE)


def test_throttled_engine_is_rested_and_bing_takes_over(monkeypatch):
    monkeypatch.setattr(websearch.time, "sleep", lambda s: None)
    session = _Session()
    run = FreeWebSearch(session=session).find_profiles("Asif Khan", "karachi", max_queries=3)
    assert run.engines == ["bing"] and not run.stopped
    assert [h.platform for h in run.hits] == ["Instagram"]
    # DuckDuckGo was asked once, then rested - not hammered on every follow-up query
    assert sum("duckduckgo" in c for c in session.calls) == 1


def test_every_engine_throttling_stops_the_search(monkeypatch):
    monkeypatch.setattr(websearch.time, "sleep", lambda s: None)

    class _AllThrottle(_Session):
        def get(self, url, params=None, headers=None, timeout=None):
            return _Resp(429, "")

    run = FreeWebSearch(session=_AllThrottle()).find_profiles("Asif Khan", "karachi")
    assert run.queries == 1 and not run.hits and "throttling" in run.stopped


# -- engine: OSINT for everyone found ---------------------------------------------------


@pytest.fixture
def osint_run(monkeypatch):
    from sherlocks.linkgraph import engine as engine_mod
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.linkgraph.websearch import WebHit, WebSearchRun
    from sherlocks.render._reportlib import _ensure_on_path
    from sherlocks.settings import load_settings

    s = load_settings()
    _ensure_on_path(s.app.report_app_src)
    pytest.importorskip("cdr_report_app.integrations.providers")
    s.ollama.enabled = False
    s.llm.enabled = False
    s.osint.enabled = True
    s.osint.free_web_search = True

    searched: list[str] = []

    class _FakeWeb:
        def find_profiles(self, name, city=None, *, max_queries=3):
            searched.append(name)
            hits = []
            if name == "Tariq Hussain":
                hits.append(WebHit("Facebook", "https://www.facebook.com/tariq.hussain.khi", "Tariq Hussain - Facebook",
                                   "Tariq Hussain lives in Gulshan-e-Iqbal, Karachi", "q", "bing"))
            return WebSearchRun(hits=hits, queries=1, engines=["bing"])

    class _FakeAgent:
        def plan(self, subject):
            return type("Plan", (), {"tools": []})()

        def extract_identifiers(self, results, subject):
            return []

    monkeypatch.setattr(engine_mod.Expansion, "_web_search", lambda self: _FakeWeb())
    monkeypatch.setattr("sherlocks.services.osint_service.build_agent", lambda settings: _FakeAgent())
    mgr = memory_manager(s)
    handle = mgr.start(GraphRunParams(cnic="99999-0000001-1", phone="0399-0000101", depth=2, backend="demo",
                                      include_osint=True, osint_scope="everyone", max_osint_people=5), wait=True)
    return mgr.get(handle.id), searched


def test_osint_everyone_searches_found_people_by_name(osint_run):
    run, searched = osint_run
    assert run["status"] == "completed"
    assert searched[0] == "Kamran Ahmed"                 # the subject first
    assert 1 < len(searched) <= 5 and "Tariq Hussain" in searched
    tariq = next(n for n in run["graph"]["nodes"] if n["kind"] == "person" and n["label"] == "Tariq Hussain")
    osint = next(r for r in tariq["data"]["records"] if r["system"] == "osint")
    record = next(n for n in run["graph"]["nodes"] if n["id"] == osint["node"])
    fields = {f["label"]: f["value"] for f in record["data"]["fields"]}
    assert "facebook.com/tariq.hussain.khi" in fields["Facebook"]
    assert "free name search" in fields["Searched"]
