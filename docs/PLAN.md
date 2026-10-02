# Sherlocks — Person Intelligence & Link Analysis Platform

## Context

`CDR-Analysis/Report_App` today is **phone-number centric**. Feed it a CDR file and it
analyses one MSISDN's behaviour, enriches it against ~20 police/government providers,
and renders a bilingual PDF. Cross-subject correlation exists (`analysis/multi_analyzer.py`)
but it correlates *numbers*, not *people* — and it only ever runs the `subscriber`
provider for multi-subject jobs.

Sherlocks inverts the unit of work. The subject becomes a **person**, and the deliverable
becomes a **relationship graph backed by evidence**:

1. One CNIC or phone → every identifier, address, employer, FIR, property and device
   that person touches → a detailed dossier PDF.
2. Many CNICs/phones → how they relate: blood relation, shared address, shared
   workplace, co-accused on the same FIR, landlord/tenant, shared hotel stay, shared
   device, phone contact, tower co-location.
3. A CDR Excel → resolve the significant numbers to real people → the same graph.

The judgements that resist hard-coding — *is `H.No 12, St 4, Gulshan-e-Iqbal Blk 13D, Khi`
the same place as `House 12 Street-4 Block 13-D Gulshan Iqbal Karachi`?* — go to LLM
agents, but only inside a deterministic, evidence-scored frame.

---

## Decisions taken

| Question | Decision |
|---|---|
| Codebase | **New service in `/Documents/Office/Sherlocks`, reusing `cdr_report_app` as a library.** Production report app untouched. |
| LLM backend | **Local Ollama only.** No citizen data leaves the network. |
| Graph store | **Postgres tables + networkx in-process.** networkx 3.6.1 is already installed. |
| Education links | **OSINT-only, permanently low-confidence.** No provider API returns education. |
| Interface | **REST API + generated PDF/Excel for v1.** No new UI. |
| Build order | **OSINT / social-media layer first**, then dossier, then graph, then CDR linking. |

### One flag on the build order

You chose OSINT first. It is buildable standalone — it takes a name/phone/email/username
directly and needs no resolved person — so Phase 1 below is scoped that way and ships a
real deliverable. Two things to know going in, then I'll stop raising it: the tools that
would actually find a Pakistani citizen's social footprint (`search_dorks_live`,
`search_footprint`, `scrape_url`) **all require a paid Bright Data key**, and OSINT output
is the only category in this system that can never be treated as evidence. The provider
spine (Phase 2) is what makes OSINT results *checkable*, so the value of Phase 1
compounds once Phase 2 lands.

---

## Constraints discovered during exploration

**The local model is small.** `ollama list` → `qwen3.5:4b` (Q4_K_M) on an RTX 5050 with
8151 MiB VRAM. Ceiling is roughly an 8B Q4 model. A 4–8B model cannot be trusted to
*decide* whether two people are related. This shapes everything:

> **Deterministic rules and fuzzy matching produce every link score.
> The LLM only converts messy free text into structured fields, and drafts prose.
> Every LLM output is schema-validated before it may touch a score.**

Recommend pulling `qwen3:8b` (~5 GB Q4) for the normalization agents and keeping
`qwen3.5:4b` as the fast path. Both fit in 8 GB.

**No AI infrastructure exists in the current repo.** Confirmed by grep across `*.py`,
`*.txt`, `*.yaml`, `*.md`, `.env*`: no `anthropic`, `openai`, `langchain`, embeddings or
vector store. Every "agent" hit is an HTTP `User-Agent` header. Everything AI is new build.

**Nothing is persisted except job rows.** `domain/db_models.py` is 55 lines and holds one
table, `report_jobs`. No person table, no provider-result cache, no CDR records. Every
report re-hits all 20 external APIs from scratch (30–120 s per run). There is no alembic —
schema comes from `Base.metadata.create_all`.

**Provider calls are live queries against police systems and are logged upstream**
(`docs/PROVIDER_APIS.md:528`). Fan-out must be budgeted, cached and de-duplicated, never looped.

