# Sherlocks tab for the Laravel portal: integration guide

**Goal.** A "Sherlocks" tab in the portal where an officer types a CNIC, mobile number or
email and watches the link graph **build live**, then chats with it, exactly as in the
Sherlocks portal today. The graph search, the live stream and the AI chat all run on the
**Sherlocks server**. The portal only shows the page, signs the officer in, and saves the
finished graphs. **Users, permissions and saved graphs are entirely the portal's.**

> **Already integrated an earlier version? Do this now (once):**
> 1. Copy `laravel/app/Http/Controllers/SherlocksProxyController.php` (new),
>    `SherlocksController.php`, `config/sherlocks.php`, `routes/sherlocks.php`.
> 2. Replace `resources/views/sherlocks/tab.blade.php` with the new short one (keep your own
>    `saveRun` / `SHERLOCKS_CONFIG` edits).
> 3. `.env`: add `SHERLOCKS_PROXY=true`, then `php artisan config:clear`.
> 4. Leave `sherlocks/api/*` out of the CSRF check (step 3 below).
> 5. Live server: `curl http://192.168.200.239:7401/sindhpolice-sherlocks/health` must
>    answer; set the nginx/PHP values in "Proxy through Laravel: server settings".
>
> After this, design and feature changes on the Sherlocks side appear in the tab by
> themselves: there is nothing to copy again unless "This version" says so.

```
 Officer's browser ── the Sherlocks tab (a Blade page)          portal's own domain (https)
    │  GET  /sherlocks/api/portal/app.js …   script and styles     ┐
    │  POST /sherlocks/api/graph/runs        start a search        │ Laravel route
    │  GET  /sherlocks/api/graph/runs/{id}/stream  live graph (SSE)│ sherlocks.api
    │  POST /sherlocks/api/graph/investigate chat (streams)        ┘   │
    │                                                                   ▼
    │                                   Laravel SERVER ── http://192.168.200.239:7401/sindhpolice-sherlocks
    └── Laravel: page, token, save the finished graph (onRunComplete)
```

**The browser talks only to the portal.** `SherlocksProxyController` passes each call on
to Sherlocks and streams the answer back as it arrives, so the graph still grows live.
Only the Laravel server needs to reach Sherlocks; officers' browsers never see
`192.168.200.239`, and there is no mixed-content or CORS problem on an https domain.

**Before anything else, check from the live Laravel server** (not from your PC):

```bash
curl -s http://192.168.200.239:7401/sindhpolice-sherlocks/health
```

If this does not answer, the live server is outside the police network and no Laravel
code can fix it: the network team must open a route (VPN or firewall rule) from the
Laravel server to `192.168.200.239:7401`.

The proxy keeps one PHP worker busy for each open stream (a live search: a few minutes;
a chat reply: up to a minute). See "Proxy through Laravel: server settings" below.
Same network and same scheme as Sherlocks? `SHERLOCKS_PROXY=false` sends the browser
straight to Sherlocks as before.

---

## What is in this folder

| File | Put it at | What it does |
|---|---|---|
| `laravel/config/sherlocks.php` | `config/sherlocks.php` | Sherlocks address, secret, token lifetime |
| `laravel/env.example` | add to `.env` | the three settings |
| `laravel/app/Support/SherlocksToken.php` | `app/Support/` | signs the short-lived browser token |
| `laravel/app/Http/Controllers/SherlocksController.php` | `app/Http/Controllers/` | shows the tab, renews the token, receives finished graphs (**2 TODOs: your storage**) |
| `laravel/app/Http/Controllers/SherlocksProxyController.php` | `app/Http/Controllers/` | passes the tab's calls on to Sherlocks and streams the answers back (nothing to fill in) |
| `laravel/routes/sherlocks.php` | into `routes/web.php` | 5 routes under `/sherlocks`, behind your auth middleware |
| `laravel/resources/views/sherlocks/tab.blade.php` | `resources/views/sherlocks/` | the tab page: only the portal's config (token, saving) and one script, `embed.js` |
| `test/make-token.php` | run with PHP CLI | checks the secret works **before** writing Laravel code |
| `test/sherlocks-test.html` | open in a browser | the tab without Laravel, on demo data (same `embed.js`) |
| `API.md` | reference | every endpoint, if you ever need more than the page gives |
| `AGENTS.md` | reference | how the agents behind the chat and the report work together (no code to write from it) |

