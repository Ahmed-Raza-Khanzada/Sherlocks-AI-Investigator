"""Portal sign-in.

One operator account, credentials read from ``.env`` (never the repo or the YAML). A
successful sign-in returns a signed, expiring session token; the portal sends it back on
every request. The token is signed with HMAC-SHA256, so the server keeps no session
store and a tampered or expired token simply fails to verify.

The existing ``X-API-Key`` still works for machine-to-machine callers - sign-in is for
people, the key is for scripts.

**Host-issued tokens.** When Sherlocks runs inside another application (the Laravel
portal), that application owns the accounts. It signs tokens itself with the shared
``SHERLOCKS_AUTH_SECRET``, in exactly the format ``issue`` produces, and hands them to
the browser; Sherlocks verifies them and records the user named in them. No password
is set on this side in that mode - see ``docs/INTEGRATION.md``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from sherlocks.settings import Settings

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Sign-in refused, or a session token that cannot be trusted."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SessionAuth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        cfg = settings.auth
        self.enabled = bool(cfg.enabled and (cfg.password or cfg.password_sha256))
        self.username = (cfg.username or "").strip()
        self.ttl = max(1, cfg.session_hours) * 3600
        # A per-boot random secret is fine and safer than a default one: it only means
        # everyone signs in again after a restart.
        self._secret = (cfg.secret or secrets.token_urlsafe(32)).encode()
        # A configured secret means someone else may be issuing tokens (the host app), so
        # tokens are checked even when there is no local sign-in.
        self.verifies_tokens = self.enabled or bool(cfg.enabled and cfg.secret)
        if cfg.enabled and not self.enabled and not cfg.secret:
            logger.warning(
                "Portal sign-in is enabled but no password is set - set SHERLOCKS_AUTH_PASSWORD "
                "in .env. The portal is OPEN until you do."
            )

    # -- credentials ----------------------------------------------------------------

    def check_password(self, username: str, password: str) -> bool:
        cfg = self.settings.auth
        ok_user = hmac.compare_digest((username or "").strip().lower(), self.username.lower())
        if cfg.password:
            ok_pass = hmac.compare_digest(password or "", cfg.password)
        elif cfg.password_sha256:
            given = hashlib.sha256((password or "").encode()).hexdigest()
            ok_pass = hmac.compare_digest(given.lower(), cfg.password_sha256.strip().lower())
        else:
            ok_pass = False
        # Both are compared before returning, so a wrong username and a wrong password
        # take the same time.
        return ok_user and ok_pass

    # -- tokens ---------------------------------------------------------------------

    def _sign(self, payload: bytes) -> str:
        return _b64(hmac.new(self._secret, payload, hashlib.sha256).digest())

    def issue(self, username: str) -> dict[str, object]:
        expires = int(time.time()) + self.ttl
        payload = json.dumps({"u": username, "exp": expires}, separators=(",", ":")).encode()
        body = _b64(payload)
        return {"token": f"{body}.{self._sign(payload)}", "expires_at": expires,
                "username": username, "expires_in": self.ttl}

    def verify(self, token: str | None) -> str:
        """Return the username the token belongs to, or raise ``AuthError``."""
        if not token or "." not in token:
            raise AuthError("No session token")
        body, signature = token.rsplit(".", 1)
        try:
            payload = _unb64(body)
        except (ValueError, TypeError) as exc:
            raise AuthError("Malformed session token") from exc
        if not hmac.compare_digest(signature, self._sign(payload)):
            raise AuthError("Session token failed verification")
        try:
            claims = json.loads(payload)
        except ValueError as exc:
            raise AuthError("Malformed session token") from exc
        if int(claims.get("exp", 0)) < time.time():
            raise AuthError("Session expired - sign in again")
        return str(claims.get("u") or "")

    def login(self, username: str, password: str) -> dict[str, object]:
        if not self.enabled:
            raise AuthError("Sign-in is not configured on this deployment")
        if not self.check_password(username, password):
            logger.warning("Failed portal sign-in for username %r", username)
            raise AuthError("Wrong username or password")
        logger.info("Portal sign-in: %s", username)
        return self.issue(self.username)
