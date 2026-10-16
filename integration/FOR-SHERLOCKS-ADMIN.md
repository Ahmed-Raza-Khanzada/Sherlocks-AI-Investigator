# Sherlocks side of the integration (for you, not the Laravel developer)

1. **Give the developer:** the `integration/` folder, the address
   `http://192.168.200.239:7401/sindhpolice-sherlocks`, and **privately** the value of
   `SHERLOCKS_AUTH_SECRET` from Sherlocks' `.env` (in person or a password manager, not
   email, chat or the repository).
2. **Keep Sherlocks running** on 192.168.200.239 with docker compose, as now. One API
   process, the default, is required for the live stream.
3. **The portal's server calls Sherlocks, not the officers' browsers** (the tab goes
   through the portal's `/sherlocks/api` route, `SHERLOCKS_PROXY=true`). So Sherlocks
   only has to be reachable **from the Laravel server**: if that server is outside the
   police network, ask the network team for a route (VPN or firewall rule) to
   `192.168.200.239:7401`. Best: allow port 7401 only from the Laravel server's address.
   `SHERLOCKS_CORS_ORIGINS` is then not needed (same-origin for the browser).
4. **https**: the officer sees only the portal's https domain, so Sherlocks can stay on
   http inside the network. Only if the developer turns the proxy off
   (`SHERLOCKS_PROXY=false`) must Sherlocks be on https and in `SHERLOCKS_CORS_ORIGINS`.
5. Your own login to the standalone Sherlocks portal keeps working alongside the tab.
6. **After pulling this version**, on the Sherlocks server: rebuild the image (or
   `pip install -e .`) - the agent teams now need the `langgraph` package; run
   `alembic upgrade head` (the `case_board` table); restart. Nothing changes in the Laravel
   portal: the tab's script and styles are served by Sherlocks and update with it.
7. **If nginx is in front of Sherlocks**, a chat reply can take up to 40 seconds as one open
   stream: set `proxy_read_timeout 120s;` and `proxy_buffering off;` for
   `/graph/investigate` and `/graph/runs/`.
8. **The model stays on the police network.** LangGraph's optional LangSmith tracing is
   switched off in code (`src/sherlocks/team/runtime.py`); do not set `LANGSMITH_*`
   variables on the server.
9. **After pulling the October 2026 version**: rebuild and restart as in point 6 (no new
   migration). Give the developer the updated `integration/` folder - `README.md` has a
   section "This version: what changes for the portal" (short answer: nothing to copy
   again). Runs made before the update keep their old findings and report until their
   next background run or a new search; download a report with `?rebuild=true` to get the
   new structure at once.
10. **The agent trace** (`GET /graph/runs/{id}/trace`, see `API.md`) shows how every chat
    question was worked, agent by agent. It carries case data: give access to developers
    and admins only. One line per question is also written to the server log
    (`Turn 5 trace: read(…ms) -> brief(…) -> draft(…) | … ms | 5 model call(s)`).
11. **Tenant register (TRUST) links.** New searches label each person by their part in a
    tenancy (tenancy witness, landlord, tenant, family member) with the property, the
    lease dates and the police station. A run searched before the update keeps "Named in
    property record": a link's label is fixed when the graph is built, so search again.
    No setting to change.
12. **What changed, in one place:** `docs/sherlock-agent-plan.html`, section "What changed
    in v6", with the diagram of one question, one background run and one report. The
    developer's short version is in `README.md` ("How Sherlock works in this version").
13. **Before deploying, one line to check:** `src/sherlocks/team/trace.py`, in `AGENT`, the
    key of "O1 Officer agent" must be `"draft"`. While it reads anything else, the draft
    step of a trace shows the agent as `draft` instead of `O1 Officer agent` (the `API.md`
    example shows the right name) and one test fails. Answers are not affected.
14. **Front-end changes need no Laravel change.** The portal's tab is a short page that
    loads `portal/embed.js`, which pulls the Sherlocks page (`web/index.html`, `app.css`,
    `app.js`, `vendor/`) from this server each time the tab opens. Change the design, add
    a button or panel in `src/sherlocks/web/`, redeploy Sherlocks, and the tab shows it.
    Keep the element ids `app.js` uses. Tell the developer only when `SHERLOCKS_CONFIG`,
    a route or the token format changes.
