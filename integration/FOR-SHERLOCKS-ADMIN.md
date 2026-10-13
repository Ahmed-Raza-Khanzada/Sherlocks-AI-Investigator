# Sherlocks side of the integration (for you, not the Laravel developer)

1. **Give the developer:** the `integration/` folder, the address
   `http://192.168.200.239:7401/sindhpolice-sherlocks`, and **privately** the value of
   `SHERLOCKS_AUTH_SECRET` from Sherlocks' `.env` (in person or a password manager, not
   email, chat or the repository).
2. **Keep Sherlocks running** on 192.168.200.239 with docker compose, as now. One API
   process, the default, is required for the live stream.
3. **When the portal's address is known**, set it in Sherlocks' `.env` and restart:
   `SHERLOCKS_CORS_ORIGINS=http://portal-address` (comma-separate several). Until then
   any site may call the API with a valid token. Tokens are still required.
4. **If the portal runs on https**, Sherlocks must too, or browsers block it (mixed
   content): put nginx with a certificate in front of port 7401, and give the developer the
   new https address for `SHERLOCKS_API_BASE`.
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
