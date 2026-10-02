"""The surface a host portal (Laravel) integrates against: host-issued tokens, the live
stream, and analysis of a graph the host saved and posts back."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sherlocks.api.auth import SessionAuth
from sherlocks.linkgraph.models import GraphRunParams
from sherlocks.linkgraph.stream import GraphDiffer
from sherlocks.render._reportlib import _ensure_on_path
from sherlocks.settings import load_settings

SECRET = "shared-with-laravel"


def host_token(user: str, *, secret: str = SECRET, ttl: int = 900) -> str:
    """What the Laravel side does, written independently of SessionAuth.issue (the PHP
    snippet in docs/INTEGRATION.md is this, line for line)."""
    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    payload = json.dumps({"u": user, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode()
    return f"{b64(payload)}.{b64(hmac.new(secret.encode(), payload, hashlib.sha256).digest())}"


def apply(state: dict, delta: dict) -> None:
    """The client side of a ``graph`` event, as the portal applies it."""
    if delta["full"]:
        state["nodes"], state["edges"] = {}, {}
    for kind in ("nodes", "edges"):
        for item in delta[kind]:
            state[kind][item["id"]] = item
        for gone in delta[f"removed_{kind}"]:
            state[kind].pop(gone, None)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.split("\n\n"):
        lines = [x for x in block.splitlines() if x and not x.startswith(":")]
        if not lines:
            continue
        event = next(x[7:] for x in lines if x.startswith("event: "))
        data = json.loads("".join(x[6:] for x in lines if x.startswith("data: ")))
        out.append((event, data))
    return out


# -- the differ -------------------------------------------------------------------------


def test_differ_sends_everything_first_then_only_changes():
    d = GraphDiffer()
    g1 = {"version": 1, "nodes": [{"id": "p1", "label": "A"}, {"id": "p2", "label": "B"}],
          "edges": [{"id": "e1", "source": "p1", "target": "p2"}]}
    first = d.diff(g1)
    assert first["full"] and len(first["nodes"]) == 2 and len(first["edges"]) == 1
    assert d.diff(g1) is None                                   # nothing changed
    g2 = {"version": 2, "nodes": [{"id": "p1", "label": "A Khan"}, {"id": "p3", "label": "C"}],
          "edges": []}
    second = d.diff(g2)
    assert not second["full"]
    assert {n["id"] for n in second["nodes"]} == {"p1", "p3"}  # changed + new
    assert second["removed_nodes"] == ["p2"] and second["removed_edges"] == ["e1"]


# -- host-issued tokens -----------------------------------------------------------------


def _auth_settings():
    s = load_settings()
    s.api.api_key = None
    s.auth.enabled = True
    s.auth.password = None
    s.auth.password_sha256 = None
    s.auth.secret = SECRET
    return s


def test_host_token_verifies_without_a_local_password():
    a = SessionAuth(_auth_settings())
    assert a.enabled is False and a.verifies_tokens is True
    assert a.verify(host_token("officer.42")) == "officer.42"


def test_host_token_signed_with_another_secret_is_refused():
    from sherlocks.api.auth import AuthError

    with pytest.raises(AuthError):
        SessionAuth(_auth_settings()).verify(host_token("x", secret="guess"))


def test_graph_routes_refuse_requests_without_a_host_token():
    from sherlocks.api.linkgraph import build_router

    s = _auth_settings()
    app = FastAPI()
    app.include_router(build_router(s, lambda: None, SessionAuth(s)))
    client = TestClient(app)
    url = f"{s.api.prefix}/graph/analyze/ask"
    assert client.post(url, json={"graph": {"nodes": [], "edges": []}}).status_code == 401


# -- stream and analysis over a real demo run -------------------------------------------


@pytest.fixture(scope="module")
def demo():
    s = load_settings()
    _ensure_on_path(s.app.report_app_src)
    pytest.importorskip("cdr_report_app.integrations.providers")
    s.ollama.enabled = False
    s.llm.enabled = False
    s.osint.enabled = False
    s.api.api_key = None
    s.auth.enabled = True
    s.auth.password = None
    s.auth.password_sha256 = None
    s.auth.secret = SECRET
    from sherlocks.api.linkgraph import build_router
    from sherlocks.linkgraph.runs import memory_manager

    mgr = memory_manager(s)
    app = FastAPI()
    app.include_router(build_router(s, lambda: mgr, SessionAuth(s)))
    return mgr, TestClient(app), s.api.prefix


def test_run_records_the_host_user_and_streams_to_the_same_graph(demo):
    mgr, client, prefix = demo
    token = host_token("officer.42")
    created = client.post(f"{prefix}/graph/runs", headers={"X-Session-Token": token},
                          json={"cnic": "99999-0000001-1", "depth": 2, "max_persons": 15, "backend": "demo"})
    assert created.status_code == 202
    run_id = created.json()["run_id"]

    # EventSource cannot send headers, so the token rides in the query string.
    with client.stream("GET", f"{prefix}/graph/runs/{run_id}/stream", params={"token": token}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        events = parse_sse(r.read().decode())

    kinds = [e for e, _ in events]
    assert kinds[-1] == "done" and "graph" in kinds and "status" in kinds and "log" in kinds
    state: dict = {"nodes": {}, "edges": {}}
    for event, data in events:
        if event == "graph":
            apply(state, data)
    final = mgr.get(run_id)
    assert final["status"] == "completed" and final["created_by"] == "officer.42"
    # Applying the deltas in order rebuilds exactly the finished graph.
    assert set(state["nodes"]) == {n["id"] for n in final["graph"]["nodes"]}
    assert set(state["edges"]) == {e["id"] for e in final["graph"]["edges"]}


def test_stream_of_unknown_run_is_404(demo):
    _, client, prefix = demo
    r = client.get(f"{prefix}/graph/runs/nope/stream", params={"token": host_token("x")})
    assert r.status_code == 404


def test_saved_graph_can_be_analysed_without_the_run(demo):
    """The host keeps the graph; Sherlocks answers about it from the posted copy."""
    mgr, client, prefix = demo
    handle = mgr.start(GraphRunParams(cnic="99999-0000001-1", phone="0399-0000101", depth=3,
                                      max_persons=30, backend="demo"), wait=True)
    saved = json.loads(json.dumps(mgr.get(handle.id)["graph"]))   # as the host stored it
    people = {n["label"]: n["id"] for n in saved["nodes"] if n["kind"] == "person"}
    headers = {"X-Session-Token": host_token("officer.42")}
    url = f"{prefix}/graph/analyze"

    ask = client.post(f"{url}/ask", headers=headers, json={
        "graph": saved, "question": "who is the landlord?", "history": [{"q": "hi", "a": "hello"}]})
    assert ask.status_code == 200 and "answer" in ask.json()

    cmp = client.post(f"{url}/compare", headers=headers,
                      json={"graph": saved, "a": people["Kamran Ahmed"], "b": people["Tariq Hussain"]})
    assert cmp.status_code == 200 and cmp.json()["strength"] == "strong"

    brief = client.post(f"{url}/brief", headers=headers, json={"graph": saved, "pid": people["Kamran Ahmed"]})
    assert brief.status_code == 200 and brief.json()["narrative"]

    conn = client.post(f"{url}/connection", headers=headers,
                       json={"graph": saved, "a": people["Asif Ali"], "b": people["Bilal Khan"]})
    assert conn.status_code == 200 and conn.json()["hops"]

    assert client.post(f"{url}/relations", headers=headers, json={"graph": saved}).status_code == 200
    # the same analysis by run id, for a run this server still holds
    by_id = client.post(f"{url}/compare", headers=headers, json={
        "run_id": handle.id, "a": people["Kamran Ahmed"], "b": people["Tariq Hussain"]})
    assert by_id.json()["verdict"] == cmp.json()["verdict"]


def test_analyze_validates_its_input(demo):
    _, client, prefix = demo
    headers = {"X-Session-Token": host_token("x")}
    url = f"{prefix}/graph/analyze"
    assert client.post(f"{url}/ask", headers=headers, json={}).status_code == 422
    assert client.post(f"{url}/ask", headers=headers, json={"graph": {"nodes": "x"}}).status_code == 422
    assert client.post(f"{url}/compare", headers=headers,
                       json={"graph": {"nodes": [], "edges": []}}).status_code == 422
    assert client.post(f"{url}/nonsense", headers=headers, json={}).status_code == 422
