"""Comparing two selected people: every kind of connection, each with its source."""

from __future__ import annotations

import pytest

from sherlocks.linkgraph.compare import compare_people, explain_with_llm, rule_explanation
from sherlocks.linkgraph.models import GraphRunParams
from sherlocks.render._reportlib import _ensure_on_path
from sherlocks.settings import load_settings


@pytest.fixture(scope="module")
def demo():
    s = load_settings()
    _ensure_on_path(s.app.report_app_src)
    pytest.importorskip("cdr_report_app.integrations.providers")
    s.ollama.enabled = False
    s.llm.enabled = False
    from sherlocks.linkgraph.runs import memory_manager

    mgr = memory_manager(s)
    handle = mgr.start(GraphRunParams(cnic="99999-0000001-1", phone="0399-0000101", depth=3,
                                      max_persons=30, backend="demo"), wait=True)
    run = mgr.get(handle.id)
    people = {n["label"]: n["id"] for n in run["graph"]["nodes"] if n["kind"] == "person"}
    return mgr, run, people


def test_directly_linked_pair_names_relation_and_system(demo):
    _, run, p = demo
    c = compare_people(run["graph"], p["Kamran Ahmed"], p["Tariq Hussain"])
    assert c["strength"] == "strong" and "Old Tenant" in c["verdict"]
    assert any(d["relation"] == "Landlord" and d["via"] == "Old Tenant" for d in c["direct"])
    # the same statement from two records is listed once
    keys = [(d["from"], d["to"], d["relation"], d["via"], d["detail"]) for d in c["direct"]]
    assert len(keys) == len(set(keys))


def test_indirect_pair_gets_a_stated_route(demo):
    _, run, p = demo
    c = compare_people(run["graph"], p["Asif Ali"], p["Bilal Khan"])
    assert not c["direct"] and c["route"]["hops"] and not c["route"]["inferred"]
    assert c["route"]["nodes"][0] == p["Asif Ali"] and c["route"]["nodes"][-1] == p["Bilal Khan"]
    assert all(h["via"] for h in c["route"]["hops"])      # every step cites a system


def test_shared_details_are_found_even_beside_links(demo):
    _, run, p = demo
    c = compare_people(run["graph"], p["Kamran Ahmed"], p["Imran Ahmed"])
    kinds = {s["kind"] for s in c["shared"]}
    assert {"Same mobile number", "Same address", "Same father's name"} <= kinds
    text = rule_explanation(c)
    assert "Kamran Ahmed" in text and "Imran Ahmed" in text


def test_mutual_contacts_listed_with_both_relations(demo):
    _, run, p = demo
    c = compare_people(run["graph"], p["Kamran Ahmed"], p["Tariq Hussain"])
    assert c["mutual"] and all(m["to_a"]["relation"] and m["to_b"]["relation"] for m in c["mutual"])


def test_same_person_or_unknown_is_rejected(demo):
    _, run, p = demo
    assert compare_people(run["graph"], p["Asif Ali"], p["Asif Ali"]) is None
    assert compare_people(run["graph"], p["Asif Ali"], "nope") is None


def test_ai_explanation_is_optional_and_grounded(demo):
    mgr, run, p = demo
    c = mgr.compare(run["graph"], p["Asif Ali"], p["Bilal Khan"], explain=True)   # no LLM configured
    assert c["explanation"]["model"] is None and c["explanation"]["summary"]    # rule text stands in

    class _Llm:
        model = "test-model"

        def generate_structured(self, *, prompt, schema, **_):
            assert "Asif Ali" in prompt and "Bilal Khan" in prompt
            return schema(summary="Linked via Kamran Ahmed [Old Tenant] [FIR Roster].",
                          key_points=["Tenant [Old Tenant]"], next_steps=["Pull FIR 112/2024"]), None

    out = explain_with_llm(c, _Llm())
    assert out["model"] == "test-model" and "[Old Tenant]" in out["summary"]


def test_compare_endpoint(demo):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sherlocks.api.linkgraph import build_router

    mgr, run, p = demo
    s = mgr.settings
    app = FastAPI()
    app.include_router(build_router(s, lambda: mgr))
    client = TestClient(app)
    url = f"{s.api.prefix}/graph/runs/{run['id']}/compare"
    ok = client.get(url, params={"a": p["Asif Ali"], "b": p["Bilal Khan"]})
    assert ok.status_code == 200 and ok.json()["route"]["hops"]
    assert client.get(url, params={"a": p["Asif Ali"], "b": p["Asif Ali"]}).status_code == 404


def test_multiple_people_relations(demo):
    """Multi-seed graph: every pair of chosen people related, sourced; AI reads the group."""
    from sherlocks.linkgraph.compare import explain_group, group_relations

    _, run, p = demo
    graph = run["graph"]
    ids = [p["Kamran Ahmed"], p["Tariq Hussain"], p["Bilal Khan"]]
    g = group_relations(graph, ids)
    assert [x["id"] for x in g["people"]] == ids
    assert len(g["pairs"]) == 3                                   # every unordered pair
    km_th = next(x for x in g["pairs"] if {x["a"], x["b"]} == {p["Kamran Ahmed"], p["Tariq Hussain"]})
    assert km_th["strength"] == "strong"
    assert any("landlord of" in s or "tenant of" in s for s in km_th["sentences"])

    class _Llm:
        model = "test-model"

        def generate_structured(self, *, prompt, schema, **_):
            assert "Kamran" in prompt and "Bilal" in prompt
            return schema(overview="They form a group.", findings=["x [FIR Roster]"], next_steps=[]), None

    out = explain_group(g, _Llm())
    assert out["model"] == "test-model" and out["overview"]
    assert explain_group(g, None) is None


def test_multi_seed_run_marks_all_seeds(demo):
    """A seeds=[...] run searches each into one graph and marks them all as seeds."""
    from sherlocks.linkgraph.models import GraphRunParams, Seed

    mgr, _, _ = demo
    handle = mgr.start(GraphRunParams(
        seeds=[Seed(cnic="99999-0000001-1"), Seed(cnic="99999-0000003-7")], depth=1, backend="demo"), wait=True)
    run = mgr.get(handle.id)
    assert run["status"] == "completed"
    seeds = [n["data"]["name"] for n in run["graph"]["nodes"] if n["kind"] == "person" and n["data"].get("seed")]
    assert "Kamran Ahmed" in seeds and "Tariq Hussain" in seeds and len(seeds) == 2
    g = mgr.group(mgr.graph_for(handle.id))
    assert len(g["pairs"]) == 1
