# Sherlocks

Person intelligence and link analysis for Sindh Police.

`CDR-Analysis/Report_App` is phone-number centric: one MSISDN in, one behavioural report
out. Sherlocks changes the unit of work to a **person**, and the deliverable to a
**relationship graph backed by evidence** — who shares an address, an employer, an FIR,
a landlord, a device, a tower.

Two things are in this repository today:

* **The link graph portal** — type a CNIC or mobile, pick a depth, and it searches every
  connected police/government system for that person, then everyone those records name,
  and draws the result as a graph. See [Link graph](#link-graph) below.
* **The OSINT layer** — give it a name, phone, email or username and it produces an
  online-footprint PDF with every finding stamped *unverified*.

CDR/BTS integration follows the plan in `docs/`.

## Link graph

```bash
.venv/bin/alembic upgrade head        # adds graph_run + provider_cache
.venv/bin/sherlocks-api               # then open http://127.0.0.1:7401/
```

### Docker

```bash
cp .env.example .env        # edit: set SHERLOCKS_API_KEY, and the LLM URL if used
docker compose up --build   # Postgres + the API; migrations run on start
# open http://localhost:7401/
```

Ships its own Postgres (the `sherlocks` schema). It starts in **demo** mode — nothing
leaves the machine. For live police-system lookups, uncomment the three Report_App
bind-mounts under the `app` service in `docker-compose.yml` and set
`SHERLOCKS_LINKGRAPH_BACKEND=live` in `.env`. The AI model
(`SHERLOCKS_LLM_BASE_URL`) is an OpenAI-compatible server on the LAN; if it runs on the
docker host, point it at `http://host.docker.internal:PORT/v1` and uncomment
`extra_hosts`.

One CNIC or phone in; out comes a graph with two kinds of node and three kinds of edge:

| | What it is |
|---|---|
| **Person node** | A human being. Photo when a system returned one (DLS, SBVS), name, CNIC, mobiles, addresses, father's name. Border colour = flags: gold subject, red criminal record / watchlist, orange named in an FIR, blue police officer. |
| **Record node** | *One person's record in one system* — "Kamran's CRO record", not "CRO". Colour = category (identity, criminal, property, employment, travel, traffic, police, complaints, OSINT). Click it for every field and the raw response. |
| `found_in` | person → their own record |
| **Strong** (solid, arrow) | record → another person that record names, labelled with the relation the system states: *Landlord*, *Tenant*, *Registered owner of SIM 0300…*, *Co-accused in FIR 45/2023*, *Investigating officer*, *Vehicle owner*, *Driver of vehicle*, *Employer*, *Shared phone*… |
| **Weak** (dashed, scored) | person ↔ person, inferred: same address spelled differently, same father's name + surname, overlapping hotel stays, same employer, same police station, OSINT overlap. Always shows its reasons. A lead, never evidence, never merges two identities. |

### How it expands

1. The subject is searched in every selected system — identity systems first, because a
   phone-only person needs the SIMs database to give a CNIC before CRO, EVS, TRACS and the
   other CNIC-only systems can be asked. The seed's extra numbers are searched too (each
   can be registered to someone else).
2. Every person those records name becomes a person node one level deeper.
3. PSRMS FIRs are opened (the FIR report) and everyone named in them is added:
   nominated co-accused, witnesses, investigating officers, complainant.
4. Repeat for each new person until **depth** is reached or the **person budget** is
   spent. People not searched are still drawn, with the reason (depth limit, budget, or
   nothing searchable — e.g. an FIR witness with no CNIC).
5. Shared-record links (same FIR, same vehicle) and weak links are computed last.

Depth 1 = the subject and whoever their records name. Depth 2 = those people searched
too. It grows fast; the budget exists because **each searched person is ~20 queries that
the live systems log**.

### Identity rules

Same CNIC is one person. Different CNICs are always different people — if they share a
phone that becomes a *Shared phone* strong edge, not a merge. A phone-only person joins
the one node holding that phone; a person with neither matches only on name *and*
father's name. A record found through a shared identifier but belonging to someone else
(a licence registered on a number the subject uses) is drawn as a relation to that other
person, and its photo goes with it.

### Backends: demo · ems · live

