"""Names by sound across scripts: "altaf" finds الطاف, in the graph search and in the chat."""

from __future__ import annotations

import pytest

from sherlocks.linkgraph.normalize import sound_key, sounds_like

PAIRS = [("altaf", "الطاف"), ("muhammad", "محمد"), ("ahmed", "احمد"), ("rashid", "رشید"), ("wasim", "وسیم"),
         ("waqas", "وقاص"), ("zahid", "زاہد"), ("saima", "صائمہ"), ("khalid", "خالد"), ("abdul rehman", "عبدالرحمن"),
         ("shahzeb", "شاہ زیب"), ("sajid", "ساجد"), ("kamran", "کامران"), ("imran", "عمران")]


@pytest.mark.parametrize(("english", "urdu"), PAIRS)
def test_english_and_urdu_spellings_sound_alike(english, urdu):
    assert sounds_like(english, urdu) and sounds_like(urdu, english)


def test_partial_names_and_no_false_matches():
    assert sounds_like("altaf", "محمد الطاف حسین")          # a later word of the name
    assert sounds_like("muhammad altaf", "محمد الطاف")
    assert not sounds_like("ali", "الطاف")
    assert not sounds_like("kamran", "عمران")
    assert sound_key("Mohammad") == sound_key("Muhammad") == "MD"


def test_chat_finds_an_urdu_named_person_from_roman_urdu():
    from sherlocks.linkgraph.investigator import _mentioned
    from sherlocks.linkgraph.network import PersonNetwork

    net = PersonNetwork({"nodes": [{"id": "a", "kind": "person", "label": "محمد الطاف", "data": {}},
                                   {"id": "b", "kind": "person", "label": "Kamran Ahmed", "data": {}}], "edges": []})
    assert set(_mentioned(net, "altaf ka kamran se kya taluq hai?")) == {"a", "b"}
    assert net.resolve("altaf") == "a" and net.resolve("کامران") == "b"


def test_a_distinctive_name_word_finds_the_person_and_their_cases():
    from sherlocks.linkgraph.investigator import investigate
    from sherlocks.linkgraph.network import PersonNetwork

    graph = {"nodes": [
        {"id": "a", "kind": "person", "label": "Muhammad Danish Rafiq", "data": {
            "seed": True, "flags": ["criminal_record"], "cnic": "9999900000011", "records": [{"system": "psrms", "summary": ""}],
            "firs": [{"label": "45/2023", "ps": "PS Gulshan", "role": "Accused", "offence": "395 PPC", "system": "psrms"},
                     {"label": "112/2024", "ps": "PS Ferozabad", "role": "Complainant", "system": "cro"}]}},
        {"id": "b", "kind": "person", "label": "Muhammad Ali", "data": {}}], "edges": []}
    assert PersonNetwork(graph).resolve("danish") == "a"
    answer = list(investigate(graph, "I mean which cases danish is involve ?"))[-1]["answer"]
    assert answer.startswith("Muhammad Danish Rafiq") and "FIR 45/2023 at PS Gulshan - Accused - 395 PPC" in answer
    assert "FIR 112/2024 at PS Ferozabad - Complainant" in answer
