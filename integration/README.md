# Sherlocks tab for the Laravel portal: integration guide

**Goal.** A "Sherlocks" tab in the portal where an officer types a CNIC, mobile number or
email and watches the link graph **build live**, then chats with it, exactly as in the
Sherlocks portal today. The graph search, the live stream and the AI chat all run on the
**Sherlocks server**. The portal only shows the page, signs the officer in, and saves the
finished graphs. **Users, permissions and saved graphs are entirely the portal's.**

```
 Officer's browser ── the Sherlocks tab (a Blade page, scripts loaded from Sherlocks)
    │  POST /graph/runs              start a search            ┐
    │  GET  /graph/runs/{id}/stream  graph grows live (SSE)    │ straight to the
    │  POST /graph/analyze/ask …     chat, briefs, leads       │ Sherlocks server
    │  POST /graph/investigate       AI investigator (live)    ┘
    │
    └── Laravel: page, token, save the finished graph (onRunComplete)
```

The browser talks to Sherlocks **directly**. Do not proxy these calls through PHP: a
live search streams for minutes and would hold a PHP-FPM worker the whole time.

---

## What is in this folder

| File | Put it at | What it does |
|---|---|---|
| `laravel/config/sherlocks.php` | `config/sherlocks.php` | Sherlocks address, secret, token lifetime |
| `laravel/env.example` | add to `.env` | the three settings |
| `laravel/app/Support/SherlocksToken.php` | `app/Support/` | signs the short-lived browser token |
| `laravel/app/Http/Controllers/SherlocksController.php` | `app/Http/Controllers/` | shows the tab, renews the token, receives finished graphs (**2 TODOs: your storage**) |
| `laravel/routes/sherlocks.php` | into `routes/web.php` | 4 routes under `/sherlocks`, behind your auth middleware |
| `laravel/resources/views/sherlocks/tab.blade.php` | `resources/views/sherlocks/` | the tab page: Sherlocks' markup + config |
| `test/make-token.php` | run with PHP CLI | checks the secret works **before** writing Laravel code |
| `test/sherlocks-test.html` | open in a browser | the tab without Laravel, on demo data |
| `API.md` | reference | every endpoint, if you ever need more than the page gives |

Nothing from Sherlocks' source is copied into the portal. The tab loads Sherlocks'
`app.js` / `app.css` from the Sherlocks server, so updates on that side reach the
portal without a redeploy.

---

## Step by step

**What you get from the Sherlocks admin:** the Sherlocks address
(`http://192.168.200.239:7401/sindhpolice-sherlocks`) and the **shared secret**,
which is given privately and must never be committed.

### 1. Check the connection (5 minutes, no Laravel code yet)

```bash
php test/make-token.php "<SHERLOCKS_SECRET>"
```

Run the two `curl` commands it prints. The first must say `"authenticated":true`. If it
says `false`, the secret is wrong. If the connection is refused, the Sherlocks server isn't
reachable from this network.

Then paste the token into `test/sherlocks-test.html` (`token: '…'`), open the file in a
browser, search `99999-0000001-1`, and watch the demo graph build. If that works, the
browser side works too.

### 2. Settings

Copy `laravel/config/sherlocks.php` to `config/`, and add the lines from
`laravel/env.example` to `.env`:

```ini
SHERLOCKS_API_BASE=http://192.168.200.239:7401/sindhpolice-sherlocks
SHERLOCKS_SECRET=<from the Sherlocks admin>
SHERLOCKS_TOKEN_TTL=900
```

### 3. Token, controller, routes, view

Copy `SherlocksToken.php`, `SherlocksController.php` and `tab.blade.php` into place, and
add the routes from `laravel/routes/sherlocks.php` inside your authenticated route group.
Then open `/sherlocks`: the search form appears, and a search builds the graph live.

In `SherlocksController` fill the two **TODOs** with your own storage:

- `store()` receives every finished search as JSON (`id`, `seed_label`, `params`,
  `stats`, `graph` {nodes, edges}, `events`). Save it however you like, e.g. one row per
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

---

## What the tab does by itself (nothing to build)

- **Live graph** over Server-Sent Events: people and links appear as each police system answers.
- **Search by** CNIC, mobile, email, or several people at once ("Multiple people").
- **Live EMS only.** The page is pinned with `backend: 'ems'`, so the data-source choice is hidden. It asks for confirmation before each live search.
- **Chat** about the graph, **person briefs**, **compare two people**, **leads** (linkage patterns), and the **AI investigator**, which streams its steps.
- **Token renewal:** when the token expires, the page calls `/sherlocks/token` and carries on.
- `onRunComplete(run)` fires once per finished search → POST to `store()`.

`window.Sherlocks` also exposes `loadGraph(run)`, `openRun(runId)`, `setToken(token)` and
`currentRun()`, in case the portal wants to drive the page from its own buttons.

---

## If something doesn't work

| Symptom | Cause / fix |
|---|---|
| `curl …/auth/session` → `"authenticated": false`, `"failed verification"` | `SHERLOCKS_SECRET` differs from the Sherlocks server's secret |
| `"Session expired"` | the server clocks differ by minutes: sync time (NTP) on both |
| Page loads unstyled, `app.js` 404 or blocked | `SHERLOCKS_API_BASE` wrong, or the browser can't reach `192.168.200.239:7401` |
| Browser console: **mixed content** | the portal is **https** and Sherlocks is **http**. The Sherlocks admin must put Sherlocks behind https (tell them) |
| Browser console: **CORS** error | the Sherlocks admin must add the portal's address to `SHERLOCKS_CORS_ORIGINS` |
| Graph appears only at the end, not live | a proxy is buffering: `proxy_buffering off;` for `/sindhpolice-sherlocks/graph/runs/` and `/graph/investigate` |
| `419` when saving | the page lacks the CSRF meta tag (it is in `tab.blade.php`; keep it) |

---

## Prompt for Cursor

> Integrate the Sherlocks tab into this Laravel portal using the files in `integration/`
> (read `integration/README.md` first). Copy `laravel/config/sherlocks.php`,
> `laravel/app/Support/SherlocksToken.php`, `laravel/app/Http/Controllers/SherlocksController.php`
> and `laravel/resources/views/sherlocks/tab.blade.php` into the matching paths, add the
> env keys from `laravel/env.example`, and register the routes from
> `laravel/routes/sherlocks.php` inside our authenticated route group. Implement the two
> TODOs in `SherlocksController` with a new `sherlocks_graphs` table (migration + model:
> user_id, run_id, subject, payload longText, timestamps) and our existing permission
> checks, and add a "Saved graphs" list linking to `route('sherlocks.graph', $id)`. Add a
> "Sherlocks" menu item opening `route('sherlocks.tab')` inside our layout via a same-site
> iframe. Do not copy or modify Sherlocks' JavaScript/CSS; the view loads them from
> `SHERLOCKS_API_BASE`. Keep everything between `@verbatim` and `@endverbatim` in the view unchanged.