**No education data exists upstream.** Across all 21 providers the only occupational
attributes are `EmploymentDatabaseRecord.designation` (EVS/HOPE), `SbvsEntry.profession`
and `.organization_name`, and `HrmisRecord.rank` / `.current_posting` / `.police_station_name`.

**OpenOSINT is infrastructure-centric, not identity-centric.** Its 20 tools cover
email / username / breach / IP / domain / DNS / Shodan / VirusTotal / GitHub. For a
Pakistani citizen identified by CNIC only `search_phone` (phoneinfoga), `search_username`
(sherlock), `search_email` (holehe) and the Bright Data trio are relevant. None of the
four external binaries are installed on this machine — all report `NOT INSTALLED`.
`openosint` on PyPI is **2.27.0**, `requires_python >=3.10`, with an `[ollama]` extra that
matches our local-model decision. Local Python is **3.12.3**.

**Two incompatible CDR schemas already exist.** Subject-CDR uses `MSISDN`/`B_NUMBER`/`START_TIME`;
BTS tower dumps use `A_PARTY`/`B_PARTY`/`CALL_TIME`. Any unified link analysis needs an adapter.

---

## Architecture

New package `Sherlocks/src/sherlocks/`, installed alongside `cdr_report_app` (add the
Report_App `src/` to `PYTHONPATH`, or `pip install -e` it).

```
sherlocks/
  settings.py        pydantic config, mirrors cdr_report_app.settings loader style
  db/models.py       SQLAlchemy: person, identifier, claim, edge, evidence, caches
  db/migrations/     alembic — introduce it here, do NOT retrofit report_jobs

  collect/           identifier expansion + provider fan-out (wraps UnifiedLookupService)
  claims/            ProviderResult -> typed Claim rows, one extractor per provider
  normalize/         address / name / org canonicalisation (agent-assisted, cached)
  resolve/           blocking, scoring, union-find -> person clusters
  links/             one rule module per edge type, each emitting scored edges + evidence
  graph/             Postgres <-> networkx, paths, centrality, communities
  agents/            Ollama client, schema-constrained agents, prompt templates
  osint/             OpenOSINT bridge, sandboxed and opt-in
  render/            dossier PDF, network PDF, Excel — built on cdr_report_app's ReportPdf
  api/               FastAPI app, job endpoints
  services/          job workers, following the fork-per-job pattern
```

### What we reuse from `cdr_report_app` (do not rewrite)

| Need | Reuse | Location |
|---|---|---|
| Provider fan-out, 20 adapters | `UnifiedLookupService.lookup_all` | `integrations/providers.py:1815, :1865` |
| Mobile→CNIC seed expansion | subscriber-first enrichment | `integrations/providers.py:1883-1890` |
| Canonical lookup cache key | `_subject_cache_key` | `integrations/providers.py:1929` |
| Query/result types | `SearchSubject`, `ProviderResult[T]` | `domain/provider_models.py:15, :32` |
| CNIC/mobile format variants | `dashed_cnic`, `dashed_mobile`, `mobile_10` | `integrations/helpers.py:21, :28, :35` |
| Phone/CNIC normalization | `normalize_mobile`, `normalize_cnic` | `utils/phones.py:10, :30` |
| Untyped JSON → flat labelled dict | `extract_scalar_details` | `integrations/helpers.py:590` |
| **Per-provider person-field dictionary** | `_provider_detail_pairs` | `rendering/pdf/report_renderer.py:673` |
| Status factories (`no_record`/`error`/…) | `helpers.py:42-60` | `integrations/helpers.py` |
| CDR parsing, 4 operators | `ingest_cdr_file` | `services/ingestion_service.py:19` |
| Bilingual Urdu/English PDF engine | `ReportPdf` (fpdf2 + harfbuzz fallback) | `rendering/pdf/builder.py:68` |
| Link-graph PNG renderer | `create_network_graph` — **written, imported, never called** | `rendering/charts.py:561` |
| Existing CDR correlation | `run_multi_analysis` | `analysis/multi_analyzer.py:74` |
| IMEI → brand/model, offline | `lookup_by_imei` (local 22 MB SQLite) | `services/tac_lookup.py` |
| Upload handling | `save_upload_to_temp` | `api/utils.py:33` |
| Fork-per-job memory isolation | parent thread + `mp.Process` child | `services/job_worker.py:31, :53` |

