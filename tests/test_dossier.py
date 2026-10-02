"""Per-person intelligence brief: sourced facts + rule narrative (LLM off)."""

from __future__ import annotations

import pytest

from sherlocks.linkgraph.dossier import build_person_brief, person_facts
from sherlocks.linkgraph.models import GraphRunParams
from sherlocks.render._reportlib import _ensure_on_path
from sherlocks.settings import load_settings


@pytest.fixture(scope="module")
def demo_run():
    s = load_settings()
    _ensure_on_path(s.app.report_app_src)
    pytest.importorskip("cdr_report_app.integrations.providers")
    s.ollama.enabled = False
    s.llm.enabled = False
    from sherlocks.linkgraph.runs import memory_manager

    mgr = memory_manager(s)
    handle = mgr.start(GraphRunParams(cnic="99999-0000001-1", phone="0399-0000101", depth=2, backend="demo"), wait=True)
    return mgr, mgr.get(handle.id)


def _seed_pid(run):
    return next(n["id"] for n in run["graph"]["nodes"] if n["data"].get("seed"))


def test_facts_carry_their_source_system(demo_run):
    _, run = demo_run
    facts = person_facts(run["graph"], _seed_pid(run))
    assert facts["is_target"] and facts["cnic"] == "9999900000011"
    # FIRs name the system they came from
    assert facts["firs"] and any(f["source"] in ("CRO", "PSRMS") for f in facts["firs"])
    # found in several systems, each labelled
    assert {r["label"] for r in facts["found_in"]} & {"CRO", "PSRMS", "SIMs Database"}
    # strong links carry the stating system
    assert any(s["via"] for s in facts["strong_links"])


def test_rule_brief_is_sourced(demo_run):
    _, run = demo_run
    brief = build_person_brief(run["graph"], _seed_pid(run), llm=None)
    text = brief["narrative"]
    assert "FIR 45/2023" in text
    assert "PSRMS" in text or "CRO" in text          # FIR source cited
    assert "Found in:" in text
    assert brief["model"] is None                     # rule brief, no LLM


def test_brief_via_manager_is_cached(demo_run):
    mgr, run = demo_run
    pid = _seed_pid(run)
    b1 = mgr.person_brief(run["graph"], pid, scope=run["id"])
    b2 = mgr.person_brief(run["graph"], pid, scope=run["id"])
    assert b1 is b2 and "FIR 45/2023" in b1["narrative"]


def test_connection_to_target_is_traced_hop_by_hop(demo_run):
    _, run = demo_run
    people = {n["data"].get("cnic"): n["id"] for n in run["graph"]["nodes"] if n["kind"] == "person"}
    asif = people["9999900000079"]          # co-tenant of the subject's landlord
    facts = person_facts(run["graph"], asif)
    conn = facts["connection_to_target"]
    assert conn["hops"], conn
    # every hop names the relation and the system that states it
    assert all(h["relation"] for h in conn["hops"])
    assert any("Tenant" in h["relation"] or "Landlord" in h["relation"] for h in conn["hops"])
    assert any(h["via"] == "Old Tenant" for h in conn["hops"])
    assert "Asif Ali" in conn["summary"]
    # and the rule brief spells the chain out
    text = build_person_brief(run["graph"], asif, llm=None)["narrative"]
    assert "Connection to the subject" in text


def test_connection_between_any_two_people(demo_run):
    from sherlocks.linkgraph.dossier import path_between

    mgr, run = demo_run
    people = {n["data"].get("cnic"): n["id"] for n in run["graph"]["nodes"] if n["kind"] == "person"}
    out = path_between(run["graph"], people["9999900000079"], people["9999900000037"])  # Asif -> Tariq
    assert out["degrees"] == 1 and out["hops"][0]["via"] == "Old Tenant"
    # via the manager (what the API serves)
    same = mgr.connection(run["graph"], people["9999900000079"], people["9999900000037"])
    assert same["summary"] == out["summary"]


def test_graph_digest_and_question_answering(demo_run):
    from sherlocks.linkgraph.dossier import answer_question, graph_digest

    _, run = demo_run
    digest = graph_digest(run["graph"])
    assert digest["people"] and any(p["is_target"] for p in digest["people"])
    # links carry the system that states them, and mark inferred ones
    assert any(link["type"] == "stated" and link["source_system"] for link in digest["links"])

    class _Llm:
        model = "test-model"

        def generate_structured(self, *, prompt, schema, **_):
            assert "Kamran" in prompt and "Question:" in prompt   # graph + question grounded
            return schema(answer="Tariq Hussain is his landlord (Old Tenant).", confident=True), None

    out = answer_question(run["graph"], "who is the landlord?", llm=_Llm())
    assert "Old Tenant" in out["answer"] and out["model"] == "test-model"

    # with no LLM it says so instead of guessing
    none = answer_question(run["graph"], "who is the landlord?", llm=None)
    assert none["confident"] is False and "No LLM" in none["answer"]


def test_missing_person_returns_none(demo_run):
    _, run = demo_run
    assert person_facts(run["graph"], "nope") is None
