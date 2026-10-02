"""The link graph end to end, over cdr_report_app's real adapters and the demo transport.

Skipped when cdr_report_app is not importable. Never touches a live system: every
request goes to ``DemoHttp``, and every run uses in-memory stores.
"""

from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sherlocks.linkgraph.images import MemoryImageStore
from sherlocks.linkgraph.models import GraphRunParams, PersonRef
from sherlocks.render._reportlib import _ensure_on_path
from sherlocks.settings import load_settings


@pytest.fixture(scope="module")
def lg_settings():
    s = load_settings()
    _ensure_on_path(s.app.report_app_src)
    pytest.importorskip("cdr_report_app.integrations.providers")
    s.ollama.enabled = False
    s.llm.enabled = False  # the LAN model is real network traffic; tests stay offline
    s.osint.enabled = False
    s.api.api_key = None
    s.linkgraph.backend = "demo"
    return s


@pytest.fixture
def backend(lg_settings):
    from sherlocks.linkgraph.demo_data import build_demo_backend

    return build_demo_backend()


def _people():
    from sherlocks.linkgraph.demo_data import PEOPLE

    return PEOPLE


def _record(backend, system, query: PersonRef):
    from sherlocks.linkgraph.extractors import extract_record

    phone = query.phones[0] if query.phones else None
    return extract_record(system, backend.lookup(system, query.cnic, phone), query, MemoryImageStore())


# -- extractors over real adapters ------------------------------------------------------


def test_phone_resolves_to_cnic(backend):
    farhan = _people()["farhan"]
    rec = _record(backend, "simsdb", PersonRef(phones=[farhan.phones[0]]))
    assert rec.subject.cnic == farhan.cnic and rec.related == []


def test_sim_registered_to_someone_else_is_a_relation(backend):
    people = _people()
    rec = _record(backend, "simsdb", PersonRef(cnic=people["kamran"].cnic, phones=["03990000102"]))
    assert rec.subject.cnic is None
    assert rec.related[0].ref.cnic == people["imran"].cnic
    assert "Registered owner of SIM" in rec.related[0].relation


def test_tenancy_is_read_from_both_sides(backend):
    people = _people()
    tenant_side = _record(backend, "old_tenant", PersonRef(cnic=people["kamran"].cnic, phones=[people["kamran"].phones[0]]))
    assert [(r.relation, r.ref.cnic) for r in tenant_side.related] == [("Landlord", people["tariq"].cnic)]
    owner_side = _record(backend, "old_tenant", PersonRef(cnic=people["tariq"].cnic, phones=[people["tariq"].phones[0]]))
    assert {r.relation for r in owner_side.related} == {"Tenant"} and len(owner_side.related) == 2


def test_vehicle_owner_and_driver(backend):
    people = _people()
    kamran = _record(backend, "tracs", PersonRef(cnic=people["kamran"].cnic))
    assert (kamran.related[0].relation, kamran.related[0].ref.cnic) == ("Vehicle owner", people["farhan"].cnic)
    farhan = _record(backend, "tracs", PersonRef(cnic=people["farhan"].cnic))
    assert (farhan.related[0].relation, farhan.related[0].ref.cnic) == ("Driver of vehicle", people["rashid"].cnic)


def test_licence_photo_belongs_to_the_licence_holder(backend):
    people = _people()
    own = _record(backend, "dls", PersonRef(cnic=people["kamran"].cnic, phones=[people["kamran"].phones[0]]))
    assert len(own.subject.images) == 1
    # Imran searched by the number Kamran uses: the licence - and its photo - are Kamran's.
    other = _record(backend, "dls", PersonRef(cnic=people["imran"].cnic, phones=["03990000102"]))
    assert other.subject.images == [] and other.subject.name is None
    assert other.related[0].ref.cnic == people["kamran"].cnic and len(other.related[0].ref.images) == 1


def test_fir_report_names_everyone(backend):
    from sherlocks.linkgraph.extractors import extract_record

    people = _people()
    payload = backend.fir_roster("45", "2023", "501")
    rec = extract_record("fir_roster", payload, PersonRef(cnic=people["kamran"].cnic), MemoryImageStore())
    relations = {r.relation.split(" in ")[0]: r.ref for r in rec.related}
    assert relations["Co-accused (nominated)"].cnic == people["sajid"].cnic
    assert relations["Investigating officer"].cnic == people["zahid"].cnic
    assert relations["Complainant"].cnic == people["waqas"].cnic
    assert relations["Witness"].cnic is None and relations["Witness"].father_name == "Khalid Mehmood"


# -- expansion ---------------------------------------------------------------------------


def _run(manager, **params):
    handle = manager.start(GraphRunParams(backend="demo", **params), wait=True)
    run = manager.get(handle.id)
    assert run["status"] == "completed", run["events"][-3:]
    persons = {n["data"]["cnic"]: n for n in run["graph"]["nodes"] if n["kind"] == "person" and n["data"]["cnic"]}
    return run, persons


@pytest.fixture
def manager(lg_settings):
    from sherlocks.linkgraph.runs import memory_manager

    return memory_manager(lg_settings)


def test_depth_one_searches_only_the_subject(manager):
    people = _people()
    run, persons = _run(manager, cnic=people["kamran"].cnic, depth=1)
    assert run["stats"]["searched"] == 1
    assert persons[people["tariq"].cnic]["data"]["search_status"] == "depth_limit"
    assert people["asif"].cnic not in persons  # two hops away