**Nothing from Sherlocks is copied into the portal, and nothing has to be copied again
when Sherlocks changes.** `tab.blade.php` holds only the portal's own part
(`SHERLOCKS_CONFIG`: token, saving, the saved graph) and one script tag,
`/sherlocks/api/portal/embed.js`. When the tab opens, `embed.js` reads the Sherlocks page
from the Sherlocks server - its markup, styles, graph library and `app.js` - and puts
it into the tab. A new design, button or panel on the Sherlocks side shows up in the
portal as soon as Sherlocks is redeployed: no Laravel change, no copy, no cache to clear
(the files are served with `no-store`).

The portal changes only when the **contract** changes: a new field in
`SHERLOCKS_CONFIG`, a new route, or the token format. Those are rare and are listed under
"This version" whenever they happen.

---

## Step by step

**What you get from the Sherlocks admin:** the Sherlocks address
(`http://192.168.200.239:7401/sindhpolice-sherlocks`) and the **shared secret**,
which is given privately and must never be committed.

### 1. Check the connection (5 minutes, no Laravel code yet)

```bash
php test/make-token.php "<SHERLOCKS_SECRET>"
```

Run this on the **Laravel server** (the live one, not only your PC): with the proxy, that
is the machine that must reach Sherlocks. Run the two `curl` commands it prints. The first must say `"authenticated":true`. If it
says `false`, the secret is wrong. If the connection is refused, the Sherlocks server isn't
reachable from this network.

Then paste the token into `test/sherlocks-test.html` (`token: '…'`) - a short page that
loads Sherlocks through `embed.js` like the real tab - open the file in a
browser, search `99999-0000001-1`, and watch the demo graph build. If that works, the
browser side works too.

### 2. Settings

Copy `laravel/config/sherlocks.php` to `config/`, and add the lines from
`laravel/env.example` to `.env`:

```ini
SHERLOCKS_API_BASE=http://192.168.200.239:7401/sindhpolice-sherlocks
SHERLOCKS_PROXY=true
SHERLOCKS_SECRET=<from the Sherlocks admin>
SHERLOCKS_TOKEN_TTL=900
```

`SHERLOCKS_API_BASE` is where the **Laravel server** reaches Sherlocks. With
`SHERLOCKS_PROXY=true` (the default) the page is given `/sherlocks/api` instead, on the
portal's own domain. Run `php artisan config:clear` after changing `.env`.

### 3. Token, controller, routes, view

Copy `SherlocksToken.php`, `SherlocksController.php`, `SherlocksProxyController.php` and
`tab.blade.php` into place, and add the routes from `laravel/routes/sherlocks.php` inside
your authenticated route group.

Leave the proxy route out of the CSRF check. The page's script sends the Sherlocks token
in a header, not Laravel's CSRF token; the proxy refuses any change without that header,
and a cross-site form cannot set one.

```php
// Laravel 11 / 12 - bootstrap/app.php
->withMiddleware(function (Middleware $middleware) {
    $middleware->validateCsrfTokens(except: ['sherlocks/api/*']);
})

// Laravel 10 and older - app/Http/Middleware/VerifyCsrfToken.php
protected $except = ['sherlocks/api/*'];
```

Then open `/sherlocks`: the search form appears, and a search builds the graph live. In
the browser's Network tab every Sherlocks call must go to `/sherlocks/api/...` on the
portal's domain, none to `192.168.200.239`.

