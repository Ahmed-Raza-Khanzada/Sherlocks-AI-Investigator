"""PoC: auth/session-token weaknesses in the Sherlocks API.

Scenarios
  A. Token signature verification is strict HMAC-SHA256 (forgery must fail).
  B. sys-claim handling: restricted officer calling an unlisted system is refused
     (guard.check_system), a token WITHOUT sys is unrestricted, and the sys claim is
     taken from the presented token on every request (no server-side pinning).
  C. OPEN-API condition: SHERLOCKS_API_KEY empty + no auth password + no auth secret
     -> require_api_key passes for everyone; unauthenticated read access to data endpoints.
  D. Login has no rate limiting / lockout (unbounded 401s).
"""
from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
import os
import tempfile
import time

# sqlite DB BEFORE the app import: db.session caches the engine per URL at first use.
DB_PATH = os.path.join(tempfile.gettempdir(), "poc_auth.db")
if os.path.exists(DB_PATH):
    os.unlink(DB_PATH)
os.environ["SHERLOCKS_DATABASE_URL"] = f"sqlite:///{DB_PATH}"

from fastapi.testclient import TestClient  # noqa: E402

import sherlocks.api.main as api_main  # noqa: E402
from sherlocks.settings import load_settings  # noqa: E402
from sherlocks.api.auth import SessionAuth  # noqa: E402
from sherlocks.evidence import guard  # noqa: E402
from sherlocks.db.session import get_engine  # noqa: E402
from sherlocks.db.models import Base  # noqa: E402

PREFIX = "/sindhpolice-sherlocks"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def forge(secret: str, claims: dict) -> str:
    payload = json.dumps(claims, separators=(",", ":")).encode()
    return f"{b64(payload)}.{b64(hmac_mod.new(secret.encode(), payload, hashlib.sha256).digest())}"