Pick in the portal's **Data source** dropdown, per run, or set `linkgraph.backend`.

- **`demo`** (default) — cdr_report_app's real adapters over a synthetic network (CNICs
  `99999…`, mobiles `0399…`, neither prefix issued). Whole pipeline runs, nothing leaves
  the machine. Try `99999-0000001-1`.
- **`ems`** — the real **EMS / iCOP** endpoints (`src/sherlocks/linkgraph/ems.py`): CRO,
  ARMS, NADRA, PSRMS, Watchlist, CFMS, Hotel Eye, SBVS, PRVS, EVS, HOPE, DLS, IGP CMS,
  PFC, HRMIS, TRUST, Old Tenant, MILAP, Subscriber, plus vehicles (Excise, TRACS, AVLC).
  Endpoints/keys/formats in `reference/EMS_person_vehicle_apis.md`; override any with an
  `EMS_*` env var. This is the set to use for live Sindh Police work.
- **`live`** — the Shield-gateway providers via `CDR-Analysis/Report_App/.env`.

The portal asks for confirmation before any live run. Each searched person's SIMs are
each run through the phone-driven systems, and each person's CNIC through the CNIC-keyed
ones, so nothing is missed; every found person is then searched the same way, to depth.

Provider answers are cached (`provider_cache`, 24 h) — reopening or re-running a network
costs zero upstream queries. `error` answers (expired cookie, timeout) are never cached
and are shown as *unknown*, not *no record*.

### Social media (OSINT)

With the **OSINT footprint** toggle on, the subject is searched online and every hit is
classified into a platform. Seven platforms are the **required core, checked first and
always reported** (found *or* not): **Facebook, WhatsApp, Instagram, Twitter/X, YouTube,
Snapchat, Telegram**. Then secondary ones (TikTok, LinkedIn, …) if they turn up.

Each candidate profile is scored for whether it is *the subject's* — handle built from
their name, the number saved under their name (Caller ID), and (with Bright Data) the
profile page mentioning their city / area / employer. Two or more corroborations →
"corroborated ✓"; otherwise "unconfirmed ?". Never evidence, never a strong link.

**Search by email.** The search box also takes an email, alone or with a CNIC/mobile.
Police systems cannot be searched by email, so an email on its own is first looked up
on Pipl / FullContact; a Pakistani mobile they return starts the normal search.
Otherwise the person is searched online only.

**Emails.** Any email a person's own records carry (an entry that names them, by
CNIC, phone or name, not a landlord's email on their tenancy record), and any email the
OSINT results reveal, is searched online in turn: accounts, breaches, and the person
behind it. Emails those searches reveal are followed one level deeper
(`SHERLOCKS_OSINT_EMAIL_PIVOTS`, default 3 per person). With no email anywhere, the
name is looked up on Pipl (name + city, Pakistan) and Hunter (name + employer). A result
is kept only when the records **corroborate** it: the same phone or email settles it;
otherwise it needs two of the same city/area, the subject's father as a relative, or
the same employer (more for a common name). Its emails are then searched the same way.

WhatsApp/Telegram/Signal can't be enumerated, so a known number is reported as a
*reachable channel* (`wa.me/…`), not a confirmed account. Deep search (reading profiles,
open-web footprint) needs a Bright Data key — see `reference/OSINT_APIS.md`.

### Finding the links: scenarios, network, investigator

After every run, **leads** appear in the sidebar. These are linkage patterns found by
rule (`linkgraph/scenarios.py`), each tiered *stated* (one system says it),
*corroborated* (two independent systems agree), *inferred* (a pattern built from stated
facts) or *speculative* (names or OSINT only), and each with its evidence:

| Area | Patterns |
|---|---|
| FIRs | repeat co-offenders · counter-FIRs (feud) · recurring witness · recurring investigating officer · same charges at the same station with no shared accused |
| Property | landlord with many tenants (possible safe house when one is flagged) · recurring tenancy witness |
| Vehicles, SIMs | owner is not the driver · shared vehicle · SIMs used by others registered on one CNIC |
| People | possible insider (servant/employee of a complainant tied to the accused) · officer linked to a criminal (marked *sensitive*) · travelled together · second identity · aliases |
| Structure | corroborated link · hidden associate (shared rare contacts) · broker between clusters · target close to a criminal |

