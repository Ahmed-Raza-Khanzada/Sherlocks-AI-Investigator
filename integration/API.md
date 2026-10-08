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

The `investigate` stream is one chat turn worked by the agent team. Besides `start`,
`step` and `final` it sends `agent` events `{agent, text}` - Conversation agent, Fact
collector, Understanding agent, Facts agent, Investigator, Briefing agent, Questioner,
Validator - and each `step` carries `agent`. `final` carries:

| field | meaning |
|---|---|
| `answer` | the reply, in the officer's language |
| `language` | `en`, `roman` (Roman Urdu) or `ur` |
| `intent` | `question`, `statement`, `answer` (to Sherlock's question) or `greeting` |
| `query` | what was understood: `{type, people, pronoun}` (`cases`, `count_cases`, `fir_status`, `criminals_near`, `criminals_all`, `profile`, `connection`, `hotels`, `phones`, `vehicles`, `documents`, `graph_stats`, `news`, `summary`, `open`) |
| `facts` | the exact answer computed from the records: `{type, subject, count, items[{text, source, pid}]}` |
| `asking` | the one question Sherlock asks back, if any: `{id, key, text, action, options}` (`action`: `pin_location`, `choose_person`, `upload`, `text`) |
| `questions` | every open question |
| `checks` | the Validator: `{ok, issues, revised}` |
| `citations` | the case-file ids the reply cites |

The case file lives **inside the graph**: `run.graph.case` in every export and in the
`onRunComplete` payload. A run with documents is larger (document texts, typically
100 KB - 2 MB): store `payload` as `longText`, and allow a request body of at least
16 MB on the save route (`post_max_size` / `client_max_body_size`).

### Errors

`401`: token missing, expired or signed with another secret. `404`: unknown run or
person. `422`: bad input (the `detail` says which field).
