"""Linkage scenarios, network analytics and the AI investigator - over small hand-built
graphs, so each pattern is tested on exactly the records that should trigger it."""

from __future__ import annotations

import pytest

from sherlocks.linkgraph.investigator import investigate
from sherlocks.linkgraph.network import HUB_PERSON, PersonNetwork
from sherlocks.linkgraph.scenarios import find_scenarios, fir_id, sections


class G:
    """A graph dict in the shape GraphBuilder.snapshot() produces."""

    def __init__(self) -> None:
        self.nodes: list[dict] = []
        self.edges: list[dict] = []

    def person(self, pid: str, name: str, **data) -> G:
        base = {"name": name, "names": [name], "father_name": None, "cnic": None, "phones": [], "addresses": [],
                "flags": [], "firs": [], "stays": [], "organisations": [], "vehicles": [], "police_stations": [],
                "records": [], "discovered_via": [], "osint": [], "seed": False, "search_status": "searched",
                "searchable": True, "depth": 1}
        self.nodes.append({"id": pid, "kind": "person", "label": name, "data": {**base, **data}})
        return self

    def record(self, sid: str, owner: str, system: str, label: str | None = None, **data) -> G:
        self.nodes.append({"id": sid, "kind": "system", "label": label or system,
                           "data": {"system": system, "owner": owner, "fields": [], **data}})
        self.edges.append({"id": f"f:{owner}>{sid}", "source": owner, "target": sid, "kind": "found_in",
                           "label": "", "system": system})
        return self

    def on(self, pid: str, sid: str) -> G:
        self.edges.append({"id": f"f:{pid}>{sid}", "source": pid, "target": sid, "kind": "found_in", "label": ""})
        return self

    def strong(self, sid: str, target: str, label: str, system: str) -> G:
        self.edges.append({"id": f"s:{sid}>{target}:{label}", "source": sid, "target": target, "kind": "strong",
                           "label": label, "system": system, "reasons": []})
        return self

    def weak(self, a: str, b: str, score: float, label: str = "Same address") -> G:
        self.edges.append({"id": f"w:{a}>{b}", "source": a, "target": b, "kind": "weak", "label": label,
                           "score": score, "system": "rules", "reasons": []})
        return self

    def dict(self) -> dict:
        return {"version": 1, "nodes": self.nodes, "edges": self.edges}


def fir(label: str, role: str, ps: str = "PS Gulshan", offence: str | None = None, system: str = "psrms") -> dict:
    return {"key": label, "label": label, "ps": ps, "role": role, "system": system, "offence": offence}


