"""Email pivot and name matching: every email found is searched online; with none, the
name is looked up and a result is kept only when the records corroborate it."""

from __future__ import annotations

from types import SimpleNamespace

from sherlocks.linkgraph.engine import Expansion, _Pivot
from sherlocks.linkgraph.graph import GraphBuilder
from sherlocks.linkgraph.models import PersonRef, SystemRecord
from sherlocks.osint.match import KnownFacts, emails_in, match_pipl, score_pipl_person
from sherlocks.settings import Settings

KNOWN = KnownFacts(name="Qurban Sarfraz Mirbahar", father_name="Allah Dino Mirbahar", phones=["03001234567"],
                   addresses=["House 4, Street 9, Latifabad, Hyderabad"], organisations=["Sindh Bank"])


def person(name: str, **extra) -> dict:
    return {"names": [{"display": name}], **extra}


def test_emails_are_found_anywhere_in_a_structure():
    assert emails_in({"a": ["x", {"email": "Q.Mirbahar@Gmail.com."}], "b": "logo@site.png"}) == ["q.mirbahar@gmail.com"]


def test_the_same_phone_settles_it():
    m = score_pipl_person(KNOWN, person("Qurban Mirbahar", phones=[{"display_international": "+92 300 1234567"}],
                                        emails=[{"address": "qurban@example.pk"}]))
    assert m.status == "corroborated" and m.emails == ["qurban@example.pk"] and "same phone" in m.reasons[0]


def test_city_and_father_together_corroborate():
    m = score_pipl_person(KNOWN, person("Qurban Sarfraz", addresses=[{"display": "Latifabad, Hyderabad, Sindh"}],
                                        relationships=[{"names": [{"display": "Allah Dino Mirbahar"}]}]))
    assert m.status == "corroborated" and any("relative named" in r for r in m.reasons)


def test_one_supporting_fact_is_only_possible_and_a_different_name_is_rejected():
    assert score_pipl_person(KNOWN, person("Qurban Sarfraz", addresses=[{"display": "Hyderabad"}])).status == "possible"
    assert score_pipl_person(KNOWN, person("Bilal Khan", phones=[{"display": "03001234567"}])).status == "rejected"


def test_a_common_name_needs_more_than_a_city():
    common = KnownFacts(name="Muhammad Ali", addresses=["Gulshan-e-Iqbal, Karachi"], organisations=["KE"])
    m = score_pipl_person(common, person("Muhammad Ali", addresses=[{"display": "Karachi"}],
                                         jobs=[{"display": "Clerk at KE"}]))
    assert m.status == "possible" or m.score >= 0.65


def test_candidates_are_ranked_corroborated_first():
    body = {"possible_persons": [person("Someone Else"),
                                 person("Qurban Mirbahar", phones=[{"display": "0300-1234567"}])]}
    assert [m.status for m in match_pipl(KNOWN, body)] == ["corroborated", "rejected"]


# -- ownership of emails in records ------------------------------------------------------


def test_only_the_persons_own_email_is_taken_from_a_record():
    b = GraphBuilder()
    pid, _ = b.upsert_person(PersonRef(cnic="9999900000011", name="Kamran Ahmed", phones=["03990000101"]), depth=0)
    rec = SystemRecord(system="sbvs", status="success", hit=True, subject=PersonRef(cnic="9999900000011"),
                       raw={"employee": {"name": "Kamran Ahmed", "cnic": "99999-0000001-1", "email": "kamran@x.pk"},
                            "employer": {"name": "Tariq Hussain", "cnic": "9999900000037", "email": "tariq@x.pk"}})
    rec.add_field("Email", "k.ahmed@y.pk")
    b.add_record(pid, rec)
    x = SimpleNamespace(builder=b)
    assert dict(Expansion._record_emails(x, pid)) == {"k.ahmed@y.pk": "SBVS", "kamran@x.pk": "SBVS"}


# -- the pivot -----------------------------------------------------------------------------