`_provider_detail_pairs` is the single highest-value find: it is already a hand-written
map of *which field in which provider means name / father / CNIC / phone / address /
employer*. Lift it into `claims/extractors.py` rather than re-deriving it.

---

## Data model (new Postgres schema, alembic-managed)

```
person                  id, display_name, canonical_cnic, confidence, created_at, merged_into
identifier              id, person_id, kind(cnic|msisdn|imei|imsi|passport|licence|email|username),
                        value_norm, value_raw, first_seen, last_seen
claim                   id, identifier_id, person_id, attribute, value_raw, value_norm,
                        provider, provider_status, observed_at, evidence_id, confidence
                        -- attribute: name|father_name|address|employer|designation|
                        --            profession|dob|police_station|property|licence|education
address_norm            id, address_key, house, street, block, sector, area, city, district,
                        province, lat, lng, source_raw_hash, normalizer(rule|llm), confidence
org_norm                id, org_key, canonical_name, sector, city, source_raw_hash
edge                    id, src_person_id, dst_person_id, edge_type, score, direction,
                        first_seen, last_seen, evidence_ids[], status(auto|confirmed|rejected)
evidence                id, provider, endpoint, request_key, raw_payload(jsonb),
                        fetched_at, response_status
lookup_cache            request_key PK, provider, raw_payload(jsonb), fetched_at, ttl_expires_at
normalization_cache     input_hash PK, kind(address|name|org), output(jsonb), model, created_at
osint_finding           id, person_id, tool, query, result(jsonb), fetched_at,
                        confidence='low', verified_by_user(bool)
case                    id, title, created_by, created_at
case_person             case_id, person_id, role(seed|discovered)
job                     reuse the existing report_jobs pattern, new job_type values
```

`evidence` + `lookup_cache` are the same data viewed two ways: a durable audit trail
*and* a cache. Persisting `UnifiedLookupService._lookup_cache` — which is currently wiped
at `services/report_service.py:209` — instead of discarding it gives both for free, plus a
longitudinal identity corpus that grows with every case.

**Every claim and every edge carries provenance.** This is non-negotiable for law
enforcement: an analyst must be able to click any assertion and see which provider
returned it, when, and the raw payload.

---

## The AI agents

All run against local Ollama. All are narrow, schema-constrained, and cached by input hash
so the same address is never sent to the model twice.

| Agent | Job | Why an LLM | Guardrail |
|---|---|---|---|
| **AddressAgent** | raw address string → `{house, street, block, sector, area, city, district, province}` | Pakistani addresses are free-text, bilingual, wildly abbreviated (`Khi`, `Gulshan-e-Iqbal Blk 13-D`, `Mohalla ...`). Rules alone fail. | Output validated against a pydantic schema **and** a gazetteer of Pakistani provinces/districts/cities/towns. City/district must exist in the gazetteer or the parse is rejected and falls back to rule-based tokens. |
| **NameAgent** | name string → `{given, father, surname, honorifics}` + transliteration variants | `S/O`, `D/O`, `W/O`, `Muhammad`/`Mohammad`/`Md`, Urdu↔Latin variance. | Regex handles the ~80% common patterns first; LLM only sees what regex can't split. Output must be a substring-consistent decomposition of the input. |
| **OrgAgent** | employer/org string → canonical org key | `SSP Office Larkana` vs `Office of the SSP, Larkana`. | Restricted to canonicalisation — never invents an org not present in the input. |
| **OsintAgent** | picks which OpenOSINT tools to run for a subject, reads results, extracts candidate identifiers | Genuinely open-ended search planning. | Tool allowlist; every finding written as `confidence='low', verified_by_user=false`; can never write an `edge` row directly. |
| **NarrativeAgent** | structured findings → Urdu/English report prose | This is what small LLMs are actually good at. | Runs *after* all scoring; consumes only structured data; cannot alter any score or fact. |

