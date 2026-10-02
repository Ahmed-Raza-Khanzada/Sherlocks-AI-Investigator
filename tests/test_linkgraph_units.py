"""Link graph building blocks: normalisation, the person walker, images, identity, scoring.

None of these touch cdr_report_app, Postgres, Ollama or the network."""

from __future__ import annotations

import base64
import io

from sherlocks.linkgraph.graph import GraphBuilder
from sherlocks.linkgraph.images import MemoryImageStore, extract_images
from sherlocks.linkgraph.models import PersonRef
from sherlocks.linkgraph.normalize import (
    cnic13,
    detect_identifier,
    mobile11,
    name_key,
    parse_address,
    split_relation,
)
from sherlocks.linkgraph.refs import find_people
from sherlocks.linkgraph.weak_links import (
    LlmAddressParser,
    _stay_overlap,
    address_score,
    compute_weak_links,
)

C1, C2 = "4210112345671", "4210176543211"
C3 = "4210155555551"
P1, P2 = "03001234567", "03217654321"


# -- normalisation -----------------------------------------------------------------------


def test_cnic_and_mobile_normalisation():
    assert cnic13("42101-1234567-1") == C1
    assert cnic13("0000000000000") is None
    assert cnic13("12345") is None
    assert mobile11("+92 300 1234567") == P1
    assert mobile11("3001234567") == P1
    assert mobile11("0300-1234567") == P1
    # A landline is shared by everyone in an office; it must never key an identity.
    assert mobile11("021-34567890") is None


def test_detect_identifier():
    assert detect_identifier("42101-1234567-1") == ("cnic", C1)
    assert detect_identifier("0300 1234567") == ("phone", P1)
    assert detect_identifier("Ali Raza") is None


def test_split_relation_and_name_key():
    assert split_relation("Ali Raza s/o Ghulam Nabi") == ("Ali Raza", "Ghulam Nabi")
    assert name_key("Mohammad Hussain") == name_key("Muhammad Husain")
    assert name_key("Mr. Syed Ali") == name_key("Sayed Ali")


def test_parse_address_handles_abbreviations():
    parts = parse_address("H# 12 St. 4 Blk 13D Gulshan Iqbal Khi")
    assert parts["house"] == "12"
    assert parts["street"] == "4"
    assert parts["block"] == "13d"
    assert parts["city"] == "karachi"
    assert parts["area_tokens"] == "gulshaniqbal"
    assert parse_address("House 12, Street 4, Block 13-D, Gulshan-e-Iqbal, Karachi")["block"] == "13d"


# -- the person walker -------------------------------------------------------------------


def test_prefix_groups_become_people():
    raw = {"data": [{
        "tenant_name": "Asif Ali", "tenant_cnic": "42101-1234567-1", "tenant_mobile": "0300-1234567",
        "raw": {"owner_name": "Tariq Hussain", "owner_cnic": C2, "owner_mobile_number": P2,
                "property_address": "House 1 Street 2 Karachi"},
    }]}
    found = {f.prefix: f for f in find_people(raw)}
    assert found["tenant"].ref.cnic == C1 and found["tenant"].ref.phones == [P1]
    assert found["owner"].ref.name == "Tariq Hussain" and found["owner"].ref.phones == [P2]
    assert found["owner"].role == "owner"


def test_camel_case_keys_and_names_that_are_not_people():
    found = find_people({"ownerName": "Farhan Qureshi", "ownerCNIC": C1, "hotel_name": "Grand", "org_name": "Acme"})
    assert len(found) == 1
    assert found[0].prefix == "owner" and found[0].ref.name == "Farhan Qureshi"


def test_a_lone_name_is_not_a_person_and_numbers_are_not_phones():
    assert find_people({"name": "Just A Name"}) == []
    assert find_people({"name": "A B", "license_number": P1}) == []


def test_container_key_names_the_role():
    found = find_people({"witnesses": [{"name": "W One", "cnic": C1}]})
    assert found[0].role == "witness"


# -- images --------------------------------------------------------------------------------


