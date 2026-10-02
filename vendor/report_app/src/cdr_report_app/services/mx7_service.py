from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import re
import time
from typing import Any

from cdr_report_app.domain.provider_models import ProviderResult, SearchSubject
from cdr_report_app.integrations.caller_id import CallerIdClient, to_intl_number
from cdr_report_app.integrations.providers import (
    IDENTITY_ENRICHMENT_PROVIDERS,
    UnifiedLookupService,
)
from cdr_report_app.settings import Settings
from cdr_report_app.utils.phones import normalize_cnic, normalize_mobile

logger = logging.getLogger(__name__)

MAX_RELATED = 12


def _digits(value: object) -> str:
    return re.sub(r"\D", "", str(value or ""))


def split_identifier(raw: str | None) -> tuple[str | None, str | None]:
    digits = _digits(raw)
    if not digits:
        return None, None
    if len(digits) == 13:
        return digits, None
    if len(digits) in (10, 11, 12):
        return None, normalize_mobile(digits)
    return None, None


def _dump(result: ProviderResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "hit": result.hit,
        "summary": result.summary,
        "errors": result.errors,
        "data": result.data.model_dump(mode="json") if result.data is not None else None,
        "raw": result.raw,
    }


def _identity_from(results: dict[str, ProviderResult]) -> dict[str, Any]:
    for name in IDENTITY_ENRICHMENT_PROVIDERS:
        result = results.get(name)
        if not result or not result.hit or result.data is None:
            continue
        data = result.data.model_dump(mode="json")
        if data.get("cnic") or data.get("name"):
            return {
                "source": name,
                "name": data.get("name"),
                "cnic": data.get("cnic"),
                "mobile": data.get("mobile"),
                "address": data.get("address"),
            }
    return {"source": None, "name": None, "cnic": None, "mobile": None, "address": None}


def _sims_from(results: dict[str, ProviderResult]) -> list[dict[str, Any]]:
    result = results.get("simsdb")
    if not result or not result.hit or result.data is None:
        return []
    return result.data.model_dump(mode="json").get("sims") or []


def _caller_id(settings: Settings, service: UnifiedLookupService, numbers: list[str]) -> dict[str, Any]:
    cfg = settings.caller_id
    if not cfg.ready:
        return {}
    client = CallerIdClient(
        service.http,
        cfg.base_url,
        cfg.api_keys,
        delay_seconds=cfg.delay_seconds,
        timeout_seconds=cfg.timeout_seconds,
    )
    out: dict[str, Any] = {}
    for number in numbers:
        intl = to_intl_number(number)
        if not intl or number in out:
            continue
        outcome = client.lookup(intl)
        out[number] = {
            "ok": outcome.ok,
            "hit": outcome.hit,
            "error": outcome.error,
            "errors": outcome.errors,
            "data": outcome.result,
        }
    return out


def collect_mx7(
    settings: Settings,
    *,
    identifier: str | None = None,
    cnic: str | None = None,
    mobile: str | None = None,
    imei: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    providers: list[str] | None = None,
    related: bool = True,
    deep_related: bool = False,
    include_caller_id: bool = True,
    include_raw: bool = True,
) -> dict[str, Any]:
    started = time.perf_counter()

    guess_cnic, guess_mobile = split_identifier(identifier)
    cnic = normalize_cnic(cnic) or guess_cnic
    mobile = normalize_mobile(mobile) or guess_mobile
    imei = _digits(imei) or None

    if not cnic and not mobile and not imei:
        raise ValueError("Provide a CNIC, a mobile number, or an IMEI")

    service = UnifiedLookupService(settings)
    try:
        names = [name for name in (providers or list(service.providers)) if name in service.providers]
        subject = SearchSubject(
            cnic=cnic,
            mobile=mobile,
            imei=imei,
            latitude=latitude,
            longitude=longitude,
        )
        logger.info("mx7 started | cnic=%s mobile=%s imei=%s providers=%d", cnic, mobile, imei, len(names))
        results = service.lookup_all(subject, provider_names=names)

        identity = _identity_from(results)
        resolved_cnic = cnic or identity.get("cnic")

        if resolved_cnic and not cnic:
            missed = [
                name
                for name, result in results.items()
                if result.status in {"invalid_input", "no_record"} and name not in IDENTITY_ENRICHMENT_PROVIDERS
            ]
            if missed:
                second = service.lookup_all(
                    SearchSubject(
                        cnic=resolved_cnic,
                        mobile=mobile,
                        imei=imei,
                        latitude=latitude,
                        longitude=longitude,
                    ),
                    provider_names=missed,
                )
                for name, result in second.items():
                    if result.hit or result.status != "invalid_input":
                        results[name] = result

        sims = _sims_from(results)
        other_numbers = [
            str(sim.get("number"))
            for sim in sims
            if sim.get("number") and normalize_mobile(sim.get("number")) != mobile
        ][:MAX_RELATED]

        related_out: dict[str, Any] = {}
        if related and other_numbers:
            fanout = names if deep_related else list(IDENTITY_ENRICHMENT_PROVIDERS)
            fanout = [name for name in fanout if name in service.providers]

            def _one(number: str) -> tuple[str, dict[str, Any]]:
                sub_results = service.lookup_all(SearchSubject(mobile=number), provider_names=fanout)
                return number, {
                    "identity": _identity_from(sub_results),
                    "providers": {name: _dump(result) for name, result in sub_results.items()},
                }

            with ThreadPoolExecutor(max_workers=min(4, len(other_numbers))) as executor:
                futures = {executor.submit(_one, number): number for number in other_numbers}
                for future in as_completed(futures):
                    number = futures[future]
                    try:
                        key, value = future.result()
                        related_out[key] = value
                    except Exception as exc:
                        logger.exception("mx7 related failed | number=%s error=%s", number, exc)
                        related_out[number] = {"error": str(exc)}

        caller_id: dict[str, Any] = {}
        if include_caller_id:
            targets = [mobile] if mobile else []
            if deep_related:
                targets += other_numbers
            caller_id = _caller_id(settings, service, [number for number in targets if number])

        provider_payload = {name: _dump(result) for name, result in results.items()}
        if not include_raw:
            for entry in provider_payload.values():
                entry.pop("raw", None)
            for entry in related_out.values():
                for sub in (entry.get("providers") or {}).values():
                    sub.pop("raw", None)

        hits = sorted(name for name, result in results.items() if result.hit)
        payload = {
            "input": {
                "identifier": identifier,
                "cnic": cnic,
                "mobile": mobile,
                "imei": imei,
                "latitude": latitude,
                "longitude": longitude,
            },
            "resolved": {
                "cnic": resolved_cnic,
                "name": identity.get("name"),
                "mobile": mobile or identity.get("mobile"),
                "address": identity.get("address"),
                "identity_source": identity.get("source"),
            },
            "sims": sims,
            "other_numbers": other_numbers,
            "hits": hits,
            "counts": {
                "providers_queried": len(results),
                "providers_hit": len(hits),
                "sims": len(sims),
                "related": len(related_out),
            },
            "providers": provider_payload,
            "related": related_out,
            "caller_id": caller_id,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        logger.info("mx7 completed | hits=%s elapsed_ms=%s", hits, payload["elapsed_ms"])
        return payload
    finally:
        service._lookup_cache.clear()
        try:
            service.http.session.close()
        except Exception:
            pass
