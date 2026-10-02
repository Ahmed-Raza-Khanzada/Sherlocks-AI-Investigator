"""The OSINT agent: plans a run, executes it, and reads the results back.

Division of labour, and it is deliberate:

* **Deterministic code chooses the tools.** Which tool can consume which identifier is
  a fixed property of the catalogue, not a judgement call, so ``tools_for_kinds`` picks
  the set and :class:`OsintClient` vetoes anything unavailable. A 4B model given control
  of tool selection would produce an unauditable run.
* **Regex extracts the identifiers that have a syntax.** Emails, URLs and phone numbers
  are lexical, so they are pulled out of the raw output deterministically and are always
  complete.
* **The LLM does the two jobs it is actually good at**: composing a natural-language
  search query out of scattered subject fields, and turning the results into prose.
  Anything it claims to have *found* is checked against the raw text before it survives.

Every identifier this agent surfaces is a **lead**, not a fact. Nothing here writes an
``edge`` row, and nothing here raises a finding above ``confidence='low'``. That is a
human decision, recorded on the database row.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from sherlocks.agents.ollama import OllamaClient, OllamaError
from sherlocks.osint.client import OsintClient
from sherlocks.osint.models import (
    FindingStatus,
    IdentifierKind,
    OsintPlan,
    OsintReport,
    OsintSubject,
    PlannedTool,
    ToolResult,
)
from sherlocks.osint.tools import TOOL_CATALOG, tools_for_kinds

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+")
_PHONE_RE = re.compile(r"\+\d[\d\s\-()]{7,17}\d")

# Kinds the LLM is permitted to label a discovered identifier with. Anything else is
# dropped rather than stored - an unbounded vocabulary would make findings unqueryable.
_ALLOWED_DISCOVERY_KINDS = {
    "username",
    "email",
    "url",
    "phone",
    "platform",
    "employer",
    "location",
    "associate_name",
}

# Search-engine result pages are not findings about the subject; they are the query
# echoed back. Filtering them stops a "discovered URL" list that is 80% google.com.
_NOISE_URL_HOSTS = ("google.com", "google.co", "bing.com", "duckduckgo.com")


class _SearchQuery(BaseModel):
    """LLM output: one search string built from the subject's known attributes."""

    query: str = Field(description="A single search-engine query, no operators, no quotes.")
    username_guesses: list[str] = Field(
        default_factory=list,
        description="Plausible usernames derived ONLY from the subject's own name or email.",
    )
    rationale: str = ""


class _DiscoveredIdentifier(BaseModel):
    kind: str
    value: str
    context: str = ""


class _Extraction(BaseModel):
    identifiers: list[_DiscoveredIdentifier] = Field(default_factory=list)
    summary: str = ""


class _Narrative(BaseModel):
    assessment: str
    caveats: str = ""


_PLAN_SYSTEM = (
    "You build search queries for a police OSINT analyst. Use only the facts given. "
    "Never invent a name, city, employer or username that is not present in the input. "
    "Reply with JSON only."
)

_EXTRACT_SYSTEM = (
    "You read raw OSINT tool output and list the identifiers it contains. "
    "Copy values verbatim from the text. Never infer, expand, correct or guess a value "
    "that is not literally present. Reply with JSON only."
)

_NARRATIVE_SYSTEM = (
    "You write a short, factual summary of an OSINT scan for a police report. "
    "State only what the findings show. Do not speculate about guilt, character or "
    "relationships. Reply with JSON only."
)


