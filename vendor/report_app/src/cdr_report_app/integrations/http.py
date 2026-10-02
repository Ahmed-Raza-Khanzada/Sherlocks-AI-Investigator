"""HTTP transport helpers for provider adapters."""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

import requests

from cdr_report_app.utils.request_logger import log_external_api_call

if TYPE_CHECKING:
    from cdr_report_app.integrations.shield_auth import ShieldAuthClient

logger = logging.getLogger(__name__)


class HttpClient:
    def __init__(
        self,
        verify_ssl: bool = False,
        timeout: int = 10,
        shield: ShieldAuthClient | None = None,
        shield_base: str | None = None,
        shield_cookie: str | None = None,
    ) -> None:
        self.session = requests.Session()
        self.session.verify = verify_ssl
        self.session.trust_env = False
        self.timeout = timeout
        self._shield = shield
        self._shield_base = shield_base.rstrip("/") if shield_base else None
        self._shield_cookie = shield_cookie or None
        if not verify_ssl:
            requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass

    def __del__(self) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    #  Shield helpers                                                     #
    # ------------------------------------------------------------------ #

    def _is_shield_url(self, url: str) -> bool:
        return bool(self._shield and self._shield_base and url.startswith(self._shield_base))

    def _with_shield_auth(self, url: str, kwargs: dict, token: str | None = None) -> dict:
        """Return a copy of kwargs with Authorization + Cookie injected for shield URLs.

        SPAG handles all downstream provider credentials internally. Caller only
        needs Bearer (from login) and a shared session Cookie.
        """
        if not self._is_shield_url(url):
            return dict(kwargs)
        tok = token or (self._shield.get_token() if self._shield else None)
        headers = dict(kwargs.get("headers") or {})
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        if self._shield_cookie:
            existing = headers.get("Cookie")
            headers["Cookie"] = f"{existing}; {self._shield_cookie}" if existing else self._shield_cookie
        # SPAG mislabels uncompressed responses with Content-Encoding: gzip.
        # Force plain transport so `requests` doesn't try to decompress.
        headers["Accept-Encoding"] = "identity"
        return {**kwargs, "headers": headers}

    # ------------------------------------------------------------------ #
    #  Request methods                                                    #
    # ------------------------------------------------------------------ #

    def request(self, method: str, url: str | None, **kwargs) -> dict[str, Any] | list[Any]:
        if not url:
            logger.warning("HTTP request skipped | method=%s reason=url_not_configured", method)
            log_external_api_call(method=method, url=url, request_kwargs=kwargs, skipped_reason="url_not_configured")
            return {"error": "URL not configured"}

        timeout = kwargs.pop("timeout", self.timeout)
        log_kwargs = dict(kwargs)  # snapshot for audit log \u2014 no token

        call_kwargs = self._with_shield_auth(url, kwargs)
        logger.info("HTTP request started | method=%s url=%s timeout=%s", method, url, timeout)

        started = time.perf_counter()
        try:
            response = self.session.request(method, url, timeout=timeout, **call_kwargs)
        except Exception as exc:
            logger.exception("HTTP request failed | method=%s url=%s error=%s", method, url, exc)
            log_external_api_call(
                method=method, url=url, timeout=timeout, request_kwargs=log_kwargs,
                duration_ms=(time.perf_counter() - started) * 1000, error=repr(exc),
            )
            return {"error": str(exc)}

        # Transparent 401 retry for shield-protected URLs
        if response.status_code == 401 and self._is_shield_url(url):
            logger.info("Shield 401 \u2014 refreshing token and retrying | url=%s", url)
            new_token = self._shield.refresh_token() if self._shield else None  # type: ignore[union-attr]
            if new_token:
                call_kwargs = self._with_shield_auth(url, kwargs, token=new_token)
                try:
                    response = self.session.request(method, url, timeout=timeout, **call_kwargs)
                except Exception as exc:
                    logger.exception("HTTP retry failed | method=%s url=%s error=%s", method, url, exc)
                    log_external_api_call(
                        method=method, url=url, timeout=timeout, request_kwargs=log_kwargs,
                        duration_ms=(time.perf_counter() - started) * 1000, error=repr(exc),
                    )
                    return {"error": str(exc)}

        logger.info("HTTP response received | method=%s url=%s status=%s", method, url, response.status_code)
        response_text = response.text
        log_external_api_call(
            method=method, url=url, timeout=timeout, request_kwargs=log_kwargs,
            response_status=response.status_code, response_headers=dict(response.headers),
            response_body=response_text, duration_ms=(time.perf_counter() - started) * 1000,
        )

        parsed: Any
        try:
            text_clean = response_text.lstrip("\ufeff")
            parsed = json.loads(text_clean)
        except ValueError:
            logger.info("HTTP response was non-JSON | method=%s url=%s content_type=%s", method, url, response.headers.get("Content-Type", ""))
            parsed = {
                "status_code": response.status_code,
                "content_type": response.headers.get("Content-Type", ""),
                "text_response": response_text,
            }

        # 404 = downstream signaled "record not found"; let provider fall
        # through to its no_record path (no "error" key, so is_transport_error
        # returns False).
        if response.status_code == 404:
            if isinstance(parsed, dict):
                return {**parsed, "not_found": True, "status_code": 404}
            return {"not_found": True, "status_code": 404, "raw": parsed}

        # 4xx/5xx (except 404) = real failure. Surface as transport error so
        # providers don't misread empty/HTML body as "no record found".
        if response.status_code >= 400:
            err_msg = f"HTTP {response.status_code}"
            if isinstance(parsed, dict):
                detail = parsed.get("message") or parsed.get("error") or parsed.get("text_response")
                if detail:
                    err_msg = f"{err_msg}: {str(detail)[:200]}"
                return {**parsed, "error": err_msg, "status_code": response.status_code}
            return {"error": err_msg, "status_code": response.status_code, "raw": parsed}

        return parsed

    def request_bytes(self, method: str, url: str | None, **kwargs) -> tuple[dict[str, Any], bytes | None]:
        if not url:
            logger.warning("HTTP bytes request skipped | method=%s reason=url_not_configured", method)
            log_external_api_call(method=method, url=url, request_kwargs=kwargs, skipped_reason="url_not_configured")
            return {"error": "URL not configured"}, None

        timeout = kwargs.pop("timeout", self.timeout)
        log_kwargs = dict(kwargs)

        call_kwargs = self._with_shield_auth(url, kwargs)
        logger.info("HTTP bytes request started | method=%s url=%s timeout=%s", method, url, timeout)
        started = time.perf_counter()
        try:
            response = self.session.request(method, url, timeout=timeout, **call_kwargs)
        except Exception as exc:
            logger.exception("HTTP bytes request failed | method=%s url=%s error=%s", method, url, exc)
            log_external_api_call(
                method=method, url=url, timeout=timeout, request_kwargs=log_kwargs,
                duration_ms=(time.perf_counter() - started) * 1000, error=repr(exc),
            )
            return {"error": str(exc)}, None

        # Transparent 401 retry for shield-protected URLs
        if response.status_code == 401 and self._is_shield_url(url):
            logger.info("Shield 401 (bytes) \u2014 refreshing token and retrying | url=%s", url)
            new_token = self._shield.refresh_token() if self._shield else None  # type: ignore[union-attr]
            if new_token:
                call_kwargs = self._with_shield_auth(url, kwargs, token=new_token)
                try:
                    response = self.session.request(method, url, timeout=timeout, **call_kwargs)
                except Exception as exc:
                    logger.exception("HTTP bytes retry failed | method=%s url=%s error=%s", method, url, exc)
                    log_external_api_call(
                        method=method, url=url, timeout=timeout, request_kwargs=log_kwargs,
                        duration_ms=(time.perf_counter() - started) * 1000, error=repr(exc),
                    )
                    return {"error": str(exc)}, None

        meta: dict[str, Any] = {
            "status_code": response.status_code,
            "content_type": response.headers.get("Content-Type", ""),
        }
        logger.info("HTTP bytes response received | method=%s url=%s status=%s bytes=%s", method, url, response.status_code, len(response.content or b""))
        log_external_api_call(
            method=method, url=url, timeout=timeout, request_kwargs=log_kwargs,
            response_status=response.status_code, response_headers=dict(response.headers),
            response_bytes=len(response.content or b""),
            duration_ms=(time.perf_counter() - started) * 1000,
        )

        # 404 = downstream "no record"; signal via not_found, no error key.
        if response.status_code == 404:
            meta["not_found"] = True
            return meta, None

        # 4xx/5xx (except 404) = real failure. Mark for is_transport_error.
        if response.status_code >= 400:
            meta["error"] = f"HTTP {response.status_code}"
            return meta, None

        return meta, response.content