In `SherlocksController` fill the two **TODOs** with your own storage. A finished graph
carries its case file (document texts), so it can be a few MB: use a `longText` column
and allow at least 16 MB request bodies on `sherlocks.graphs.store` (PHP
`post_max_size`/`upload_max_filesize`, nginx `client_max_body_size`).

- `store()` receives every finished search as JSON (`id`, `seed_label`, `params`,
  `stats`, `graph` {nodes, edges, case}, `events`). **The same run is posted again**
  whenever its case changes after the search (a file uploaded and read, the incident
  pinned, a question answered): update the row for that `run_id` (`updateOrCreate`), do not
  add a new one. Save it however you like, e.g. one row per
  graph with a `longText`/`json` `payload` column and the user who ran it.
- `show($graphId)` loads one saved graph back (with your own permission check). The tab
  then shows it, and **chat, leads and the AI investigator work on it** because the page
  sends the graph to Sherlocks with each question. Sherlocks doesn't need to have kept it.

Don't want saving yet? Set `'saveUrl' => null` in `show()`.

### 4. Put it in the portal's navigation

The tab is a **full page on purpose**. Sherlocks' stylesheet sets `html`/`body` and uses
class names (`.card`, `.row`, `.badge`) that Bootstrap/Tailwind layouts also use, so
pasting it inside the portal layout would break both. Either:

- **Menu link** to `route('sherlocks.tab')`, as its own page (simplest), or
- **Inside the portal layout** with a same-site iframe, which isolates the styles:

```blade
<iframe src="{{ route('sherlocks.tab') }}" title="Sherlocks"
        style="width:100%; height:calc(100vh - 64px); border:0;"></iframe>
```

For a saved graph: `route('sherlocks.graph', $id)`.

Your own "saved graphs" list simply links to `route('sherlocks.graph', $id)` for each row.

### Proxy through Laravel: server settings

The proxy passes the answer on as it arrives. For that to reach the officer live, and
not be cut off, the live server needs:

| Where | Setting | Why |
|---|---|---|
| nginx in front of PHP-FPM | `fastcgi_read_timeout 300s;` for `/sherlocks/api/` | a chat reply or report can be quiet for over a minute (default 60 s) |
| nginx | `fastcgi_buffering off;` for `/sherlocks/api/` (the proxy also sends `X-Accel-Buffering: no`) | otherwise the graph appears only at the end |
| nginx / Apache | no gzip on `text/event-stream`; Apache: `SetEnv no-gzip 1` for `/sherlocks/api/` | compression holds the stream back |
| nginx | `client_max_body_size 32m;` | uploaded files and saved graphs pass through Laravel now |
| PHP | `upload_max_filesize = 32M`, `post_max_size = 32M` | same |
| PHP-FPM | `pm.max_children` large enough: about 3 per officer using the tab at once, on top of the portal's own need | each open stream holds one worker |
| Cloudflare / load balancer | no response buffering, idle timeout ≥ 300 s on `/sherlocks/api/` | same as nginx |

Local testing with `php artisan serve`: it serves **one request at a time** by default,
so the live stream blocks every other call and the tab hangs. Start it with
`PHP_CLI_SERVER_WORKERS=4 php artisan serve`, or set `SHERLOCKS_PROXY=false` locally.

Not wanting PHP workers held by streams? The same result at the web server, without PHP:
on the Laravel server's nginx,

```nginx
location /sherlocks/api/ {
    proxy_pass http://192.168.200.239:7401/sindhpolice-sherlocks/;
    proxy_http_version 1.1;
    proxy_buffering off;
    proxy_read_timeout 300s;
    client_max_body_size 32m;
}
```

and remove the `sherlocks.api` route. Every call still needs a valid Sherlocks token,
but this path no longer checks the portal's login: use it only if that is acceptable.

---

## What the tab does by itself (nothing to build)

All of it comes from Sherlocks' own page script: deploying Sherlocks updates the tab, with no change on the portal side.

