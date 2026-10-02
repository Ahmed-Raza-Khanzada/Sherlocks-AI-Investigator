#!/usr/bin/env bash
# Sherlocks container entrypoint: wait for Postgres, run migrations, then serve.
set -euo pipefail

echo "[entrypoint] Sherlocks starting"

# cdr_report_app must be importable from the source baked into the image.
if [ ! -d "${SHERLOCKS_REPORT_APP_SRC}/cdr_report_app" ]; then
  echo "[entrypoint] WARNING: cdr_report_app not found at ${SHERLOCKS_REPORT_APP_SRC}." >&2
  echo "[entrypoint]          Live provider lookups and PDF rendering will be unavailable;" >&2
  echo "[entrypoint]          live (EMS) search is unaffected. Rebuild with scripts/deploy.sh to include it." >&2
fi

# Wait for the database. The DSN comes from SHERLOCKS_DATABASE_URL / DATABASE_URL.
python - <<'PY'
import os
import sys
import time

import psycopg2

dsn = os.environ.get("SHERLOCKS_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not dsn:
    print("[entrypoint] No database URL set; skipping wait.")
    sys.exit(0)

for attempt in range(1, 61):
    try:
        psycopg2.connect(dsn).close()
        print(f"[entrypoint] Database reachable after {attempt} attempt(s).")
        break
    except Exception as exc:  # noqa: BLE001
        print(f"[entrypoint] Waiting for database ({attempt}/60): {exc}")
        time.sleep(2)
else:
    print("[entrypoint] Database never became reachable.", file=sys.stderr)
    sys.exit(1)
PY

# Apply migrations (creates the sherlocks schema, graph_run, provider_cache, ...).
echo "[entrypoint] Running database migrations"
alembic upgrade head

echo "[entrypoint] Launching API on ${SHERLOCKS_API_HOST:-0.0.0.0}:${SHERLOCKS_API_PORT:-7401}"
exec uvicorn sherlocks.api.main:app \
  --host "${SHERLOCKS_API_HOST:-0.0.0.0}" \
  --port "${SHERLOCKS_API_PORT:-7401}"