`linkgraph/network.py` projects the graph onto people and ranks routes by **cost**:
stated hops before inferred ones, and around hubs (a big landlord, an investigating
officer on every FIR) rather than through them. From that it finds clusters (Louvain),
brokers, hidden associates (Adamic-Adar) and the nearest criminal.

The **AI investigator** answers a question by querying the graph with those tools, one
step at a time, streamed to the sidebar. The LLM chooses the tools and writes the answer,
but every fact comes from a tool, people it invents are dropped, and it only *suggests*
searching someone. It never runs a live query itself. With no model configured it
runs the same tools by rule.

Weak links weigh **rarity**: a match on a common name ("Muhammad Ali") counts for less
than one on a rare name (`linkgraph/rarity.py`). They also ignore **crowds**: an
employer or police station shared by more than 5 people barely counts. And one fact
seen twice (same address and neighbour) is counted once.

### AI's part

The local model (Ollama) only splits free-text addresses into house / street / block /
area / city for borderline pairs, and a value survives only if it appears in the original
text. Scores come from deterministic rules, combined by noisy-OR.

### CLI

```bash
.venv/bin/sherlocks graph 99999-0000001-1 0399-0000101 --depth 3 --no-db
.venv/bin/sherlocks graph 42101-1234567-1 --backend live --depth 2 --max-persons 15 --json out.json
```

| Endpoint | Purpose |
|---|---|
| `GET  {prefix}/portal/` | the portal (`/` redirects here) |
| `GET  {prefix}/graph/config` | limits, systems, demo seeds |
| `GET  {prefix}/graph/systems?backend=live` | which systems are configured |
| `POST {prefix}/graph/runs` | `{cnic, phone, depth, max_persons, systems, include_fir_rosters, include_caller_id, include_osint, ai_address_matching, backend}` |
| `GET  {prefix}/graph/runs[/{id}]` | list / poll (nodes, edges, log, stats) |
| `GET  {prefix}/graph/runs/{id}/stream` | the run as it builds: Server-Sent Events, changes only |
| `POST {prefix}/graph/analyze/{action}` | chat / brief / compare / relations on a run id **or a posted graph** |
| `POST {prefix}/graph/runs/{id}/cancel` | stop a run |
| `GET  {prefix}/graph/runs/{id}/export` | the whole run as JSON |
| `GET  {prefix}/graph/images/{id}` | a stored photo (`?key=` accepted for `<img>`) |

### Embedding in another portal

Sherlocks can run as a tab inside the Laravel portal. The portal owns accounts and
saved graphs, signs a short-lived token per user with a shared secret, and gets each
finished graph back through a callback. The graph still builds live on screen, and chat
works on any saved graph posted back. Setup, PHP token code and API reference:
[`docs/INTEGRATION.md`](docs/INTEGRATION.md); a working page: `docs/embed-example.html`.

### Known limits

* CFMS, PFC, IGP CMS and MILAP are not configured in Report_App's `.env`; they report
  *not configured* and cost nothing.
* Response formats for untyped systems (TRUST, PRVS, MILAP, PFC, IGP CMS, Watchlist) are
  undocumented; people in them are found by field-name prefixes (`owner_*`, `tenant_*`,
  `complainant_*`…). Check the first live runs' raw responses in the record panel.
* FIR report tables have Urdu headers; people are read by shape (CNIC / mobile anywhere
  in the row, name / ولدیت / پتہ columns).
* PSRMS `WIT`/`SUS` codes follow cdr_report_app's reading (WIT = accused, SUS = witness).

## The two rules that shape the code

1. **Deterministic rules score; the LLM only structures text.** The local model is
   `qwen3.5:4b` on an 8 GB card. It has been observed placing "Khi" in Sargodha District
   on one run and calling it "Khyber" on the next. So it composes queries and drafts
   prose, and every value it claims to have *found* is checked against the raw source
   text before it survives. It never decides whether two people are related.
2. **OSINT findings are never evidence.** They are stored in their own table, always at
   `confidence='low'` with `verified_by_user=false`, they cannot create a link on their
   own, and the PDF says so on page one.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[osint,osint-tools,dev]"
