# Sherlocks API reference

The tab page (`tab.blade.php`) already uses all of this; it is here in case the portal
wants to call Sherlocks directly (e.g. its own "chat with saved graph" widget).


All paths are under `{apiBase}` = `https://<host>/sindhpolice-sherlocks`. Send the
token as `X-Session-Token: <token>`. Where a header can't be set (EventSource, `<img>`),
use `?token=<token>` instead. The full schema is at `{host}/docs` (OpenAPI).

### Start a search, then watch it build

`POST /graph/runs` → `202 {run_id, status, backend}`

```json
{ "cnic": "42101-1234567-1", "phone": null, "depth": 2, "max_persons": 25,
  "systems": null, "include_fir_rosters": true, "include_osint": false,
  "ai_address_matching": true, "backend": "ems" }
```

`"email"` can be given alongside a CNIC/phone (it is searched online for that person) or
on its own. On its own it is looked up on Pipl / FullContact, and a Pakistani mobile they
return starts the police-system search; with none, the email is searched online only
(this needs OSINT enabled, otherwise the run is refused with 422).

For several people at once, use `"seeds": [{"cnic": "…"}, {"phone": "03…"}, {"email": "…"}]`. Each
searched person costs about 20 logged queries against live systems, which is why
`max_persons` exists. When the budget can't cover everyone at a depth, the most
promising people are searched first (`"strategy": "priority"`, the default; `"breadth"`
searches in discovery order instead). With several people, `"stop_when_connected": true`
stops the search as soon as stated links join them all, which is the cheapest way to
answer "how are these people connected?".

`GET /graph/runs/{id}/stream?token=…` (Server-Sent Events)

| event | data | client does |
|---|---|---|
| `graph` | `{version, full, nodes[], edges[], removed_nodes[], removed_edges[]}` | `full`: clear first. Then upsert nodes/edges by `id` and drop the removed ids. |
| `status` | `{id, seed_label, backend, params, status, progress_pct, message, stats, …}` | update the header and progress |
| `log` | `{events: [{at, level, message}]}` | append to the log |
| `case` | `{counts, documents[], facts[], links[], attempts[], report_status}` | the case file so far (FIR files, lab reports, CRO dossiers read; quoted facts; people found in them). Document texts are not streamed: `GET /graph/runs/{id}/documents/{D#}` |
| `done` | `{status}` | **close the EventSource** (or the browser reconnects), then fetch the export |

The first `graph` event carries everything so far. Each later one carries only what
changed, so the graph grows on screen as each system answers. `app.js` already does all
of this. The table is for anyone building their own client.

`POST /graph/runs/{id}/cancel` stops a search.
`GET /graph/runs/{id}/export` returns the whole run as JSON: the thing to save.
`GET /graph/config` returns the limits, the systems available per backend and the demo seeds.

### Chat and analysis: on a saved graph or a live run

`POST /graph/analyze/{action}`. The body always contains **either** `"graph": <run.graph>`
(a graph Laravel saved) **or** `"run_id"` (a run Sherlocks still holds).

| action | body adds | returns |
|---|---|---|
| `ask` | `question`, `history: [{q, a}]` | `{answer, confident, model}`: chat about the whole graph |
| `ask_pair` | `a`, `b`, `question`, `history` | chat about two people |
| `brief` | `pid` | intelligence brief for one person, each fact tagged with its source system |
| `compare` | `a`, `b`, `explain` | everything connecting two people |
| `connection` | `a`, `b` | the chain between two people, hop by hop |
| `relations` | `ids?`, `explain` | how a group relates, pair by pair |
| `findings` | `scenario?` | linkage patterns found by rule (repeat co-offenders, owner ≠ driver, possible safe house, SIM front, insider…), each tiered *stated / corroborated / inferred / speculative* with evidence |
| `network` | none | clusters, brokers, key people, hidden associates, hubs, people worth searching next |
| `paths` | `a`, `b`, `k` | the `k` best routes: stated hops before inferred ones, avoiding hubs |
| `investigate` | `question`, `history` | the AI investigator's steps and conclusion, all at once |

`a`, `b`, `pid` and `ids` are person node ids from the graph (`p1`, `p7`…). `paths`
also accepts a name, CNIC or phone. Answers come only from the graph's own data, and
each claim cites the system it came from.

### AI investigator, live

`POST /graph/investigate` takes the same body as `/analyze` (`graph` or `run_id`,
`question`, `history`) and answers as Server-Sent Events over the POST response.
`EventSource` can't POST, so read it with `fetch()` and a stream reader; `app.js` already
does this. The events are:

| event | data |
|---|---|
| `start` | `{question, people, targets, findings, model, documents, live_calls}` |
| `step` | `{tool, args, args_text, thought, summary, live}`: one per query the investigator makes; `live: true` = it queried a police system |
| `final` | `{answer, confident, hypotheses[{statement, tier, people, people_ids, evidence}], key_people, next_steps, suggestions, findings, model, citations[{id, type, title, quote, doc}], case?}` |

This is what the **Sherlock chat bubble** uses. It reads the case file (documents `D#`,
quoted facts `F#`, people found in documents `L#`) and cites them in its answer. It may
fetch a document itself (a FIR file, the lab reports of a FIR, a CRO dossier) or ask one
police system about one person, at most `SHERLOCKS_EVIDENCE_CHAT_LIVE_CALLS` per question.
It never expands the graph: people worth searching come back as `suggestions`.

Page hooks for the host: `onRunComplete(run)` once when a search finishes, and
`onCaseUpdated(run)` whenever a finished run's case changes later (uploads read, incident
pinned, answers, chat fetches) - save both under the same run id.

For a **saved graph** (posted as `graph`), a final event carrying `case` means the chat
fetched new documents: replace `graph.case` with it before saving the graph again. The
page does this itself (`window.Sherlocks.currentRun()` returns the updated graph).

### Case evidence and the case report

| | |
|---|---|
| `GET /graph/runs/{id}/documents/{D#}` | one document in full: `text`, `fact_rows` (each with its verbatim `quote`), `link_rows`, `images` (ids for `/graph/images/{id}`), `urls` |
| `GET /graph/runs/{id}/report.pdf` | the case report PDF (written by Sherlock when the run finished or was stopped). `?rebuild=true` writes it again, e.g. after the chat fetched more documents. Token as header, or `?token=` for a plain link |
| `POST /graph/report` | `{graph}` of a saved run (with its `case`) -> the case report PDF. `explain: true` rewrites it |

### Uploads, the incident, Sherlock's questions

| | |
|---|---|
| `POST /graph/runs/{id}/uploads` | multipart `file` (+ optional `note`, `owner` person id). Image (JPG, PNG), PDF, `.docx`, `.xlsx` only, 25 MB. `202` = accepted and being read; `422` = refused, `detail` says why |
| `GET /graph/runs/{id}/uploads/{D#}/file` | the original uploaded file |
| `POST /graph/runs/{id}/incident` | `{lat, lon, place, date (YYYY-MM-DD), time (HH:MM), fir}` - where and when it happened |
| `POST /graph/runs/{id}/questions/{Q#}` | `{answer, pid?}` - answer one of Sherlock's questions |
| `GET /graph/runs/{id}/case` | the case board now (documents, facts, incident, roles, open questions); poll it while uploads are read after the stream has ended |

The `investigate` stream is one chat turn worked by the Officer team (a LangGraph graph on
the Sherlocks server; see `AGENTS.md`). A reply takes up to 40 seconds: first a `start`
event, then `status` events (`{key, tool, text}` - the one-line "what is running now", in
the officer's language), `agent` events `{agent, text}` (Question reader, Statement
recorder, Dossier agent, Case briefer, Facts agent, Officer agent, API agent, Sherlock,
Insight desk, Questioner, Gatekeeper, Validator, Presenter), a `step` event for each tool
the Officer agent called, and one `final`. `final` carries:

| field | meaning |
|---|---|
| `answer` | the reply, in the officer's language. Plain text with a few line markers the bundled page renders: `**bold**`, `• ` bullets, `## ` a section heading, `> ` a quote from a record, `VIEW: ` Sherlock's view, `FINDING: ` one finding of his team, `ASK:<red|orange|green>: ` his question |
| `language` | `en`, `roman` (Roman Urdu) or `ur` |
| `intent` | `question`, `statement`, `answer` (to Sherlock's question) or `greeting` |
| `query` | how the message was read: `{type, people, role, depth, scope, shape, ...}` |
| `shape`, `sections` | the kind of answer (`fir`, `person`, `incident`, `statement`, `link`, `phone`, `count`, `yes_no`, `what_next`, `other`) and its sections `[{name, status}]` - `filled`, `not on record` or `not checked yet` |
| `facts` | the exact answer computed from the records: `{type, subject, count, items[{text, source, pid}]}` (`type` `not_found`: nothing was found, with `checked[]`) |
| `lookups` | the tools called for this answer `[{tool, args}]` - the live ones are calls to police systems |
| `asking` | the one question Sherlock asks back, if any: `{id, key, text, action, options, priority}` (`action`: `pin_location`, `choose_person`, `upload`, `text`) |
| `finding` | the one finding of the Sherlock team told in this reply, if any: `{ref, kind, severity, text}` (`ref`: `X#` a contradiction, `K#` a lead, `H#` a hypothesis) |
| `questions` | every open question |
| `checks` | the Answer checker: `{ok, issues, revised, rules, restored_sections}` |
| `citations` | the case-board ids the reply cites (`D#` document, `F#` fact, `L#` link, `H#` hypothesis, `Q#` question, `W#` recorded statement, `X#` contradiction, `K#` lead) |
| `timing`, `model_calls`, `late`, `followup_pending` | how long each step took; what ran over its time. When `followup_pending`, poll `GET /graph/runs/{id}/case` - `followups[]` carries what finished later |

`GET /graph/runs/{id}/case` also returns the Sherlock team's casework, read-only:
`statements` (W#), `contradictions` (X#), `leads` (K#), `matrix` (hypotheses against
evidence, ranked), `challenges`, `presence`, and `critical_waiting` (critical findings the
officer has not been told yet - the bundled page shows them as a badge).

`GET /graph/runs/{id}/report.pdf?version=file` gives the report for court and the case
file: the same board version, without suspicions, the ranking of hypotheses and leads.

The case file lives **inside the graph**: `run.graph.case` in every export and in the
`onRunComplete` payload. A run with documents is larger (document texts, typically
100 KB - 2 MB): store `payload` as `longText`, and allow a request body of at least
16 MB on the save route (`post_max_size` / `client_max_body_size`).

### Errors

`401`: token missing, expired or signed with another secret. `404`: unknown run or
person. `422`: bad input (the `detail` says which field).

### Which case this is (what the officer told Sherlock)

Kept on the case board and returned by `GET /graph/runs/{id}/case` and in `graph.case`:

| field | values | meaning |
|---|---|---|
| `dialog.case_basis` | absent, `new_matter`, `fir_on_record` | not said yet / a new matter (FIRs on record are earlier cases) / this case IS an FIR already on record |
| `incident.fir` | e.g. `604/25` | this case's own FIR, when the officer gave one |
| `incident.target_status` | `arrested`, `not arrested`, `on bail`, `absconding`, absent | only as the officer said |
| `incident.offence`, `date`, `time`, `place` | | what the officer said - or, with `fir_on_record`, taken from that FIR's file (`incident.from_fir` lists which) |

Question keys Sherlock may ask (`final.asking.key`, `questions[].key`): `case:which`,
`incident:what`, `incident:when`, `incident:place`, `incident:pin`, `incident:fir`,
`incident:vehicle`, `roles:main` (only with several targets), `roles:victim`,
`cdr_owner:<D#>`, `cdr_of:<pid>`, `confirm_lookup:<id>`, `link:why`, `suspects:other`,
and `source:<X#|K#|E#>` (a question that comes from a contradiction, a lead or an identity
check - at most four of these wait at once). Question `status`: `open`, `answered`,
`muted`, `withdrawn` (its reason went away). Answer any of them with
`POST /graph/runs/{id}/questions/{Q#}` or simply in the chat.

### The case report

`GET /graph/runs/{id}/report.pdf` (`?rebuild=true` writes it again, `?version=file` gives
the court-file version) - `POST /graph/report` with `{graph}` for a saved run.

The PDF always has this structure (`report.structure` in `graph.case.report` lists it with
the agent that wrote each section):

| # | Section | Holds |
|---|---|---|
| 1 | Case summary | short points under ten headings: the case in brief, the target, connections, earlier cases on record, what the evidence establishes, what is disputed, leads to check, where the case stands, what is not clear yet, next steps |
| 2 | Person by person | the target, then each related person: identity, how tied to the target, FIRs that name them, medical / laboratory / scene reports, what they said, police system records |
| 3 | Who said what | each person's recorded statements, what changed between them, what does not fit a record; what the officer told Sherlock |
| 4 | People and their relations | two people a row with every tie between them (roles in the same FIR with its number, tenancy, verification, phone...), each with its register; the network picture; patterns across the records |
| 5 | Timeline | the story of each case, the earliest first: the FIR, the people in it, what each of them said, the medical and laboratory reports and what they found, what the investigation did; then the dated records that belong to no case |
| 6 | Contradictions with the evidence | both sides, how serious, what would settle it |
| 7 | Hints and leads | what was noticed, why it could matter, the next step, the status (left out of the court-file version) |
| 8 | Next steps | what to do now, questions waiting for the officer, reports awaited |
| - | Annex | Sherlock's lines of inquiry, police system records, evidence by topic, evidence register, references, the checker's notes, the live calls made |

The same content as data, in `graph.case.report`: `summary_parts` (the ten headings, each
a list of points) with `summary_order` and `summary_titles`; `parts.persons.people[]`
(`name`, `target`, `role`, `tie{to, how, source, stated, steps}`, `identity`, `firs[]`,
`reports[]`, `said[]`, `stays[]`, `systems[]`, `nothing_in[]`); `parts.relations`
(`pairs[{a, b, ties[{relation, source, stated}]}]` - two people and every tie between them; `rows[]` the older flat list; `patterns[]`); `parts.story` (`cases[{fir, police_station, offence, occurred, registered, place, this_case, people[{name, role}], statements[{id, speaker, role, kind, date, recorded_by, text}], reports[{id, kind, title, when, found[]}], steps[{when, what}], result}]` in date order, `reports_without_a_case[]`, `other[{when, what, ref}]`) and the flat `parts.E.timeline[{when, what, ref}]`;
`contradictions_found[]`; `leads[]`; `parts.F` (`next_steps`, `open_points`);
`case_declared` (false while the officer has not said what case is being investigated -
the summary then says so in fixed words and assumes nothing about it).

## Agent trace (how each question was worked)

```
GET {prefix}/graph/runs/{run_id}/trace
GET {prefix}/graph/runs/{run_id}/trace/{turn}      turn = the question's number, or "latest"
```

Same sign-in as every other run endpoint (`X-Session-Token` or `X-API-Key`).

The list returns, per question: `turn`, `at`, `question`, `answer` (first 300 characters),
`path` (the graph nodes in order, e.g. `read, brief, draft, lookup, draft, desk, check,
present`), `agents`, `ms`, `model_calls`, `model_ms`, `failed_calls`, `slow_steps`,
`checks` - and `background_runs` (the Sherlock team's runs, agent by agent).

One turn returns `steps` (agent, `ms`, what it did, its notes, `passes` = what it handed on),
`model_calls` (`agent`, `kind`, `step`, `ms`, `ok`, `cached`, `given`, `answer` or `error`),
`flow` (`from`, `to`, `handed_over`), `late`, the `answer` and the checker's `checks`.
The last 30 questions are kept. `404` when no trace is kept for that turn.

```json
{ "run_id": "…", "turn": 6, "at": "2026-10-13T08:10:02+00:00", "question": "Danish kya kaam karta hai?",
  "path": ["read", "brief", "draft", "lookup", "draft", "desk", "check", "present"], "ms": 20536,
  "steps": [
    { "n": 1, "node": "read", "agent": "O0 Question reader", "ms": 3050,
      "reading": {"act": "question", "depth": "detailed", "scope": "person", "shape": "person", "language": "roman"},
      "decided": "the message asks; language roman", "next": "brief", "passes": "a question to answer", "notes": [] },
    { "n": 3, "node": "draft", "agent": "O1 Officer agent", "ms": 9800, "round": 1, "answered": false,
      "lookups_asked": [{"tool": "api_router", "args": {"who": "Danish", "topic": "work"}, "why": "his employer"}],
      "next": "lookup", "passes": "asks for: api_router(Danish · work)" },
    { "n": 4, "node": "lookup", "agent": "Toolbox (board tools and the API agent)", "ms": 2100,
      "lookups": [{"tool": "api_router", "found": true, "summary": "EVS: 1 record"}], "passes": "api_router: found" }
  ],
  "model_calls": [
    { "n": 3, "agent": "O1 Officer agent", "kind": "officer_draft", "step": "draft", "ms": 9760, "ok": true,
      "cached": false, "max_tokens": 1400, "input_chars": 18250,
      "given": {"question": "Danish kya kaam karta hai?", "the_new_case": "5 field(s), 310 chars", "given": "9 field(s), 16200 chars"},
      "answer": {"first_line": "…", "answered": false, "lookups": [{"tool": "api_router"}], "sections": []} }
  ],
  "flow": [
    {"from": "O1 Officer agent", "to": "Toolbox (board tools and the API agent)", "handed_over": "asks for: api_router(Danish · work)"},
    {"from": "Toolbox (board tools and the API agent)", "to": "O1 Officer agent", "handed_over": "api_router: found"},
    {"from": "Presenter", "to": "the officer", "handed_over": "the reply"}
  ],
  "late": [], "answer": "**Danish Karachi Port Trust mein Clerk hai.** …", "checks": {"ok": true, "issues": []} }
```

`given` shows the parts of what an agent received and their size, not the full input (the
input is the case itself). A failed call has `"ok": false` and `"error"`. Work that
finished after the reply went out (a slow draft, Sherlock's view) is under `late`, and its
model calls carry `"after_reply": true`.

Traces need a run the Sherlocks server still holds (`run_id`). For a graph the portal
saved, the same list is in the saved payload at `graph.case.traces` (each item is one
turn, exactly as `/trace/{turn}` returns it).