def scenarios_of(graph: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for f in find_scenarios(graph):
        out.setdefault(f["scenario"], []).append(f)
    return out


# -- helpers ----------------------------------------------------------------------------


def test_fir_ids_and_sections_are_normalised():
    assert fir_id("FIR 045/23") == "45/2023" == fir_id("Co-accused in FIR 45/2023")
    assert sections("395/34 PPC, 489-F") == {"395", "489F"}      # 34 rides along with anything


# -- FIR patterns -----------------------------------------------------------------------


def test_repeat_co_offenders_need_two_shared_firs():
    g = (G().person("a", "Kamran Jatoi", firs=[fir("45/2023", "Accused"), fir("12/2024", "Accused")])
         .person("b", "Sajid Brohi", firs=[fir("45/2023", "Accused"), fir("12/2024", "Accused / suspect")])
         .person("c", "Asif Magsi", firs=[fir("45/2023", "Accused")]))
    found = scenarios_of(g.dict())["repeat_co_offenders"]
    assert [f["people"] for f in found] == [["a", "b"]] and found[0]["tier"] == "stated"


def test_counter_firs_are_a_feud():
    g = (G().person("a", "Waqas Javed", firs=[fir("10/2023", "Complainant"), fir("77/2024", "Accused")])
         .person("b", "Kamran Jatoi", firs=[fir("10/2023", "Accused"), fir("77/2024", "Complainant")]))
    assert scenarios_of(g.dict())["counter_fir"][0]["people"] == ["a", "b"]


def test_recurring_witness_needs_unrelated_cases():
    g = (G().person("w", "Shahid Stock", firs=[fir("1/2024", "Witness"), fir("2/2024", "Witness")])
         .person("x", "Accused One", firs=[fir("1/2024", "Accused")])
         .person("y", "Accused Two", firs=[fir("2/2024", "Accused")]))
    assert "recurring_witness" in scenarios_of(g.dict())
    # the same witness on two cases against the same man is ordinary
    g2 = (G().person("w", "Shahid Stock", firs=[fir("1/2024", "Witness"), fir("2/2024", "Witness")])
          .person("x", "Accused One", firs=[fir("1/2024", "Accused"), fir("2/2024", "Accused")]))
    assert "recurring_witness" not in scenarios_of(g2.dict())


def test_same_charges_same_station_no_shared_accused():
    g = (G().person("x", "Accused One", firs=[fir("1/2024", "Accused", offence="395/34 PPC")])
         .person("y", "Accused Two", firs=[fir("9/2024", "Accused", offence="395 PPC")])
         .person("z", "Elsewhere", firs=[fir("5/2024", "Accused", ps="PS Korangi", offence="395 PPC")]))
    found = scenarios_of(g.dict())["same_method"]
    assert [sorted(f["people"]) for f in found] == [["x", "y"]]


# -- property, vehicles, SIMs -----------------------------------------------------------


def test_landlord_of_flagged_tenants_is_a_possible_safe_house():
    g = G().person("L", "Tariq Landlord")
    for i, flags in enumerate([["criminal_record"], [], []]):
        g.person(f"t{i}", f"Tenant {i}", flags=flags).record(f"s:old_tenant:t{i}", f"t{i}", "old_tenant") \
         .strong(f"s:old_tenant:t{i}", "L", "Landlord", "old_tenant")
    found = scenarios_of(g.dict())["safe_house"][0]
    assert found["people"][0] == "L" and found["score"] == 0.65 and "safe house" in found["summary"]


def test_vehicle_driven_by_someone_other_than_its_owner():
    g = (G().person("d", "Kamran Driver").person("o", "Farhan Owner")
         .record("s:tracs:d", "d", "tracs").strong("s:tracs:d", "o", "Vehicle owner", "tracs"))
    found = scenarios_of(g.dict())["vehicle_owner_driver"][0]
    assert found["people"] == ["o", "d"] and found["tier"] == "stated"


def test_sims_of_others_on_one_cnic():
    g = G().person("o", "Front Owner")
    for u in ("u1", "u2"):
        g.person(u, f"User {u}").record(f"s:simsdb:{u}", u, "simsdb") \
         .strong(f"s:simsdb:{u}", "o", "Registered owner of SIM 0300", "simsdb")
    assert scenarios_of(g.dict())["sim_front"][0]["people"][0] == "o"


def test_servant_of_a_complainant_tied_to_the_accused_is_an_insider_lead():
    g = (G().person("boss", "Complainant Sahib", firs=[fir("3/2024", "Complainant")])
         .person("servant", "Driver Khan").person("acc", "Robber Brohi", firs=[fir("3/2024", "Accused")])
         .record("s:sbvs:servant", "servant", "sbvs").strong("s:sbvs:servant", "boss", "Employer", "sbvs")
         .record("s:old_tenant:servant", "servant", "old_tenant").strong("s:old_tenant:servant", "acc", "Landlord", "old_tenant"))
    found = scenarios_of(g.dict())["insider"][0]
    assert set(found["people"]) == {"boss", "servant", "acc"}


def test_officer_linked_to_a_criminal_is_flagged_sensitive_but_not_as_investigator():
    g = (G().person("io", "SI Officer", flags=["police_officer"])
         .person("crim", "Known Accused", flags=["criminal_record"])
         .record("s:fir_roster:1", "crim", "fir_roster", "FIR 1/2024")
         .strong("s:fir_roster:1", "io", "Investigating officer in FIR 1/2024", "fir_roster"))
    assert "police_link" not in scenarios_of(g.dict())
    g.record("s:prvs:io", "io", "prvs").strong("s:prvs:io", "crim", "Landlord (PRVS)", "prvs")
    found = scenarios_of(g.dict())["police_link"][0]
    assert found["sensitive"] is True


# -- identity and corroboration ---------------------------------------------------------


def test_one_rare_name_under_two_cnics():
    g = (G().person("a", "Qurban Sarfraz", father_name="Allah Dino Mirbahar", cnic="9999900000011")
         .person("b", "Qurban Sarfraz", father_name="Allah Dino Mirbahar", cnic="9999900000029")
         .person("c", "Muhammad Ali", father_name="Muhammad Iqbal", cnic="9999900000037")
         .person("d", "Muhammad Ali", father_name="Muhammad Iqbal", cnic="9999900000045"))
    found = scenarios_of(g.dict())["dual_identity"]
    assert [f["people"] for f in found] == [["a", "b"]]          # the common name is not flagged


def test_shared_identifier_counts_once_toward_corroboration():
    g = (G().person("a", "Kamran").person("b", "Imran")
         .record("s:psrms:a", "a", "psrms").strong("s:psrms:a", "b", "Same identifier in FIR 1/2023 (Accused)", "psrms")
         .record("s:dls:a", "a", "dls").strong("s:dls:a", "b", "Driving licence registered with the same number", "dls"))
    assert "multi_source_link" not in scenarios_of(g.dict())
    g.record("s:old_tenant:a", "a", "old_tenant").strong("s:old_tenant:a", "b", "Landlord", "old_tenant")
    assert scenarios_of(g.dict())["multi_source_link"][0]["tier"] == "corroborated"


# -- network ----------------------------------------------------------------------------


def test_routes_prefer_stated_links_and_avoid_hubs():
    g = G().person("a", "Start Person").person("b", "End Person").person("m", "Middle Person").person("h", "Big Landlord")
    g.record("s:x:a", "a", "old_tenant").strong("s:x:a", "m", "Tenant", "old_tenant")
    g.record("s:x:m", "m", "old_tenant").strong("s:x:m", "b", "Tenant", "old_tenant")
    for i in range(HUB_PERSON + 2):   # h is landlord to a crowd, including a and b
        g.person(f"t{i}", f"Tenant {i}").record(f"s:t:{i}", f"t{i}", "prvs").strong(f"s:t:{i}", "h", "Landlord", "prvs")
    for p in ("a", "b"):
        g.record(f"s:t:{p}", p, "prvs").strong(f"s:t:{p}", "h", "Landlord", "prvs")
    g.weak("a", "b", 0.4)
    best = PersonNetwork(g.dict()).paths("a", "b", k=3)
    assert best[0]["nodes"] == ["a", "m", "b"] and not best[0]["inferred"]
    assert any(r["inferred"] for r in best[1:]) and any("h" in r["nodes"] for r in best[1:])


def test_hidden_associates_share_rare_contacts():
    g = G().person("a", "Alpha").person("b", "Bravo").person("c1", "Contact One").person("c2", "Contact Two")
    for x, y in (("a", "c1"), ("a", "c2"), ("b", "c1"), ("b", "c2")):
        g.record(f"s:r:{x}{y}", x, "sbvs").strong(f"s:r:{x}{y}", y, "Reference", "sbvs")
    pairs = PersonNetwork(g.dict()).hidden_associates()
    assert {(p["a"], p["b"]) for p in pairs} >= {("a", "b")}


def test_names_resolve_fuzzily_and_by_identifier():
    net = PersonNetwork(G().person("p1", "Kamran Ahmed", cnic="9999900000011", phones=["03990000101"]).dict())
    assert net.resolve("kamran ahmad") == net.resolve("99999-0000001-1") == net.resolve("0399-0000101") == "p1"
    assert net.resolve("Somebody Else") is None


# -- investigator -----------------------------------------------------------------------


def _chain() -> dict:
    g = (G().person("a", "Asif Ali", seed=True).person("t", "Tariq Hussain").person("k", "Kamran Ahmed", flags=["criminal_record"])
         .record("s:ot:a", "a", "old_tenant").strong("s:ot:a", "t", "Landlord", "old_tenant")
         .record("s:ot:k", "k", "old_tenant").strong("s:ot:k", "t", "Landlord", "old_tenant"))
    return g.dict()


def test_without_a_model_the_investigator_answers_route_questions_by_rule():
    events = list(investigate(_chain(), "how is Asif linked to Kamran Ahmed?"))
    assert events[0]["type"] == "start" and events[1]["tool"] == "paths"
    final = events[-1]
    assert final["type"] == "final" and final["model"] is None
    assert "Stated route, 2 step(s)" in final["answer"] and "Tariq Hussain" in final["answer"]


class _ScriptedLlm:
    """Plays the model: a fixed plan, then a conclusion that names one real and one
    invented person."""

    model = "scripted"

    def __init__(self, steps: list[dict]) -> None:
        self.steps = list(steps)
        self.prompts: list[str] = []

    def generate_structured(self, *, prompt, schema, **_):
        self.prompts.append(prompt)
        if schema.__name__ == "_Step":
            return schema(**(self.steps.pop(0) if self.steps else {"thought": "done", "action": "final"})), None
        return schema(answer="Asif and Kamran share a landlord [Old Tenant].", confident=True,
                      hypotheses=[{"statement": "Asif and Kamran share a landlord.", "tier": "stated",
                                   "people": ["Asif Ali", "Kamran Ahmed"], "evidence": ["Landlord [Old Tenant]"]},
                                  {"statement": "Mr X funds them.", "tier": "speculative", "people": ["Mr X"]}],
                      next_steps=["Search Tariq Hussain's other tenants."]), None


def test_the_model_drives_the_tools_and_invented_people_are_dropped():
    llm = _ScriptedLlm([{"thought": "find the route", "action": "paths", "args": {"a": "Asif", "b": "Kamran Ahmed"}},
                        {"thought": "same again", "action": "paths", "args": {"a": "Asif", "b": "Kamran Ahmed"}}])
    events = list(investigate(_chain(), "how is Asif linked to Kamran?", llm=llm))
    steps = [e for e in events if e["type"] == "step"]
    assert len(steps) == 1 and "Tariq Hussain" in steps[0]["summary"]          # the repeat is not re-run
    assert "Tariq Hussain" in llm.prompts[-1]                                  # results reach the conclusion
    final = events[-1]
    assert final["model"] == "scripted" and len(final["hypotheses"]) == 1      # "Mr X" is not in the graph
    assert {p["name"] for p in final["key_people"]} == {"Asif Ali", "Kamran Ahmed"}


def test_a_bad_tool_argument_is_reported_not_raised():
    llm = _ScriptedLlm([{"thought": "look up", "action": "person", "args": {"who": "Nobody Known"}}])
    step = next(e for e in investigate(_chain(), "who?", llm=llm) if e["type"] == "step")
    assert "error" in step["summary"]


# -- engine and API over the demo network -----------------------------------------------


@pytest.fixture(scope="module")
def demo():
    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.render._reportlib import _ensure_on_path
    from sherlocks.settings import load_settings

    s = load_settings()
    _ensure_on_path(s.app.report_app_src)
    pytest.importorskip("cdr_report_app.integrations.providers")
    s.ollama.enabled = s.llm.enabled = s.osint.enabled = False
    s.api.api_key = None
    s.auth.password = s.auth.password_sha256 = s.auth.secret = None
    return memory_manager(s)


def test_priority_spends_a_small_budget_on_the_co_accused_first(demo):
    from sherlocks.linkgraph.models import GraphRunParams

    run = demo.get(demo.start(GraphRunParams(cnic="99999-0000001-1", depth=3, max_persons=4, backend="demo"), wait=True).id)
    searched = {n["label"] for n in run["graph"]["nodes"] if n["kind"] == "person" and n["data"]["search_status"] == "searched"}
    assert "Sajid Mehmood" in searched                         # co-accused in FIR 45/2023
    assert any("most promising first" in e["message"] for e in run["events"])


def test_stop_when_connected_saves_queries(demo):
    from sherlocks.linkgraph.demo_data import PEOPLE
    from sherlocks.linkgraph.models import GraphRunParams, Seed

    seeds = [Seed(cnic=PEOPLE["asif"].cnic), Seed(cnic=PEOPLE["bilal"].cnic)]
    full = demo.get(demo.start(GraphRunParams(seeds=seeds, depth=4, max_persons=30, backend="demo"), wait=True).id)
    quick = demo.get(demo.start(GraphRunParams(seeds=seeds, depth=4, max_persons=30, backend="demo",
                                               stop_when_connected=True), wait=True).id)
    assert quick["stats"]["searched"] < full["stats"]["searched"]
    net = PersonNetwork(quick["graph"])
    a, b = net.seeds()
    assert net.paths(a, b) and not net.paths(a, b)[0]["inferred"]


def test_findings_network_and_investigator_over_http(demo):
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sherlocks.api.linkgraph import build_router
    from sherlocks.linkgraph.models import GraphRunParams

    app = FastAPI()
    app.include_router(build_router(demo.settings, lambda: demo))
    client = TestClient(app)
    url = f"{demo.settings.api.prefix}/graph"
    run_id = demo.start(GraphRunParams(cnic="99999-0000001-1", phone="0399-0000101", depth=3, max_persons=30,
                                       backend="demo"), wait=True).id
    graph = demo.get(run_id)["graph"]

    found = client.post(f"{url}/analyze/findings", json={"run_id": run_id}).json()
    assert {"vehicle_owner_driver", "safe_house", "broker"} <= {f["scenario"] for f in found["findings"]}
    assert client.post(f"{url}/analyze/network", json={"graph": graph}).json()["brokers"][0]["name"] == "Kamran Ahmed"
    routes = client.post(f"{url}/analyze/paths", json={"graph": graph, "a": "Asif Ali", "b": "Bilal Khan"}).json()
    assert routes["routes"] and routes["routes"][0]["hops"]

    with client.stream("POST", f"{url}/investigate", json={"graph": graph, "question": "how is Bilal linked to Asif Ali?"}) as r:
        text = r.read().decode()
    kinds = [line[7:] for line in text.splitlines() if line.startswith("event: ")]
    assert kinds[0] == "start" and kinds[-1] == "final" and "agent" in kinds
    final = json.loads(text.strip().split("data: ")[-1])
    assert "linked through" in final["answer"]