**The orchestrator is deterministic code, not an LLM.** Agents are called at fixed pipeline
steps. There is no free-roaming agent loop deciding control flow — with a 4–8B model that
would be unreliable and, worse, unauditable.

---

## Link types — the core of the product

Each rule lives in its own module under `links/`, emits `edge` rows with a score in
`[0,1]`, and attaches `evidence_ids`. Scores combine by noisy-OR, never by summing.

| Edge type | Rule | Source | Strength |
|---|---|---|---|
| `CO_FIR` | same `(fir_no, fir_year, ps_id)` across two people; roles from `person_type` (`FIR`=complainant, `SUS`/`WIT`=suspect/witness) | PSRMS, CRO | **Very strong** |
| `FIR_ROSTER` | both named in one FIR's `nominated_suspects` / `witnesses` / `investigating_officers` | `PsrmsFirDetails` (`domain/provider_models.py:89`) | **Very strong** |
| `LANDLORD_TENANT` | explicit owner↔tenant relation | `OldTenantTenantInfo` (`provider_models.py:188`), PRVS, TRUST | **Very strong** |
| `FAMILY_BLOOD` | normalized `father_name` match + surname + same/adjacent address → sibling; A's `father_name` == B's `name` → parent–child | CRO, PSRMS, SBVS, EVS/HOPE | Strong |
| `SAME_ADDRESS` | equal `address_key`; or same house+street+block; or geo within radius | subscriber, EVS/HOPE, DLS, SBVS, HRMIS, old_tenant | Strong (weaker in dense apartment blocks) |
| `CO_STAY` | same hotel with **overlapping** check-in/check-out windows | Hotel Eye raw records (`providers.py:1620`) | Strong |
| `SHARED_DEVICE` | same IMEI used by both | CDR + `multi_analyzer._build_imei_cross_matches:352` | Strong |
| `SAME_EMPLOYER` | equal `org_key` | `SbvsEntry.organization_name`, EVS/HOPE `designation` | Moderate |
| `POLICE_COLLEAGUE` | both in HRMIS at the same `police_station_name` | HRMIS | Moderate |
| `PHONE_CONTACT` | direct call/SMS between two resolved persons' numbers | CDR, `multi_analyzer.py:189-241` | Moderate — **upgrade to directed + duration-weighted** |
| `SHARED_CONTACT` | both talk to the same third-party number | `contact_to_targets` (`multi_analyzer.py:191`) | Weak–moderate |
| `CO_LOCATION` | same tower key within an overlapping time bucket | **new — does not exist today** | Moderate |
| `SAME_EDUCATION` | shared institution from social profile | OSINT only | **Low, never scored above advisory** |
| `OSINT_LINK` | shared username/email/social connection | OpenOSINT | **Low, advisory** |

Three of these are near-free wins already sitting unused in the codebase: `CO_FIR`,
`FIR_ROSTER` and `LANDLORD_TENANT` come from data the providers already return and that
the current app renders as flat text without ever joining across people.

`CO_LOCATION` genuinely does not exist. `TimelineEvent.event_type` declares `"Co-location"`
(`domain/analysis_models.py:190`) and `charts.py:512` styles it, but nothing ever emits it.
It needs three new pieces: a **tower identity key** (today locations are keyed on the raw
`SITE_ADDRESS` *string* at `analysis/locations.py:25`, so two spellings are two towers),
time bucketing, and a pairwise spatio-temporal join.

---

## Build phases

### Phase 1 — OSINT layer (first, per your decision)

