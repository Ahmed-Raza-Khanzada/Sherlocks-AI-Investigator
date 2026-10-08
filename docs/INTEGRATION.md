# Embedding Sherlocks in the Laravel portal

Sherlocks runs as its own service and does the searching, the live graph building and
the AI chat. **The Laravel portal owns everything else**: user accounts, permissions,
and the saved graphs. Sherlocks stores no users. Laravel can store every finished graph
and send one back whenever someone wants to chat with it again.

```
 Browser (Laravel tab: Sherlocks markup + app.js)
   │  1. page load: Laravel puts apiBase + a short-lived token in SHERLOCKS_CONFIG
   │  2. POST  /graph/runs                 start a search
   │  3. GET   /graph/runs/{id}/stream     live graph (Server-Sent Events)
   │  4. onRunComplete(run) ──────────────► Laravel saves the run JSON
   │  5. POST  /graph/analyze/ask {graph}  chat with a saved graph
   ▼
 Sherlocks API ──► police / government systems (EMS), local LLM
```

The browser talks to Sherlocks **directly**. Don't proxy through PHP: a live stream
stays open for the whole search, and PHP-FPM would hold a worker for all of it.

---

## 1. Sherlocks server settings (`.env`)

```ini
SHERLOCKS_AUTH_ENABLED=true
SHERLOCKS_AUTH_PASSWORD=                          # empty: no sign-in form on this side
SHERLOCKS_AUTH_SECRET=<long random string>        # shared with Laravel, see step 2
SHERLOCKS_CORS_ORIGINS=https://portal.example.local   # the Laravel origin(s), comma-separated
SHERLOCKS_API_KEY=<another random string>         # optional: Laravel server-to-server calls
```

Generate a secret with `python -c "import secrets;print(secrets.token_urlsafe(48))"`.

**Run one API process** (the default `sherlocks-api` / docker compose). A running search
lives in that process's memory, and the stream must reach the same process.

If nginx sits in front, streaming needs `proxy_buffering off;` for
`/sindhpolice-sherlocks/graph/runs/` and `/sindhpolice-sherlocks/graph/investigate`. Sherlocks
already sends `X-Accel-Buffering: no`.

## 2. Laravel: issue a token per user

A token is `base64url(payload) . "." . base64url(HMAC-SHA256(payload, secret))`, where
payload is `{"u": "<user>", "exp": <unix time>}`. Sherlocks logs the `u` value and
stores it as `created_by` on every run the user starts. That is your audit trail of
who searched which CNIC.

```php
// config/sherlocks.php:  'api_base' => env('SHERLOCKS_API_BASE'), 'secret' => env('SHERLOCKS_SECRET')

function sherlocks_token(\App\Models\User $user, int $ttl = 900): string
{
    $b64 = fn (string $s) => rtrim(strtr(base64_encode($s), '+/', '-_'), '=');
    $payload = json_encode(['u' => $user->username ?? (string) $user->id, 'exp' => time() + $ttl],
                           JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
    return $b64($payload) . '.' . $b64(hash_hmac('sha256', $payload, config('sherlocks.secret'), true));
}

// routes/web.php (behind your auth + permission middleware)
Route::get('/sherlocks/token', fn () => ['token' => sherlocks_token(auth()->user())]);
```

`SHERLOCKS_SECRET` in Laravel must equal `SHERLOCKS_AUTH_SECRET` in Sherlocks. Keep
tokens short-lived (15 min). The portal asks for a new one through `onTokenExpired`.

**Which systems the officer may use.** Add `"sys"` to the payload - the system keys this
user is cleared for, e.g. `["nadra", "simsdb", "psrms", "cro"]` - and Sherlock's agents make
live calls for that user only to those systems; any other call is refused and logged. Without
`sys` the user is not restricted (older hosts keep working).

```php
$payload = json_encode(['u' => $user->username, 'exp' => time() + $ttl,
                        'sys' => $user->sherlocksSystems()],          // e.g. from roles / permissions
                       JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE);
```