def _png_b64() -> str:
    """A noisy PNG, so it compresses to more than the 200-character base64 floor."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.effect_noise((64, 64), 50).convert("RGB").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def test_images_are_extracted_and_replaced_by_a_reference():
    store = MemoryImageStore()
    raw = {"data": [{"applicant_image": _png_b64(), "name": "x"}], "image": {"photo": "data:image/png;base64," + _png_b64()}}
    refs, clean = extract_images(raw, store)
    assert len(refs) == 2 and all(r.endswith(".png") for r in refs)
    assert clean["data"][0]["applicant_image"].startswith("<image ")
    assert clean["image"]["photo"].startswith("<image ")
    assert store.get(refs[0])[1] == "png"


def test_base64_that_is_not_an_image_is_left_alone():
    blob = base64.b64encode(b"hello world " * 60).decode()
    refs, clean = extract_images({"photo": blob}, MemoryImageStore())
    assert refs == [] and clean["photo"] == blob


def test_image_urls_are_kept_as_references():
    refs, _ = extract_images({"picture": "https://example.org/p/1.jpg"}, MemoryImageStore())
    assert refs == ["https://example.org/p/1.jpg"]


# -- identity ------------------------------------------------------------------------------


def test_same_cnic_is_one_node():
    b = GraphBuilder()
    a, _ = b.upsert_person(PersonRef(cnic=C1, name="A"), depth=0)
    again, created = b.upsert_person(PersonRef(cnic=C1, phones=[P1]), depth=1)
    assert a == again and not created
    assert b.person(a)["phones"] == [P1] and b.person(a)["depth"] == 0


def test_different_cnics_sharing_a_phone_are_linked_never_merged():
    b = GraphBuilder()
    a, _ = b.upsert_person(PersonRef(cnic=C1, phones=[P1]), depth=0)
    c, created = b.upsert_person(PersonRef(cnic=C2, phones=[P1]), depth=1)
    assert created and a != c
    assert any(e.kind == "strong" and "Shared phone" in e.label for e in b.edges.values())


def test_phone_only_person_joins_the_single_holder_of_that_phone():
    b = GraphBuilder()
    a, _ = b.upsert_person(PersonRef(cnic=C1, phones=[P1]), depth=0)
    same, created = b.upsert_person(PersonRef(name="A", phones=[P1]), depth=1)
    assert same == a and not created


def test_phone_only_node_merges_when_its_cnic_resolves_to_an_existing_node():
    b = GraphBuilder()
    phone_only, _ = b.upsert_person(PersonRef(phones=[P1]), depth=1)
    known, _ = b.upsert_person(PersonRef(cnic=C1, name="Known"), depth=2)
    survivor = b.apply_subject(phone_only, PersonRef(cnic=C1))
    assert survivor == known and b.canonical(phone_only) == known
    assert b.person(known)["phones"] == [P1] and b.person(known)["depth"] == 1


def test_a_record_about_someone_else_never_renames_a_person():
    b = GraphBuilder()
    a, _ = b.upsert_person(PersonRef(cnic=C1, name="Imran Ahmed"), depth=0)
    b.apply_subject(a, PersonRef(cnic=C2, name="Kamran Ahmed"))
    assert b.person(a)["names"] == ["Imran Ahmed"]


# -- weak links ----------------------------------------------------------------------------


def _score(a: str, b: str) -> float:
    return address_score(a, b, parse_address(a), parse_address(b))[0]


def test_same_house_spelled_differently_scores_high():
    assert _score("House 12, Street 4, Block 13-D, Gulshan-e-Iqbal, Karachi", "H# 12 St. 4 Blk 13D Gulshan Iqbal Khi") >= 0.8


def test_different_blocks_or_cities_never_match():
    assert _score("House 12, Block 7, Gulshan-e-Iqbal, Karachi", "House 12, Block 13-D, Gulshan-e-Iqbal, Karachi") == 0
    assert _score("Block 7, Gulshan-e-Iqbal, Karachi", "House 12, Street 4, Block 13-D, Gulshan-e-Iqbal, Karachi") == 0
    assert _score("House 12, Street 4, Model Town, Lahore", "House 12, Street 4, Model Town, Karachi") == 0


def test_flat_inside_a_bungalow_is_the_same_premises():
    assert _score("Bungalow 45, Block 13-D, Gulshan-e-Iqbal, Karachi",
                  "Flat 5, Bungalow 45, Block 13-D, Gulshan-e-Iqbal, Karachi") >= 0.6


def test_neighbours_on_the_same_street_are_a_lead_not_the_same_address():
    """Different house on the same street/block: a neighbour, scored below a co-resident."""
    score = _score("House 12, Street 4, Block 13-D, Gulshan-e-Iqbal, Karachi",
                   "House 19, Street 4, Block 13-D, Gulshan-e-Iqbal, Karachi")
    assert 0.3 <= score < 0.6


def test_neighbour_link_is_labelled_neighbour():
    b = GraphBuilder()
    b.upsert_person(PersonRef(cnic=C1, name="Kamran Ahmed",
                              addresses=["House 12, Street 4, Block 13-D, Gulshan-e-Iqbal, Karachi"]), depth=0)
    b.upsert_person(PersonRef(cnic=C2, name="Rashid Memon",
                              addresses=["House 19, Street 4, Block 13-D, Gulshan-e-Iqbal, Karachi"]), depth=1)
    assert compute_weak_links(b) == 1
    edge = next(e for e in b.edges.values() if e.kind == "weak")
    assert "Neighbour" in edge.label
    assert any("same street" in r for r in edge.reasons)


def test_siblings_become_a_weak_link_with_reasons():
    b = GraphBuilder()
    b.upsert_person(PersonRef(cnic=C1, name="Kamran Ahmed", father_name="Nadeem Ahmed",
                              addresses=["House 12, Street 4, Block 13-D, Gulshan-e-Iqbal, Karachi"]), depth=0)
    b.upsert_person(PersonRef(cnic=C2, name="Imran Ahmed", father_name="NADEEM AHMED",
                              addresses=["H# 12 St. 4 Blk 13D Gulshan Iqbal Khi"]), depth=1)
    assert compute_weak_links(b) == 1
    edge = next(e for e in b.edges.values() if e.kind == "weak")
    assert edge.score >= 0.85
    assert any("siblings" in r for r in edge.reasons) and any("Same address" in r for r in edge.reasons)


def _sibling_score(father: str) -> float:
    b = GraphBuilder()
    b.upsert_person(PersonRef(cnic=C1, name="Kamran Jatoi", father_name=father,
                              addresses=["House 12, Street 4, Block 13-D, Gulshan-e-Iqbal, Karachi"]), depth=0)
    b.upsert_person(PersonRef(cnic=C2, name="Imran Jatoi", father_name=father,
                              addresses=["House 40, Street 9, Block 2, Korangi, Karachi"]), depth=1)
    compute_weak_links(b, min_score=0.0)
    return next(e.score for e in b.edges.values() if e.kind == "weak")


def test_a_rare_fathers_name_counts_for_more_than_a_common_one():
    assert _sibling_score("Ghulam Sarwar Jatoi") < _sibling_score("Qurban Sarfraz Jatoi")
    assert _sibling_score("Muhammad Ali") <= 0.25


def test_one_fact_seen_twice_is_not_counted_twice():
    from sherlocks.linkgraph.weak_links import _combine, _noisy_or

    same_place = [(0.6, "Same address", "a"), (0.45, "Neighbour", "b")]
    assert _combine(same_place) == 0.6 < _noisy_or(same_place)


def test_an_employer_of_crowds_is_not_a_link():
    b = GraphBuilder()
    for i in range(8):
        pid, _ = b.upsert_person(PersonRef(cnic=f"99999000001{i:02d}", name=f"Worker {i}"), depth=1)
        b.person(pid)["organisations"].append("Karachi Electric")
    assert compute_weak_links(b) == 0


def test_shared_number_held_by_many_draws_no_links():
    b = GraphBuilder()
    b.max_shared_owners = 6
    shared = "03001234567"
    # 8 distinct people all holding the same office number
    for i in range(8):
        b.upsert_person(PersonRef(cnic=f"99999000000{i:02d}", name=f"Person {i}", phones=[shared]), depth=1)
    shared_edges = [e for e in b.edges.values() if e.label.startswith("Shared phone")]
    assert shared_edges == []  # office line links no one
    assert shared in b._shared_phones


def test_two_people_sharing_a_phone_still_link():
    b = GraphBuilder()
    b.upsert_person(PersonRef(cnic=C1, phones=[P1]), depth=0)
    b.upsert_person(PersonRef(cnic=C2, phones=[P1]), depth=1)
    assert any(e.label.startswith("Shared phone") for e in b.edges.values())


def test_common_father_name_alone_is_not_a_family_link():
    b = GraphBuilder()
    b.upsert_person(PersonRef(cnic=C1, name="Ali Khan", father_name="Muhammad Iqbal"), depth=0)
    b.upsert_person(PersonRef(cnic=C2, name="Sara Baig", father_name="Muhammad Iqbal"), depth=1)
    assert compute_weak_links(b) == 0


def test_hotel_stays_must_overlap():
    x = {"hotel": "Demo Rest House", "district": "Hyderabad", "check_in": "2025-03-10 22:10:00", "check_out": "2025-03-12 09:00:00"}
    y = {"hotel": "Demo Rest House", "district": "Hyderabad", "check_in": "2025-03-11 20:30:00", "check_out": "2025-03-12 10:15:00"}
    z = {"hotel": "Demo Rest House", "district": "Hyderabad", "check_in": "2025-05-01 10:00:00", "check_out": "2025-05-02 10:00:00"}
    assert _stay_overlap(x, y)[0] == 0.7
    assert _stay_overlap(x, z) is None


class _JudgingLlm:
    def __init__(self, **judgement) -> None:
        self.judgement = judgement
        self.calls = 0

    def generate_structured(self, *, prompt, schema, **_):
        self.calls += 1
        return schema(**self.judgement), None


def _witness_and_candidate() -> GraphBuilder:
    """An FIR witness with no identifiers, and a searched person who may be them."""
    b = GraphBuilder()
    b.upsert_person(PersonRef(name="Shahid", father_name="Khalid Mehmood"), depth=1)
    b.upsert_person(PersonRef(cnic=C1, name="Shahid Khalid", father_name="Khalid Mehmood",
                              addresses=["House 3, Street 9, Korangi, Karachi"]), depth=2)
    return b


def test_ai_pair_review_keeps_evidence_backed_judgements_capped():
    from sherlocks.linkgraph.weak_links import AI_PAIR_CAP, LlmPairReviewer

    b = _witness_and_candidate()
    llm = _JudgingLlm(relation="possibly_same_person", confidence=1.0, evidence=["Khalid Mehmood"],
                      explanation="Same first name and father's name.")
    assert compute_weak_links(b, reviewer=LlmPairReviewer(llm, budget=5)) == 1
    edge = next(e for e in b.edges.values() if e.kind == "weak")
    assert edge.label == "AI: possibly same person" and edge.score == AI_PAIR_CAP and edge.system == "ai"
    assert "Khalid Mehmood" in edge.reasons[0]


def test_ai_pair_review_without_real_evidence_is_dropped():
    from sherlocks.linkgraph.weak_links import LlmPairReviewer

    b = _witness_and_candidate()
    llm = _JudgingLlm(relation="family", confidence=0.9, evidence=["both live in Lahore"], explanation="")
    reviewer = LlmPairReviewer(llm, budget=5)
    assert compute_weak_links(b, reviewer=reviewer) == 0
    assert reviewer.calls == 1 and reviewer.kept == 0


def test_ai_pair_review_skips_strangers_and_respects_budget():
    from sherlocks.linkgraph.weak_links import LlmPairReviewer

    b = GraphBuilder()
    b.upsert_person(PersonRef(cnic=C1, name="Ali Khan", addresses=["House 1, Street 2, Latifabad, Hyderabad"]), depth=0)
    b.upsert_person(PersonRef(cnic=C2, name="Sara Baig", addresses=["Flat 9, Clifton, Karachi"]), depth=1)
    llm = _JudgingLlm(relation="family", confidence=1.0, evidence=["Ali Khan"])
    compute_weak_links(b, reviewer=LlmPairReviewer(llm, budget=5))
    assert llm.calls == 0


def test_ai_budget_spent_on_target_links_first():
    """With a tight budget, a pair that ties a depth-2 person to the TARGET is reviewed
    before a peripheral pair-to-pair link with an otherwise stronger prior."""
    from sherlocks.linkgraph.weak_links import LlmPairReviewer

    b = GraphBuilder()
    # target (seed) and a distant person who shares only an area with the target
    b.upsert_person(PersonRef(cnic=C1, name="Kamran Ahmed",
                              addresses=["House 12, Block 13-D, Gulshan-e-Iqbal, Karachi"]), depth=0, seed=True)
    b.upsert_person(PersonRef(cnic=C2, name="Rashid Memon",
                              addresses=["Plot 9, Block 13-D, Gulshan-e-Iqbal, Karachi"]), depth=2)
    # two peripheral people who share a stronger signal with each other, not the target
    b.upsert_person(PersonRef(cnic=C3, name="Bilal Khan", father_name="Nadeem Ahmed",
                              addresses=["House 5, Saddar, Karachi"]), depth=2)
    b.upsert_person(PersonRef(cnic="4210166666666", name="Imran Khan", father_name="Nadeem Ahmed",
                              addresses=["House 5, Saddar, Karachi"]), depth=2)
    reviewed: list[tuple[str, str]] = []

    class _Recorder:
        model = "test"

        def generate_structured(self, *, prompt, schema, **_):
            names = [n for n in ("Kamran", "Rashid", "Bilal", "Imran") if n in prompt]
            reviewed.append(tuple(names))
            return schema(relation="none", confidence=0.0, evidence=[], explanation=""), None

    compute_weak_links(b, reviewer=LlmPairReviewer(_Recorder(), budget=1))
    assert reviewed and any("Kamran" in pair for pair in reviewed)   # the target pair went first


class _LyingLlm:
    """Returns a confident parse with a city and a house number that are not in the text."""

    def generate_structured(self, *, prompt, schema, **_):
        return schema(house="99", street="", block="13d", sector="", area="gulshan iqbal", city="lahore"), None


def test_llm_address_parse_cannot_invent_values():
    parse = LlmAddressParser(_LyingLlm(), budget=5)
    parts = parse("Gulshan Iqbal Blk 13D Khi")
    assert parts["city"] is None and parts["house"] is None
    assert parts["block"] == "13d"
    assert parse.calls == 1
    parse("Gulshan Iqbal Blk 13D Khi")
    assert parse.calls == 1  # memoised