- `pip install "openosint[ollama]"` into the Sherlocks venv (2.27.0, py3.12 OK).
  Install the external binaries it shells out to: `holehe`, `sherlock`, `sublist3r`,
  `phoneinfoga` — **none are currently on this machine**.
- `osint/client.py` — thin wrapper calling OpenOSINT tools as a library, never the REPL.
  Per-tool timeout, rate limit, and a hard allowlist. Runs in a subprocess so a hung
  binary cannot wedge the API.
- `osint/tools.py` — the relevant subset only: `search_phone`, `search_username`,
  `search_email`, plus `search_dorks_live` / `search_footprint` / `scrape_url` gated on a
  Bright Data key being present. Everything else (Shodan, VirusTotal, Censys, subdomains)
  is out of scope for people-finding.
- `agents/ollama.py` — the shared Ollama client: JSON-schema-constrained generation,
  retry-on-invalid-JSON, model routing (`qwen3:8b` for reasoning, `qwen3.5:4b` for bulk).
- `agents/osint_agent.py` — plans and interprets runs; writes `osint_finding` rows only.
- `db/models.py` + alembic baseline with `person`, `identifier`, `osint_finding`, `case`.
  Enough of the schema to persist findings; the rest lands in Phase 2.
- `render/osint_report.py` — footprint PDF on `cdr_report_app`'s `ReportPdf`.
- `api/` — `POST /osint/jobs`, `GET /osint/jobs/{id}/status`, `/download`.

Deliverable: give it a name, phone, email or username → an online-footprint PDF, every
finding stamped *unverified — OSINT*.

### Phase 2 — Collection spine and the person dossier

- `collect/expander.py` — generalise the subscriber-first CNIC trick
  (`providers.py:1883-1890`) into a breadth-first **identifier expansion** with a depth
  budget: mobile → CNIC → *all SIMs registered to that CNIC* (the subscriber endpoint
  returns a full `SubscriberList`) → IMEIs → addresses → household members.
  Budgeted and de-duplicated so one dossier cannot fan out into thousands of live
  police-system queries.
- `collect/cache.py` — persist every `ProviderResult` to `evidence` + `lookup_cache`
  keyed by `_subject_cache_key`. Stop discarding it at `report_service.py:209`.
- `claims/extractors.py` — one extractor per provider, seeded from `_provider_detail_pairs`
  (`report_renderer.py:673`), falling back to `extract_scalar_details` for untyped providers.
  Type the currently-untyped Hotel Eye stay records while here.
- `normalize/` — AddressAgent, NameAgent, OrgAgent + the Pakistani gazetteer + caches.
- `resolve/` — blocking on CNIC/phone/name-soundex, scoring with `rapidfuzz`, union-find
  into `person` clusters. CNIC exact = identity. Anything below threshold becomes a
  *candidate merge* for analyst confirmation, never an automatic one.
- `render/dossier.py` — the single-person PDF.
- API: `POST /persons/resolve`, `POST /dossier/jobs`.

### Phase 3 — Link engine and network report

- `links/` — one module per edge type from the table above.
- `graph/store.py` + `graph/analytics.py` — load a case subgraph into networkx; shortest
  paths between seeds, betweenness (who is the broker), Louvain communities, ego networks.
  Edge weight = confidence score.
- `render/network_report.py` — finally wire up `create_network_graph` (`charts.py:561`),
  which is written and imported at `multi_report_renderer.py:12` but never called.
- API: `POST /cases`, `POST /cases/{id}/persons` (bulk CNIC/phone upload), `POST /cases/{id}/analyze`.

### Phase 4 — CDR and BTS integration

- `collect/cdr_bridge.py` — `ingest_cdr_file` → resolve significant B-numbers to persons
  via the subscriber provider → emit `PHONE_CONTACT` / `SHARED_CONTACT` / `SHARED_DEVICE` edges.