Every live call the agents make - who (the token's `u`), which case, which agent, why, which
system and identifier, made or refused - is kept with the case: `GET .../graph/runs/{id}/audit`.

**Never put `SHERLOCKS_API_KEY` in a page.** The key is for server-to-server calls only.

## 3. The tab

`docs/embed-example.html` is a working page generated from the real portal. Use it as
the template:

1. Copy `src/sherlocks/web/` (`app.js`, `app.css`, `vendor/`) into `public/sherlocks/`,
   or load them from the Sherlocks server as the example does.
2. Put the markup between `<!-- SHERLOCKS MARKUP -->` and `<!-- /SHERLOCKS MARKUP -->`
   into the tab's Blade view. The element ids are what `app.js` binds to. Keep them.
3. Before `app.js`, define the config:

```html
<script>
  window.SHERLOCKS_CONFIG = {
    embedded: true,                                   // hides sign-in, history, key buttons
    apiBase: @json(config('sherlocks.api_base')),     // e.g. https://sherlocks.lan/sindhpolice-sherlocks
    token: @json(sherlocks_token(auth()->user())),
    savedRun: {!! isset($graph) ? $graph->payload : 'null' !!},   // reopen a saved graph

    onRunStarted(runId) { /* optional: show "searching…" in your UI */ },

    // Finished search: the whole run (graph, params, stats, log). Save it.
    onRunComplete(run) {
      fetch('/sherlocks/graphs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json',
                   'X-CSRF-TOKEN': document.querySelector('meta[name=csrf-token]').content },
        body: JSON.stringify(run),
      });
    },

    // Token expired mid-session: return a fresh one (or null).
    async onTokenExpired() {
      const r = await fetch('/sherlocks/token');
      return r.ok ? (await r.json()).token : null;
    },
  };
</script>
<script src="/sherlocks/vendor/cytoscape.min.js"></script>
<script src="/sherlocks/vendor/layout-base.js"></script>
<script src="/sherlocks/vendor/cose-base.js"></script>
<script src="/sherlocks/vendor/cytoscape-fcose.js"></script>
<script src="/sherlocks/app.js"></script>
```

The page also exposes `window.Sherlocks`:

| Call | What it does |
|---|---|
| `Sherlocks.loadGraph(run)` | Show a saved run (e.g. picked from your own history list). Chat and analysis then work on it. |
| `Sherlocks.openRun(runId)` | Attach to a search that is still running (e.g. after a page reload). |
| `Sherlocks.setToken(token)` | Swap in a fresh token. |
| `Sherlocks.currentRun()` | The run as currently shown, in the same shape `onRunComplete` gets. |

## 4. What to store

Store the object `onRunComplete` receives, as JSON (a `longText`/`json` column).
Suggested table:

| column | from |
|---|---|
| `sherlocks_run_id` | `run.id` |
| `user_id` | your user |
| `subject` | `run.seed_label` |
| `params` | `run.params` (depth, systems, backend…) |
| `stats` | `run.stats` (people, records, links) |
| `payload` | the whole object, to pass back as `savedRun` / `loadGraph` |

Photos in a graph are fetched from Sherlocks (`/graph/images/{id}`), so Sherlocks's
`output/images` volume must be kept.

---

## API reference

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


## Case updates after a search

The case of a finished search keeps growing: files uploaded in the Sherlock chat, the
incident pinned on the map, answers to Sherlock's questions, documents the chat fetched.
The page calls `onCaseUpdated(run)` (debounced) with the full run export each time; save it
like `onRunComplete`, updating the row for that run id. A saved graph that Sherlocks still
holds reopens with its latest case automatically.

Sherlocks also stores each case board on its own (table `case_board`; run
`alembic upgrade head` once after updating) with a version number. If the portal posts
back an older copy of a case than the one Sherlocks holds, the newer board is kept and only
the officer's own edits in the older copy (roles, incident details) are merged in - an old
save can never undo newer work.