- **Live graph** over Server-Sent Events: people and links appear as each police system answers.
- **Light / dark theme** (moon/sun button in the header, kept per browser) and **Select group**: pick a person with everyone attached, or draw a box, and drag them together.
- **Search by** CNIC, mobile, email, or several people at once ("Multiple people").
- **Live EMS only.** The page is pinned with `backend: 'ems'`, so the data-source choice is hidden. It asks for confirmation before each live search.
- **Chat** about the graph, **person briefs**, **compare two people**, **leads** (linkage patterns), and the **AI investigator**, which streams its steps.
- **Case evidence** read while the graph builds: the FIR file of every FIR found (complainant, accused, witnesses, case diaries), the forensic/medical lab reports of that FIR (DNA, chemical, FSL, MLO; PDFs read, scanned pages read by the AI vision model), and the CRO dossier of every criminal (poses, fingerprints). Listed in the "Case evidence" card; each opens with its quoted facts and pictures.
- **Sherlock chat bubble** (bottom right), usable from the moment the graph starts building - a messenger-style chat (Sherlock's messages on the left with his avatar, the officer's on the right, time stamps, lists, tables and sources as chips). While a reply is worked out, one shimmering line says what runs now ("Checking NADRA · Kamran Ahmed"). It is a team of agents behind one voice (see `AGENTS.md`):
  - replies in the officer's language - **English, اردو or Roman Urdu** - in natural sentences;
  - remembers who is being discussed ("ye kitni FIRs mein hai?" means the person just mentioned);
  - answers fact questions **exactly, computed from the records** (how many FIRs, criminals among his connections or in the whole graph, links between two people, hotel stays, phones, vehicles, documents, what's new, case summary), each item with its source;
  - investigates open questions, and can fetch a document or check one system itself;
  - records what the officer states and **asks the officer questions throughout the chat** (where/when it happened, the crime, the main suspect, the FIR, whose CDR) with answer buttons - remembering every answer, never asking the same thing again;
  - checks every reply before it is shown (numbers, sources, quotes, no contradiction with what the officer said); cites every answer (`F3` opens the quote); shows sources that disagree instead of hiding one;
  - takes corrections ("nahi, 2 March tha"): the old answer is kept but no longer used, and what rested on it is redone;
  - sends what takes longer (a slow lookup, Sherlock's own view) as a follow-up message;
  - works the case in the background (Sherlock's hypotheses, summary, next questions); the downloaded case report is his full, current assessment.
  - **asks which case this is** when the target already has FIRs on record - naming the recent crimes - because only the officer knows whether this is a new matter (the old FIRs are then history) or the very case of one of those FIRs (the target was arrested first, the investigation follows). Until the officer says, a past FIR's date, place or offence is never given as "this case";
  - keeps a **trace of every question** (which agent ran, what it was given, what it answered, who handed what to whom) - no screen for it, read it over the API (below).
- **Uploads in the chat** (📎 or drag and drop): image (JPG, PNG), PDF, Word (.docx), Excel (.xlsx) only, up to 25 MB. CDRs and tower dumps go to the CDR agent (and the CDR server). The browser sends them straight to Sherlocks; if a proxy sits in front of Sherlocks, allow 30 MB bodies there.
- **Incident map** (📍): the officer pins where and when it happened. Map tiles load from OpenStreetMap in the officer's browser (or a tile server set on the Sherlocks side).
- **Find in graph** understands names by sound across scripts: "altaf" finds الطاف, and the other way round.
- **Connections filter** (toolbar): tick which kinds of link to show - co-accused, complaint (FIR filed), FIR witness, PRVS / verification witness, police, family, property, vehicle, phone/SIM, work, hotel, online; only those links and the people they connect are drawn.
- **Case report (PDF)**: written automatically when the graph finishes or is stopped, and again whenever the case changes; the "📑 Case report" button downloads it, "Court file version" gives the same report without suspicions and leads. One fixed structure, everything under headings and in points: **1 Case summary · 2 Person by person** (the target, then each related person: identity, how tied to the target, FIRs, medical and other reports, what they said, police system records) **· 3 Who said what · 4 People and their relations · 5 Timeline** (each FIR as a story in date order: the people in it, what each said, the medical and laboratory reports and what they found) **· 6 Contradictions with the evidence · 7 Hints and leads · 8 Next steps**, then an annex with the working (evidence register, references). While the officer has not said what case is being investigated, the report assumes nothing about it: it describes the target, the connections and the earlier cases only.
- **Token renewal:** when the token expires, the page calls `/sherlocks/token` and carries on.
- `onRunComplete(run)` fires once per finished search → POST to `store()`.

`onCaseUpdated(run)` fires (debounced) when a finished run's case changes; the tab view
saves it the same way as `onRunComplete`.

`window.Sherlocks` also exposes `loadGraph(run)`, `openRun(runId)`, `setToken(token)` and
`currentRun()`, in case the portal wants to drive the page from its own buttons.

---

## This version (October 2026): what changes for the portal

**One Laravel change: the proxy route** (for the live domain). Copy
`SherlocksProxyController.php`, the new route in `routes/sherlocks.php`, the changed
`SherlocksController.php` and `config/sherlocks.php`, add `SHERLOCKS_PROXY=true` to
`.env`, leave `sherlocks/api/*` out of the CSRF check, and set the server settings in
"Proxy through Laravel: server settings".

**Copy `tab.blade.php` one last time.** It is now a short page: your config and the
`embed.js` script, with no Sherlocks markup in it. From then on every change to the
Sherlocks page reaches the tab by itself. If you changed your copy, keep your edits to
`saveRun` / `SHERLOCKS_CONFIG` and drop everything else. (An older full copy keeps working
too - the page script adds new parts to it - but only the short page is fully automatic.)

**New look and a light/dark switch**: the tab now has the light design with a dark
graph, and a moon/sun button in its header. The officer's choice is kept in their
browser. To start officers in dark instead of light, uncomment `theme: 'dark'` in
`SHERLOCKS_CONFIG`.

**Move a group of people together** (nothing to build): the graph toolbar has a
**Select group** button. Click a person to take them with everyone linked to them, or drag a
box; then drag any one of them and the whole group moves. Outside the mode: double-click a
person, or Shift + drag a box. A bar at the bottom offers "+ next ring" (add everyone linked
to the group) and "Clear" (also Esc).

Everything else is served by the Sherlocks server. Check these six things:

| | What | Do |
|---|---|---|
| 1 | The saved graph is a little larger: `graph.case` now also holds `traces` (how the agents worked on the last 30 questions, up to about 1 MB) | keep `payload` as `longText` and the 16 MB body limit on the save route - nothing new if step 3 was followed |
| 2 | The case report PDF has a new fixed structure (8 sections + annex) and a second version, `?version=file` | nothing, unless the portal has its own download button: see `API.md` |
| 3 | Sherlock asks one more kind of question ("which case is this?", key `case:which`); answers are plain text | nothing with the bundled page; an own chat client must show `final.asking` like any other text question |
| 4 | Two new read-only endpoints: the agent trace | optional - for developers and admins, not for officers (below) |
| 5 | Links from the tenant register (TRUST) have real labels now: "Tenancy witness", "Landlord", "Family member", "Tenant (home the subject lives in)"... instead of "Named in property record" | nothing with the bundled page; a portal screen that lists or filters links by their label must accept the new ones (`API.md`, "Link labels"). Graphs saved before this version keep the old label |
| 6 | `graph.case.incident` has three more fields (`from_fir`, `date_as_recorded`, `offence_said`) and `graph.case.report.parts` has `persons`, `relations.pairs` and `story` | nothing, unless the portal reads these itself (`API.md`) |

If the portal built its **own** chat client instead of loading `app.js`, read "The
`investigate` stream" in `API.md` again: the reply text uses `## ` for a section heading
and `FINDING: ` for a finding of the Sherlock team.

## How Sherlock works in this version (for orientation)

Nothing here has to be built in the portal. It is the picture behind the tab, so a
developer reading a trace or a saved graph knows what the names mean. The full plan, with
the same diagram drawn out, is `docs/sherlock-agent-plan.html` ("What changed in v6").

```
 ONE CHAT QUESTION  (Officer graph - node names as they appear in a trace)

   officer ─> read ─┬─> record ──┬──────────────────────────> desk ─> check ─> present ─> officer
              O0    │   O3       │ (also asked something)      ▲       O2       O5
                    │   + which  ▼                             │
                    └─────────> brief ─> draft <─> lookup ─────┘
                                 O4       O1       toolbox + API agent (2 drafts at most)

   model off or no answer -> rules (fallback).  Every node and model call -> AGENT TRACE.

 CASE BOARD  (graph.case in every saved graph)
   incident + which case (new matter | FIR on record) · the officer's words, whom he suspects
   facts F# · documents D# · statements W# · contradictions X# · leads K# · hypotheses H#
   questions Q# (4 open at most) · traces (last 30 questions) · report

 IN THE BACKGROUND  (Sherlock graph, after new information)
   records read, who is who, register, claims ─> contradictions | statements | leads
     ─> summary ─> Sherlock's hypotheses <─> Skeptic ─> questions ─> findings for the officer

 THE REPORT  (Report graph, after every background run)
   freeze ─> evidence | statements | analysis ─> editor ─> persons ─> relations
     ─> summary <─> check ─> build
   1 Case summary · 2 Person by person · 3 Who said what · 4 People and their relations
   5 Timeline · 6 Contradictions with the evidence · 7 Hints and leads · 8 Next steps · annex
```

Three rules the portal will see the effect of:

- **Which case is this?** When the person searched already has FIRs on record, Sherlock's
  first question names them and asks whether this is one of those cases or a new matter.
  The model decides from the officer's words - also when he gives no FIR number ("I suspect
  the father killed his daughter himself" is matched to the murder FIR on file).
- **Nothing is assumed about the current case** until the officer has said what it is: not
  in the chat, not in the report. The earlier cases are told as earlier cases.
- **A link says what it is.** Each link's label names the part a person has in a record,
  for example "Tenancy witness" or "Landlord" - see "Link labels" in `API.md`.

## Checking how a question was answered: the agent trace

When an answer looks wrong, nobody has to guess which part failed. Every chat question
leaves a trace on the Sherlocks server, readable with the same token as the tab:

```bash
# every question of a run, in brief - and the background runs of the Sherlock team
curl -H "X-Session-Token: $TOKEN" "$SHERLOCKS_API_BASE/graph/runs/$RUN_ID/trace"

# one question in full (its number in the conversation, or "latest")
curl -H "X-Session-Token: $TOKEN" "$SHERLOCKS_API_BASE/graph/runs/$RUN_ID/trace/latest"
```

`$RUN_ID` is the id in the tab's address (`#run=<id>`) and in every saved graph (`id`).
The answer names each agent in the order it worked, what it did, how long it took, every
call to the language model (what it was given, what it answered, failed or not) and who
handed what to whom. For a graph the portal saved and Sherlocks no longer holds, the same
traces are inside the saved payload: `graph.case.traces`. Fields: `API.md`, "Agent trace".

The trace holds what the case holds (names, numbers, record text). Show it to developers
and admins only - not on an officer's screen, not in logs outside the police network.

---

## If something doesn't work

| Symptom | Cause / fix |
|---|---|
| `curl …/auth/session` → `"authenticated": false`, `"failed verification"` | `SHERLOCKS_SECRET` differs from the Sherlocks server's secret |
| `"Session expired"` | the server clocks differ by minutes: sync time (NTP) on both |
| Works on your PC, not on the live domain; Network tab shows calls to `192.168.200.239` | `SHERLOCKS_PROXY` is off, or the config is cached: set `SHERLOCKS_PROXY=true`, `php artisan config:clear` |
| `/sherlocks/api/...` answers **502** "cannot be reached from the portal server" | the live Laravel server cannot reach `192.168.200.239:7401` (`curl` it from that server). A network route is needed; no code change helps |
| `/sherlocks/api/...` answers **419** | the proxy route is still under the CSRF check (step 3) |
| `/sherlocks/api/...` answers **413** | upload/body size limits (table under "Proxy through Laravel: server settings") |
| Page loads unstyled, `app.js` 404 or the login page instead | proxy route missing, or the officer's portal session expired |
| Tab hangs locally with `php artisan serve` | it serves one request at a time: `PHP_CLI_SERVER_WORKERS=4 php artisan serve` |
| Browser console: **mixed content** or **CORS** | the browser still calls Sherlocks directly: `SHERLOCKS_PROXY=true` (see first row) |
| Graph appears only at the end, not live | something buffers the stream: `fastcgi_buffering off;` and no gzip for `/sherlocks/api/` (and `proxy_buffering off;` on any proxy in front of Sherlocks) |
| A chat reply is cut off, or the chat shows an error after about a minute | `fastcgi_read_timeout 300s;` for `/sherlocks/api/` (nginx's default is 60 s), and `proxy_read_timeout 120s;` on any proxy in front of Sherlocks |
| Portal slow or `502` everywhere while officers search | PHP-FPM workers all held by streams: raise `pm.max_children` |
| `419` when saving | the page lacks the CSRF meta tag (it is in `tab.blade.php`; keep it) |
| An answer is wrong, slow or says "not known" | read its trace: `GET /graph/runs/{id}/trace/latest` shows which agent gave what (section above) and send it to the Sherlocks admin |
| `404` on `/trace/{turn}` | the question was asked before this version was deployed, or more than 30 questions ago; or Sherlocks no longer holds the run - read `graph.case.traces` in the saved graph |
| A link reads "is named in the property record of" | the graph was searched before this version, and a link's label is fixed when the graph is built: run the search again. New searches say what the person's part is (tenancy witness, landlord, tenant, family member) |
| Sherlock asks "which case is this?" although the officer already said | the message did not say it clearly enough for the model; answer the question once - it is not asked again. If it keeps happening, send the trace of that question to the Sherlocks admin |
| The report still shows old wording ("has an inferred link", SIM items as findings) | it was written before this version: download it with `?rebuild=true`; old leads on a run are tidied by the next background run (any new statement or upload) |

---

## Prompt for Cursor

> Integrate the Sherlocks tab into this Laravel portal using the files in `integration/`
> (read `integration/README.md` first). Copy `laravel/config/sherlocks.php`,
> `laravel/app/Support/SherlocksToken.php`, `laravel/app/Http/Controllers/SherlocksController.php`,
> `laravel/app/Http/Controllers/SherlocksProxyController.php`
> and `laravel/resources/views/sherlocks/tab.blade.php` into the matching paths, add the
> env keys from `laravel/env.example` (with `SHERLOCKS_PROXY=true`), and register the routes from
> `laravel/routes/sherlocks.php` inside our authenticated route group. Exclude
> `sherlocks/api/*` from CSRF verification (the proxy route; every call it passes on carries
> the Sherlocks token header). Implement the two
> TODOs in `SherlocksController` with a new `sherlocks_graphs` table (migration + model:
> user_id, run_id (indexed), subject, payload longText, timestamps) and our existing permission
> checks - `store()` must `updateOrCreate` by run_id and user_id because the same run is posted
> again when its case changes, and the save route must accept bodies up to 16 MB, and add a "Saved graphs" list linking to `route('sherlocks.graph', $id)`. Add a
> "Sherlocks" menu item opening `route('sherlocks.tab')` inside our layout via a same-site
> iframe. Do not copy or modify Sherlocks' markup, JavaScript or CSS: the view only holds
> `SHERLOCKS_CONFIG` and loads everything else through `/sherlocks/api/portal/embed.js`, so
> Sherlocks updates reach the tab without changes here. Change only `saveRun` and
> `SHERLOCKS_CONFIG` in the view.
