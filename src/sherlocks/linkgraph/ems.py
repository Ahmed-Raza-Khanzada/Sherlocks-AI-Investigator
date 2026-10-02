"""Native EMS / iCOP backend — the real police & government endpoints.

This is a second live backend, distinct from the cdr_report_app one. It speaks the
exact endpoints, keys and identifier formats documented for the EMS / iCOP deployment
(different hosts, headers and shapes than the Shield-gateway providers), and normalises
every response into the record shapes the extractors in ``extractors.py`` already parse
- so the graph engine, identity resolution, linking and depth expansion are unchanged.

Each system searchable by phone is queried once per number by the engine's per-number
sweep; each CNIC-keyed system once per CNIC. Nothing is dropped: a system that errors
is reported as ``error`` (unknown), never silently as "no record".

Every endpoint and credential is read from ``EMS_*`` variables in ``.env`` (names in
``.env.example``); none is kept in source, so a credential can be rotated without a code
change and the repository can be shared. The face-match Tenant FR
service (``/sindhpolice-fr``) is image-driven, not an id lookup, and is out of scope
here.

Vehicle systems (Excise, TRACS, AVLC) return the owner as the record's subject and any
co-owner / complainant / driver as related people, plus the plates as attributes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from sherlocks.linkgraph.normalize import (
    cnic13,
    dashed_cnic,
    dashed_mobile,
    mobile11,
    phone_variants,
)
from sherlocks.linkgraph.systems import SYSTEMS, system_label

logger = logging.getLogger(__name__)


def _load_env_file() -> dict[str, str]:
    """Read .env once so EMS_* overrides work in a plain local run too (the process env
    is not pre-populated from .env; Docker's env_file already injects it)."""
    from pathlib import Path

    from dotenv import dotenv_values

    from sherlocks.settings import PROJECT_ROOT

    merged: dict[str, str] = {}
    for path in (PROJECT_ROOT / ".env", Path.cwd() / ".env"):
        if path.exists():
            merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    return merged


_ENV_FILE = _load_env_file()


def _env(name: str, default: str = "") -> str:
    # Real env var wins, then the .env file. No credential or internal host is kept in
    # source: every EMS_* value lives in .env (see .env.example for the names).
    return os.environ.get(name) or _ENV_FILE.get(name) or default


# --------------------------------------------------------------------------------------
# Endpoints + credentials - all read from EMS_* in .env, never kept in source
# --------------------------------------------------------------------------------------

CONF = {
    "cro_url": _env("EMS_CRO_URL"),
    "cro_key": _env("EMS_CRO_KEY"),
    "arms_url": _env("EMS_ARMS_URL"),
    "arms_api_key": _env("EMS_ARMS_API_KEY"),
    "arms_secret_key": _env("EMS_ARMS_SECRET_KEY"),
    "psrms_url": _env("EMS_PSRMS_URL"),
    "psrms_key": _env("EMS_PSRMS_KEY"),
    "psrms_check_url": _env("EMS_PSRMS_CHECK_URL"),
    "cfms_url": _env("EMS_CFMS_URL"),
    "watchlist_url": _env("EMS_WATCHLIST_URL"),
    "watchlist_key": _env("EMS_WATCHLIST_KEY"),
    "hotel_cnic_url": _env("EMS_HOTEL_CNIC_URL"),
    "hotel_mobile_url": _env("EMS_HOTEL_MOBILE_URL"),
    "hotel_key": _env("EMS_HOTEL_KEY"),
    "sbvs_base": _env("EMS_SBVS_BASE"),
    "sbvs_key": _env("EMS_SBVS_KEY"),
    "prvs_base": _env("EMS_PRVS_BASE"),
    "prvs_key": _env("EMS_PRVS_KEY"),
    "evs_url": _env("EMS_EVS_URL"),
    "hope_emp_url": _env("EMS_HOPE_EMP_URL"),
    "hope_empr_url": _env("EMS_HOPE_EMPR_URL"),
    "wtower_i_key": _env("EMS_WTOWER_I_KEY"),
    "wtower_j_key": _env("EMS_WTOWER_J_KEY"),
    "wtower_api_token": _env("EMS_WTOWER_API_TOKEN"),
    "dls_login_url": _env("EMS_DLS_LOGIN_URL"),
    "dls_api": _env("EMS_DLS_API"),
    "dls_user": _env("EMS_DLS_USER"),
    "dls_pass": _env("EMS_DLS_PASS"),
    "igp_url": _env("EMS_IGP_URL"),
    "igp_key": _env("EMS_IGP_KEY"),
    "pfc_url": _env("EMS_PFC_URL"),
    "pfc_key": _env("EMS_PFC_KEY"),
    "hrmis_url": _env("EMS_HRMIS_URL"),
    "hrmis_key": _env("EMS_HRMIS_KEY"),
    "hrmis_basic": _env("EMS_HRMIS_BASIC"),
    "tenant_base": _env("EMS_TENANT_BASE"),
    "tenant_key": _env("EMS_TENANT_KEY"),
    "milap_url": _env("EMS_MILAP_URL"),
    "milap_key": _env("EMS_MILAP_KEY"),
    "subscriber_url": _env("EMS_SUBSCRIBER_URL"),
    "subscriber_appkey": _env("EMS_SUBSCRIBER_APPKEY"),
    "nadra_url": _env("EMS_NADRA_URL"),
    "nadra_autokenn": _env("EMS_NADRA_AUTOKENN"),
    "nadra_secrt": _env("EMS_NADRA_SECRT"),
    "nadra_requester": _env("EMS_NADRA_REQUESTER"),
    "excise_url": _env("EMS_EXCISE_URL"),
    "excise_basic": _env("EMS_EXCISE_BASIC"),
    "tracs_url": _env("EMS_TRACS_URL"),
    "tracs_basic": _env("EMS_TRACS_BASIC"),
    "avlc_url": _env("EMS_AVLC_URL"),
    "avlc_key": _env("EMS_AVLC_KEY"),
    # JWT expires (exp ~2026); rotate via EMS_AVLC_BEARER when AVLC starts returning 401.
    "avlc_bearer": _env("EMS_AVLC_BEARER"),
}

# EMS adds these systems on top of the shared registry.
EMS_EXTRA_SYSTEMS = ["arms", "nadra", "excise", "avlc"]
# Order the EMS run visits systems in. Identity first (subscriber resolves the CNIC),
# then criminal, then the rest. CFMS/PFC/etc. included - they are configured here.
EMS_ORDER = [
    "subscriber", "nadra", "cro", "arms", "psrms", "watchlist", "cfms",
    "prvs", "old_tenant", "trust", "hotel_eye", "sbvs", "evs", "hope", "hrmis",
    "dls", "tracs", "excise", "avlc", "igp_cms", "pfc", "milap",
]

# Some endpoints are slow: TRUST's comprehensive-info aggregates several tables (~40s),
# and Hotel Eye's by-mobile search can return hundreds of stays (~90s). A short timeout
# aborts a request that would have returned real data, so these two get a longer one.
# Override with EMS_SLOW_TIMEOUT in .env.
SLOW_TIMEOUT = int(_env("EMS_SLOW_TIMEOUT", _env("EMS_TRUST_TIMEOUT", "120")))
TRUST_TIMEOUT = SLOW_TIMEOUT


# --------------------------------------------------------------------------------------
# result helpers
# --------------------------------------------------------------------------------------


def _result(system: str, status: str, summary: str, *, hit: bool = False, data: Any = None,
            raw: Any = None, errors: list[str] | None = None) -> dict[str, Any]:
    return {"provider": system, "hit": hit, "status": status, "summary": summary,
            "data": data, "raw": raw, "errors": errors or ([summary] if status == "error" else [])}


def _no(system: str) -> dict[str, Any]:
    return _result(system, "no_record", "No record found")


def _err(system: str, message: str, raw: Any = None) -> dict[str, Any]:
    return _result(system, "error", message, raw=raw)


_EMPTY_STRINGS = {"", "null", "none", "nan", "-", "n/a", "0"}


def _meaningful(value: Any) -> bool:
    """True if there is a real value somewhere in here - not just empty lists, nulls or
    placeholder strings. ``{"employees": [], "picture_new": null}`` is NOT meaningful."""
    if isinstance(value, dict):
        return any(_meaningful(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_meaningful(v) for v in value)
    if isinstance(value, str):
        return value.strip().lower() not in _EMPTY_STRINGS
    return value not in (None, False)


def _rows(value: Any) -> list[dict]:
    """Dict rows that actually carry data. An all-empty envelope row is dropped, so the
    adapter reports no_record and the engine creates no node."""
    if isinstance(value, list):
        return [r for r in value if isinstance(r, dict) and _meaningful(r)]
    if isinstance(value, dict):
        return [value] if _meaningful(value) else []
    return []


def _b64_to_data_uri(value: Any) -> Any:
    """A bare base64 photo (no data: prefix) → data URI so the image layer keeps it."""
    if isinstance(value, str) and len(value) > 200 and "://" not in value and not value.startswith("data:"):
        return "data:image/jpeg;base64," + value
    return value


def _first(d: dict, *keys: str) -> Any:
    """First present, non-empty value among ``keys`` - upstream systems disagree on names."""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


# What NADRA counts as "the request succeeded". Different deployments use different flags.
_NADRA_OK = {"true", "1", "success", "verified", "ok", "found"}

# Phrases an API returns when it REFUSES the request (IP not whitelisted, bad key, etc).
# This is an access failure, NOT "this person has no record" - it must show as FAILED so
# the operator fixes access instead of believing the person is unknown.
_ACCESS_DENIED = ("invalid ip", "ip address", "not allowed", "unauthor", "forbidden",
                  "access denied", "not permitted", "whitelist", "invalid token",
                  "invalid api", "authentication failed", "invalid credentials")


def _access_error(body: Any) -> str | None:
    """If a 200 body is really an access refusal, return its message; else None."""
    if not isinstance(body, dict):
        return None
    msg = str(body.get("message") or body.get("messaage") or body.get("msg")
              or body.get("error") or body.get("detail") or "")
    low = msg.lower()
    if msg and any(p in low for p in _ACCESS_DENIED):
        return msg
    return None


def _nadra_record(body: dict) -> dict | None:
    """Pull the citizen record out of a NADRA response, whatever shape it takes.

    Deployments vary: the flag may be ``status`` / ``success`` / ``code`` and boolean,
    "true", 1 or "success"; the record may sit under ``data`` / ``result`` / ``citizen``
    / ``details`` / ``record``, be a one-item list, or be the body's own top-level fields.
    A record is anything carrying a name or a citizen number.
    """
    flag = body.get("status")
    if flag is None:
        flag = body.get("success")
    if flag is None:
        flag = body.get("code")
    ok = flag is True or (isinstance(flag, (int, float)) and flag in (1, 200)) \
        or (isinstance(flag, str) and flag.strip().lower() in _NADRA_OK)
    # An explicit failure flag with no payload → no record.
    if flag is not None and not ok and not any(k in body for k in ("data", "result", "citizen", "record", "details")):
        return None

    def looks_like_person(d: Any) -> bool:
        return isinstance(d, dict) and bool(_first(
            d, "name", "Name", "full_name", "fullName", "citizen_name", "person_name", "nameEng",
            "citizen_number", "cnic", "cnic_no", "nic", "father_husband_name", "father_name"))

    for key in ("data", "result", "citizen", "record", "details", "response"):
        payload = body.get(key)
        if isinstance(payload, list):
            payload = next((x for x in payload if looks_like_person(x)), None)
        if looks_like_person(payload):
            return payload
    return body if looks_like_person(body) else None


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


@dataclass
class Req:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] | None = None
    data: dict[str, Any] | None = None
    json: Any = None
    timeout: int | None = None   # override the client default for a slow endpoint


_SECRET_HEADERS = {"autokenn", "secrt", "api_key", "secret_key", "x-api-key", "api-key",
                   "authorization", "psrms-api-key", "i_key", "j_key", "api_token", "x-key", "cookie"}
_SECRET_FIELDS = {"appkey", "psrms-api-key", "password", "api_key", "secret_key", "token"}


def _redact(mapping: dict[str, Any] | None, secret_keys: set[str]) -> dict[str, Any]:
    """Copy a header/body dict with secret values masked, so a log can be shared and
    pasted into a ticket without leaking a live key. Non-secret values (the CNIC, the
    number, page size) are kept in full - they are the whole point of the log."""
    out: dict[str, Any] = {}
    for k, v in (mapping or {}).items():
        if k.lower() in secret_keys and v:
            s = str(v)
            out[k] = f"<set:{len(s)} chars>" if len(s) <= 8 else f"{s[:3]}…<{len(s)} chars>"
        else:
            out[k] = v
    return out


class EmsHttp:
    def __init__(self, timeout: int = 30, verify: bool = False, log_path: str | None = None,
                 failures_path: str | None = None) -> None:
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = verify
        self.timeout = timeout
        self.log_path = log_path
        self.failures_path = failures_path
        self._log_lock = threading.Lock()
        self._ctx = threading.local()
        if not verify:
            requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

    def set_context(self, system: str | None, queried: str | None) -> None:
        """Tag the calls the current thread is about to make, so the log shows which
        system was asked and what identifier it was given."""
        self._ctx.system = system
        self._ctx.queried = queried

    def send(self, req: Req) -> tuple[int, Any]:
        import time as _time

        started = _time.time()
        error: str | None = None
        status = 0
        text = ""
        try:
            resp = self.session.request(req.method, req.url, headers=req.headers, params=req.params,
                                        data=req.data, json=req.json, timeout=req.timeout or self.timeout)
            status = resp.status_code
            # PSRMS (and others) prefix the JSON with a UTF-8 BOM, which resp.json() chokes
            # on - it would fall through to a text blob and every hit would read as
            # no_record. Strip the BOM and any surrounding whitespace before parsing.
            text = (resp.text or "").lstrip("\ufeff").strip()
            try:
                parsed: Any = json.loads(text)
            except ValueError:
                parsed = {"text_response": resp.text}
            # A gateway/server error (502/503/504\u2026) or a non-JSON error page is a BROKEN
            # API, not "no record". Raise so lookup() reports it as `error` and it shows
            # in "Failed systems" instead of silently reading as no_record. 404 is left
            # alone - several systems (CRO, PRVS, NADRA\u2026) use it to mean "no record".
            non_json = isinstance(parsed, dict) and "text_response" in parsed
            if status >= 500 or (status >= 400 and status != 404 and non_json):
                snippet = text[:200].replace("\n", " ").strip()
                error = f"HTTP {status}: {snippet or 'server/gateway error'}"
                raise requests.HTTPError(error, response=resp)
            return status, parsed
        except requests.RequestException as exc:
            error = repr(exc)
            raise
        finally:
            self._log(req, status, text, error, (_time.time() - started) * 1000)

    def _log(self, req: Req, status: int, response_text: str, error: str | None, ms: float) -> None:
        """Write the full outbound call (system, input, URL, body, response) to the audit
        file, so exactly what Sherlocks sent - and got back - is auditable and comparable
        to a manual curl. A call that failed (transport error or HTTP >= 400) is ALSO
        written to a failures-only file, so broken APIs can be found without wading through
        every successful lookup. Secret keys are masked in both."""
        from datetime import datetime

        system = getattr(self._ctx, "system", None)
        queried = getattr(self._ctx, "queried", None)
        failed = bool(error) or status == 0 or status >= 400
        tag = f"[{system or '?'}]" + (f" input={queried}" if queried else "")
        head = (f"{datetime.now().isoformat(timespec='seconds')}  {tag}  "  # noqa: DTZ005
                f"{req.method} {req.url}  ({ms:.0f} ms) -> {status or 'ERR'}")
        lines = ["=" * 90, head, f"  headers: {_redact(req.headers, _SECRET_HEADERS)}"]
        if req.params:
            lines.append(f"  params:  {_redact(req.params, _SECRET_FIELDS)}")
        if req.data:
            lines.append(f"  data:    {_redact(req.data, _SECRET_FIELDS)}")
        if req.json is not None:
            lines.append(f"  json:    {_redact(req.json, _SECRET_FIELDS) if isinstance(req.json, dict) else req.json}")
        if error:
            lines.append(f"  ERROR:   {error}")
        lines.append(f"  response: {response_text[:1500]}")
        lines.append("")
        block = "\n".join(lines) + "\n"

        try:
            with self._log_lock:
                if self.log_path:
                    with open(self.log_path, "a", encoding="utf-8") as fh:
                        fh.write(block)
                if failed and self.failures_path:
                    with open(self.failures_path, "a", encoding="utf-8") as fh:
                        fh.write(block)
        except OSError:
            pass  # logging must never break a lookup


# --------------------------------------------------------------------------------------
# The backend
# --------------------------------------------------------------------------------------


class EmsBackend:
    name = "ems"
    # EMS has no Caller ID service; the engine skips that step instead of reporting it
    # FAILED. The Telecom Subscriber (SIMs) lookup already gives names for every number.
    supports_caller_id = False

    def __init__(self, http: EmsHttp | None = None) -> None:
        self.http = http or EmsHttp()
        self._dls_token: str | None = None
        self._dls_lock = threading.Lock()
        self._adapters: dict[str, Callable[[str | None, str | None], dict[str, Any]]] = {
            "subscriber": self._subscriber, "nadra": self._nadra, "cro": self._cro, "arms": self._arms,
            "psrms": self._psrms, "watchlist": self._watchlist, "cfms": self._cfms, "prvs": self._prvs,
            "old_tenant": self._old_tenant, "trust": self._trust, "hotel_eye": self._hotel_eye,
            "sbvs": self._sbvs, "evs": self._evs, "hope": self._hope, "hrmis": self._hrmis,
            "dls": self._dls, "tracs": self._tracs, "excise": self._excise, "avlc": self._avlc,
            "igp_cms": self._igp_cms, "pfc": self._pfc, "milap": self._milap,
        }

    # -- introspection --------------------------------------------------------------

    def systems_status(self) -> list[dict[str, Any]]:
        rows = []
        order = [s for s in EMS_ORDER if s in self._adapters]
        for key in order:
            info = SYSTEMS.get(key)
            rows.append({
                "system": key, "label": info.label if info else key.upper(),
                "category": info.category if info else "identity",
                "description": info.description if info else "", "needs": info.needs if info else "either",
                "enabled": True, "ready": True, "missing": [],
            })
        return rows

    def lookup(self, system: str, cnic: str | None, phone: str | None) -> dict[str, Any]:
        adapter = self._adapters.get(system)
        if adapter is None:
            return _result(system, "error", f"System {system} not available on the EMS backend")
        # Tag every HTTP call this lookup makes with the system and the identifier, so the
        # log/failures file shows which API was asked and with what.
        ctx = getattr(self.http, "set_context", None)
        if callable(ctx):
            ctx(system, " & ".join(x for x in (cnic13(cnic), mobile11(phone)) if x) or None)
        try:
            return adapter(cnic13(cnic), mobile11(phone))
        except requests.RequestException as exc:
            return _err(system, f"{system_label(system)} request failed: {exc}")
        except Exception as exc:  # an adapter bug costs one system, never the run
            logger.exception("EMS adapter %s crashed", system)
            return _err(system, f"{type(exc).__name__}: {exc}")

    def _both(self, by_cnic: Any, by_mobile: Any) -> list[tuple[str, int, Any]] | None:
        """Send the CNIC query AND the mobile query, and return both answers.

        Asking only by CNIC loses records that are filed under the person's number with
        a different CNIC on them - a PRVS tenancy case registered by the landlord, a SIM
        registered in a relative's name. Falsy (no identifier) requests are skipped;
        ``None`` back means neither identifier was usable.
        """
        pairs = [(branch, req) for branch, req in (("by_cnic", by_cnic), ("by_mobile", by_mobile)) if req]
        if not pairs:
            return None
        out: list[tuple[str, int, Any]] = []
        for branch, req in pairs:
            code, body = self.http.send(req)
            out.append((branch, code, body))
        return out

    def fir_roster(self, fir_no: str, fir_year: str, ps_id: str) -> dict[str, Any]:
        # EMS personsearch does not return the ps id needed to fetch a FIR file report.
        return _result("fir_roster", "no_record", "FIR roster not available on the EMS backend")

    def caller_id(self, phone: str) -> dict[str, Any]:
        return _result("caller_id", "error", "Caller ID excluded from the EMS backend")

    # -- identity -------------------------------------------------------------------

    def _subscriber(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        # One field "number": 13-digit CNIC, or 10-digit mobile (leading 0 stripped).
        # Both are asked: the CNIC gives every SIM on it, the number gives the SIM's
        # registered owner - who is often somebody else, and is a link worth having.
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        probes = self._both(
            cnic and Req("POST", CONF["subscriber_url"], headers=headers,
                         data={"number": cnic, "appkey": CONF["subscriber_appkey"]}),
            phone and Req("POST", CONF["subscriber_url"], headers=headers,
                          data={"number": phone[1:], "appkey": CONF["subscriber_appkey"]}))
        if probes is None:
            return _result("subscriber", "invalid_input", "CNIC or mobile required")
        sims: list[dict] = []
        seen: set[str] = set()
        source: dict[str, Any] = {}
        for branch, _code, body in probes:
            if not isinstance(body, dict):
                continue
            source[branch] = body
            for key in sorted(k for k in body if str(k).isdigit()):
                row = body[key]
                if not (isinstance(row, dict) and (mobile11(row.get("number")) or cnic13(row.get("cnic")))):
                    continue
                sim = {"number": mobile11(row.get("number")), "name": row.get("name"),
                       "cnic": cnic13(row.get("cnic")), "address": row.get("address")}
                ident = f"{sim['number']}|{sim['cnic']}"
                if ident not in seen:
                    seen.add(ident)
                    sims.append(sim)
        if not source:
            return _err("subscriber", "Subscriber lookup failed", raw=[b for _, _, b in probes])
        if not sims:
            return _no("subscriber")
        named = next((s for s in sims if s.get("name")), sims[0])
        # Reshape to the subscriber-branch raw the _subscriber extractor reads, one
        # SubscriberList row per SIM so every number on the CNIC becomes searchable.
        branch = "by_cnic" if cnic else "by_mobile"
        rows = [{"Name": s.get("name"), "Cnic": s.get("cnic"), "Phone": s.get("number"),
                 "Address": s.get("address")} for s in sims]
        return _result("subscriber", "success",
                       f"Telecom record found (Name: {named.get('name') or 'Unknown'}, SIMs: {len(sims)})",
                       hit=True, raw={branch: {"Data": {"SubscriberList": rows}}, "_source": source},
                       data={"name": named.get("name"), "cnic": named.get("cnic"),
                             "mobile": named.get("number"), "address": named.get("address")})

    def _nadra(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        if not cnic:
            return _result("nadra", "invalid_input", "CNIC required")
        headers = {"autokenn": CONF["nadra_autokenn"], "secrt": CONF["nadra_secrt"],
                   "requester": CONF["nadra_requester"], "Content-Type": "application/x-www-form-urlencoded"}
        # Archive first (older/richer record), then live if archive has nothing.
        denied: str | None = None
        for source, extra in (("archive", {"archive": "true"}), ("live", {})):
            code, body = self.http.send(Req("POST", CONF["nadra_url"], headers=headers,
                                            data={"cnic": cnic, **extra}))
            if code == 404 or not isinstance(body, dict):
                continue
            # NADRA refusing the request (IP not whitelisted, bad key) is not "no record".
            denied = _access_error(body)
            if denied:
                continue
            d = _nadra_record(body)
            if not d:
                continue
            d = {**d, "photograph": _b64_to_data_uri(d.get("photograph") or d.get("Photo") or d.get("photo") or d.get("image"))}
            name = _first(d, "name", "Name", "full_name", "fullName", "citizen_name", "person_name", "nameEng")
            return _result("nadra", "success",
                           f"NADRA verified ({source}): {name or 'record'}",
                           hit=True, raw={"source": source, "data": d},
                           data={"details": {
                               "name": name,
                               "father_husband_name": _first(d, "father_husband_name", "father_name", "fatherName",
                                                             "father", "guardian_name", "husband_name"),
                               "gender": _first(d, "gender", "Gender", "sex"),
                               "date_of_birth": _first(d, "date_of_birth", "dob", "dateOfBirth", "birth_date"),
                               "present_address": _first(d, "present_address", "presentAddress", "current_address", "address"),
                               "permanent_address": _first(d, "permanent_address", "permanentAddress", "perm_address"),
                               "cnic": _first(d, "citizen_number", "cnic", "cnic_no", "nic") or cnic, "source": source,
                           }})
        if denied:
            return _err("nadra", f"NADRA refused the request: {denied}. This is an access "
                                 "problem (e.g. server IP not whitelisted), not 'no record'.")
        return _no("nadra")

    # -- criminal -------------------------------------------------------------------

    def _cro(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        if not cnic:
            return _result("cro", "invalid_input", "CNIC required")
        code, body = self.http.send(Req("GET", CONF["cro_url"], headers={"Accept": "application/json", "X-Key": CONF["cro_key"]},
                                        params={"cnic": cnic}))
        if code == 404:
            return _no("cro")
        rows = _rows(body.get("data")) if isinstance(body, dict) else []
        if not rows:
            return _no("cro")
        first = rows[0]
        firs = [{"fir_no": f.get("fir_no"), "fir_year": f.get("fir_year"),
                 "police_station": f.get("ps_name") or f.get("ps_desc"), "offence": f.get("fir_offence"),
                 "status": f.get("status_desc")} for f in _rows(first.get("FIRList"))]
        return _result("cro", "success", f"CRO #{first.get('cro_no') or ''} - {len(firs)} FIR(s)".strip(),
                       hit=True, raw={"data": [{**first, "cro_photo_front": _b64_to_data_uri(first.get("cro_photo_front"))}]},
                       data={"cro_no": first.get("cro_no"), "name": first.get("cro_full_name"),
                             "father_name": first.get("cro_father_name"), "age": str(first.get("cro_age") or ""),
                             "category": first.get("category_desc"), "district": first.get("record_district"),
                             "fir_count": len(firs), "firs": firs})

    def _arms(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        if not cnic:
            return _result("arms", "invalid_input", "CNIC required")
        _code, body = self.http.send(Req("POST", CONF["arms_url"],
                                        headers={"Accept": "application/json", "Content-Type": "application/json",
                                                 "api_key": CONF["arms_api_key"], "secret_key": CONF["arms_secret_key"]},
                                        json={"cnic": int(cnic)}))
        rows = _rows(body.get("data")) if isinstance(body, dict) and body.get("status") else []
        if not rows:
            return _no("arms")
        return _result("arms", "success", f"ARMS profile found ({len(rows)})", hit=True, raw={"data": rows},
                       data={"records": rows})

    def _psrms(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        if not (cnic or phone):
            return _result("psrms", "invalid_input", "CNIC or mobile required")
        # PSRMS matches the phone as a literal string, and our DB stores it in several
        # formats (0300-1234567, 03001234567, 3001234567, 923001234567, +92...). So the
        # phone is retried in every format; CNIC likewise dashed then plain.
        queries: list[tuple[str, str]] = []
        if cnic:
            queries.append(("cnic", dashed_cnic(cnic) or ""))
            queries.append(("cnic", cnic))
        for variant in phone_variants(phone):
            queries.append(("phone", variant))

        rows: list[dict] = []
        raw: dict[str, Any] = {}
        seen: set[str] = set()
        phone_hit = False
        for qfield, value in queries:
            if not value:
                continue
            # once a phone format has matched, stop hammering the remaining formats
            if phone_hit and qfield == "phone":
                break
            data = {"PSRMS-API-KEY": CONF["psrms_key"], qfield: value, "page": "1", "page_size": "50"}
            _code, body = self.http.send(Req("POST", CONF["psrms_url"],
                                             headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                                             data=data))
            raw[f"{qfield}:{value}"] = body
            if isinstance(body, dict) and body.get("status"):
                for r in _rows(body.get("data")):
                    key = f"{r.get('fir_no')}/{r.get('fir_year')}/{r.get('ps_name')}/{r.get('person_cnic')}"
                    if key not in seen:
                        seen.add(key)
                        rows.append(r)
                        if qfield == "phone":
                            phone_hit = True
        # Legacy criminal check (checkperson, CNIC only). It answers a different question
        # than personsearch - "is this CNIC a known criminal" - so it is asked as well,
        # and can flag a person personsearch returned no FIR row for.
        checked = self._psrms_checkperson(cnic)
        if checked:
            raw["checkperson"] = checked

        if not rows:
            if checked and checked.get("flagged"):
                return _result("psrms", "success", f"PSRMS criminal check: {checked.get('message') or 'record flagged'}",
                               hit=True, raw=raw, data={"record_count": 0, "firs": [], "criminal_check": checked})
            return _no("psrms")
        firs = [{"person_type": r.get("person_type"), "person_name": r.get("person_name"),
                 "person_father": r.get("person_father"), "person_cnic": cnic13(r.get("person_cnic")),
                 "person_phone": mobile11(r.get("person_phone")), "person_address": r.get("person_address"),
                 "fir_no": r.get("fir_no"), "fir_year": r.get("fir_year"), "ps_id": "",
                 "fir_status": r.get("fir_status"),
                 # charges, under whichever name this PSRMS build uses for them
                 "offence": next((r.get(k) for k in ("offence", "fir_offence", "sections", "section",
                                                     "sections_of_law", "crime_head", "crime") if r.get(k)), None),
                 "ps_name": r.get("ps_name")} for r in rows]
        return _result("psrms", "success", f"{len(firs)} FIR record(s) found in PSRMS", hit=True, raw=raw,
                       data={"record_count": len(firs), "person_name": firs[0]["person_name"],
                             "person_cnic": firs[0]["person_cnic"], "person_phone": firs[0]["person_phone"], "firs": firs})

    def _psrms_checkperson(self, cnic: str | None) -> dict[str, Any] | None:
        """PSRMS ``checkperson``: a yes/no criminal flag for a CNIC (dashed)."""
        if not cnic:
            return None
        _code, body = self.http.send(Req("POST", CONF["psrms_check_url"],
                                         headers={"PSRMS-API-KEY": CONF["psrms_key"],
                                                  "Content-Type": "application/x-www-form-urlencoded"},
                                         data={"cnic": dashed_cnic(cnic)}))
        if not isinstance(body, dict):
            return None
        payload = body.get("data")
        flagged = bool(body.get("status")) and bool(_rows(payload) or _meaningful(payload if isinstance(payload, dict) else {}))
        return {"flagged": flagged, "message": body.get("message"), "body": body}

    def _watchlist(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        if not cnic:
            return _result("watchlist", "invalid_input", "CNIC required")
        _code, body = self.http.send(Req("POST", CONF["watchlist_url"],
                                        headers={"Accept": "application/json", "Content-Type": "application/json",
                                                 "x-api-key": CONF["watchlist_key"]},
                                        json={"cnic": cnic, "source": 1}))
        rows = _rows(body.get("data")) if isinstance(body, dict) and body.get("status") else []
        if not rows:
            return _no("watchlist")
        return _result("watchlist", "success", f"Record Found in watchlist ({len(rows)} records)", hit=True,
                       raw={"data": rows}, data={"matched": True})

    def _cfms(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        if not cnic:
            return _result("cfms", "invalid_input", "CNIC required")
        code, body = self.http.send(Req("GET", CONF["cfms_url"], headers={"Accept": "application/json"}, params={"CNIC": cnic}))
        if code == 404:
            return _no("cfms")
        rows = _rows(body.get("data")) if isinstance(body, dict) else (_rows(body) if isinstance(body, list) else [])
        if not rows:
            return _no("cfms")
        return _result("cfms", "success", f"Record found in CFMS ({len(rows)})", hit=True, raw={"data": rows},
                       data={"details": {k: v for k, v in rows[0].items() if not isinstance(v, (dict, list))}})

    # -- property / tenancy ---------------------------------------------------------

    # PRVS unified profile: one answer per person - their details (with email and
    # passport), every verification case and their role in it, the witnesses who vouched
    # for them, and the CRO record PRVS itself matched. Searchable by CNIC, mobile, email
    # or passport. The older per-case endpoints below remain the fallback.

    def _prvs_profile(self, query: dict[str, str]) -> tuple[str, dict[str, Any] | None]:
        """``("hit", data)``, ``("none", None)`` or ``("down", None)`` for one query."""
        try:
            code, body = self.http.send(Req("GET", f"{CONF['prvs_base']}/api/person/profile",
                                            headers={"api-key": CONF["prvs_key"], "Accept": "application/json"},
                                            params=query))
        except requests.RequestException:
            return "down", None
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict) and body.get("success") and isinstance(data.get("person_details"), dict) \
                and _meaningful(data["person_details"]):
            return "hit", {"query": query, **data}
        if code == 404 or (isinstance(body, dict) and body.get("success") is False and code < 500):
            return "none", None
        return ("none", None) if code == 200 else ("down", None)

    def _prvs_result(self, profiles: list[dict[str, Any]]) -> dict[str, Any]:
        seen, unique = set(), []
        for prof in profiles:
            ident = cnic13(prof["person_details"].get("cnic")) or prof["person_details"].get("mobile")
            if ident not in seen:
                seen.add(ident)
                unique.append(prof)
        pd = unique[0]["person_details"]
        cases = pd.get("cases") or []
        crim = sum(len(c.get("records") or []) for p_ in unique for c in p_.get("criminal_links") or [])
        wits = sum(len(p_.get("witnesses") or []) for p_ in unique)
        summary = (f"PRVS: {pd.get('name') or 'profile'} - {len(cases)} verification case(s), {wits} witness(es)"
                   + (f", {crim} CRO record(s) linked" if crim else ""))
        return _result("prvs", "success", summary, hit=True, raw={"profiles": unique},
                       data={"name": pd.get("name"), "police_station": pd.get("zone_name") or pd.get("district_name"),
                             "remarks": pd.get("address")})

    def lookup_email(self, email: str) -> dict[str, Any]:
        """PRVS by email - the one police system that can be searched by it. Used to
        turn an email-only search into a CNIC/mobile the other systems understand."""
        ctx = getattr(self.http, "set_context", None)
        if callable(ctx):
            ctx("prvs", email)
        state, prof = self._prvs_profile({"email": email})
        if state == "down":
            return _err("prvs", "PRVS profile search by email failed")
        return self._prvs_result([prof]) if prof else _no("prvs")

    def _prvs(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        if not (cnic or phone):
            return _result("prvs", "invalid_input", "CNIC or mobile required")
        profiles = []
        for query in ({"cnic": dashed_cnic(cnic)} if cnic else None, {"mobile": phone} if phone else None):
            if query:
                _state, prof = self._prvs_profile(query)
                if prof:
                    profiles.append(prof)
        if profiles:
            return self._prvs_result(profiles)
        # Nothing (or the unified endpoint is down): the per-case endpoints still find a
        # case filed under this number/CNIC by someone else - a landlord's registration.
        return self._prvs_cases(cnic, phone)

    def _prvs_cases(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        key = {"api-key": CONF["prvs_key"]}
        probes = self._both(
            cnic and Req("GET", f"{CONF['prvs_base']}/api/cases/by-cnic", headers=key,
                         params={"cnic": dashed_cnic(cnic)}),
            phone and Req("GET", f"{CONF['prvs_base']}/api/cases/by-mobile", headers=key,
                          params={"mobile": phone}))
        if probes is None:
            return _result("prvs", "invalid_input", "CNIC or mobile required")
        rows, seen = [], set()
        for _branch, _code, body in probes:
            if not (isinstance(body, dict) and body.get("success")):
                continue
            for r in _rows(body.get("data")):
                ident = str(r.get("case_id") or (r.get("cnic"), r.get("mobile"), r.get("name")))
                if ident not in seen:
                    seen.add(ident)
                    rows.append(r)
        if not rows:
            return _no("prvs")
        r = rows[0]
        return _result("prvs", "success", f"PRVS: {len(rows)} case(s)", hit=True,
                       raw={"data": [{**x, "photograph_url": x.get("photograph_url")} for x in rows]},
                       data={"name": r.get("name"), "police_station": r.get("district_or_zone"),
                             "record_reference": r.get("case_id"), "remarks": r.get("address")})

    def _old_tenant(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        url = f"{CONF['tenant_base']}/api/v1/tenants/old-db/search"
        headers = {"X-API-KEY": CONF["tenant_key"], "Accept": "application/json"}
        probes = self._both(cnic and Req("GET", url, headers=headers, params={"cnic": dashed_cnic(cnic)}),
                            phone and Req("GET", url, headers=headers, params={"mobile": phone}))
        if probes is None:
            return _result("old_tenant", "invalid_input", "CNIC or mobile required")
        raw, count = {}, 0
        for branch, _code, body in probes:
            if isinstance(body, dict) and body.get("success") and (rows := _rows(body.get("data"))):
                raw[branch] = body
                count += len(rows)
        if not raw:
            return _no("old_tenant")
        return _result("old_tenant", "success", f"Old Tenant: {count} record(s)", hit=True, raw=raw, data={})

    def _trust(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        url = f"{CONF['tenant_base']}/api/v1/user/comprehensive-info"
        headers = {"X-API-KEY": CONF["tenant_key"]}
        # comprehensive-info is slow: it aggregates several tables and can take ~40s.
        # A shorter timeout aborts a request that would have returned real data.
        t = TRUST_TIMEOUT
        probes = self._both(cnic and Req("GET", url, headers=headers, params={"cnic": dashed_cnic(cnic)}, timeout=t),
                            phone and Req("GET", url, headers=headers, params={"mobile": dashed_mobile(phone)}, timeout=t))
        if probes is None:
            return _result("trust", "invalid_input", "CNIC or mobile required")
        for _branch, _code, body in probes:
            if isinstance(body, dict) and body.get("success") and isinstance(body.get("data"), dict):
                return _result("trust", "success", "Record found in TRUST", hit=True, raw={"data": body["data"]},
                               data={"details": _scalar(body["data"].get("person_details", {}))})
        return _no("trust")

    # -- travel / employment / police -----------------------------------------------

    def _hotel_eye(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        headers = {"x-api-key": CONF["hotel_key"], "Content-Type": "application/x-www-form-urlencoded"}
        # by-mobile can return hundreds of stays and take ~90s; give it the slow timeout.
        probes = self._both(cnic and Req("POST", CONF["hotel_cnic_url"], headers=headers, data={"cnic": dashed_cnic(cnic)}, timeout=SLOW_TIMEOUT),
                            phone and Req("POST", CONF["hotel_mobile_url"], headers=headers, data={"cell_no": phone}, timeout=SLOW_TIMEOUT))
        if probes is None:
            return _result("hotel_eye", "invalid_input", "CNIC or mobile required")
        records: list[dict] = []
        raw: dict[str, Any] = {}
        seen: set[str] = set()
        for branch, _code, body in probes:
            raw[branch] = body
            if isinstance(body, dict) and str(body.get("status")).lower() == "success":
                for r in _rows(body.get("data")):
                    ident = str((r.get("hotel_name"), r.get("room_no"), r.get("check_in"), r.get("guest_cnic")))
                    if ident not in seen:
                        seen.add(ident)
                        records.append(r)
        if not records:
            return _no("hotel_eye")
        norm = [{"guest_name": r.get("guest_name"), "guest_cnic": r.get("guest_cnic"), "guest_cell": r.get("guest_cell"),
                 "hotel_name": r.get("hotel_name"), "district": r.get("hotel_district"), "room_no": r.get("room_no"),
                 "check_in": r.get("check_in"), "check_out": r.get("check_out"), "visit_purpose": r.get("visit_purpose")}
                for r in records]
        raw["records"] = norm
        return _result("hotel_eye", "success", f"{len(norm)} Hotel Eye record(s)", hit=True, raw=raw, data={})

    def _sbvs(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        base = CONF["sbvs_base"]
        for url in ([f"{base}/api/single-request-by-mobile/{phone}"] if phone else []) + \
                   ([f"{base}/api/single-request/{dashed_cnic(cnic)}"] if cnic else []):
            _code, body = self.http.send(Req("GET", url, headers={"X-API-KEY": CONF["sbvs_key"]}))
            if isinstance(body, dict) and body.get("success") and isinstance(body.get("data"), dict):
                det = body["data"].get("details", {})
                img = body["data"].get("image") or {}
                image = (img.get("url") if isinstance(img, dict) else None) or _b64_to_data_uri(img.get("base64") if isinstance(img, dict) else None)
                return _result("sbvs", "success", f"Record Found in sbvs ({det.get('full_name') or 'record'})", hit=True,
                               raw={"data": {"details": det, "image_ref": image}},
                               data={"name": det.get("full_name"), "father_name": det.get("father_name"),
                                     "cnic": cnic13(det.get("cnic")), "phone": mobile11(det.get("mobile")),
                                     "address": det.get("permanent_address") or det.get("address"),
                                     "passport": None, "entries": [{"organization_name": det.get("district_name"),
                                                                     "district": det.get("district_name")}]})
        return _no("sbvs")

    def _evs(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        # The mobile goes in the SAME "cnic" field (11 digits), so both are tried.
        values = [v for v in (cnic, phone) if v]
        if not values:
            return _result("evs", "invalid_input", "CNIC or mobile required")
        for value in values:
            out = self._wtower("evs", CONF["evs_url"], value)
            if out["hit"]:
                return out
        return _no("evs")

    def _hope(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        values = [v for v in (cnic, phone) if v]  # mobile reuses the "cnic" field
        if not values:
            return _result("hope", "invalid_input", "CNIC or mobile required")
        for value in values:
            emp = self._wtower("hope", CONF["hope_emp_url"], value)
            if emp["hit"]:
                return emp
        # Employer fallback: the person sits under data.employer (a dict, not a list),
        # with data.employees as their staff.
        employer, data = None, None
        for value in values:
            _code, body = self.http.send(Req("POST", CONF["hope_empr_url"],
                                             headers={"i_key": CONF["wtower_i_key"], "j_key": CONF["wtower_j_key"],
                                                      "api_token": CONF["wtower_api_token"],
                                                      "Content-Type": "application/x-www-form-urlencoded"},
                                             data={"cnic": value}))
            data = body.get("data") if isinstance(body, dict) else None
            candidate = data.get("employer") if isinstance(data, dict) else None
            if isinstance(candidate, dict) and _meaningful(candidate):
                employer = candidate
                break
        if employer is None:
            return _no("hope")
        employees = _rows(data.get("employees"))
        return _result("hope", "success", f"Employer record found in HOPE ({employer.get('name') or 'record'})",
                       hit=True,
                       raw={"data": {"employer": {**employer, "picture": _b64_to_data_uri(employer.get("picture"))},
                                     "employees": employees}},
                       data={"name": employer.get("name"), "cnic": cnic13(employer.get("cnic")),
                             "contact": mobile11(employer.get("contact")), "father_name": None,
                             "permanent_address": employer.get("org_address"), "designation": "Employer",
                             "organisation": employer.get("org_name")})

    def _wtower(self, system: str, url: str, value: str) -> dict[str, Any]:
        _code, body = self.http.send(Req("POST", url,
                                        headers={"i_key": CONF["wtower_i_key"], "j_key": CONF["wtower_j_key"],
                                                 "api_token": CONF["wtower_api_token"],
                                                 "Content-Type": "application/x-www-form-urlencoded"},
                                        data={"cnic": value}))
        payload = body.get("data") if isinstance(body, dict) else None
        rows = _rows(payload)
        if not rows:
            return _no(system)
        r = rows[0]
        org = r.get("comp_title") or r.get("org_name") or r.get("company_name")
        return _result(system, "success", f"Employee record found in {system_label(system)} ({r.get('name') or 'record'})",
                       hit=True, raw={"data": [{**x, "picture_new": _b64_to_data_uri(x.get("picture_new") or x.get("picture"))} for x in rows]},
                       data={"name": r.get("name"), "father_name": r.get("father_name"), "cnic": cnic13(r.get("cnic")),
                             "contact": mobile11(r.get("contact")), "other_contact": None,
                             "permanent_address": r.get("perm_address"), "designation": r.get("designation"),
                             "organisation": org})

    def _hrmis(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        headers = {"X-API-KEY": CONF["hrmis_key"], "Authorization": f"Basic {CONF['hrmis_basic']}",
                   "Content-Type": "application/x-www-form-urlencoded"}
        probes = self._both(cnic and Req("POST", CONF["hrmis_url"], headers=headers, data={"cnic": dashed_cnic(cnic)}),
                            phone and Req("POST", CONF["hrmis_url"], headers=headers, data={"mobile": dashed_mobile(phone)}))
        if probes is None:
            return _result("hrmis", "invalid_input", "CNIC or mobile required")
        rows, seen = [], set()
        for _branch, _code, body in probes:
            if isinstance(body, dict) and body.get("status"):
                for r in _rows(body.get("output")):
                    ident = str(r.get("ofc_cnic") or r.get("ofc_belt_no") or r)
                    if ident not in seen:
                        seen.add(ident)
                        rows.append(r)
        if not rows:
            return _no("hrmis")
        o = rows[0]
        return _result("hrmis", "success", f"Officer record found in HRMIS ({o.get('ofc_name') or 'record'})", hit=True,
                       raw={"output": [{**x, "picture_new": x.get("picture_new")} for x in rows]},
                       data={"officer_name": o.get("ofc_name"), "officer_cnic": cnic13(o.get("ofc_cnic")),
                             "officer_phone": mobile11(o.get("ofc_mobile")), "officer_belt_no": o.get("ofc_belt_no"),
                             "date_of_birth": o.get("ofc_dateofbirth"), "current_posting": o.get("current_posting"),
                             "police_station_name": o.get("current_posting"), "district": o.get("grd_name"),
                             "officer_address": o.get("ofc_address"), "rank": o.get("rnk_name")})

    # -- traffic --------------------------------------------------------------------

    def _dls_login(self) -> str | None:
        with self._dls_lock:
            if self._dls_token:
                return self._dls_token
            _code, body = self.http.send(Req("POST", CONF["dls_login_url"], headers={"Content-Type": "application/json"},
                                            json={"username": CONF["dls_user"], "password": CONF["dls_pass"]}))
            if isinstance(body, dict):
                d = body.get("data") if isinstance(body.get("data"), dict) else {}
                # Login returns {"data":{"accessToken": "..."}}; older shapes use token /
                # access_token, at the top level or under data.
                token = (body.get("token") or body.get("access_token")
                         or d.get("token") or d.get("access_token") or d.get("accessToken"))
                self._dls_token = str(token).strip() if token else None
            return self._dls_token

    def _dls(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        token = self._dls_login()
        if not token:
            return _err("dls", "DLS authentication failed")
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        probes = self._both(
            cnic and Req("POST", f"{CONF['dls_api']}/licenseDataWithImage/{dashed_cnic(cnic)}", headers=headers, json={}),
            phone and Req("POST", f"{CONF['dls_api']}/licenseDataWithImageByMobile/{dashed_mobile(phone)}", headers=headers, json={}))
        if probes is None:
            return _result("dls", "invalid_input", "CNIC or mobile required")
        # A hit is a list of licences. Empty comes back as {"message":"No records found"}
        # (a dict) or []; neither is a record.
        rows, seen = [], set()
        for _branch, _code, body in probes:
            payload = body.get("data") if isinstance(body, dict) else None
            for r in (_rows(payload) if isinstance(payload, list) else []):
                ident = str(r.get("license_no") or (r.get("cnic"), r.get("license_category")))
                if ident not in seen:
                    seen.add(ident)
                    rows.append(r)
        if not rows:
            return _no("dls")
        r = rows[0]
        return _result("dls", "success", f"{len(rows)} driving license record(s) in DLS", hit=True,
                       raw={"data": [{**x, "applicant_image": _b64_to_data_uri(x.get("applicant_image"))} for x in rows]},
                       data={"firstname": r.get("firstname"), "lastname": r.get("lastname"),
                             "phone": mobile11(r.get("mobile")), "cnic": cnic13(r.get("cnic")), "address": r.get("address"),
                             "licenses": [{"license_no": x.get("license_no"), "category": x.get("license_category"),
                                           "license_type": x.get("license_type"), "issued_office": x.get("issued_office"),
                                           "issued_date": x.get("issued_date"), "expiry_date": x.get("expiry_date"),
                                           "status": x.get("status")} for x in rows]})

    def _tracs(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        if not cnic:
            return _result("tracs", "invalid_input", "CNIC required")
        _code, body = self.http.send(Req("GET", CONF["tracs_url"],
                                        headers={"Accept": "application/json", "Authorization": f"Basic {CONF['tracs_basic']}"},
                                        params={"cnic": cnic}))
        payload = body.get("data") if isinstance(body, dict) else None
        challans = (_rows(payload.get("challans")) + _rows(payload.get("oldChallans"))) if isinstance(payload, dict) else _rows(payload)
        if not challans:
            return _no("tracs")
        return _result("tracs", "success", f"{len(challans)} challan(s) in TRACS", hit=True, raw=body if isinstance(body, dict) else {"data": challans},
                       data={"details": {"challans": len(challans)}})

    def _excise(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        if not cnic:
            return _result("excise", "invalid_input", "CNIC required")
        vehicles: list[dict] = []
        for db in ("4w", "2w"):
            _code, body = self.http.send(Req("GET", CONF["excise_url"], headers={"Authorization": f"Basic {CONF['excise_basic']}"},
                                            params={"DB": db, "cnic": cnic}))
            if isinstance(body, dict) and body.get("statusCode") == 0:
                vehicles += _rows(body.get("data"))
        if not vehicles:
            return _no("excise")
        return _result("excise", "success", f"{len(vehicles)} vehicle(s) in Excise", hit=True, raw={"data": vehicles},
                       data={"vehicles": vehicles})

    def _avlc(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        headers = {"x-api-key": CONF["avlc_key"]}
        if CONF["avlc_bearer"]:
            headers["Authorization"] = f"Bearer {CONF['avlc_bearer']}"
        probes = self._both(cnic and Req("GET", CONF["avlc_url"], headers=headers, params={"CNIC": cnic}),
                            phone and Req("GET", CONF["avlc_url"], headers=headers, params={"Mobile": dashed_mobile(phone)}))
        if probes is None:
            return _result("avlc", "invalid_input", "CNIC or mobile required")
        rows, seen = [], set()
        for _branch, _code, body in probes:
            if isinstance(body, dict) and body.get("status"):
                for r in _rows(body.get("data")):
                    ident = str(r.get("RegNo") or r.get("ChassisNo") or r.get("EngineNo") or r)
                    if ident not in seen:
                        seen.add(ident)
                        rows.append(r)
        real = [r for r in rows if any(r.get(k) for k in ("CompCode", "RegNo", "EngineNo", "ChassisNo", "Make", "Crime", "PoliceStation"))]
        if not real:
            return _no("avlc")
        return _result("avlc", "success", f"{len(real)} AVLC record(s)", hit=True, raw={"data": real}, data={"vehicles": real})

    # -- complaint ------------------------------------------------------------------

    def _igp_cms(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        headers = {"X-API-KEY": CONF["igp_key"], "Content-Type": "application/x-www-form-urlencoded"}
        probes = self._both(cnic and Req("POST", CONF["igp_url"], headers=headers, data={"cnic": dashed_cnic(cnic)}),
                            phone and Req("POST", CONF["igp_url"], headers=headers, data={"contact": dashed_mobile(phone)}))
        if probes is None:
            return _result("igp_cms", "invalid_input", "CNIC or mobile required")
        complaints, seen = [], set()
        for _branch, code, body in probes:
            if code == 404 or not (isinstance(body, dict) and body.get("success")):
                continue
            for c in _rows(body.get("complaints")):
                ident = str(c.get("complaint_no") or c.get("id") or c)
                if ident not in seen:
                    seen.add(ident)
                    complaints.append(c)
        if not complaints:
            return _no("igp_cms")
        return _result("igp_cms", "success", f"{len(complaints)} IGP CMS complaint(s)", hit=True,
                       raw={"complaints": complaints}, data={})

    def _pfc(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        headers = {"x-api-key": CONF["pfc_key"], "Content-Type": "application/x-www-form-urlencoded"}
        probes = self._both(
            cnic and Req("POST", CONF["pfc_url"], headers=headers, data={"complainant_cnic": dashed_cnic(cnic)}),
            phone and Req("POST", CONF["pfc_url"], headers=headers, data={"complainant_contact_number": dashed_mobile(phone)}))
        if probes is None:
            return _result("pfc", "invalid_input", "CNIC or mobile required")
        rows, seen = [], set()
        for _branch, _code, body in probes:
            for r in (_rows(body.get("data")) if isinstance(body, dict) else []):
                ident = str(r.get("complaint_no") or r.get("id") or r)
                if ident not in seen:
                    seen.add(ident)
                    rows.append(r)
        if not rows:
            return _no("pfc")
        return _result("pfc", "success", f"{len(rows)} PFC complaint(s)", hit=True, raw={"data": rows}, data={})

    def _milap(self, cnic: str | None, phone: str | None) -> dict[str, Any]:
        headers = {"X-API-KEY": CONF["milap_key"]}
        probes = self._both(
            cnic and Req("GET", CONF["milap_url"], headers=headers,
                         params={"reporting_cnic": dashed_cnic(cnic), "reporting_contact": "null"}),
            phone and Req("GET", CONF["milap_url"], headers=headers,
                          params={"reporting_cnic": "null", "reporting_contact": phone}))
        if probes is None:
            return _result("milap", "invalid_input", "CNIC or mobile required")
        rows, seen = [], set()
        for _branch, _code, body in probes:
            if isinstance(body, dict) and str(body.get("status")).lower() == "success":
                for r in _rows(body.get("data")):
                    ident = str(r.get("id") or r.get("record_no") or r)
                    if ident not in seen:
                        seen.add(ident)
                        rows.append(r)
        if not rows:
            return _no("milap")
        return _result("milap", "success", f"{len(rows)} MILAP record(s)", hit=True, raw={"data": rows}, data={})


def _scalar(d: dict) -> dict[str, str]:
    return {k: str(v) for k, v in d.items() if not isinstance(v, (dict, list)) and v not in (None, "")}


def build_ems_backend(http: EmsHttp | None = None) -> EmsBackend:
    if http is None:
        from datetime import date

        from sherlocks.settings import PROJECT_ROOT

        # Every outbound EMS call is written here: URL, headers, body, response. This is
        # what to check when a system returns no_record but a manual curl works.
        log_dir = PROJECT_ROOT / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            log_dir = None
        today = date.today().isoformat()  # noqa: DTZ011
        log_path = str(log_dir / f"ems_api_{today}.txt") if log_dir else None
        # Failed calls only (transport error or HTTP >= 400) - the file to open when a
        # system shows FAILED: it has the system, the input, the URL and the error body.
        failures_path = str(log_dir / f"ems_failures_{today}.txt") if log_dir else None
        http = EmsHttp(log_path=log_path, failures_path=failures_path)
    return EmsBackend(http=http)