class _Agent:
    """Plays the OSINT agent: records which emails it was asked about and 'discovers'
    one more email the first time."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.client = SimpleNamespace(run_planned=lambda tools: [SimpleNamespace(is_hit=True, tool="search_email")])

    def plan(self, subject):
        self.asked.append(subject.email)
        return SimpleNamespace(tools=[SimpleNamespace(tool="search_email", skip_reason=None)])

    def extract_identifiers(self, results, subject):
        return [{"kind": "email", "value": "second@x.pk", "tool": "search_email"}] if len(self.asked) == 1 else []


def _expansion(**osint) -> SimpleNamespace:
    s = Settings()
    for k, v in osint.items():
        setattr(s.osint, k, v)
    b = GraphBuilder()
    b.upsert_person(PersonRef(cnic="9999900000011", name="Qurban Sarfraz Mirbahar", phones=["03001234567"],
                              addresses=["House 4, Street 9, Latifabad, Hyderabad"]), depth=0)
    log: list[str] = []
    return SimpleNamespace(settings=s, builder=b, _emit=lambda level, msg: log.append(msg), _check=lambda: None, log=log)


def test_every_found_email_is_searched_once_and_new_ones_are_followed_one_level():
    x = _expansion(email_pivots=3)
    pivot = _Pivot(x, "p1")
    pivot._agent = agent = _Agent()
    emails = {"first@x.pk": "NADRA"}
    _, _, searched = pivot.emails(emails, set())
    assert agent.asked == ["first@x.pk", "second@x.pk"] and searched == {"first@x.pk", "second@x.pk"}
    assert emails["second@x.pk"].startswith("OSINT (search_email) via first@x.pk")
    assert any("first@x.pk (from NADRA)" in line for line in x.log)


def test_the_pivot_budget_is_respected():
    x = _expansion(email_pivots=1)
    pivot = _Pivot(x, "p1")
    pivot._agent = agent = _Agent()
    pivot.emails({"a@x.pk": "r", "b@x.pk": "r", "c@x.pk": "r"}, set())
    assert agent.asked == ["a@x.pk"]


def test_with_no_email_the_name_is_matched_against_the_records(monkeypatch):
    import sherlocks.osint.custom_tools as ct

    asked = {}

    def fake_search(name, key, city=None, country="PK", timeout=60):
        asked.update(name=name, city=city, country=country)
        return {"possible_persons": [
            person("Qurban Sarfraz Mirbahar", phones=[{"display": "+923001234567"}], emails=[{"address": "qurban@x.pk"}]),
            person("Qurban Sarfraz Mirbahar", emails=[{"address": "stranger@x.pk"}]),
        ]}

    monkeypatch.setattr(ct, "pipl_name_search", fake_search)
    x = _expansion(pipl_api_key="k")
    matches, emails = _Pivot(x, "p1").name_match("hyderabad")
    assert asked == {"name": "Qurban Sarfraz Mirbahar", "city": "hyderabad", "country": "PK"}
    assert list(emails) == ["qurban@x.pk"]                      # the stranger's email is not taken
    assert [m.status for m in matches] == ["corroborated", "rejected"]


def test_hunter_finds_a_work_email_from_name_and_employer(monkeypatch):
    import sherlocks.osint.custom_tools as ct

    monkeypatch.setattr(ct, "hunter_find_email", lambda name, org, key, timeout=30: {"email": "qurban@sindhbank.pk", "score": 88})
    x = _expansion(hunter_api_key="k")
    x.builder.person("p1")["organisations"].append("Sindh Bank")
    _, emails = _Pivot(x, "p1").name_match(None)
    assert "qurban@sindhbank.pk" in emails and "confidence 88" in emails["qurban@sindhbank.pk"]


def test_without_keys_name_matching_says_what_it_needs():
    x = _expansion()
    assert _Pivot(x, "p1").name_match(None) == ([], {})
    assert any("PIPL_API_KEY or HUNTER_API_KEY" in line for line in x.log)


# -- search by email -----------------------------------------------------------------------


def _demo_manager(**osint):
    import pytest

    from sherlocks.linkgraph.runs import memory_manager
    from sherlocks.render._reportlib import _ensure_on_path
    from sherlocks.settings import load_settings

    s = load_settings()
    _ensure_on_path(s.app.report_app_src)
    pytest.importorskip("cdr_report_app.integrations.providers")
    s.ollama.enabled = s.llm.enabled = False
    s.osint.enabled = True
    s.osint.free_web_search = False
    s.osint.allowed_tools = []
    s.osint.name_match = False
    for k in ("pipl_api_key", "fullcontact_api_key", "hunter_api_key"):
        setattr(s.osint, k, None)
    for k, v in osint.items():
        setattr(s.osint, k, v)
    return memory_manager(s)


def test_an_email_that_resolves_to_a_pakistani_mobile_searches_the_police_systems(monkeypatch):
    import sherlocks.osint.custom_tools as ct
    from sherlocks.linkgraph.models import GraphRunParams

    monkeypatch.setattr(ct, "pipl_lookup", lambda key, timeout=60, **q: {"person": {
        "names": [{"display": "Kamran Ahmed"}], "phones": [{"display_international": "+92 399 0000101"}]}})
    m = _demo_manager(pipl_api_key="k")
    run = m.get(m.start(GraphRunParams(email="Kamran.K@Example.pk", depth=1, max_persons=5, backend="demo"), wait=True).id)
    seed = next(n for n in run["graph"]["nodes"] if n["kind"] == "person" and n["data"]["seed"])
    assert seed["data"]["cnic"] == "9999900000011"                 # the demo Kamran, found through his phone
    assert {"email": "kamran.k@example.pk", "source": "search input"} in seed["data"]["emails"]
    assert any("Pakistani mobile 03990000101" in e["message"] for e in run["events"])
    osint = next(n for n in run["graph"]["nodes"] if n["kind"] == "system" and n["data"]["system"] == "osint")
    assert any(f["label"] == "Email" and "kamran.k@example.pk" in f["value"] for f in osint["data"]["fields"])


def test_an_email_nobody_resolves_is_searched_online_only():
    from sherlocks.linkgraph.models import GraphRunParams

    m = _demo_manager()
    run = m.get(m.start(GraphRunParams(email="ghost@example.pk", depth=2, max_persons=5, backend="demo"), wait=True).id)
    seed = next(n for n in run["graph"]["nodes"] if n["kind"] == "person" and n["data"]["seed"])
    assert seed["label"] == "ghost@example.pk" and seed["data"]["search_status"] == "not_searchable"
    assert any("resolve it to a phone" in e["message"] for e in run["events"])


def test_email_search_is_refused_when_osint_is_off():
    import pytest

    from sherlocks.linkgraph.models import GraphRunParams

    m = _demo_manager()
    m.settings.osint.enabled = False
    with pytest.raises(ValueError, match="needs OSINT"):
        m.start(GraphRunParams(email="a@b.pk", backend="demo"))
    m.start(GraphRunParams(cnic="99999-0000001-1", email="a@b.pk", depth=1, backend="demo"), wait=True)  # fine with a CNIC
