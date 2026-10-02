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
| `start` | `{question, people, targets, findings, model}` |
| `step` | `{tool, args, args_text, thought, summary}`: one per query the investigator makes |
| `final` | `{answer, confident, hypotheses[{statement, tier, people, people_ids, evidence}], key_people, next_steps, suggestions, findings, model}` |

The investigator never searches the live systems itself. The people it suggests
searching next come back as `suggestions`, and the officer decides whether to search them.

### Errors

`401`: token missing, expired or signed with another secret. `404`: unknown run or
person. `422`: bad input (the `detail` says which field).
