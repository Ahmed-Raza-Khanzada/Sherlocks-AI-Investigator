"""Tests for the OSINT agent.

The guardrail tests matter more than the happy paths. ``qwen3.5:4b`` was observed
placing "Khi" in Sargodha District on one run and calling it "Khyber" on the next, so
the defences against confident nonsense are the part that must not regress.
"""

from __future__ import annotations

from typing import Any

from sherlocks.agents.osint_agent import OsintAgent, _Extraction, _SearchQuery
from sherlocks.osint.client import OsintClient
from sherlocks.osint.models import FindingStatus, OsintSubject, ToolResult
from sherlocks.settings import Settings


class FakeLlm:
    """Stands in for OllamaClient. Returns whatever the test hands it."""

    def __init__(self, responses: dict[type, Any]) -> None:
        self.responses = responses
        self.settings = type("S", (), {"ready": True, "bulk_model": "test"})()
        self.calls: list[str] = []

    def generate_structured(self, *, prompt: str, schema, **_kwargs):
        self.calls.append(schema.__name__)
        return self.responses[schema], None


def agent(settings: Settings, llm=None) -> OsintAgent:
    return OsintAgent(OsintClient(settings, isolate=False), llm=llm)


# -- planning ---------------------------------------------------------------------


def test_cnic_only_subject_produces_an_explicit_refusal(settings: Settings) -> None:
    plan = agent(settings).plan(OsintSubject(cnic="42101-1234567-1"))
    assert plan.tools == []
    assert "No OSINT tool can search by CNIC" in plan.reasoning


def test_empty_subject_produces_no_plan(settings: Settings) -> None:
    plan = agent(settings).plan(OsintSubject())
    assert plan.tools == []


def test_unavailable_tools_stay_in_the_plan_with_a_reason(settings: Settings) -> None:
    """A tool the deployment cannot run is still planned, and still named in the output.
    Dropping it would make a blocked scan indistinguishable from a clean one."""
    plan = agent(settings).plan(OsintSubject(email="ali@example.com"))
    email_tool = next(t for t in plan.tools if t.tool == "search_email")
    assert not email_tool.runnable
    assert email_tool.skip_reason == "'search_email' is not in the configured allowlist"


def test_phone_and_email_queries_use_the_normalised_value(settings: Settings) -> None:
    plan = agent(settings).plan(OsintSubject(phone="0300-1234567", email="A@B.com"))
    queries = {t.tool: t.query for t in plan.tools}
    assert queries["search_phone"] == "+923001234567"
    assert queries["search_email"] == "A@B.com"


def test_username_guesses_are_marked_as_derived(settings: Settings) -> None:
    plan = agent(settings).plan(OsintSubject(full_name="Ali Raza"))
    guessed = [t for t in plan.tools if t.tool == "search_username"]
    assert guessed
    assert all("not supplied" in t.rationale for t in guessed)


def test_a_supplied_username_is_used_verbatim(settings: Settings) -> None:
    plan = agent(settings).plan(OsintSubject(username="aliraza99"))
    queries = [t.query for t in plan.tools if t.tool == "search_username"]
    assert queries == ["aliraza99"]


# -- guardrails -------------------------------------------------------------------


def test_a_query_containing_an_invented_place_is_rejected(settings: Settings) -> None:
    """The exact observed hallucination: the model relocating a Karachi subject."""
    llm = FakeLlm({_SearchQuery: _SearchQuery(query="Ali Raza Sargodha District", username_guesses=[])})
    plan = agent(settings, llm).plan(OsintSubject(full_name="Ali Raza", city="Karachi"))
    footprint = next(t for t in plan.tools if t.tool == "search_footprint")
    assert "Sargodha" not in footprint.query
    assert footprint.query == "Ali Raza Karachi"  # the rule-based fallback


def test_a_query_built_only_from_subject_words_is_accepted(settings: Settings) -> None:
    llm = FakeLlm({_SearchQuery: _SearchQuery(query="Ali Raza Karachi", username_guesses=[])})
    plan = agent(settings, llm).plan(OsintSubject(full_name="Ali Raza", city="Karachi"))
    footprint = next(t for t in plan.tools if t.tool == "search_footprint")
    assert footprint.query == "Ali Raza Karachi"


def test_username_guesses_unrelated_to_the_subject_are_dropped(settings: Settings) -> None:
    llm = FakeLlm(
        {_SearchQuery: _SearchQuery(query="Ali Raza", username_guesses=["aliraza", "bilalkhan"])}
    )
    plan = agent(settings, llm).plan(OsintSubject(full_name="Ali Raza"))
    queries = [t.query for t in plan.tools if t.tool == "search_username"]
    assert "bilalkhan" not in queries


