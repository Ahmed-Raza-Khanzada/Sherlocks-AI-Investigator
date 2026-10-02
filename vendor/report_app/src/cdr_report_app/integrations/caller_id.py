"""Caller ID lookup client.

Truecaller-style name + social-footprint lookup over a rate-limited 3rd-party API.
The API is rate limited, so the client round-robins across multiple API keys
(key 1 -> 2 -> 3 -> 1 ...) and paces requests with a small delay between calls.
On a rate-limit / transport error for a single number it rotates to the next key
and retries; if every key fails the lookup is reported as *failed* (ok=False) so
callers can distinguish "rate limited / unavailable" from "looked up, no record".
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from cdr_report_app.integrations.http import HttpClient

logger = logging.getLogger(__name__)


def to_intl_number(value: object) -> str | None:
    """Normalize a phone number to the 92XXXXXXXXXX form the API expects.

    Returns ``None`` for values that are clearly not a Pakistani mobile number
    (short codes, junk) so we don't waste rate-limited quota on them.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("0"):
        digits = "92" + digits[1:]
    elif len(digits) == 10 and digits.startswith("3"):
        digits = "92" + digits
    elif len(digits) == 10 and digits.startswith("2"):
        # PTCL landline without leading 0 (e.g. 21XXXXXXXX for Karachi)
        digits = "92" + digits
    elif len(digits) == 12 and digits.startswith("92"):
        pass
    else:
        return None
    # Pakistani mobile 92 + 3XXXXXXXXX; PTCL landline 92 + 2XXXXXXXXX
    if len(digits) != 12 or not (digits.startswith("923") or digits.startswith("922")):
        return None
    return digits


@dataclass
class CallerIdLookup:
    """Outcome of a single caller-id lookup.

    ``ok`` is True when the API actually answered (even if it found nothing);
    False means every key failed (rate-limited / network / server error).
    """

    ok: bool
    result: dict[str, Any] | None = None   # normalized provider-style dict (only when ok and hit)
    hit: bool = False
    error: str | None = None
    errors: list[str] = field(default_factory=list)


class CallerIdClient:
    """Round-robin, rate-limit-aware client for the caller-id API."""

    def __init__(
        self,
        http: HttpClient,
        base_url: str,
        api_keys: list[str],
        *,
        delay_seconds: float = 1.5,
        timeout_seconds: int = 20,
    ) -> None:
        self.http = http
        self.base_url = (base_url or "").rstrip("/")
        self.api_keys = [str(key).strip() for key in (api_keys or []) if str(key).strip()]
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.timeout_seconds = int(timeout_seconds)
        self._index = 0

    @property
    def usable(self) -> bool:
        return bool(self.base_url and self.api_keys)

    def _next_key(self) -> str:
        key = self.api_keys[self._index % len(self.api_keys)]
        self._index += 1
        return key

    def lookup(self, intl_number: str) -> CallerIdLookup:
        """Look up one number, rotating keys on rate-limit / failure."""
        if not self.usable:
            return CallerIdLookup(ok=False, error="Caller ID not configured")

        last_error: str | None = None
        # one attempt per key at most — once every key is rate-limited we give up
        for _ in range(len(self.api_keys)):
            key = self._next_key()
            url = f"{self.base_url}/api/v1/key/{key}/search/single"
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            resp = self.http.request(
                "POST",
                url,
                headers=headers,
                json={"phoneNumber": intl_number},
                timeout=self.timeout_seconds,
            )
            # Pace requests regardless of outcome so we stay under the rate limit.
            if self.delay_seconds:
                time.sleep(self.delay_seconds)

            if isinstance(resp, dict) and resp.get("error"):
                last_error = str(resp.get("error"))
                status = resp.get("status_code")
                logger.info(
                    "Caller ID attempt failed | number=%s status=%s error=%s — rotating key",
                    intl_number, status, last_error,
                )
                continue

            normalized = _normalize_caller_id_response(resp, intl_number)
            return CallerIdLookup(ok=True, result=normalized, hit=bool(normalized.get("hit")))

        logger.warning("Caller ID lookup failed for %s after trying all keys | error=%s", intl_number, last_error)
        return CallerIdLookup(ok=False, error=last_error, errors=[last_error] if last_error else [])


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _is_found(value: object) -> bool:
    text = _clean_text(value).upper()
    return bool(text) and text != "NOT FOUND"


def _normalize_caller_id_response(resp: Any, intl_number: str) -> dict[str, Any]:
    """Shape the raw API payload into the provider-result dict the renderer reads."""
    result_block = resp.get("result") if isinstance(resp, dict) else None
    accounts_raw = result_block.get("accounts") if isinstance(result_block, dict) else None
    facebook_raw = result_block.get("facebook") if isinstance(result_block, dict) else None

    accounts: list[dict[str, Any]] = []
    if isinstance(accounts_raw, list):
        for item in accounts_raw:
            if not isinstance(item, dict):
                continue
            name = _clean_text(item.get("name"))
            if not name:
                continue
            accounts.append(
                {
                    "name": name[:120],
                    "spam": bool(item.get("suspicious_spam")),
                    "type": _clean_text(item.get("type")),
                }
            )

    spam = any(acc["spam"] for acc in accounts)

    # Primary name = first (most-relevant) account, skipping auto-saved
    # "Contact 03..." address-book echoes. Spam is kept as a separate flag —
    # a spam-reported number can still carry the person's real name.
    primary_name: str | None = None
    for acc in accounts:
        if acc["name"].lower().startswith("contact "):
            continue
        primary_name = acc["name"]
        break
    if not primary_name and accounts:
        primary_name = accounts[0]["name"]

    facebook_id = None
    facebook_link = None
    if isinstance(facebook_raw, dict):
        if _is_found(facebook_raw.get("FB_ID")):
            facebook_id = _clean_text(facebook_raw.get("FB_ID"))
        if _is_found(facebook_raw.get("FB_Link")):
            facebook_link = _clean_text(facebook_raw.get("FB_Link"))

    hit = bool(accounts or facebook_id or facebook_link)

    summary_bits: list[str] = []
    if primary_name:
        summary_bits.append(primary_name)
    extra_alias = max(0, len(accounts) - 1)
    if extra_alias:
        summary_bits.append(f"+{extra_alias} more name(s)")
    if spam:
        summary_bits.append("Reported as Spam")
    if facebook_id or facebook_link:
        summary_bits.append("Facebook profile found")
    summary = " | ".join(summary_bits) if summary_bits else "No caller ID record found"

    return {
        "provider": "caller_id",
        "hit": hit,
        "status": "found" if hit else "no_record",
        "summary": summary[:200],
        "data": {
            "number": intl_number,
            "primary_name": primary_name,
            "spam": spam,
            "aliases": [acc["name"] for acc in accounts],
            "accounts": accounts,
            "facebook_id": facebook_id,
            "facebook_link": facebook_link,
        },
    }
