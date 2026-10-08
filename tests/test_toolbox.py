"""The shared toolbox and the API router's catalog."""

from __future__ import annotations

from sherlocks.evidence.case_file import CaseFile
from sherlocks.linkgraph import api_router
from sherlocks.linkgraph.network import PersonNetwork


def _graph(cnic=None):
    data = {"seed": True, "phones": ["03990000101"], "records": [{"system": "psrms"}]}
    if cnic:
        data["cnic"] = cnic
    return {"nodes": [{"id": "a", "kind": "person", "label": "Kamran Ahmed", "data": data},
                      {"id": "b", "kind": "person", "label": "Sajid Mehmood", "data": {"phones": ["03990000201"]}}],
            "edges": [{"id": "e", "source": "a", "target": "b", "kind": "strong", "label": "Co-accused in FIR 45/2023"}]}


def test_cnic_only_systems_wait_for_the_cnic_lookup():
    net, case = PersonNetwork(_graph()), CaseFile()
    planned = api_router.plan(net, case, "a", ["vehicle"])
    systems = [c["system"] for c in planned["calls"]]
    assert systems[0] == "simsdb" and "excise" in systems            # SIMs finds the CNIC Excise needs


def test_searched_systems_are_skipped_including_nothing_found():
    net, case = PersonNetwork(_graph("4210112345671")), CaseFile()
    api_router.remember(case, "a", "excise", "nothing found")
    planned = api_router.plan(net, case, "a", ["vehicle", "record"])
    systems = [c["system"] for c in planned["calls"]]
    assert "excise" not in systems and "psrms" not in systems         # psrms: on the graph already
    assert {"system": "excise", "why": "nothing found"} in planned["skipped"]


def test_a_topic_no_system_holds_says_so_and_the_officers_systems_apply():
    net, case = PersonNetwork(_graph("4210112345671")), CaseFile()
    assert api_router.plan(net, case, "a", ["horoscope"])["unheld"] == ["horoscope"]
    limited = api_router.plan(net, case, "a", ["vehicle"], allowed=["dls"])
    assert [c["system"] for c in limited["calls"]] == ["dls"]
    assert api_router.topics_for(["work", "vehicle"]) == ["work", "vehicle"]
    assert api_router.topics_for([], "hotels") == ["hotel"]
    rows = api_router.catalog_rows()
    assert any(r["system"] == "caller_id" and r["trust"] == "unverified" for r in rows)


def test_toolbox_tools_read_the_board():
    from sherlocks.linkgraph.investigator import _Tools

    graph, case = _graph("4210112345671"), CaseFile()
    doc = case.add_document(kind="cdr", key="upload:1", title="Upload: kamran.xlsx", source="Uploaded by the officer",
                            text="Top contact 1: 03990000201 (Sajid Mehmood) - 14 call(s)/SMS.")
    case.add_fact(doc, "Top contact 1: 03990000201 (Sajid Mehmood) - 14 call(s)/SMS.",
                  "Top contact 1: 03990000201 (Sajid Mehmood) - 14 call(s)/SMS.", by="cdr", rows="2-15")
    case.set_role("a", "main suspect")
    case.add_turn("Kamran ki bike KDE-1234 thi", "noted")
    tools = _Tools(graph, case)
    names = set(tools.registry())
    assert {"board", "dossier", "chat_memory", "cdr_query"} <= names and "api_router" not in names   # no backend
    card = tools.dossier({"who": "Kamran Ahmed"})
    assert card["role"] == "main suspect" and card["cnic"] == "4210112345671"
    assert tools.dossier({"who": "Kamran Ahmed"}) is case.dossiers["a"]             # cached: nothing changed
    assert tools.chat_memory({"query": "bike"})["said"][0]["turn"] == 1
    cdr = tools.cdr_query({"who": "Sajid Mehmood"})
    assert cdr["facts"][0]["rows"] == "2-15"
    assert tools.board({"query": "Sajid contact"})["entries"]