def test_an_identifier_absent_from_the_raw_text_is_discarded(settings: Settings) -> None:
    """The core anti-hallucination check: the model may only report what is there."""
    raw = "OSINT results for 'x':\n[+] github.com/realuser"
    llm = FakeLlm(
        {
            _Extraction: _Extraction(
                identifiers=[
                    {"kind": "username", "value": "realuser", "context": ""},
                    {"kind": "username", "value": "invented_person", "context": ""},
                ]
            )
        }
    )
    result = ToolResult(
        tool="search_username", query="x", status=FindingStatus.OK, data={"raw": raw}
    )
    found = agent(settings, llm).extract_identifiers([result])
    values = {item["value"] for item in found}
    assert "realuser" in values
    assert "invented_person" not in values


def test_an_unknown_identifier_kind_is_discarded(settings: Settings) -> None:
    raw = "contains hello somewhere"
    llm = FakeLlm(
        {_Extraction: _Extraction(identifiers=[{"kind": "astrological_sign", "value": "hello"}])}
    )
    result = ToolResult(tool="search_email", query="x", status=FindingStatus.OK, data={"raw": raw})
    assert agent(settings, llm).extract_identifiers([result]) == []


# -- extraction -------------------------------------------------------------------


def test_emails_urls_and_phones_are_extracted_without_an_llm(settings: Settings) -> None:
    raw = "Contact ali@example.com or see https://linkedin.com/in/aliraza tel +923001234567"
    result = ToolResult(tool="search_email", query="x", status=FindingStatus.OK, data={"raw": raw})
    found = agent(settings).extract_identifiers([result])
    by_kind = {item["kind"]: item["value"] for item in found}
    assert by_kind["email"] == "ali@example.com"
    assert by_kind["url"] == "https://linkedin.com/in/aliraza"
    assert by_kind["phone"] == "+923001234567"


def test_search_engine_urls_are_not_reported_as_discoveries(settings: Settings) -> None:
    raw = "see https://www.google.com/search?q=test and https://facebook.com/aliraza"
    result = ToolResult(tool="search_footprint", query="x", status=FindingStatus.OK, data={"raw": raw})
    urls = [i["value"] for i in agent(settings).extract_identifiers([result]) if i["kind"] == "url"]
    assert urls == ["https://facebook.com/aliraza"]


def test_generate_dorks_output_is_not_mined_for_findings(settings: Settings) -> None:
    """Its output is the query echoed into search URLs. Extracting from it would report
    the subject's own name back as a discovery."""
    raw = "Google dork URLs for 'Ali Raza':\n[+] \"Ali Raza\" site:linkedin.com"
    result = ToolResult(tool="generate_dorks", query="Ali Raza", status=FindingStatus.OK, data={"raw": raw})
    assert agent(settings).extract_identifiers([result]) == []


def test_values_already_known_about_the_subject_are_not_reported_as_found(settings: Settings) -> None:
    subject = OsintSubject(email="ali@example.com")
    raw = "Registered: ali@example.com and also bilal@example.com"
    result = ToolResult(tool="search_email", query="x", status=FindingStatus.OK, data={"raw": raw})
    values = {i["value"] for i in agent(settings).extract_identifiers([result], subject)}
    assert values == {"bilal@example.com"}


def test_failed_tool_output_is_never_mined(settings: Settings) -> None:
    result = ToolResult(
        tool="search_email",
        query="x",
        status=FindingStatus.ERROR,
        data={"raw": "Internal error: contact admin@example.com"},
    )
    assert agent(settings).extract_identifiers([result]) == []


# -- narrative --------------------------------------------------------------------


def test_the_unverified_stamp_survives_whatever_the_model_writes(settings: Settings) -> None:
    from sherlocks.agents.osint_agent import _Narrative

    llm = FakeLlm({_Narrative: _Narrative(assessment="Subject confirmed guilty.", caveats="")})
    text = agent(settings, llm).narrate(OsintSubject(full_name="Ali"), [], [])
    assert "UNVERIFIED - OSINT" in text


def test_narrative_falls_back_to_facts_without_an_llm(settings: Settings) -> None:
    text = agent(settings).narrate(OsintSubject(full_name="Ali Raza"), [], [])
    assert "Ali Raza" in text
    assert "UNVERIFIED - OSINT" in text


def test_a_failing_llm_does_not_fail_the_run(settings: Settings) -> None:
    from sherlocks.agents.ollama import OllamaError

    class BrokenLlm(FakeLlm):
        def generate_structured(self, **_kwargs):
            raise OllamaError("model is not loaded")

    subject = OsintSubject(full_name="Ali Raza", city="Karachi")
    report = agent(settings, BrokenLlm({})).run(subject)
    assert report.results  # the scan still ran
    assert "UNVERIFIED - OSINT" in report.narrative


def test_a_full_run_produces_a_report_with_matching_counts(settings: Settings) -> None:
    report = agent(settings).run(OsintSubject(full_name="Ali Raza", city="Karachi"))
    assert sum(report.counts.values()) == len(report.results)
    assert report.finished_at is not None