def test_depth_three_reaches_the_second_hop_with_clean_identities(manager):
    people = _people()
    run, persons = _run(manager, cnic=people["kamran"].cnic, phone=people["kamran"].phones[0], depth=3)
    for key in ("imran", "tariq", "sajid", "farhan", "zahid", "asif", "rashid", "naveed"):
        assert people[key].cnic in persons, key
    imran = persons[people["imran"].cnic]["data"]
    assert imran["name"] == "Imran Ahmed" and all("kamran" not in n.lower() for n in imran["names"])
    assert "police_officer" in persons[people["zahid"].cnic]["data"]["flags"]
    weak = [e for e in run["graph"]["edges"] if e["kind"] == "weak"]
    assert any("siblings" in e["label"].lower() for e in weak)
    assert any(e["label"].startswith("Co-stay") for e in weak)


def test_photos_remember_which_system_they_came_from(manager):
    people = _people()
    _, persons = _run(manager, cnic=people["kamran"].cnic, phone=people["kamran"].phones[0], depth=1)
    data = persons[people["kamran"].cnic]["data"]
    assert data["images"], "subject should have a photo"
    sources = set(data["image_sources"].values())
    assert "DLS" in sources, sources           # licence photo, attributed to DLS
    assert all(img in data["image_sources"] for img in data["images"])


def test_person_budget_caps_the_search(manager):
    people = _people()
    run, persons = _run(manager, cnic=people["kamran"].cnic, depth=3, max_persons=3)
    assert run["stats"]["searched"] == 3
    assert any(p["data"]["search_status"] == "budget" for p in persons.values())


def test_repeat_run_is_served_from_the_cache(manager):
    people = _people()
    first, _ = _run(manager, cnic=people["tariq"].cnic, depth=2)
    second, _ = _run(manager, cnic=people["tariq"].cnic, depth=2)
    assert first["stats"]["upstream_calls"] > 0
    assert second["stats"]["upstream_calls"] == 0


def test_each_fir_report_is_fetched_once(manager):
    people = _people()
    _run(manager, cnic=people["kamran"].cnic, depth=2)
    calls = [url for _, url in manager.backend("demo").http.calls if url.endswith("/psrms/fir")]
    assert len(calls) == 2  # FIR 45/2023 and 112/2024, however many people share them


def test_invalid_seed_is_rejected(manager):
    with pytest.raises(ValueError):
        manager.start(GraphRunParams(cnic="12345", backend="demo"))


# -- API ---------------------------------------------------------------------------------


def _client(lg_settings, manager) -> TestClient:
    from sherlocks.api.linkgraph import build_router

    app = FastAPI()
    app.include_router(build_router(lg_settings, lambda: manager))
    return TestClient(app)


def test_api_round_trip(lg_settings, manager):
    people = _people()
    client = _client(lg_settings, manager)
    prefix = f"{lg_settings.api.prefix}/graph"
    config = client.get(f"{prefix}/config").json()
    assert len(config["systems"]) == 19 and config["demo_seeds"]

    created = client.post(f"{prefix}/runs", json={"cnic": people["kamran"].cnic, "depth": 1, "backend": "demo"})
    assert created.status_code == 202
    run_id = created.json()["run_id"]
    deadline = time.time() + 30
    while (run := client.get(f"{prefix}/runs/{run_id}").json())["status"] not in ("completed", "failed"):
        assert time.time() < deadline
        time.sleep(0.2)
    assert run["status"] == "completed"
    seed = next(n for n in run["graph"]["nodes"] if n["data"].get("seed"))
    image = client.get(f"{prefix}/images/{seed['data']['images'][0]}")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert "attachment" in client.get(f"{prefix}/runs/{run_id}/export").headers["content-disposition"]
    assert any(r["id"] == run_id for r in client.get(f"{prefix}/runs").json())


def test_config_lists_the_systems_the_chosen_backend_really_queries(lg_settings, manager):
    """The portal ticks what /config lists, so a system missing here is never searched.
    EMS runs NADRA/ARMS/Excise/AVLC, which the shared default list does not carry."""
    client = _client(lg_settings, manager)
    prefix = f"{lg_settings.api.prefix}/graph"
    ems = [s["system"] for s in client.get(f"{prefix}/config", params={"backend": "ems"}).json()["systems"]]
    assert {"nadra", "arms", "excise", "avlc"} <= set(ems)
    assert not {"fir_roster", "caller_id", "osint"} & set(ems)  # driven by their own steps
    demo = [s["system"] for s in client.get(f"{prefix}/config", params={"backend": "demo"}).json()["systems"]]
    assert "simsdb" in demo


def test_api_rejects_bad_identifiers(lg_settings, manager):
    client = _client(lg_settings, manager)
    response = client.post(f"{lg_settings.api.prefix}/graph/runs", json={"cnic": "abc", "backend": "demo"})
    assert response.status_code == 422


def test_api_key_is_enforced_including_for_images(lg_settings, manager):
    secured = lg_settings.model_copy(deep=True)
    secured.api.api_key = "s3cret"
    client = _client(secured, manager)
    prefix = f"{secured.api.prefix}/graph"
    assert client.get(f"{prefix}/config").status_code == 401
    assert client.get(f"{prefix}/config", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get(f"{prefix}/images/0123456789abcdef0123456789abcdef.png", params={"key": "s3cret"}).status_code == 404