```

Then point Sherlocks at cdr_report_app, whose PDF engine it reuses. That project ships no
`pyproject.toml`, so it cannot be pip-installed — set the path instead:

```bash
export SHERLOCKS_REPORT_APP_SRC=/path/to/CDR-Analysis/Report_App/src
```

### External tools

Three catalogued tools shell out to binaries. Two are pip-installable and come with the
`osint-tools` extra:

| Tool | Binary | Install |
|---|---|---|
| `search_email` | `holehe` | `pip install holehe` |
| `search_username` | `sherlock` | `pip install sherlock-project` |
| `search_phone` | `phoneinfoga` | Go binary — see the phoneinfoga releases page |

Three more (`search_footprint`, `search_dorks_live`, `scrape_url`) need a **paid Bright
Data key**. These are the tools that would actually find a Pakistani citizen's social
footprint; without a key they are reported as unavailable rather than silently skipped.

`sherlocks capabilities` tells you exactly which of the seven can run right now, and why
the others cannot. Run it first whenever a scan comes back thinner than expected.

## Database

Sherlocks owns the `sherlocks` **schema** inside an existing Postgres database rather
than a database of its own, so it deploys without `CREATEDB`. Alembic's autogenerate is
confined to that schema — it will not propose dropping cdr_report_app's tables sitting
next to it in `public`.

```bash
.venv/bin/sherlocks init-db          # create tables
.venv/bin/alembic upgrade head       # or via migrations
```

## Use

```bash
# What can this deployment actually do?
.venv/bin/sherlocks capabilities

# What would a scan run, without running it?
.venv/bin/sherlocks plan --name "Ali Raza" --city Karachi

# A full scan, no database, no job system - the fast development loop.
SHERLOCKS_OSINT_ENABLED=true .venv/bin/sherlocks osint \
    --name "Ali Raza" --city Karachi --no-db

# The API
.venv/bin/sherlocks-api
```

| Endpoint | Purpose |
|---|---|
| `GET  /health` | liveness |
| `GET  {prefix}/osint/capabilities` | which tools can run, and why the rest cannot |
| `POST {prefix}/osint/jobs` | submit a subject; returns a job id |
| `GET  {prefix}/osint/jobs/{id}/status` | poll |
| `GET  {prefix}/osint/jobs/{id}/download` | the PDF |

`{prefix}` defaults to `/sindhpolice-sherlocks`.

## Two things that will otherwise surprise you

**OSINT is off by default.** `osint.enabled` is `false` and the API returns 403 until it
is turned on. Reaching the public internet on behalf of an investigation is a policy
decision, not a default.

**No OSINT tool can search by CNIC.** A CNIC identifies a person to an investigator here,
and it searches nothing online. Submit one alone and the API says so in the response,
the plan says so in its reasoning, and the PDF says so under the subject block.

## Configuration

Layered, later wins: Pydantic defaults → `configs/default.yaml` → `.env` → process
environment. Copy `.env.example` and edit. An unset `SHERLOCKS_API_KEY` leaves the API
**open** — the service logs a warning about this at startup.

Everything the LLM touches stays on the local network. `agents/ollama.py` talks only to
`ollama.base_url`; there is no cloud LLM path in this codebase, and adding one would
break the constraint the whole design is built on.

## Tests

```bash
.venv/bin/python -m pytest
```

The suite never touches Postgres, Ollama or the public internet — the `settings` fixture
restricts the tool allowlist to `generate_dorks`, the one catalogued tool that makes no
network request. Keep it that way: `sherlock` alone queries 300+ sites, and a test suite
has no business sending those.

## Layout

```
src/sherlocks/
  settings.py        layered config
  db/                models, session, alembic migrations
  agents/            Ollama client, LLM cache, the OSINT agent
  linkgraph/         backends (live/demo), extractors, identity graph, expansion engine,
                     weak-link rules, run manager, provider cache, demo dataset
  web/               the link-graph portal (Cytoscape.js, vendored - no CDN at runtime)
  osint/             tool catalogue, OpenOSINT bridge, typed models
  services/          orchestration, persistence, fork-per-job worker
  render/            the footprint PDF, built on cdr_report_app's ReportPdf
  api/               FastAPI app
  cli.py             offline development loop
```