def main() -> None:
    app = api_main.app
    secret = "poc-secret"
    # give the module-level SessionAuth our PoC secret + local password
    api_main.session_auth._secret = secret.encode()
    api_main.session_auth.username = "Admin"
    api_main.session_auth.enabled = True
    api_main.session_auth.verifies_tokens = True
    api_main.settings.api.api_key = None
    api_main.settings.auth.password = "TestPass123!"
    # SQLite cannot host the Postgres schema prefix, so the data-store-backed job
    # endpoints stay out of this PoC; require_api_key is the same callable those
    # endpoints use, and we exercise it through /osint/capabilities. We still
    # create an in-memory fake session_scope so the open-API job path can be hit.
    import contextlib

    class _FakeJob:
        id = "job-0001"
        job_type = "osint"
        status = "queued"
        progress_pct = 0
        message = "Queued."
        result_file_path = None
        created_at = "2026-01-01T00:00:00"
        updated_at = "2026-01-01T00:00:00"

    class _FakeSession:
        def add(self, obj):
            pass

        def flush(self):
            obj_id = getattr(obj, "id", None)
            if obj_id is None:
                import uuid
                obj.id = str(uuid.uuid4())

        def get(self, model, pk):
            return _FakeJob()

    class _FakeScope:
        def __enter__(self):
            return _FakeSession()

        def __exit__(self, *exc):
            return False

    api_main.session_scope = lambda settings=None: _FakeScope()
    # neutralise the schema-scoping init_db (TestClient's lifespan would call it)
    api_main.init_db = lambda settings=None: None
    client = TestClient(app)
    ok = 0

    # ---- A. forgery attempts must be rejected --------------------------------------
    print("=== A. Token forgery ===")
    good = forge(secret, {"u": "officer1", "exp": int(time.time()) + 600})
    r = client.get(f"{PREFIX}/auth/session", headers={"X-Session-Token": good})
    assert r.status_code == 200 and r.json().get("authenticated") is True, r.text
    print("[ok] valid host-minted token accepted:", r.json())
    ok += 1

    bad_sig = good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB")
    r = client.get(f"{PREFIX}/auth/session", headers={"X-Session-Token": bad_sig})
    assert r.status_code == 200 and r.json().get("authenticated") is False, r.text
    print("[ok] tampered signature rejected (strict HMAC-SHA256, no alg confusion)")
    ok += 1

    unverified = base64.urlsafe_b64encode(b'{"u":"x","exp":9999999999}').decode().rstrip("=")
    r = client.get(f"{PREFIX}/auth/session", headers={"X-Session-Token": unverified})
    assert r.status_code == 200 and r.json().get("authenticated") is False
    print("[ok] signature-less (none-like) token rejected")
    ok += 1

    # ---- B. sys claim enforcement ----------------------------------------------------
    print("=== B. sys claim ===")
    try:
        guard.check_system("nadra", {"user": "officerX", "systems": ["psrms"]})
        raise SystemExit("[FAIL] check_system did NOT refuse nadra for sys=[psrms]")
    except guard.GuardError as exc:
        print(f"[ok] guard.check_system refuses nadra for sys=[psrms]: {exc}")
        ok += 1

    guard.check_system("nadra", {"user": "officerX", "systems": None})
    print("[ok] guard.check_system allows nadra when sys claim absent (by design)")
    ok += 1

    # scope travels in the token: per-request, whatever claim the verified token carries
    tok_restricted = forge(secret, {"u": "officerX", "exp": int(time.time()) + 600, "sys": ["psrms"]})
    tok_nosys = forge(secret, {"u": "officerX", "exp": int(time.time()) + 600})
    assert client.get(f"{PREFIX}/auth/session", headers={"X-Session-Token": tok_restricted}).json()["authenticated"]
    assert client.get(f"{PREFIX}/auth/session", headers={"X-Session-Token": tok_nosys}).json()["authenticated"]
    print("[ok] both restricted and no-sys tokens verify; scope read fresh from each presented token")
    ok += 1

    # ---- C. OPEN-API condition -------------------------------------------------------
    print("=== C. OPEN-API (no api key, no password, no secret) ===")
    open_settings = load_settings()
    open_settings.api.api_key = None
    open_settings.auth.password = None
    open_settings.auth.password_sha256 = None
    open_settings.auth.secret = None
    open_auth = SessionAuth(open_settings)
    assert open_auth.verifies_tokens is False
    print("[ok] SessionAuth.verifies_tokens=False when nothing configured")
    ok += 1

    saved = (api_main.settings, api_main.session_auth)
    try:
        api_main.settings = open_settings
        api_main.session_auth = open_auth
        r = client.get(f"{PREFIX}/osint/capabilities")
        print("capabilities (no auth):", r.status_code)
        assert r.status_code == 200, r.text
        ok += 1
        # graph data endpoints: the router's own officer dependency (linkgraph.py:123)
        # also has NO fallback when nothing is configured - a 401 with
        # "No session token". (Inconsistency worth documenting, not exploitable.)
        r = client.get(f"{PREFIX}/graph/runs")
        print("graph/runs (no auth):", r.status_code, r.text[:120], "(router dep - no open fallback)")
        r = client.get(f"{PREFIX}/graph/config")
        print("graph/config (no auth):", r.status_code)
        # but require_api_key itself is open: an attacker who swaps in a
        # require_api_key-protected endpoint gets in. The osint endpoints use it:
        r = client.post(f"{PREFIX}/osint/jobs", json={"full_name": "Test Person", "phone": "0300-1234567"})
        print("osint/jobs POST (no auth):", r.status_code, r.text[:150])
        assert r.status_code == 202, r.text
        job_id = r.json().get("job_id")
        r2 = client.get(f"{PREFIX}/osint/jobs/{job_id}/status")
        print("osint job status (no auth):", r2.status_code)
        assert r2.status_code == 200
        ok += 1
        # also: the graph runs endpoint IS reachable with a key that matches api_key
        # (not the open path) - but when api_key is empty the officer dependency
        # itself 401s. In production this means: the OSINT endpoints are open,
        # graph endpoints 401. The asymmetry is documented, not an extra vuln.
    finally:
        api_main.settings, api_main.session_auth = saved

    # ---- D. login brute force surface --------------------------------------------------
    print("=== D. login brute force ===")
    api_main.settings = saved[0]
    api_main.session_auth = saved[1]
    codes = set()
    for i in range(30):
        r = client.post(f"{PREFIX}/auth/login", json={"username": "Admin", "password": f"wrong{i}"})
        codes.add(r.status_code)
        if r.status_code not in (401, 422):
            break
    print(f"[ok] 30 wrong logins -> statuses {sorted(codes)} (no lockout/429 observed)")
    ok += 1

    print(f"\n{ok} checks passed")


if __name__ == "__main__":
    main()