class OsintAgent:
    """Plans, runs and interprets one OSINT scan.

    ``llm`` is optional throughout. With Ollama down the agent still produces a complete
    run - the query is composed by rule and the narrative is a rendered fact list.
    """

    def __init__(self, client: OsintClient, llm: OllamaClient | None = None) -> None:
        self.client = client
        self.llm = llm

    # -- planning -------------------------------------------------------------

    def plan(self, subject: OsintSubject) -> OsintPlan:
        """Decide what to run. Unavailable tools stay in the plan, marked with a reason.

        Keeping them visible is the point: a report that silently omits ``search_email``
        looks identical to one where the subject has no email footprint.
        """
        if subject.is_empty():
            return OsintPlan(
                subject_label=subject.label(),
                reasoning="No searchable identifier was supplied.",
            )

        kinds = subject.available_kinds()
        candidates = tools_for_kinds(kinds)

        if not candidates and IdentifierKind.CNIC in kinds:
            # Worth stating plainly rather than returning an empty plan: a CNIC is how
            # an investigator identifies someone here, and it searches nothing online.
            return OsintPlan(
                subject_label=subject.label(),
                reasoning=(
                    "Only a CNIC was supplied. No OSINT tool can search by CNIC - "
                    "add a name, phone, email or username to run a scan."
                ),
            )

        name_query, username_guesses, reasoning = self._compose_queries(subject)

        # No username was supplied, but a name yields plausible handles. Scanning those
        # is standard practice and cheap; the rationale records that they are guesses so
        # a hit is never mistaken for a confirmed account.
        if (
            IdentifierKind.USERNAME not in kinds
            and username_guesses
            and "search_username" not in candidates
        ):
            candidates = candidates + ["search_username"]

        planned: list[PlannedTool] = []
        for tool in candidates:
            spec = TOOL_CATALOG[tool]
            guessed = spec.accepts == IdentifierKind.USERNAME and not subject.username
            for query in self._queries_for(spec.accepts, subject, name_query, username_guesses):
                planned.append(
                    PlannedTool(
                        tool=tool,
                        query=query,
                        rationale=(
                            f"{spec.description} Username derived from the subject's name, not supplied."
                            if guessed
                            else spec.description
                        ),
                        skip_reason=self.client.skip_reason(tool),
                    )
                )

        return OsintPlan(subject_label=subject.label(), tools=planned, reasoning=reasoning)

    def _queries_for(
        self,
        accepts: IdentifierKind,
        subject: OsintSubject,
        name_query: str,
        username_guesses: list[str],
    ) -> list[str]:
        if accepts == IdentifierKind.PHONE:
            return [subject.phone] if subject.phone else []
        if accepts == IdentifierKind.EMAIL:
            return [subject.email] if subject.email else []
        if accepts == IdentifierKind.USERNAME:
            # A supplied username is a fact; guesses are only worth spending a scan on
            # when there is nothing better, and never more than two.
            if subject.username:
                return [subject.username]
            return username_guesses[:2]
        if accepts == IdentifierKind.FULL_NAME:
            return [name_query] if name_query else []
        return []

    def _compose_queries(self, subject: OsintSubject) -> tuple[str, list[str], str]:
        """Build the free-text query. LLM if available, rule-based otherwise."""
        rule_query = " ".join(
            part for part in (subject.full_name, subject.employer, subject.city) if part
        ).strip()

        if self.llm is None or not self.llm.settings.ready or not subject.full_name:
            return rule_query, self._rule_usernames(subject), "Query composed by rule."

        facts = "\n".join(
            f"{label}: {value}"
            for label, value in (
                ("Full name", subject.full_name),
                ("City", subject.city),
                ("Employer", subject.employer),
                ("Email", subject.email),
                ("Known username", subject.username),
                ("Notes", subject.notes),
            )
            if value
        )
        prompt = (
            "Subject facts:\n"
            f"{facts}\n\n"
            "Write one search-engine query that would find this person's public online "
            "presence, and up to three plausible usernames derived only from their name "
            "or email local-part."
        )

        try:
            parsed, _meta = self.llm.generate_structured(
                prompt=prompt,
                schema=_SearchQuery,
                system=_PLAN_SYSTEM,
                cache_kind="osint_query",
                prompt_version=PROMPT_VERSION,
            )
        except OllamaError as exc:
            logger.info("osint agent: query composition fell back to rules (%s)", exc)
            return rule_query, self._rule_usernames(subject), "Query composed by rule (LLM unavailable)."

        query = self._vet_query(parsed.query, subject) or rule_query
        guesses = [
            guess
            for guess in (g.strip() for g in parsed.username_guesses)
            if guess and self._vet_username(guess, subject)
        ]
        if not guesses:
            guesses = self._rule_usernames(subject)
        return query, guesses[:3], parsed.rationale or "Query composed by the OSINT agent."

    @staticmethod
    def _subject_tokens(subject: OsintSubject) -> set[str]:
        blob = " ".join(
            part
            for part in (
                subject.full_name,
                subject.city,
                subject.employer,
                subject.username,
                (subject.email or "").split("@")[0],
                subject.notes,
            )
            if part
        )
        return {tok for tok in re.split(r"[^A-Za-z0-9]+", blob.lower()) if len(tok) > 1}

    def _vet_query(self, query: str, subject: OsintSubject) -> str | None:
        """Reject a query containing words the subject data never mentioned.

        This is the guardrail that stops the model helpfully adding "Sargodha" to a
        Karachi subject - the exact hallucination this model was observed making.
        """
        query = (query or "").strip().strip('"')
        if not query:
            return None
        allowed = self._subject_tokens(subject)
        tokens = [tok for tok in re.split(r"[^A-Za-z0-9]+", query.lower()) if len(tok) > 1]
        if not tokens:
            return None
        invented = [tok for tok in tokens if tok not in allowed]
        if invented:
            logger.info("osint agent: rejecting query with invented terms %s", invented)
            return None
        return query

    def _vet_username(self, guess: str, subject: OsintSubject) -> bool:
        """A guess must be built from the subject's own letters, not imagination."""
        if len(re.sub(r"[^a-z]", "", guess.lower())) < 3:
            return False
        source = re.sub(r"[^a-z]", "", " ".join(self._subject_tokens(subject)))
        # Every alphabetic run in the guess must appear in the subject's own text.
        return all(part in source for part in re.split(r"[^a-z]+", guess.lower()) if len(part) > 2)

    @staticmethod
    def _rule_usernames(subject: OsintSubject) -> list[str]:
        guesses: list[str] = []
        if subject.email:
            local = subject.email.split("@")[0].strip()
            if local:
                guesses.append(local)
        if subject.full_name:
            parts = [p for p in re.split(r"\s+", subject.full_name.strip().lower()) if p]
            if len(parts) >= 2:
                guesses.append(f"{parts[0]}{parts[-1]}")
                guesses.append(f"{parts[0]}.{parts[-1]}")
            elif parts:
                guesses.append(parts[0])
        seen: list[str] = []
        for guess in guesses:
            if guess not in seen:
                seen.append(guess)
        return seen

    # -- interpretation -------------------------------------------------------

    def extract_identifiers(
        self, results: list[ToolResult], subject: OsintSubject | None = None
    ) -> list[dict[str, str]]:
        """Candidate identifiers found in the raw output. Leads, never facts.

        Values already known about the subject are dropped: re-reporting the email we
        searched with as a discovery inflates the finding count and tells nobody
        anything.
        """
        found: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        known = {
            value.lower()
            for value in (
                (subject.full_name, subject.phone, subject.email, subject.username, subject.employer, subject.city)
                if subject is not None
                else ()
            )
            if value
        }

        def add(kind: str, value: str, tool: str, context: str = "") -> None:
            value = value.strip().rstrip(".,;)")
            if not value:
                return
            if value.lower() in known:
                return
            key = (kind, value.lower())
            if key in seen:
                return
            seen.add(key)
            found.append({"kind": kind, "value": value, "tool": tool, "context": context})

        for result in results:
            raw = (result.data or {}).get("raw") or ""
            if not raw or result.status != FindingStatus.OK:
                continue
            spec = TOOL_CATALOG.get(result.tool)
            if spec is not None and not spec.yields_findings:
                # generate_dorks echoes the query back inside search URLs. Mining that
                # would report the subject's own name as a discovery.
                continue
            for match in _EMAIL_RE.findall(raw):
                add("email", match, result.tool)
            for match in _URL_RE.findall(raw):
                if any(host in match.lower() for host in _NOISE_URL_HOSTS):
                    continue
                add("url", match, result.tool)
            for match in _PHONE_RE.findall(raw):
                add("phone", re.sub(r"[\s\-()]", "", match), result.tool)

            self._llm_identifiers(result, raw, add)

        return found

    def _llm_identifiers(self, result: ToolResult, raw: str, add) -> None:
        """Ask the model for the identifiers regex cannot see - usernames, employers.

        Every returned value must appear verbatim in ``raw``. A value that does not is
        discarded without ceremony: the model invented it.
        """
        if self.llm is None or not self.llm.settings.ready:
            return

        excerpt = raw[:6000]
        prompt = (
            f"Tool: {result.tool}\n"
            f"Query: {result.query}\n"
            "Raw output:\n"
            "---\n"
            f"{excerpt}\n"
            "---\n"
            "List the identifiers present in this output. Allowed kinds: "
            f"{', '.join(sorted(_ALLOWED_DISCOVERY_KINDS))}. "
            "Copy each value exactly as it appears."
        )
        try:
            parsed, _meta = self.llm.generate_structured(
                prompt=prompt,
                schema=_Extraction,
                system=_EXTRACT_SYSTEM,
                model=self.llm.settings.bulk_model,
                cache_kind="osint_extract",
                prompt_version=PROMPT_VERSION,
            )
        except OllamaError as exc:
            logger.info("osint agent: extraction skipped for %s (%s)", result.tool, exc)
            return

        lowered = raw.lower()
        for item in parsed.identifiers:
            kind = (item.kind or "").strip().lower()
            value = (item.value or "").strip()
            if kind not in _ALLOWED_DISCOVERY_KINDS or not value:
                continue
            if value.lower() not in lowered:
                logger.info(
                    "osint agent: dropping hallucinated %s %r (absent from %s output)",
                    kind,
                    value,
                    result.tool,
                )
                continue
            add(kind, value, result.tool, item.context[:200])

    def narrate(self, subject: OsintSubject, results: list[ToolResult], discovered: list[dict[str, str]]) -> str:
        """Prose summary. Consumes structured data only - it cannot alter a finding."""
        fallback = _rule_narrative(subject, results, discovered)
        if self.llm is None or not self.llm.settings.ready:
            return fallback

        lines = [f"Subject: {subject.label()}"]
        for result in results:
            lines.append(f"- {result.tool}: {result.status.value} - {result.summary or 'no summary'}")
        if discovered:
            lines.append("Identifiers found:")
            for item in discovered[:40]:
                lines.append(f"- {item['kind']}: {item['value']} (via {item['tool']})")
        else:
            lines.append("No identifiers were extracted.")

        prompt = (
            "OSINT scan results:\n"
            + "\n".join(lines)
            + "\n\nWrite a two-to-four sentence factual assessment, then the caveats an "
            "analyst must keep in mind about this evidence."
        )
        try:
            parsed, _meta = self.llm.generate_structured(
                prompt=prompt,
                schema=_Narrative,
                system=_NARRATIVE_SYSTEM,
                cache_kind="osint_narrative",
                prompt_version=PROMPT_VERSION,
            )
        except OllamaError as exc:
            logger.info("osint agent: narrative fell back to rules (%s)", exc)
            return fallback

        text = parsed.assessment.strip()
        if parsed.caveats.strip():
            text = f"{text}\n\n{parsed.caveats.strip()}"
        # The unverified stamp is appended by us, not the model, so it cannot be edited away.
        return f"{text}\n\n{_UNVERIFIED_STAMP}"

    # -- the whole run --------------------------------------------------------

    def run(self, subject: OsintSubject) -> OsintReport:
        started = datetime.now(UTC)
        plan = self.plan(subject)
        results = self.client.run_planned(plan.tools) if plan.tools else []
        discovered = self.extract_identifiers(results, subject)
        narrative = self.narrate(subject, results, discovered)
        return OsintReport(
            subject=subject,
            plan=plan,
            results=results,
            narrative=narrative,
            discovered_identifiers=discovered,
            started_at=started,
            finished_at=datetime.now(UTC),
        )


_UNVERIFIED_STAMP = (
    "UNVERIFIED - OSINT. These findings come from public internet sources. They are "
    "investigative leads only and must be corroborated against an authoritative record "
    "before being relied on."
)


def _rule_narrative(
    subject: OsintSubject, results: list[ToolResult], discovered: list[dict[str, str]]
) -> str:
    """Narrative without the LLM. Deliberately plain - it states counts, nothing more."""
    tally: dict[str, int] = {}
    for result in results:
        tally[result.status.value] = tally.get(result.status.value, 0) + 1

    parts = [f"OSINT scan of {subject.label()}."]
    if not results:
        parts.append("No tool could run against the identifiers supplied.")
    else:
        parts.append(
            "Tools run: "
            + ", ".join(f"{count} {status}" for status, count in sorted(tally.items()))
            + "."
        )
    if discovered:
        kinds: dict[str, int] = {}
        for item in discovered:
            kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
        parts.append(
            "Identifiers surfaced: "
            + ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items()))
            + "."
        )
    else:
        parts.append("No identifiers were surfaced.")
    return " ".join(parts) + "\n\n" + _UNVERIFIED_STAMP
