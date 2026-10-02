"""Where the records come from.

Both backends drive **cdr_report_app's own provider adapters**, so request formats,
auth (Shield bearer tokens, cookies, DLS login), identifier formatting and response
parsing are exactly what production CDR reports use. Nothing here re-implements a
provider.

* ``live``  - the adapters over their real HTTP client, configured from Report_App's
  ``.env``. Every call is a logged query against a police system.
* ``demo``  - the same adapters over :class:`~sherlocks.linkgraph.demo_data.DemoHttp`,
  which answers from a small synthetic dataset. The full pipeline runs, nothing leaves
  the machine. Use it to learn the portal and in tests.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Protocol

from sherlocks.linkgraph.systems import DEFAULT_SYSTEMS, SYSTEMS
from sherlocks.render._reportlib import _ensure_on_path
from sherlocks.settings import PROJECT_ROOT, Settings

logger = logging.getLogger(__name__)


class LookupBackend(Protocol):
    name: str

    def systems_status(self) -> list[dict[str, Any]]: ...

    def lookup(self, system: str, cnic: str | None, phone: str | None) -> dict[str, Any]: ...

    def fir_roster(self, fir_no: str, fir_year: str, ps_id: str) -> dict[str, Any]: ...

    def caller_id(self, phone: str) -> dict[str, Any]: ...


def _result(system: str, status: str, summary: str, *, hit: bool = False, raw: Any = None,
            data: Any = None, errors: list[str] | None = None) -> dict[str, Any]:
    return {"provider": system, "hit": hit, "status": status, "summary": summary, "raw": raw,
            "data": data, "errors": errors or ([summary] if status == "error" else [])}


class ReportAppBackend:
    def __init__(self, cdr_settings: Any, *, name: str = "live", http: Any = None) -> None:
        from cdr_report_app.integrations.providers import UnifiedLookupService

        self.name = name
        self.cdr_settings = cdr_settings
        self.service = UnifiedLookupService(cdr_settings)
        if http is not None:
            self.service.http = http
            for provider in self.service.providers.values():
                provider.http = http
        self.http = self.service.http
        self._caller: Any = None
        self._caller_lock = threading.Lock()

    # -- introspection -------------------------------------------------------------

    def systems_status(self) -> list[dict[str, Any]]:
        from cdr_report_app.settings import provider_readiness

        readiness = {row["provider"]: row for row in provider_readiness(self.cdr_settings)}
        rows: list[dict[str, Any]] = []
        for key in DEFAULT_SYSTEMS:
            info, ready = SYSTEMS[key], readiness.get(key, {})
            rows.append({
                "system": key, "label": info.label, "category": info.category,
                "description": info.description, "needs": info.needs,
                "enabled": bool(ready.get("enabled", True)), "ready": bool(ready.get("ready")),
                "missing": ready.get("missing", []),
            })
        psrms = self.cdr_settings.providers.psrms
        rows.append({"system": "fir_roster", "label": SYSTEMS["fir_roster"].label, "category": "criminal",
                     "description": SYSTEMS["fir_roster"].description, "needs": "either", "enabled": True,
                     "ready": bool(psrms.extra.get("fir_report_url")),
                     "missing": [] if psrms.extra.get("fir_report_url") else ["PSRMS_FIR_PATH / PSRMS_FIR_REPORT_URL"]})
        cid = self.cdr_settings.caller_id
        rows.append({"system": "caller_id", "label": "Caller ID", "category": "osint",
                     "description": SYSTEMS["caller_id"].description, "needs": "phone", "enabled": cid.enabled,
                     "ready": cid.ready, "missing": [] if cid.ready else ["CALLER_ID_ENABLED / _BASE_URL / _API_KEYS"]})
        return rows

    # -- queries -------------------------------------------------------------------

    def lookup(self, system: str, cnic: str | None, phone: str | None) -> dict[str, Any]:
        from cdr_report_app.domain.provider_models import SearchSubject

        provider = self.service.providers.get(system)
        if provider is None:
            return _result(system, "error", f"Unknown system: {system}")
        config = getattr(self.cdr_settings.providers, system, None)
        if config is not None and not config.enabled:
            return _result(system, "disabled", "Disabled in Report_App configuration")
        try:
            result = provider.lookup(SearchSubject(cnic=cnic, mobile=phone))
        except Exception as exc:  # an adapter bug must cost one system, not the run
            logger.exception("Provider %s crashed", system)
            return _result(system, "error", f"{type(exc).__name__}: {exc}")
        return result.model_dump(mode="json")

    def fir_roster(self, fir_no: str, fir_year: str, ps_id: str) -> dict[str, Any]:
        """Fetch one FIR report and parse who it names.

        Same request as cdr_report_app's ``PsrmsFirAttachmentProvider``, minus the
        headless-Chrome PDF render - only the parsed tables are needed here.
        """
        from cdr_report_app.integrations.helpers import is_transport_error, parse_psrms_fir_html
        from cdr_report_app.integrations.providers import PsrmsFirAttachmentProvider

        config = self.cdr_settings.providers.psrms
        url = config.extra.get("fir_report_url")
        if not url:
            return _result("fir_roster", "error", "PSRMS FIR report URL not configured")
        data = {"fir_no": str(fir_no), "fir_year": str(fir_year), "ps_id": str(ps_id)}
        if config.api_key:
            data["PSRMS-API-KEY"] = config.api_key
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        last: Any = None
        for cookie in dict.fromkeys([config.extra.get("fir_cookie"), PsrmsFirAttachmentProvider.default_fir_cookie]):
            if not cookie:
                continue
            raw = self.http.request("POST", url, headers={**headers, "Cookie": cookie}, data=data)
            last = raw
            if isinstance(raw, dict) and "text_response" in raw:
                html = str(raw.get("text_response") or "").lstrip("﻿")
                if "not found" in html.lower():
                    return _result("fir_roster", "no_record", "FIR report not found")
                if any(tag in html.lower() for tag in ("<table", "<html", "<div")):
                    parsed = parse_psrms_fir_html(html)
                    keep = {k: parsed.get(k) for k in (
                        "title", "header_fields", "fir_sections", "sections_of_law", "main_narrative",
                        "nominated_suspects", "witnesses", "investigating_officers", "case_positions",
                        "unknown_suspects",
                    )}
                    keep["fir_label"] = f"FIR {fir_no}/{fir_year}"
                    return _result("fir_roster", "success", f"FIR {fir_no}/{fir_year} report parsed", hit=True, raw=keep)
        if is_transport_error(last):
            return _result("fir_roster", "error", "FIR report fetch failed", raw=last)
        return _result("fir_roster", "no_record", "FIR report not available")

    def caller_id(self, phone: str) -> dict[str, Any]:
        from cdr_report_app.integrations.caller_id import CallerIdClient, to_intl_number

        config = self.cdr_settings.caller_id
        if not config.ready:
            return _result("caller_id", "error", "Caller ID not configured")
        number = to_intl_number(phone)
        if not number:
            return _result("caller_id", "invalid_input", "Not a Pakistani mobile")
        # The API is rate limited per key; one client, one caller at a time.
        with self._caller_lock:
            if self._caller is None:
                self._caller = CallerIdClient(self.http, config.base_url, config.api_keys,
                                              delay_seconds=config.delay_seconds, timeout_seconds=config.timeout_seconds)
            outcome = self._caller.lookup(number)
        if not outcome.ok:
            return _result("caller_id", "error", outcome.error or "Caller ID lookup failed")
        payload = outcome.result or {}
        return _result("caller_id", "success" if outcome.hit else "no_record", payload.get("summary", ""),
                       hit=outcome.hit, raw=payload.get("data"), data=payload.get("data"))


def report_app_available(settings: Settings) -> bool:
    """Is cdr_report_app importable? The demo and Shield-live backends need it; the EMS
    backend does not. On an offline server without it, only EMS works."""
    import importlib.util

    _ensure_on_path(settings.app.report_app_src)
    try:
        return importlib.util.find_spec("cdr_report_app") is not None
    except (ImportError, ValueError):
        return False


def build_backend(settings: Settings, kind: str | None = None) -> LookupBackend:
    kind = kind or settings.linkgraph.backend
    _ensure_on_path(settings.app.report_app_src)

    # EMS is fully self-contained. Demo and Shield-live drive cdr_report_app's providers,
    # so if it is not installed those two cannot run - say so clearly instead of a bare
    # ModuleNotFoundError crashing the run.
    if kind in ("demo", "live") and not report_app_available(settings):
        raise RuntimeError(
            f"The '{kind}' backend needs cdr_report_app, which is not installed on this "
            "server. Use the 'All systems (live)' EMS backend, which runs standalone."
        )

    if kind == "demo":
        from sherlocks.linkgraph.demo_data import build_demo_backend

        return build_demo_backend()

    if kind == "ems":
        from sherlocks.linkgraph.ems import build_ems_backend

        logger.info("EMS backend: native police/government endpoints")
        return build_ems_backend()

    # cdr_report_app writes an audit line for every outbound call. Keep that trail
    # beside Sherlocks' own logs rather than wherever the process happened to start.
    log_dir = Path(settings.app.log_dir)
    os.environ.setdefault("CDR_REQUEST_LOG_DIR", str(log_dir if log_dir.is_absolute() else PROJECT_ROOT / log_dir))

    from cdr_report_app.settings import load_settings as load_cdr_settings

    env_file = Path(settings.linkgraph.report_app_env) if settings.linkgraph.report_app_env else None
    cdr_settings = load_cdr_settings(env_file=env_file)
    logger.info("Live backend: Report_App settings from %s", cdr_settings.env_file)
    return ReportAppBackend(cdr_settings, name="live")
