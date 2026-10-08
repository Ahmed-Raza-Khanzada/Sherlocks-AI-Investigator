"""Relations read as directed sentences between two people."""

from __future__ import annotations

import pytest

from sherlocks.linkgraph.relations import describe, weak_relation

NAMES = {"k": "Kamran Ahmed", "t": "Tariq Hussain", "w": "Waqas Javed", "s": "Sajid Mehmood", "i": "Imran Ahmed"}


@pytest.mark.parametrize(("label", "roles", "expected"), [
    ("Landlord", None, "Tariq Hussain is landlord of Kamran Ahmed"),
    ("Tenancy witness", None, "Tariq Hussain was witness to the tenancy of Kamran Ahmed"),
    ("Family member (Wife)", None, "Tariq Hussain is wife of Kamran Ahmed"),
    ("Co-accused (nominated) in FIR 45/2023", None, "Tariq Hussain is co-accused with Kamran Ahmed in FIR 45/2023"),
    ("Registered owner of SIM 03001234567", None, "Tariq Hussain is registered owner of SIM 03001234567 used by Kamran Ahmed"),
    ("Room-mate at hotel", None, "Tariq Hussain shared hotel with Kamran Ahmed"),
    ("Tenant (tenancy record)", None, "Tariq Hussain is tenant on a tenancy record found on the phone/CNIC of Kamran Ahmed"),
])
def test_role_labels_become_sentences(label, roles, expected):
    assert describe(label, named="t", owner="k", owner_roles=roles).sentence(NAMES) == expected


def test_complainant_against_an_accused_filed_the_fir_with_charges():
    rel = describe("Complainant in FIR 45/2023", named="w", owner="k",
                   charges_for={"45/2023": "395/34 PPC"}, owner_roles={"45/2023": "Accused (CRO)"})
    assert rel.source == "w" and rel.target == "k" and not rel.symmetric
    assert rel.sentence(NAMES) == "Waqas Javed filed FIR 45/2023 against Kamran Ahmed (charges: 395/34 PPC)"


def test_accused_named_by_a_complainant_reverses_the_arrow():
    # Kamran's own record names Imran as accused; Kamran is the complainant in that FIR.
    rel = describe("Accused / suspect in FIR 7/2024", named="i", owner="k", owner_roles={"7/2024": "Complainant"})
    assert (rel.source, rel.target) == ("k", "i")
    assert rel.sentence(NAMES) == "Kamran Ahmed filed FIR 7/2024 against Imran Ahmed"


def test_psrms_subject_relative_wording():
    assert describe("Complainant against the subject in FIR 45/2023", named="w", owner="k").sentence(NAMES) \
        == "Waqas Javed filed FIR 45/2023 against Kamran Ahmed"
    rel = describe("Accused by the subject in FIR 45/2023", named="i", owner="k")
    assert (rel.source, rel.target) == ("k", "i")


def test_unknown_label_keeps_the_systems_words_and_weak_links_read_as_leads():
    assert describe("Guarantor for loan", named="t", owner="k").sentence(NAMES).startswith("Tariq Hussain is guarantor for")
    assert describe("Something new", named="t", owner="k").sentence(NAMES) == "Tariq Hussain is something new of Kamran Ahmed"
    weak = weak_relation("Same address", "k", "i", 0.85).sentence(NAMES)
    assert weak == "Kamran Ahmed shares an address with Imran Ahmed — inferred 0.85"
    both = weak_relation("Possible siblings + Same address", "k", "i", 0.9).sentence(NAMES)
    assert both == "Kamran Ahmed may be a sibling of Imran Ahmed — inferred 0.90 · also same address"


@pytest.fixture(scope="module")
def demo_graph():
    from sherlocks.linkgraph.models import GraphRunParams
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.render._reportlib import _ensure_on_path
    from sherlocks.settings import load_settings

    s = load_settings()
    _ensure_on_path(s.app.report_app_src)
    pytest.importorskip("cdr_report_app.integrations.providers")
    s.ollama.enabled = False
    s.llm.enabled = False
    mgr = memory_manager(s)
    handle = mgr.start(GraphRunParams(cnic="99999-0000001-1", phone="0399-0000101", depth=3,
                                      max_persons=30, backend="demo"), wait=True)
    run = mgr.get(handle.id)
    return mgr, run, {n["label"]: n["id"] for n in run["graph"]["nodes"] if n["kind"] == "person"}


def test_every_person_edge_in_a_real_graph_has_a_sentence(demo_graph):
    _, run, _ = demo_graph
    nodes = {n["id"]: n for n in run["graph"]["nodes"]}
    for e in run["graph"]["edges"]:
        if e["kind"] == "found_in":
            continue
        src = nodes[e["source"]]
        if src["kind"] == "system" and src["data"].get("owner") == e["target"]:
            continue
        rel = e.get("relation")
        assert rel and rel["sentence"] and rel["from"] in nodes and rel["to"] in nodes, e
    sentences = [e["relation"]["sentence"] for e in run["graph"]["edges"] if e.get("relation")]
    assert "Waqas Javed filed FIR 45/2023 against Kamran Ahmed (charges: 395/34 PPC)" in sentences


def test_brief_lists_criminal_connections(demo_graph):
    from sherlocks.linkgraph.dossier import build_person_brief, person_facts

    _, run, p = demo_graph
    facts = person_facts(run["graph"], p["Asif Ali"])
    assert any(c["name"] == "Kamran Ahmed" and "criminal_record" in c["flags"] for c in facts["criminal_links"])
    kamran = next(c for c in facts["criminal_links"] if c["name"] == "Kamran Ahmed")
    assert kamran["how"] and any("395/34 PPC" in f for f in kamran["firs"])
    assert "criminal footprint" in build_person_brief(run["graph"], p["Asif Ali"])["narrative"]


def test_compare_shows_criminal_profile_and_pair_chat_is_grounded(demo_graph):
    from sherlocks.linkgraph.compare import ask_about_pair, compare_people

    _, run, p = demo_graph
    c = compare_people(run["graph"], p["Waqas Javed"], p["Kamran Ahmed"])
    assert c["profiles"]["b"]["criminal"] and any(f["charges"] for f in c["profiles"]["b"]["firs"])
    assert any(d.get("sentence", "").startswith("Waqas Javed filed FIR 45/2023 against Kamran Ahmed") for d in c["direct"])

    class _Llm:
        model = "test-model"

        def generate_structured(self, *, prompt, schema, **_):
            assert "Question: who filed the FIR?" in prompt and "395/34 PPC" in prompt
            return schema(answer="Waqas Javed filed FIR 45/2023 against Kamran Ahmed [FIR Roster].", confident=True), None

    out = ask_about_pair(run["graph"], p["Waqas Javed"], p["Kamran Ahmed"], "who filed the FIR?", llm=_Llm())
    assert out["model"] == "test-model" and "filed FIR 45/2023" in out["answer"]
    no_llm = ask_about_pair(run["graph"], p["Waqas Javed"], p["Kamran Ahmed"], "who filed the FIR?", llm=None)
    assert no_llm["confident"] is False and "No LLM" in no_llm["answer"]


def test_complainant_is_not_a_criminal_but_accused_is(demo_graph):
    from sherlocks.linkgraph.compare import compare_people

    _, run, p = demo_graph
    c = compare_people(run["graph"], p["Waqas Javed"], p["Kamran Ahmed"])
    assert c["profiles"]["a"]["criminal"] is False          # complainant in FIR 45/2023
    assert c["profiles"]["b"]["criminal"] is True           # accused, with a CRO record
