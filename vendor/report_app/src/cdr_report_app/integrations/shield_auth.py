"""Shield (SPAG) authentication client for Sindh Police APIs."""

from __future__ import annotations

import logging
import threading
from typing import Any

from cdr_report_app.integrations.http import HttpClient

logger = logging.getLogger(__name__)


class ShieldAuthClient:
    """Thread-safe Bearer token manager for SPAG/Shield-protected services.

    Lazily logs in on first call to get_token(). On any downstream 401,
    call refresh_token() to force a re-login and get a fresh token.
    """

    def __init__(self, login_url: str, email: str, password: str, http: HttpClient) -> None:
        self._login_url = login_url
        self._email = email
        self._password = password
        self._http = http
        self._token: str | None = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._login_url and self._email and self._password)

    def get_token(self) -> str | None:
        with self._lock:
            if not self._token:
                self._token = self._login()
            return self._token

    def refresh_token(self) -> str | None:
        with self._lock:
            logger.info("Shield auth: forcing token refresh")
            self._token = self._login()
            return self._token

    def _login(self) -> str | None:
        raw: Any = self._http.request(
            "POST",
            self._login_url,
            headers={"Content-Type": "application/json"},
            json={"email": self._email, "password": self._password},
            timeout=30,
        )
        if isinstance(raw, dict):
            token = str(raw.get("access_token") or "").strip()
            if token:
                logger.info("Shield auth login succeeded")
                return token
        logger.warning(
            "Shield auth login failed | response_keys=%s",
            list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__,
        )
        return None

    @staticmethod
    def is_auth_failure(raw: Any) -> bool:
        """Detect 401 / token-expired responses from SPAG endpoints."""
        if not isinstance(raw, dict):
            return False
        if raw.get("status_code") == 401:
            return True
        combined = " ".join(
            str(raw.get(k) or "") for k in ("detail", "message", "error", "text_response")
        ).lower()
        return (
            "not authenticated" in combined
            or "could not validate" in combined
            or "unauthorized" in combined
            or ("token" in combined and ("expired" in combined or "invalid" in combined))
        )