- `links/colocation.py` — the new tower identity key, time bucketing, pairwise join.
  Fixes the `SITE_ADDRESS`-string keying by compositing `(LAC_ID, CELL_ID)` the way
  `bts/analysis/top_facts.py:40` already does correctly, plus geo-clustering.
- Upgrade `PHONE_CONTACT` to directed and duration-weighted. `DirectInteraction` currently
  symmetrizes via `tuple(sorted(pair))` (`multi_analyzer.py:207`) and leaves
  `total_duration_seconds`, `first_interaction` and `last_interaction` unpopulated.
- Adapter reconciling the CDR and BTS column schemas.

---

## Verification

Each phase is done when these pass — no phase ships on "it ran without an exception".

**Offline first.** `cdr_report_app/cli.py` exercises ingest, analysis and single-provider
lookups with no DB and no job system (`--test-provider`, `--inspect-analysis`). Build the
Sherlocks equivalent (`sherlocks/cli.py`) early; it is by far the fastest development loop.

- **Unit** — `tests/` in Report_App contains only `__init__.py`. Sherlocks starts with real
  tests. Fixtures: recorded provider JSON payloads (scrubbed), so the resolution and link
  rules are testable without touching live police systems.
- **Normalization** — a golden set of ~200 real Pakistani address strings with
  hand-labelled canonical forms. Assert AddressAgent parses ≥90% correctly and that the
  gazetteer rejects hallucinated cities. Same shape for names (S/O splitting,
  Muhammad-variants) and orgs.
- **Entity resolution** — a labelled set of identifier pairs marked same-person /
  different-person. Report precision and recall. **Precision is the metric that matters** —
  a wrong merge fabricates a relationship between two innocent people.
- **Link rules** — per rule, a fixture pair that must link and a near-miss that must not
  (e.g. two people at the same *hotel* but non-overlapping dates must not get `CO_STAY`).
- **Provider budget** — assert a single dossier issues fewer than N upstream calls and that
  a repeat run inside the TTL issues **zero**. These are logged live police queries.
- **End-to-end** — one known CNIC through the full pipeline; open the PDF and check every
  claim traces to an `evidence` row with a raw payload.
- **Load** — 20 seed identifiers in one case; watch RSS. The existing app holds whole
  DataFrames in memory (`multi_report_service.py:76`) and relies on fork-per-job plus
  `malloc_trim` (`utils/memory.py:39`) to survive. Sherlocks inherits that pattern.

---

## Risks to keep visible

1. **Wrong merges are the top risk.** A false identity merge invents relationships between
   real citizens. Mitigations: precision-weighted thresholds, candidate merges requiring
   confirmation, every merge reversible via `person.merged_into`, full evidence trail.
2. **Small-model hallucination in normalization.** Mitigated by schema validation +
   gazetteer + substring-consistency checks, and by the rule that the LLM never scores.
3. **Provider load.** Identifier expansion is combinatorial by nature. The depth budget,
   the de-duplication and the persistent cache are load-bearing, not optimisations.
4. **PSRMS/Subscriber session cookies expire** and surface as `error`, which is *not*
   `no_record` (`docs/PROVIDER_APIS.md:519`). Treat `error` as unknown; never let it
   silently weaken a link score or read as "subject is clean".
5. **Subscriber data is only current to 2020** (`PROVIDER_APIS.md:72`). Age-weight the
   confidence on subscriber-derived edges.
6. **OSINT legal exposure.** Scraping and breach lookups against citizens need policy
   sign-off. Keep OSINT opt-in per case, logged, and clearly separated in the report.
7. **Jobs die on gunicorn worker recycling** (`--max-requests 200` in
   `docker-entrypoint.sh`) because they live inside the web worker. Long agent runs make
   this worse — Sherlocks should use a real queue rather than inherit this pattern.
8. `configs/default.yaml:20` in Report_App is literally `\sections:` (stray backslash), so
   section toggles never load and are silently ignored by the renderers. Don't copy that
   config pattern; and worth a one-line fix upstream.
