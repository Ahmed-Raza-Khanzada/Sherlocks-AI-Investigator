"""Concrete provider adapters."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
from typing import Any

from cdr_report_app.domain.provider_models import (
    AttachmentArtifact,
    CroFirRecord,
    CroRecord,
    DlsLicenseRecord,
    DlsRecord,
    EmploymentDatabaseRecord,
    GenericDatabaseRecord,
    HrmisRecord,
    ImeiRecord,
    NearestPoliceStation,
    ProviderResult,
    PrvsRecord,
    PsrmsFirReference,
    PsrmsRecord,
    SbvsRecord,
    SbvsEntry,
    SearchSubject,
    SimsDbRecord,
    SimsDbSimEntry,
    SubscriberRecord,
    WatchlistRecord,
    OldTenantRecord,
    OldTenantTenantInfo,
)
from cdr_report_app.integrations.base import AttachmentProvider, PersonLookupProvider
from cdr_report_app.integrations.shield_auth import ShieldAuthClient
from cdr_report_app.integrations.helpers import (
    dashed_cnic,
    dashed_mobile,
    error_result,
    extract_scalar_details,
    extract_base64_pdf,
    html_attachment,
    invalid_input_result,
    is_transport_error,
    parse_psrms_fir_html,
    mobile_10,
    no_record_result,
    not_configured_result,
    render_psrms_fir_html_to_pdf,
)
from cdr_report_app.integrations.http import HttpClient
from cdr_report_app.settings import ProviderConfig, Settings
from cdr_report_app.utils.phones import normalize_cnic, normalize_mobile

logger = logging.getLogger(__name__)


PERSON_LOOKUP_PROVIDERS = [
    "simsdb",
    "subscriber",
    "prvs",
    "cro",
    "psrms",
    "watchlist",
    "cfms",
    "sbvs",
    "pfc",
    "hrmis",
    "igp_cms",
    "evs",
    "hope",
    "dls",
    "tracs",
    "old_tenant",
    "trust",
    "milap",
]

IDENTITY_ENRICHMENT_PROVIDERS = ["simsdb", "subscriber"]

REPORT_LOOKUP_PROVIDERS = [
    "simsdb",
    "subscriber",
    "watchlist",
    "prvs",
    "hrmis",
    "evs",
    "hope",
    "dls",
    "hotel_eye",
    "tracs",
    "old_tenant",
    "cro",
    "psrms",
    "sbvs",
]


# Names the upstream returns when NADRA gave it nothing usable.
_SIMSDB_PLACEHOLDER_NAMES = {
    "",
    "no",
    "new cust",
    "data not recieved from nadra",
    "data not received from nadra",
}


class SimsDbProvider(PersonLookupProvider):
    """simsdatabases.com number_check: one mobile -> owner identity + every SIM on that CNIC.

    Primary identity source; the telecom subscriber DB is the fallback.
    """

    provider_name = "simsdb"

    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        self.config = config
        self.http = http_client

    def lookup(self, subject: SearchSubject) -> ProviderResult[SimsDbRecord]:
        missing = [
            name
            for name, value in {
                "SIMSDB_API_URL": self.config.base_url,
                "SIMSDB_APPKEY": self.config.api_key,
            }.items()
            if not value
        ]
        if missing:
            return not_configured_result(self.provider_name, missing)

        number = mobile_10(subject.mobile)
        if not number:
            return invalid_input_result(self.provider_name, "Valid mobile required")

        raw = self.http.request(
            "POST",
            self.config.base_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": self.config.extra.get("user_agent") or "Dart/3.1 (dartio)",
            },
            data={"number": number, "appkey": self.config.api_key},
            timeout=self.http.timeout,
        )

        if is_transport_error(raw):
            return error_result(self.provider_name, "SIMs database lookup failed", raw=raw)
        if not isinstance(raw, dict):
            return no_record_result(self.provider_name)

        sims = self._extract_sims(raw)
        if not sims:
            return no_record_result(self.provider_name)

        mobile = normalize_mobile(subject.mobile)
        own = next((entry for entry in sims if entry.number == mobile), None)
        # The queried SIM's own row is often a NADRA blank while its siblings on the
        # same CNIC carry the name, so identity is resolved across the whole set.
        named = own if (own and own.name) else next((entry for entry in sims if entry.name), None)
        cnic = (own.cnic if own else None) or next((entry.cnic for entry in sims if entry.cnic), None)
        address = (own.address if own else None) or (named.address if named else None)

        record = SimsDbRecord(
            name=named.name if named else None,
            cnic=cnic,
            mobile=mobile,
            address=address,
            sim_count=len(sims),
            sims=sims,
        )
        return ProviderResult(
            provider=self.provider_name,
            hit=True,
            status="success",
            summary=f"SIMs database record found (Name: {record.name or 'Unknown'}, SIMs: {record.sim_count})",
            raw=raw,
            data=record,
        )

    def _extract_sims(self, raw: dict[str, Any]) -> list[SimsDbSimEntry]:
        """Records arrive as top-level numeric string keys ("0", "1", ...).

        The same SIM repeats across those keys, sometimes named and sometimes as a
        NADRA blank, so rows are merged per number keeping the first real value.
        """
        merged: dict[str, SimsDbSimEntry] = {}
        for key in sorted((k for k in raw if str(k).isdigit()), key=lambda k: int(k)):
            item = raw.get(key)
            if not isinstance(item, dict):
                continue
            number = normalize_mobile(item.get("number"))
            cnic = normalize_cnic(item.get("cnic"))
            if not number and not cnic:
                continue
            entry = merged.setdefault(number or cnic or key, SimsDbSimEntry(number=number))
            entry.cnic = entry.cnic or cnic
            entry.name = entry.name or self._clean_name(item.get("name"))
            entry.address = entry.address or self._clean_address(item.get("address"))
        return list(merged.values())

    @staticmethod
    def _clean_name(value: Any) -> str | None:
        text = str(value or "").strip()
        return None if text.lower() in _SIMSDB_PLACEHOLDER_NAMES else text

    @staticmethod
    def _clean_address(value: Any) -> str | None:
        text = str(value or "").strip()
        return None if text.lower() in {"", "no", "-"} else text


class SubscriberProvider(PersonLookupProvider):
    provider_name = "subscriber"

    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        self.config = config
        self.http = http_client

    def lookup(self, subject: SearchSubject) -> ProviderResult[SubscriberRecord]:
        missing = [
            name
            for name, value in {
                "SUBSCRIBER_API_KEY": self.config.api_key,
                "SUBSCRIBER_COOKIE": self.config.extra.get("cookie"),
                "SUBSCRIBER_MOBILE_URL/SUBSCRIBER_CNIC_URL": self.config.extra.get("mobile_url") or self.config.extra.get("cnic_url"),
            }.items()
            if not value
        ]
        if missing:
            return not_configured_result(self.provider_name, missing)

        cnic = normalize_cnic(subject.cnic)
        mobile = normalize_mobile(subject.mobile)
        if not cnic and not mobile:
            return invalid_input_result(self.provider_name, "Valid CNIC or mobile required")

        headers = {
            "PSRMS-API-KEY": self.config.api_key or "",
            "Cookie": self.config.extra.get("cookie", ""),
            "User-Agent": "Chrome/91.0.4472.124 Safari/537.36",
            "Content-Type": "application/json",
        }

        raw: dict[str, Any] = {}
        branch_requests: list[tuple[str, str | None, dict[str, Any]]] = []
        if cnic:
            branch_requests.append(
                (
                    "by_cnic",
                    self.config.extra.get("cnic_url"),
                    {"headers": headers, "json": {"otpArray": [{"cnic": cnic}]}, "timeout": self.http.timeout},
                )
            )
        if mobile:
            branch_requests.append(
                (
                    "by_mobile",
                    self.config.extra.get("mobile_url"),
                    {"headers": headers, "json": {"otpArray": [{"phone": mobile}]}, "timeout": self.http.timeout},
                )
            )

        if branch_requests:
            with ThreadPoolExecutor(max_workers=min(2, len(branch_requests))) as executor:
                future_map = {
                    executor.submit(self.http.request, "POST", url, **request_kwargs): branch_name
                    for branch_name, url, request_kwargs in branch_requests
                }
                for future in as_completed(future_map):
                    raw[future_map[future]] = future.result()

        branch_errors = [name for name, item in raw.items() if is_transport_error(item)]

        payload = None
        for key in ("by_mobile", "by_cnic"):
            source = raw.get(key)
            if isinstance(source, dict):
                current = source.get("Data", {}).get("SubscriberList", [])
                if isinstance(current, list) and current:
                    payload = current
                    break

        if not payload:
            if raw and len(branch_errors) == len(raw):
                return error_result(self.provider_name, "Subscriber lookup failed", raw=raw)
            return no_record_result(self.provider_name)

        first = payload[0]
        record = SubscriberRecord(
            name=first.get("Name"),
            cnic=normalize_cnic(first.get("Cnic")),
            mobile=normalize_mobile(first.get("Phone") or first.get("MSISDN") or first.get("Mobile")) or mobile,
            activation_date=first.get("ActivationDate"),
            address=first.get("Address"),
        )
        return ProviderResult(
            provider=self.provider_name,
            hit=True,
            status="partial" if branch_errors else "success",
            summary=f"Telecom record found (Name: {record.name or 'Unknown'})",
            raw=raw,
            data=record,
            errors=["One or more subscriber lookup branches failed"] if branch_errors else [],
        )


class PrvsProvider(PersonLookupProvider):
    provider_name = "prvs"

    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        self.config = config
        self.http = http_client

    def lookup(self, subject: SearchSubject) -> ProviderResult[PrvsRecord]:
        missing = [
            name
            for name, value in {
                "PRVS_API_KEY": self.config.api_key,
                "PRVS_MOBILE_URL/PRVS_CNIC_URL": self.config.extra.get("mobile_url") or self.config.extra.get("cnic_url"),
            }.items()
            if not value
        ]
        if missing:
            return not_configured_result(self.provider_name, missing)

        cnic = dashed_cnic(subject.cnic)
        mobile = dashed_mobile(subject.mobile)
        if not cnic and not mobile:
            return invalid_input_result(self.provider_name, "Valid CNIC or mobile required")

        headers = {
            "Content-Type": "application/json",
            "api-key": self.config.api_key or "",
        }

        raw: dict[str, Any] = {}
        if cnic:
            raw["by_cnic"] = self.http.request(
                "POST",
                self.config.extra.get("cnic_url"),
                headers=headers,
                json={
                    "email": "safe@sindhpolice.gov.pk",
                    "password": self.config.extra.get("cnic_password", ""),
                    "cnic": cnic,
                },
            )
        if mobile:
            raw["by_mobile"] = self.http.request(
                "POST",
                self.config.extra.get("mobile_url"),
                headers=headers,
                json={
                    "email": "iconnect_mobile@sindhpolice.gov.pk",
                    "password": self.config.extra.get("mobile_password", ""),
                    "mobile": mobile,
                },
            )

        if any(is_transport_error(item) for item in raw.values()):
            return error_result(self.provider_name, "PRVS lookup failed", raw=raw)

        data = None
        for key in ("by_mobile", "by_cnic"):
            source = raw.get(key)
            if isinstance(source, dict):
                if isinstance(source.get("data"), list) and source.get("data"):
                    data = source["data"][0]
                    break
                if "police_station" in source or "name" in source:
                    data = source
                    break

        if not data:
            return no_record_result(self.provider_name)

        record = PrvsRecord(
            name=data.get("name"),
            police_station=data.get("police_station"),
            record_reference=data.get("id") or data.get("record_id"),
            remarks=data.get("remarks"),
        )
        return ProviderResult(
            provider=self.provider_name,
            hit=True,
            status="success",
            summary=f"Record found at PS {record.police_station or 'Unknown'}",
            raw=raw,
            data=record,
        )


class CroProvider(PersonLookupProvider):
    provider_name = "cro"

    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        self.config = config
        self.http = http_client

    def lookup(self, subject: SearchSubject) -> ProviderResult[CroRecord]:
        missing = [
            name
            for name, value in {
                "CRO_API_URL": self.config.base_url,
                "CRO_API_KEY": self.config.api_key,
            }.items()
            if not value
        ]
        if missing:
            return not_configured_result(self.provider_name, missing)

        cnic = normalize_cnic(subject.cnic)
        if not cnic or len(cnic) != 13:
            return invalid_input_result(self.provider_name, "Valid CNIC required")

        raw = self.http.request(
            "GET",
            self.config.base_url,
            headers={"x-api-key": self.config.api_key or ""},
            params={"cnic": cnic},
            timeout=60,
        )

        if is_transport_error(raw):
            return error_result(self.provider_name, "CRO lookup failed", raw=raw)

        data = raw["data"] if isinstance(raw, dict) and isinstance(raw.get("data"), list) else raw if isinstance(raw, list) else []
        if not data:
            return no_record_result(self.provider_name)

        first = data[0]
        fir_map: dict[tuple[str, str, str], CroFirRecord] = {}
        for cro_item in data:
            for fir in cro_item.get("FIRList", []) or []:
                key = (
                    str(fir.get("fir_no", "")),
                    str(fir.get("fir_year", "")),
                    str(fir.get("ps_desc", "")),
                )
                offence = str(fir.get("fir_offence", "")).strip()
                if key not in fir_map:
                    fir_map[key] = CroFirRecord(
                        fir_no=fir.get("fir_no"),
                        fir_year=fir.get("fir_year"),
                        police_station=fir.get("ps_desc"),
                        offence=offence or None,
                        status=fir.get("status_desc"),
                    )
                elif offence:
                    existing = fir_map[key]
                    merged = ", ".join(sorted({part for part in [existing.offence, offence] if part}))
                    existing.offence = merged

        record = CroRecord(
            cro_no=first.get("cro_no"),
            name=first.get("cro_full_name"),
            father_name=first.get("cro_father_name"),
            age=str(first.get("cro_age", "")) if first.get("cro_age") is not None else None,
            category=first.get("category_desc"),
            district=first.get("record_district"),
            fir_count=len(fir_map),
            firs=list(fir_map.values()),
        )
        return ProviderResult(
            provider=self.provider_name,
            hit=True,
            status="success",
            summary=f"CRO #{record.cro_no or ''} - {record.fir_count} FIR(s) registered".strip(),
            raw=raw,
            data=record,
        )


class PsrmsProvider(PersonLookupProvider):
    provider_name = "psrms"
    default_personsearch_cookie = "d1796422_8a25e425=bikg3b7tacrbas4b0cfjlb7vo40ca2e5"

    def __init__(self, config: ProviderConfig, http_client: HttpClient) -> None:
        self.config = config
        self.http = http_client

    def lookup(self, subject: SearchSubject) -> ProviderResult[PsrmsRecord]:
        if not self.config.base_url:
            return not_configured_result(self.provider_name, ["PSRMS_PERSONSEARCH_URL / SHIELD_BASE_URL + SHIELD_PSRMS_PERSONSEARCH_PATH"])

        cnic = dashed_cnic(subject.cnic)
        mobile = dashed_mobile(subject.mobile)
        if not cnic and not mobile:
            return invalid_input_result(self.provider_name, "Valid CNIC or mobile required")

        search_payloads: list[tuple[str, dict[str, str]]] = []
        if mobile:
            data: dict[str, str] = {"page": "1", "page_size": "10", "phone": mobile}
            if self.config.api_key:
                data["PSRMS-API-KEY"] = self.config.api_key
            search_payloads.append(("by_mobile", data))
        if cnic:
            data = {"page": "1", "page_size": "10", "cnic": cnic}
            if self.config.api_key:
                data["PSRMS-API-KEY"] = self.config.api_key
            search_payloads.append(("by_cnic", data))

        cookies = [
            self.config.extra.get("personsearch_cookie"),
            self.default_personsearch_cookie,
        ]
        raw_results: dict[str, Any] = {}
        merged_records: list[PsrmsFirReference] = []
        seen_keys: set[tuple[str, str, str]] = set()

        for source_name, payload in search_payloads:
            last_raw: Any = {}
            for cookie in cookies:
                headers: dict[str, str] = {
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                }
                if cookie:
                    headers["Cookie"] = cookie
                raw = self.http.request("POST", self.config.base_url, headers=headers, data=payload)
                last_raw = raw
                if isinstance(raw, dict) and raw.get("status") is True:
                    raw_results[source_name] = raw
                    parsed = self._parse_success(raw)
                    if parsed.hit and parsed.data:
                        for fir in parsed.data.firs:
                            key = (
                                str(fir.fir_no or "").strip(),
                                str(fir.fir_year or "").strip(),
                                str(fir.ps_id or "").strip(),
                            )
                            if key not in seen_keys:
                                seen_keys.add(key)
                                merged_records.append(fir)
                    break
                if not cookie:
                    break  # no point retrying without a cookie
            else:
                raw_results[source_name] = last_raw

        return self._build_result(merged_records, raw_results)

    # ------------------------------------------------------------------ #

    def _build_result(
        self,
        merged_records: list[PsrmsFirReference],
        raw_results: dict[str, Any],
    ) -> ProviderResult[PsrmsRecord]:
        if merged_records:
            first = merged_records[0]
            record = PsrmsRecord(
                record_count=len(merged_records),
                person_name=first.person_name,
                person_cnic=first.person_cnic,
                person_phone=first.person_phone,
                firs=merged_records,
            )

            suspects = witnesses = complainants = 0
            for fir in merged_records:
                ptype = str(fir.person_type or "").upper().strip()
                if ptype == "WIT":
                    suspects += 1
                elif ptype == "SUS":
                    witnesses += 1
                elif ptype == "FIR":
                    complainants += 1

            roles_text = []
            if suspects:
                roles_text.append(f"{suspects} as suspect")
            if witnesses:
                roles_text.append(f"{witnesses} as witness")
            if complainants:
                roles_text.append(f"{complainants} as complainant")

            summary_desc = ", ".join(roles_text)
            if summary_desc:
                summary_desc = f" ({summary_desc})"

            return ProviderResult(
                provider=self.provider_name,
                hit=True,
                status="success",
                summary=f"{record.record_count} FIR record(s) found in PSRMS{summary_desc}",
                raw=raw_results,
                data=record,
            )

        if any(is_transport_error(item) for item in raw_results.values()):
            return error_result(self.provider_name, "PSRMS lookup failed", raw=raw_results)
        return no_record_result(self.provider_name)

    def _parse_success(self, raw: dict[str, Any]) -> ProviderResult[PsrmsRecord]:
        records: list[PsrmsFirReference] = []
        seen: set[tuple[str, str, str]] = set()
        for rec in raw.get("data", []) or []:
            fir_no = str(rec.get("fir_no", "")).strip()
            fir_year = str(rec.get("fir_year", "")).strip()
            ps_id = str(rec.get("ps_tbl_id") or rec.get("ps_id") or "").strip()
            if not fir_no or not fir_year or not ps_id:
                continue
            key = (fir_no, fir_year, ps_id)
            if key in seen:
                continue
            seen.add(key)
            records.append(
                PsrmsFirReference(
                    fir_no=fir_no,
                    fir_year=fir_year,
                    ps_id=ps_id,
                    fir_status=str(rec.get("fir_status", "")).strip() or None,
                    person_name=rec.get("person_name"),
                    person_father=rec.get("person_father"),
                    person_cnic=rec.get("person_cnic"),
                    person_phone=rec.get("person_phone"),
                    person_address=rec.get("person_address"),
                    person_type=rec.get("person_type"),
                )
            )


        if not records:
            return no_record_result(self.provider_name)

        first = records[0]
        record = PsrmsRecord(
            record_count=len(records),
            person_name=first.person_name,
            person_cnic=first.person_cnic,
            person_phone=first.person_phone,
            firs=records,
        )
        return ProviderResult(
            provider=self.provider_name,
            hit=True,
            status="success",
            summary=f"{record.record_count} FIR record(s) found in PSRMS",
            raw=raw,
            data=record,
        )


class WatchlistProvider(PersonLookupProvider):
    provider_name = "watchlist"

    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        self.config = config
        self.http = http_client

    def lookup(self, subject: SearchSubject) -> ProviderResult[WatchlistRecord]:
        missing = [
            name
            for name, value in {
                "WATCHLIST_API_URL": self.config.base_url,
                "WATCHLIST_API_KEY": self.config.api_key,
            }.items()
            if not value
        ]
        if missing:
            return not_configured_result(self.provider_name, missing)

        cnic = normalize_cnic(subject.cnic)
        if not cnic or len(cnic) != 13:
            return invalid_input_result(self.provider_name, "Valid CNIC required")

        raw = self.http.request(
            "POST",
            self.config.base_url,
            headers={
                "x-api-key": self.config.api_key or "",
                "Content-Type": "application/json",
            },
            json={"cnic": cnic, "source": 1},
        )
        if is_transport_error(raw):
            return error_result(self.provider_name, "Watchlist lookup failed", raw=raw)

        matched = bool(isinstance(raw, dict) and raw.get("status") is True and raw.get("data"))
        if not matched:
            return no_record_result(self.provider_name)
        
        if isinstance(raw, dict):
            raw["_meta"] = {"used_cnic": cnic}

        data_list = raw.get("data", []) if isinstance(raw, dict) else []
        count = len(data_list) if isinstance(data_list, list) else 0

        record = WatchlistRecord(matched=True, source="watchlist", remarks="Present in watchlist")
        return ProviderResult(
            provider=self.provider_name,
            hit=True,
            status="success",
            summary=f"Record Found in watchlist ({count} records)",
            raw=raw,
            data=record,
        )


class NearestPsProvider(PersonLookupProvider):
    provider_name = "nearest_ps"

    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        self.config = config
        self.http = http_client

    def lookup(self, subject: SearchSubject) -> ProviderResult[NearestPoliceStation]:
        if not self.config.base_url:
            return not_configured_result(self.provider_name, ["NEAREST_PS_API_URL"])

        if subject.latitude is None or subject.longitude is None:
            return invalid_input_result(self.provider_name, "Latitude and longitude are required")

        raw = self.http.request(
            "POST",
            self.config.base_url,
            json={
                "latitude": float(subject.latitude),
                "longitude": float(subject.longitude),
            },
        )
        if is_transport_error(raw):
            return error_result(self.provider_name, "Nearest police station lookup failed", raw=raw)

        payload: Any = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), (dict, list)) else raw
        if isinstance(payload, list) and payload:
            payload = payload[0]
        if not isinstance(payload, dict) or not payload:
            return no_record_result(self.provider_name)

        station = NearestPoliceStation(
            name=payload.get("name") or payload.get("police_station") or payload.get("station_name"),
            district=payload.get("district") or payload.get("district_name") or payload.get("city"),
            distance_km=_coerce_float(
                payload.get("distance_km") or payload.get("distance") or payload.get("distanceInKm")
            ),
            latitude=_coerce_float(payload.get("latitude") or payload.get("lat")),
            longitude=_coerce_float(payload.get("longitude") or payload.get("lng") or payload.get("lon")),
        )
        return ProviderResult(
            provider=self.provider_name,
            hit=True,
            status="success",
            summary=f"Nearest police station: {station.name or 'Unknown'}",
            raw=raw,
            data=station,
        )


class ImeiProvider(PersonLookupProvider):
    provider_name = "imei"

    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        self.config = config
        self.http = http_client

    def lookup(self, subject: SearchSubject) -> ProviderResult[ImeiRecord]:
        if not self.config.base_url:
            return not_configured_result(self.provider_name, ["IMEI_API_URL"])

        imei = "".join(ch for ch in str(subject.imei or "") if ch.isdigit())
        if len(imei) < 14:
            return invalid_input_result(self.provider_name, "Valid IMEI required")

        # Build list of IMEI candidates to try:
        #   - 14-digit: append digits 0-9 → try all 10
        #   - 15-digit ending in 0: replace last digit with 0-9 → try all 10
        #   - 15-digit not ending in 0: try as-is only
        if len(imei) == 14:
            candidates = [imei + str(d) for d in range(10)]
        elif len(imei) == 15 and imei[-1] == "0":
            candidates = [imei[:14] + str(d) for d in range(10)]
        else:
            candidates = [imei[:15]]  # trim anything beyond 15 digits, use as-is

        browser_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://alpha.imeicheck.com/",
        }

        for candidate in candidates:
            raw = self.http.request(
                "GET",
                self.config.base_url,
                params={"imei": candidate, "format": "json"},
                headers=browser_headers,
                timeout=30,
            )
            if is_transport_error(raw):
                continue

            # Cloudflare or other non-JSON responses wrapped by HttpClient
            if isinstance(raw, dict) and "text_response" in raw:
                continue

            if not isinstance(raw, dict) or not raw:
                continue

            # API returns nested object with brand/name/model
            obj = raw.get("object") or {}
            brand = (
                _first_value(obj, "brand") or
                _first_value(raw, "brand", "Brand", "brandName", "manufacturer")
            )
            model = (
                _first_value(obj, "model") or
                _first_value(raw, "model", "Model", "modelName")
            )
            device_name = (
                _first_value(obj, "name") or
                _first_value(raw, "deviceName", "device_name", "name", "title")
            )
            status_text = _first_value(raw, "status", "message")

            # A real hit has at least brand or model populated
            if not any([brand, model, device_name]):
                continue

            record = ImeiRecord(
                imei=candidate,
                brand=brand,
                model=model,
                device_name=device_name,
                status_text=status_text,
            )
            summary_bits = [bit for bit in [brand, model, device_name] if bit]
            return ProviderResult(
                provider=self.provider_name,
                hit=True,
                status="success",
                summary=f"IMEI found: {' / '.join(summary_bits)}" if summary_bits else "IMEI details found",
                raw=raw,
                data=record,
            )

        return no_record_result(self.provider_name)


class GenericLookupProvider(PersonLookupProvider):
    provider_name = "generic"

    def __init__(self, provider_name: str, config: ProviderConfig, http_client: HttpClient):
        self.provider_name = provider_name
        self.config = config
        self.http = http_client

    def lookup(self, subject: SearchSubject) -> ProviderResult[GenericDatabaseRecord]:
        raise NotImplementedError

    def _success(self, raw: Any, summary: str, details: dict[str, Any] | None = None) -> ProviderResult[GenericDatabaseRecord]:
        record = GenericDatabaseRecord(
            matched=True,
            title=self.provider_name.upper(),
            details=(details or extract_scalar_details(raw)),
            source=self.provider_name,
        )
        return ProviderResult(provider=self.provider_name, hit=True, status="success", summary=summary, raw=raw, data=record)


class CfmsProvider(GenericLookupProvider):
    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        super().__init__("cfms", config, http_client)

    def lookup(self, subject: SearchSubject) -> ProviderResult[GenericDatabaseRecord]:
        cnic = normalize_cnic(subject.cnic)
        if not self.config.base_url:
            return not_configured_result(self.provider_name, ["CFMS_API_URL"])
        if not cnic or len(cnic) != 13:
            return invalid_input_result(self.provider_name, "Valid CNIC required")
        raw = self.http.request("POST", self.config.base_url, headers={"Accept": "application/json"}, params={"CNIC": cnic})
        if is_transport_error(raw):
            return error_result(self.provider_name, "CFMS lookup failed", raw=raw)
        if isinstance(raw, dict) and (raw.get("status") is True or raw.get("data") or raw.get("code") == 200):
            return self._success(raw, "Record found in CFMS")
        if isinstance(raw, list) and raw:
            return self._success(raw, "Record found in CFMS")
        return no_record_result(self.provider_name)


class HotelEyeProvider(GenericLookupProvider):
    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        super().__init__("hotel_eye", config, http_client)

    def lookup(self, subject: SearchSubject) -> ProviderResult[GenericDatabaseRecord]:
        missing = [
            name
            for name, value in {
                "HOTEL_EYE_API_KEY": self.config.api_key,
                "HOTEL_EYE_GUEST_URL/HOTEL_EYE_SIMPLE_URL": self.config.extra.get("guest_url") or self.config.extra.get("simple_url"),
            }.items()
            if not value
        ]
        if missing:
            return not_configured_result(self.provider_name, missing)

        cnic = dashed_cnic(subject.cnic)
        mobile = normalize_mobile(subject.mobile)
        if not cnic and not mobile:
            return invalid_input_result(self.provider_name, "Valid CNIC or mobile required")

        user_agent = "Mozilla/5.0 (compatible; EMS/1.0)"
        mobile_headers = {
            "X-API-KEY": self.config.api_key or "",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": user_agent,
        }
        cnic_headers = {
            "x-api-key": self.config.api_key or "",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": user_agent,
        }
        raw: dict[str, Any] = {"query": {"phone": mobile, "cnic": cnic}}

        selected_records: list[dict[str, Any]] = []
        selected_branch: str | None = None

        if mobile:
            raw["by_mobile"] = self.http.request(
                "POST",
                self.config.extra.get("simple_url"),
                headers=mobile_headers,
                data={"cell_no": mobile},
                timeout=60,
            )
            selected_records = _extract_hotel_eye_records(raw["by_mobile"])
            if selected_records:
                selected_branch = "mobile"

        if not selected_records and cnic:
            raw["by_cnic"] = self.http.request(
                "POST",
                self.config.extra.get("guest_url"),
                headers=cnic_headers,
                data={"cnic": cnic},
                timeout=60,
            )
            selected_records = _extract_hotel_eye_records(raw["by_cnic"])
            if selected_records:
                selected_branch = "cnic"

        raw["query"]["used_branch"] = selected_branch
        raw["records"] = selected_records

        attempted_payloads = [value for key, value in raw.items() if key in {"by_mobile", "by_cnic"}]
        if attempted_payloads and all(is_transport_error(item) for item in attempted_payloads):
            return error_result(self.provider_name, "Hotel Eye lookup failed", raw=raw)

        if selected_records:
            branch_label = "phone" if selected_branch == "mobile" else "CNIC"
            details: dict[str, Any] = {
                "total_records": len(selected_records),
            }
            if mobile:
                details["phone"] = mobile
            if selected_branch == "cnic" and cnic:
                details["cnic"] = cnic
            elif cnic and not mobile:
                details["cnic"] = cnic
            return self._success(raw, f"{len(selected_records)} HOTEL EYE record(s) found by {branch_label}", details=details)

        return no_record_result(self.provider_name)


class SbvsProvider(GenericLookupProvider):
    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        super().__init__("sbvs", config, http_client)

    def lookup(self, subject: SearchSubject) -> ProviderResult[SbvsRecord]:
        missing = [
            name
            for name, value in {
                "SBVS_API_KEY": self.config.api_key,
                "SBVS_CNIC_URL/SBVS_MOBILE_URL": self.config.extra.get("cnic_url") or self.config.extra.get("mobile_url"),
            }.items()
            if not value
        ]
        if missing:
            return not_configured_result(self.provider_name, missing)

        cnic = dashed_cnic(subject.cnic)
        mobile = normalize_mobile(subject.mobile)
        if not cnic and not mobile:
            return invalid_input_result(self.provider_name, "Valid CNIC or mobile required")

        headers = {"X-API-KEY": self.config.api_key or ""}
        
        raw_response = None
        used_method = None
        used_value = None

        if mobile:
            resp = self.http.request("GET", f"{self.config.extra.get('mobile_url').rstrip('/')}/{mobile}", headers=headers)
            if not is_transport_error(resp) and _raw_has_data(resp):
                raw_response = resp
                used_method = "phone"
                used_value = mobile

        if not raw_response and cnic:
            resp = self.http.request("GET", f"{self.config.extra.get('cnic_url').rstrip('/')}/{cnic}", headers=headers)
            if not is_transport_error(resp) and _raw_has_data(resp):
                raw_response = resp
                used_method = "cnic"
                used_value = cnic

        if not raw_response:
            return no_record_result(self.provider_name)

        if isinstance(raw_response, dict):
            raw_response["_meta"] = {"used_value": used_value, "used_method": used_method}

        data_payload = raw_response.get("data", {}) if isinstance(raw_response, dict) else {}
        
        # Determine if data is a list or a single dict
        # Based on example {"success": true, "data": {"details": {...}, "image": {...}}}
        # We handle both single dict with "details" and list of them
        items = []
        if isinstance(data_payload, list):
            items = data_payload
        elif isinstance(data_payload, dict):
            if "details" in data_payload:
                items = [data_payload]
            else:
                items = [data_payload]

        entries = []
        latest_record = None

        for item in items:
            details = item.get("details", item) if isinstance(item, dict) else {}
            if not details:
                continue
            
            if not latest_record:
                latest_record = details

            entries.append(SbvsEntry(
                id=details.get("id"),
                organization_name=details.get("org_name"),
                profession=details.get("profession"),
                purpose=details.get("purpose_name") or details.get("purpose"),
                district=details.get("district_name") or details.get("district"),
                ps=details.get("police_station_name") or details.get("ps"),
                status=details.get("status_name") or details.get("status"),
            ))

        if not latest_record:
            return no_record_result(self.provider_name)

        record = SbvsRecord(
            name=latest_record.get("full_name") or latest_record.get("name"),
            father_name=latest_record.get("father_name"),
            cnic=latest_record.get("cnic"),
            phone=latest_record.get("mobile") or latest_record.get("phone"),
            passport=latest_record.get("passport"),
            address=latest_record.get("address"),
            entries=entries
        )

        return ProviderResult(
            provider=self.provider_name,
            hit=True,
            status="success",
            summary=f"Record Found in sbvs ({len(entries)} records)",
            raw=raw_response,
            data=record,
        )


class PfcProvider(GenericLookupProvider):
    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        super().__init__("pfc", config, http_client)

    def lookup(self, subject: SearchSubject) -> ProviderResult[GenericDatabaseRecord]:
        if not self.config.base_url or not self.config.api_key:
            return not_configured_result(self.provider_name, ["PFC_API_URL", "PFC_API_KEY"])
        cnic = dashed_cnic(subject.cnic)
        mobile = dashed_mobile(subject.mobile)
        if not cnic and not mobile:
            return invalid_input_result(self.provider_name, "Valid CNIC or mobile required")
        data = {}
        if cnic:
            data["complainant_cnic"] = cnic
        if mobile:
            data["complainant_contact_number"] = mobile
        raw = self.http.request("POST", self.config.base_url, headers={"x-api-key": self.config.api_key}, data=data)
        if is_transport_error(raw):
            return error_result(self.provider_name, "PFC lookup failed", raw=raw)
        if _raw_has_data(raw):
            return self._success(raw, "Record found in PFC")
        return no_record_result(self.provider_name)


class HrmisProvider(GenericLookupProvider):
    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        super().__init__("hrmis", config, http_client)

    def lookup(self, subject: SearchSubject) -> ProviderResult[HrmisRecord]:
        missing = [
            name
            for name, value in {
                "HRMIS_API_URL": self.config.base_url,
                "HRMIS_API_KEY": self.config.api_key,
                "HRMIS_AUTH_TOKEN": self.config.extra.get("auth_token"),
            }.items()
            if not value
        ]
        if missing:
            return not_configured_result(self.provider_name, missing)
        cnic = dashed_cnic(subject.cnic)
        mobile = dashed_mobile(subject.mobile)
        if not cnic and not mobile:
            return invalid_input_result(self.provider_name, "Valid CNIC or mobile required")
        headers = {
            "X-API-KEY": self.config.api_key or "",
            "Authorization": self.config.extra.get("auth_token", ""),
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        raw: dict[str, Any] = {}

        if mobile:
            raw["by_mobile"] = self.http.request("POST", self.config.base_url, headers=headers, data={"mobile": mobile})
            mobile_result = self._parse_hrmis_response(raw["by_mobile"], search_branch="mobile")
            if mobile_result.hit:
                mobile_result.raw = raw
                return mobile_result

        if cnic:
            raw["by_cnic"] = self.http.request("POST", self.config.base_url, headers=headers, data={"cnic": cnic})
            cnic_result = self._parse_hrmis_response(raw["by_cnic"], search_branch="cnic", cnic_used=cnic)
            if cnic_result.hit:
                cnic_result.raw = raw
                return cnic_result

        if raw and any(is_transport_error(item) for item in raw.values()):
            return error_result(self.provider_name, "HRMIS lookup failed", raw=raw)
        return no_record_result(self.provider_name)

    def _parse_hrmis_response(
        self,
        raw: Any,
        *,
        search_branch: str,
        cnic_used: str | None = None,
    ) -> ProviderResult[HrmisRecord]:
        if is_transport_error(raw):
            return error_result(self.provider_name, "HRMIS lookup failed", raw=raw)

        output = raw.get("output") if isinstance(raw, dict) else None
        if not isinstance(output, list) or not output:
            return no_record_result(self.provider_name)

        latest = self._pick_latest_record(output)
        if not isinstance(latest, dict):
            return no_record_result(self.provider_name)

        record = HrmisRecord(
            officer_name=latest.get("ofc_name") or latest.get("nadra_name"),
            officer_phone=latest.get("ofc_mobile") or latest.get("ofc_phone"),
            officer_cnic=latest.get("ofc_cnic"),
            officer_belt_no=latest.get("ofc_belt_no"),
            date_of_birth=latest.get("ofc_dateofbirth") or latest.get("nadra_dateofbirth"),
            current_posting=latest.get("current_posting_new") or latest.get("current_posting"),
            officer_address=latest.get("ofc_address") or latest.get("ofc_mailing_address"),
            officer_city=latest.get("ofc_city"),
            rank=latest.get("rnk_name") or latest.get("designation_name"),
            police_station_name=latest.get("ps_name_eng"),
            district=latest.get("dst_name"),
            search_branch=search_branch,
            cnic_used=cnic_used,
        )
        return ProviderResult(
            provider=self.provider_name,
            hit=True,
            status="success",
            summary=f"Officer record found in HRMIS ({record.officer_name or 'Unknown'})",
            raw=raw,
            data=record,
        )

    def _pick_latest_record(self, records: list[dict[str, Any]]) -> dict[str, Any] | None:
        def sort_key(item: dict[str, Any]) -> tuple[str, str]:
            arrival = str(item.get("police_station_arrival_date") or "").strip()
            joining = str(item.get("joining_date") or "").strip()
            return (arrival if arrival not in {"", "0000-00-00"} else "", joining)

        ordered = sorted(records, key=sort_key, reverse=True)
        return ordered[0] if ordered else None


class IgpCmsProvider(GenericLookupProvider):
    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        super().__init__("igp_cms", config, http_client)

    def lookup(self, subject: SearchSubject) -> ProviderResult[GenericDatabaseRecord]:
        if not self.config.base_url or not self.config.api_key:
            return not_configured_result(self.provider_name, ["IGP_CMS_API_URL", "IGP_CMS_API_KEY"])
        cnic = dashed_cnic(subject.cnic)
        mobile = dashed_mobile(subject.mobile)
        if not cnic and not mobile:
            return invalid_input_result(self.provider_name, "Valid CNIC or mobile required")
        data = {}
        if cnic:
            data["cnic"] = cnic
        if mobile:
            data["contact"] = mobile
        raw = self.http.request("POST", self.config.base_url, headers={"X-API-KEY": self.config.api_key}, data=data)
        if is_transport_error(raw):
            return error_result(self.provider_name, "IGP CMS lookup failed", raw=raw)
        if _raw_has_data(raw):
            return self._success(raw, "Record found in IGP CMS")
        return no_record_result(self.provider_name)


class EvsProvider(GenericLookupProvider):
    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        super().__init__("evs", config, http_client)

    def lookup(self, subject: SearchSubject) -> ProviderResult[EmploymentDatabaseRecord]:
        missing = [
            name
            for name, value in {
                "EVS_API_URL": self.config.base_url,
                "EVS_I_KEY": self.config.extra.get("i_key"),
                "EVS_J_KEY": self.config.extra.get("j_key"),
                "EVS_API_TOKEN": self.config.extra.get("api_token"),
            }.items()
            if not value
        ]
        if missing:
            return not_configured_result(self.provider_name, missing)
        cnic = normalize_cnic(subject.cnic)
        if not cnic:
            return invalid_input_result(self.provider_name, "Valid CNIC required")
        raw = self.http.request(
            "POST",
            self.config.base_url,
            headers={
                "i_key": self.config.extra.get("i_key", ""),
                "j_key": self.config.extra.get("j_key", ""),
                "api_token": self.config.extra.get("api_token", ""),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"cnic": cnic},
        )
        if is_transport_error(raw):
            return error_result(self.provider_name, "EVS lookup failed", raw=raw)
        records = raw.get("data") if isinstance(raw, dict) else None
        if isinstance(records, list) and records:
            latest = self._pick_latest_record(records)
            if isinstance(latest, dict):
                record = EmploymentDatabaseRecord(
                    name=latest.get("name"),
                    father_name=latest.get("father_name"),
                    cnic=normalize_cnic(latest.get("cnic")) or cnic,
                    contact=normalize_mobile(latest.get("contact")) or latest.get("contact"),
                    other_contact=normalize_mobile(latest.get("other_contact")) or latest.get("other_contact"),
                    permanent_address=latest.get("perm_address"),
                    designation=latest.get("designation"),
                )
                return ProviderResult(
                    provider=self.provider_name,
                    hit=True,
                    status="success",
                    summary=f"Employee record found in EVS ({record.name or 'Unknown'})",
                    raw=raw,
                    data=record,
                )
        return no_record_result(self.provider_name)

    def _pick_latest_record(self, records: list[dict[str, Any]]) -> dict[str, Any] | None:
        def sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
            last_verified = str(item.get("last_verified_at") or "").strip()
            updated = str(item.get("updated_at") or "").strip()
            created = str(item.get("created_at") or "").strip()
            return (last_verified, updated, created)

        ordered = sorted(records, key=sort_key, reverse=True)
        return ordered[0] if ordered else None


class HopeProvider(GenericLookupProvider):
    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        super().__init__("hope", config, http_client)

    def lookup(self, subject: SearchSubject) -> ProviderResult[EmploymentDatabaseRecord]:
        missing = [
            name
            for name, value in {
                "HOPE_EMPLOYEE_URL": self.config.extra.get("employee_url"),
                "HOPE_I_KEY": self.config.extra.get("i_key"),
                "HOPE_J_KEY": self.config.extra.get("j_key"),
                "HOPE_API_TOKEN": self.config.extra.get("api_token"),
            }.items()
            if not value
        ]
        if missing:
            return not_configured_result(self.provider_name, missing)
        cnic = normalize_cnic(subject.cnic)
        if not cnic:
            return invalid_input_result(self.provider_name, "Valid CNIC required")
        headers = {
            "i_key": self.config.extra.get("i_key", ""),
            "j_key": self.config.extra.get("j_key", ""),
            "api_token": self.config.extra.get("api_token", ""),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        raw = self.http.request(
            "POST",
            self.config.extra.get("employee_url"),
            headers=headers,
            data={"cnic": cnic},
        )
        if is_transport_error(raw):
            return error_result(self.provider_name, "Hope Employee lookup failed", raw=raw)
        records = raw.get("data") if isinstance(raw, dict) else None
        if isinstance(records, list) and records:
            latest = self._pick_latest_record(records)
            if isinstance(latest, dict):
                record = EmploymentDatabaseRecord(
                    name=latest.get("name"),
                    father_name=latest.get("father_name"),
                    cnic=normalize_cnic(latest.get("cnic")) or cnic,
                    contact=normalize_mobile(latest.get("contact")) or latest.get("contact"),
                    other_contact=normalize_mobile(latest.get("other_contact")) or latest.get("other_contact"),
                    permanent_address=latest.get("perm_address"),
                    designation=latest.get("designation"),
                )
                return ProviderResult(
                    provider=self.provider_name,
                    hit=True,
                    status="success",
                    summary=f"Employee record found in Hope Employee ({record.name or 'Unknown'})",
                    raw=raw,
                    data=record,
                )
        return no_record_result(self.provider_name)

    def _pick_latest_record(self, records: list[dict[str, Any]]) -> dict[str, Any] | None:
        def sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
            last_verified = str(item.get("last_verified_at") or "").strip()
            updated = str(item.get("updated_at") or "").strip()
            created = str(item.get("created_at") or "").strip()
            return (last_verified, updated, created)

        ordered = sorted(records, key=sort_key, reverse=True)
        return ordered[0] if ordered else None


class DlsProvider(GenericLookupProvider):
    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        super().__init__("dls", config, http_client)
        self._access_token: str | None = None

    def lookup(self, subject: SearchSubject) -> ProviderResult[DlsRecord]:
        missing = [
            name
            for name, value in {
                "DLS_API_URL": self.config.base_url,
                "DLS_LOGIN_URL": self.config.extra.get("login_url"),
                "DLS_USERNAME": self.config.extra.get("username"),
                "DLS_PASSWORD": self.config.extra.get("password"),
                "DLS_SEED_TOKEN": self.config.extra.get("seed_token"),
            }.items()
            if not value
        ]
        if missing:
            return not_configured_result(self.provider_name, missing)
        mobile = normalize_mobile(subject.mobile)
        if not mobile:
            return invalid_input_result(self.provider_name, "Valid mobile required")

        dashed = dashed_mobile(mobile)
        if not dashed:
            return invalid_input_result(self.provider_name, "Valid mobile required")

        token = self._get_access_token()
        if not token:
            return error_result(self.provider_name, "DLS authentication failed")

        raw = self._fetch_license_data(dashed, token)
        if self._is_auth_failure(raw):
            token = self._refresh_access_token(force=True)
            if not token:
                return error_result(self.provider_name, "DLS authentication failed", raw=raw)
            raw = self._fetch_license_data(dashed, token)

        result = self._parse_dls_response(raw)
        if result.hit:
            if isinstance(result.raw, dict):
                result.raw.setdefault("query", {})
                result.raw["query"]["mobile"] = dashed
            return result
        if is_transport_error(raw):
            return error_result(self.provider_name, "DLS lookup failed", raw=raw)
        return no_record_result(self.provider_name)

    def _get_access_token(self) -> str | None:
        return self._access_token or self._refresh_access_token(force=False)

    def _refresh_access_token(self, force: bool) -> str | None:
        if self._access_token and not force:
            return self._access_token

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.extra.get('seed_token', '')}",
        }
        raw = self.http.request(
            "POST",
            self.config.extra.get("login_url"),
            headers=headers,
            json={
                "username": self.config.extra.get("username"),
                "password": self.config.extra.get("password"),
            },
        )
        if is_transport_error(raw):
            return None
        data = raw.get("data") if isinstance(raw, dict) else None
        access_token = data.get("accessToken") if isinstance(data, dict) else None
        token = str(access_token or "").strip()
        if token:
            self._access_token = token
            return token
        return None

    def _fetch_license_data(self, mobile: str, token: str) -> dict[str, Any] | list[Any]:
        base_url = (self.config.base_url or "").rstrip("/")
        url = f"{base_url}/{mobile}"
        return self.http.request(
            "POST",
            url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )

    def _is_auth_failure(self, raw: Any) -> bool:
        if isinstance(raw, dict):
            text = " ".join(str(raw.get(key) or "") for key in ["message", "error", "text_response"]).lower()
            status_code = raw.get("status_code")
            if status_code == 401:
                return True
            return "unauthorized" in text or "token" in text and ("expired" in text or "invalid" in text)
        return False

    def _parse_dls_response(self, raw: Any) -> ProviderResult[DlsRecord]:
        if is_transport_error(raw):
            return error_result(self.provider_name, "DLS lookup failed", raw=raw)

        payload = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(payload, list) or not payload:
            return no_record_result(self.provider_name)

        first = payload[0] if isinstance(payload[0], dict) else {}
        licenses: list[DlsLicenseRecord] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            licenses.append(
                DlsLicenseRecord(
                    license_no=item.get("license_no"),
                    category=item.get("license_category"),
                    license_type=item.get("license_type"),
                    expiry_date=item.get("expiry_date"),
                    issued_date=item.get("issued_date"),
                    issued_office=item.get("issued_office"),
                    status=item.get("status"),
                )
            )

        record = DlsRecord(
            firstname=first.get("firstname"),
            lastname=first.get("lastname"),
            phone=first.get("mobile"),
            cnic=first.get("cnic"),
            address=first.get("address"),
            licenses=licenses,
        )
        return ProviderResult(
            provider=self.provider_name,
            hit=True,
            status="success",
            summary=f"{len(licenses)} driving license record(s) found in DLS",
            raw=raw,
            data=record,
        )


class TracsProvider(GenericLookupProvider):
    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        super().__init__("tracs", config, http_client)

    def lookup(self, subject: SearchSubject) -> ProviderResult[GenericDatabaseRecord]:
        if not self.config.base_url or not self.config.extra.get("auth_token"):
            return not_configured_result(self.provider_name, ["TRACS_API_URL", "TRACS_AUTH_TOKEN"])
        cnic = normalize_cnic(subject.cnic)
        if not cnic:
            return invalid_input_result(self.provider_name, "Valid CNIC required")
        raw = self.http.request(
            "GET",
            self.config.base_url,
            headers={"Authorization": self.config.extra.get("auth_token", "")},
            params={"cnic": cnic},
        )
        if is_transport_error(raw):
            return error_result(self.provider_name, "TRACS lookup failed", raw=raw)
        if _raw_has_data(raw):
            return self._success(raw, "Record found in TRACS")
        return no_record_result(self.provider_name)


class OldTenantProvider(PersonLookupProvider):
    provider_name = "old_tenant"

    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        self.config = config
        self.http = http_client

    def lookup(self, subject: SearchSubject) -> ProviderResult[OldTenantRecord]:
        if not self.config.base_url or not self.config.api_key:
            return not_configured_result(self.provider_name, ["OLD_TENANT_API_URL", "OLD_TENANT_API_KEY"])
            
        cnic = dashed_cnic(subject.cnic)
        mobile = dashed_mobile(subject.mobile)
        
        if not cnic and not mobile:
            return invalid_input_result(self.provider_name, "Valid CNIC or mobile required")

        headers = {
            "Accept": "application/json",
            "X-API-KEY": self.config.api_key or ""
        }

        # User wants to search with phone first, if not found then CNIC
        # Actually, the user says "search with both cnic or phone, separately or together... For suspect, first search phone no, if no phone use cnic". 
        # But for robustness we'll hit both or combined if we can. 
        # Since it supports both in query, we can try them individually to get better matches if one fails, or just combined. 
        # The prompt says: "first search with phone no, if no phone is given then use cnic provieded if not provided use telecom but mention it that you used that. For top contacts, you will also search it with phone no, if not found you will use telecom one if found but will mention it."
        # This fallback to telecom is actually handled in `analysis_service.py` where it orchestrates. 
        # Here in the provider, we just try to fetch based on what's provided. 
        # We will try mobile first, if we have it, else cnic. We can also try both if we have both, but let's query both simultaneously and see which hits.
        
        raw: dict[str, Any] = {}
        branch_requests: list[tuple[str, dict[str, Any]]] = []
        if mobile:
            branch_requests.append(("by_mobile", {"mobile": mobile}))
        if cnic:
            branch_requests.append(("by_cnic", {"cnic": cnic}))

        searched = False
        selected_record = None
        selected_branch = None
        last_raw = None
        
        for branch_name, params in branch_requests:
            res = self.http.request("GET", self.config.base_url, headers=headers, params=params)
            last_raw = res
            raw[branch_name] = res
            
            if isinstance(res, dict) and res.get("success") is True:
                data = res.get("data", [])
                if isinstance(data, list) and data:
                    selected_record = data
                    selected_branch = branch_name
                    break

        if not selected_record:
            if all(is_transport_error(v) for v in raw.values()) and raw:
                return error_result(self.provider_name, "Old Tenant lookup failed", raw=raw)
            return no_record_result(self.provider_name)

        first_rec = selected_record[0]
        record = OldTenantRecord(
            owner_name=first_rec.get("raw", {}).get("owner_name") or "Unknown",
            owner_phone=first_rec.get("owner_mobile_number"),
            owner_cnic=first_rec.get("owner_cnic"),
            search_branch=selected_branch
        )
        
        for ten in selected_record:
            tinfo = OldTenantTenantInfo(
                tenant_id=ten.get("tenant_id"),
                tenant_name=ten.get("tenant_name"),
                tenant_cnic=ten.get("tenant_cnic"),
                tenant_phone=ten.get("tenant_mobile") or ten.get("raw", {}).get("tenant_mobile_number"),
                house_no=ten.get("raw", {}).get("property_house_no"),
                street_mohalla=ten.get("raw", {}).get("property_street_mohalla"),
                address=ten.get("raw", {}).get("property_address")
            )
            record.tenants.append(tinfo)

        return ProviderResult(
            provider=self.provider_name,
            hit=True,
            status="success",
            summary=f"Record found in Old Tenant ({len(record.tenants)} tenants)",
            raw=raw,
            data=record,
        )


class TrustProvider(GenericLookupProvider):
    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        super().__init__("trust", config, http_client)

    def lookup(self, subject: SearchSubject) -> ProviderResult[GenericDatabaseRecord]:
        if not self.config.base_url or not self.config.api_key:
            return not_configured_result(self.provider_name, ["TRUST_API_URL", "TRUST_API_KEY"])
        cnic = dashed_cnic(subject.cnic)
        mobile = mobile_10(subject.mobile)
        if not cnic and not mobile:
            return invalid_input_result(self.provider_name, "Valid CNIC or mobile required")
        params: dict[str, Any] = {}
        if cnic:
            params["cnic"] = cnic
        if mobile:
            params["mobile"] = mobile
        raw = self.http.request(
            "GET",
            self.config.base_url,
            headers={"X-API-KEY": self.config.api_key},
            params=params,
        )
        if is_transport_error(raw):
            return error_result(self.provider_name, "TRUST lookup failed", raw=raw)
        if isinstance(raw, dict):
            data = raw.get("data")
            if isinstance(data, dict):
                user_details = data.get("user_details")
                properties_owned = data.get("properties_owned") or []
                tenacies = data.get("tenacies") or data.get("tenancies") or []
                if user_details or properties_owned or tenacies:
                    details = {
                        "has_user_details": bool(user_details),
                        "properties_owned_count": len(properties_owned) if isinstance(properties_owned, list) else 0,
                        "tenacies_count": len(tenacies) if isinstance(tenacies, list) else 0,
                    }
                    return self._success(raw, "Record found in TRUST", details=details)
        return no_record_result(self.provider_name)


class MilapProvider(GenericLookupProvider):
    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        super().__init__("milap", config, http_client)

    def lookup(self, subject: SearchSubject) -> ProviderResult[GenericDatabaseRecord]:
        if not self.config.base_url or not self.config.api_key:
            return not_configured_result(self.provider_name, ["MILAP_LOST_RECORDS_URL", "MILAP_API_KEY"])
        cnic = dashed_cnic(subject.cnic)
        mobile = normalize_mobile(subject.mobile)
        if not cnic and not mobile:
            return invalid_input_result(self.provider_name, "Valid CNIC or mobile required")
        data: dict[str, Any] = {}
        if cnic:
            data["reporting_cnic"] = cnic
        if mobile:
            data["reporting_contact"] = mobile
        raw = self.http.request(
            "POST",
            self.config.base_url,
            headers={"X-API-KEY": self.config.api_key},
            data=data,
        )
        if is_transport_error(raw):
            return error_result(self.provider_name, "MILAP lookup failed", raw=raw)
        if _raw_has_data(raw):
            return self._success(raw, "Record found in MILAP")
        return no_record_result(self.provider_name)


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_value(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _raw_has_data(raw: Any) -> bool:
    if isinstance(raw, dict):
        data = raw.get("data")
        code = raw.get("code")
        if _has_meaningful_payload(data):
            return True
        for key in ("result", "results", "record", "records", "payload", "response"):
            value = raw.get(key)
            if _has_meaningful_payload(value):
                return True
        if code == 200:
            meaningful_keys = [
                key
                for key, value in raw.items()
                if key not in {"status", "message", "code", "summary"}
                and _has_meaningful_payload(value)
            ]
            return bool(meaningful_keys)
        return False
    return _has_meaningful_payload(raw)


def _has_meaningful_payload(value: Any) -> bool:
    if isinstance(value, dict):
        if not value:
            return False
        for key, nested in value.items():
            if str(key).lower() in {"status", "message", "code", "summary"}:
                continue
            if _has_meaningful_payload(nested):
                return True
        return False
    if isinstance(value, list):
        return any(_has_meaningful_payload(item) for item in value)
    if isinstance(value, str):
        text = value.strip()
        return bool(text) and text.lower() not in {"request successful", "success", "ok", "none", "null", "[]", "{}"}
    return value not in (None, False, "")


def _extract_hotel_eye_records(raw: Any) -> list[dict[str, Any]]:
    payload = []
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, list):
            payload = data
        elif isinstance(data, dict) and data:
            payload = [data]
    records: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        normalized = {
            "guest_name": item.get("guest_name") or item.get("name") or "",
            "hotel_name": item.get("hotel_name") or "",
            "district": item.get("hotel_district") or item.get("district") or "",
            "room_no": item.get("room_no") or "",
            "check_in": item.get("check_in") or "",
            "check_out": item.get("check_out") or "",
            "visit_purpose": item.get("visit_purpose") or "",
            "guest_cell": item.get("guest_cell") or item.get("cell_no") or "",
            "guest_cnic": item.get("guest_cnic") or item.get("cnic") or "",
        }
        if any(str(value).strip() for value in normalized.values()):
            records.append(normalized)
    return records


class CroPdfAttachmentProvider(AttachmentProvider):
    provider_name = "cro_pdf"

    def __init__(self, settings: Settings, http_client: HttpClient):
        self.settings = settings
        self.http = http_client

    def _request_timeout(self) -> int:
        configured = self.settings.providers.cro.extra.get("report_timeout_seconds")
        try:
            configured_timeout = int(configured) if configured is not None else 0
        except (TypeError, ValueError):
            configured_timeout = 0
        # Legacy code used 30s here and CRO PDFs are noticeably slower than the
        # regular person-data lookups, so keep a provider-specific floor.
        return max(self.http.timeout, configured_timeout, 30)

    def fetch_attachment(self, cro_no: str) -> ProviderResult[AttachmentArtifact]:
        if not cro_no:
            return invalid_input_result(self.provider_name, "CRO number required")
        report_key = self.settings.providers.cro.extra.get("report_api_key", "")
        if not report_key:
            return not_configured_result(self.provider_name, ["CRO_REPORT_API_KEY"])

        url = self.settings.providers.cro.extra.get("report_url") 
        meta, payload = self.http.request_bytes(
            "POST",
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-api-key": report_key,
            },
            json={"cro_no": str(cro_no)},
            timeout=self._request_timeout(),
        )

        if isinstance(meta, dict) and meta.get("error"):
            return error_result(self.provider_name, "CRO PDF fetch failed", raw=meta)

        content_type = str(meta.get("content_type", ""))
        if payload and "application/pdf" in content_type:
            artifact = AttachmentArtifact(
                source=self.provider_name,
                label=f"CRO#{cro_no}",
                media_type="pdf",
                bytes_content=payload,
                metadata={"cro_no": cro_no},
            )
            return ProviderResult(provider=self.provider_name, hit=True, status="success", summary="CRO PDF fetched", raw=meta, data=artifact)

        if payload:
            try:
                decoded = extract_base64_pdf(__import__("json").loads(payload.decode("utf-8")))
            except Exception:
                decoded = payload if payload[:4] == b"%PDF" else None
            if decoded:
                artifact = AttachmentArtifact(
                    source=self.provider_name,
                    label=f"CRO#{cro_no}",
                    media_type="pdf",
                    bytes_content=decoded,
                    metadata={"cro_no": cro_no},
                )
                return ProviderResult(provider=self.provider_name, hit=True, status="success", summary="CRO PDF fetched", raw=meta, data=artifact)

        return no_record_result(self.provider_name, "CRO PDF not available")


class PsrmsFirAttachmentProvider(AttachmentProvider):
    provider_name = "psrms_fir"
    default_fir_cookie = "d1796422_8a25e425=j0jc9ncosml42alclhukbrrduc0bh88f"

    def __init__(self, config: ProviderConfig, http_client: HttpClient):
        self.config = config
        self.http = http_client

    def fetch_attachment(self, fir_no: str, fir_year: str, ps_id: str) -> ProviderResult[AttachmentArtifact]:
        if not fir_no or not fir_year or not ps_id:
            return invalid_input_result(self.provider_name, "fir_no, fir_year, and ps_id are required")
        missing = [
            name
            for name, value in {
                "PSRMS_FIR_REPORT_URL": self.config.extra.get("fir_report_url"),
                "PSRMS_API_KEY": self.config.api_key,
                "PSRMS_FIR_COOKIE/PSRMS_COOKIE": self.config.extra.get("fir_cookie"),
            }.items()
            if not value
        ]
        if missing:
            return not_configured_result(self.provider_name, missing)

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "PSRMS-API-KEY": self.config.api_key or "",
            "fir_no": str(fir_no).strip(),
            "fir_year": str(fir_year).strip(),
            "ps_id": str(ps_id).strip(),
        }

        last_raw: Any = None
        for cookie in [self.config.extra.get("fir_cookie"), self.default_fir_cookie]:
            if not cookie:
                continue
            attempt_headers = dict(headers)
            attempt_headers["Cookie"] = cookie
            raw = self.http.request(
                "POST",
                self.config.extra.get("fir_report_url"),
                headers=attempt_headers,
                data=data,
                timeout=self.http.timeout,
            )
            last_raw = raw
            if isinstance(raw, dict) and "text_response" in raw:
                html_text = str(raw.get("text_response", "")).lstrip("\ufeff")
                content_type = str(raw.get("content_type", "")).lower()
                html_like = any(marker in html_text.lower() for marker in ["<html", "<table", "<div", "<h1", "<h2", "<h3"])
                if "not found" in html_text.lower():
                    return no_record_result(self.provider_name, "PSRMS FIR report not found for the provided FIR details")
                if "html" in content_type or html_like:
                    pdf_bytes = render_psrms_fir_html_to_pdf(html_text)
                    parsed_html_metadata = parse_psrms_fir_html(html_text)
                    if pdf_bytes and len(pdf_bytes) > 100:
                        artifact = AttachmentArtifact(
                            source=self.provider_name,
                            label=f"FIR {fir_no}/{fir_year}",
                            media_type="pdf",
                            bytes_content=pdf_bytes,
                            metadata={
                                "fir_no": fir_no,
                                "fir_year": fir_year,
                                "ps_id": ps_id,
                                **parsed_html_metadata,
                            },
                        )
                        return ProviderResult(
                            provider=self.provider_name,
                            hit=True,
                            status="success",
                            summary="PSRMS FIR rendered to PDF",
                            raw=raw,
                            data=artifact,
                        )
                    artifact = html_attachment(
                        self.provider_name,
                        f"FIR {fir_no}/{fir_year}",
                        html_text,
                    )
                    artifact.metadata.update({"fir_no": fir_no, "fir_year": fir_year, "ps_id": ps_id})
                    return ProviderResult(
                        provider=self.provider_name,
                        hit=True,
                        status="success",
                        summary="PSRMS FIR HTML fetched",
                        raw=raw,
                        data=artifact,
                    )

        if is_transport_error(last_raw):
            return error_result(self.provider_name, "PSRMS FIR fetch failed", raw=last_raw)
        return no_record_result(self.provider_name, "PSRMS FIR report not available")


class UnifiedLookupService:
    """Orchestrates normalized provider lookups."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lookup_cache: dict[tuple[tuple[str, ...], tuple[Any, ...]], dict[str, ProviderResult]] = {}

        # Build Shield auth client if configured; uses its own HttpClient for login calls
        shield_cfg = settings.shield
        shield: ShieldAuthClient | None = None
        if shield_cfg.configured:
            login_http = HttpClient(timeout=30)
            shield = ShieldAuthClient(
                login_url=shield_cfg.login_url,  # type: ignore[arg-type]
                email=shield_cfg.email,           # type: ignore[arg-type]
                password=shield_cfg.password,     # type: ignore[arg-type]
                http=login_http,
            )
            logger.info("Shield auth configured | base=%s", shield_cfg.base_url)

        self.http = HttpClient(
            timeout=settings.app.request_timeout_seconds,
            shield=shield,
            shield_base=shield_cfg.base_url,
            shield_cookie=shield_cfg.cookie,
        )

        self.providers: dict[str, PersonLookupProvider] = {
            "simsdb": SimsDbProvider(settings.providers.simsdb, self.http),
            "subscriber": SubscriberProvider(settings.providers.subscriber, self.http),
            "prvs": PrvsProvider(settings.providers.prvs, self.http),
            "cro": CroProvider(settings.providers.cro, self.http),
            "psrms": PsrmsProvider(settings.providers.psrms, self.http),
            "watchlist": WatchlistProvider(settings.providers.watchlist, self.http),
            "nearest_ps": NearestPsProvider(settings.providers.nearest_ps, self.http),
            "cfms": CfmsProvider(settings.providers.cfms, self.http),
            "hotel_eye": HotelEyeProvider(settings.providers.hotel_eye, self.http),
            "sbvs": SbvsProvider(settings.providers.sbvs, self.http),
            "pfc": PfcProvider(settings.providers.pfc, self.http),
            "hrmis": HrmisProvider(settings.providers.hrmis, self.http),
            "igp_cms": IgpCmsProvider(settings.providers.igp_cms, self.http),
            "imei": ImeiProvider(settings.providers.imei, self.http),
            "evs": EvsProvider(settings.providers.evs, self.http),
            "hope": HopeProvider(settings.providers.hope, self.http),
            "dls": DlsProvider(settings.providers.dls, self.http),
            "tracs": TracsProvider(settings.providers.tracs, self.http),
            "old_tenant": OldTenantProvider(settings.providers.old_tenant, self.http),
            "trust": TrustProvider(settings.providers.trust, self.http),
            "milap": MilapProvider(settings.providers.milap, self.http),
        }

    def lookup_all(self, subject: SearchSubject, provider_names: list[str] | None = None) -> dict[str, ProviderResult]:
        requested = provider_names or PERSON_LOOKUP_PROVIDERS
        requested = [name for name in requested if name in self.providers and getattr(self.settings.providers, name).enabled]
        logger.info(
            "Lookup batch started | providers=%s cnic=%s mobile=%s imei=%s lat=%s lng=%s",
            requested,
            subject.cnic,
            subject.mobile,
            subject.imei,
            subject.latitude,
            subject.longitude,
        )
        cache_key = (tuple(requested), self._subject_cache_key(subject))
        if cache_key in self._lookup_cache:
            logger.info("Lookup batch cache hit | providers=%s", requested)
            return dict(self._lookup_cache[cache_key])
        results: dict[str, ProviderResult] = {}

        enriched_subject = SearchSubject.model_validate(subject.model_dump())
        # CNIC enrichment: the SIMs database is the primary source, the telecom
        # subscriber DB the fallback. Everything downstream (CRO, PSRMS, HRMIS,
        # ...) keys off the CNIC these two resolve.
        for identity_provider in IDENTITY_ENRICHMENT_PROVIDERS:
            if enriched_subject.cnic or not enriched_subject.mobile:
                break
            if identity_provider not in requested:
                continue
            logger.info("Identity enrichment started | provider=%s mobile=%s", identity_provider, enriched_subject.mobile)
            identity_result = self.providers[identity_provider].lookup(enriched_subject)
            results[identity_provider] = identity_result
            if identity_result.hit and identity_result.data and getattr(identity_result.data, "cnic", None):
                enriched_subject.cnic = identity_result.data.cnic
                logger.info(
                    "Identity enrichment succeeded | provider=%s mobile=%s cnic=%s",
                    identity_provider,
                    enriched_subject.mobile,
                    enriched_subject.cnic,
                )

        with ThreadPoolExecutor(max_workers=min(5, len(requested) or 1)) as executor:
            future_map = {}
            for name in requested:
                if name in results:
                    continue
                future_map[executor.submit(self.providers[name].lookup, enriched_subject)] = name

            for future in as_completed(future_map):
                name = future_map[future]
                try:
                    results[name] = future.result()
                    logger.info(
                        "Lookup provider completed | provider=%s status=%s hit=%s summary=%s",
                        name,
                        results[name].status,
                        results[name].hit,
                        results[name].summary,
                    )
                except Exception as exc:
                    logger.exception("Lookup provider crashed | provider=%s error=%s", name, exc)
                    results[name] = error_result(name, str(exc))

        self._lookup_cache[cache_key] = results
        logger.info("Lookup batch completed | providers=%s", list(results))
        return results

    def lookup_one(self, provider_name: str, subject: SearchSubject) -> ProviderResult:
        provider = self.providers.get(provider_name)
        if not provider:
            return error_result(provider_name, f"Unknown provider: {provider_name}")
        if not getattr(self.settings.providers, provider_name).enabled:
            return error_result(provider_name, "Provider is disabled in configuration")
        logger.info("Single provider lookup started | provider=%s cnic=%s mobile=%s imei=%s lat=%s lng=%s", provider_name, subject.cnic, subject.mobile, subject.imei, subject.latitude, subject.longitude)
        result = provider.lookup(subject)
        logger.info("Single provider lookup completed | provider=%s status=%s hit=%s summary=%s", provider_name, result.status, result.hit, result.summary)
        return result

    def _subject_cache_key(self, subject: SearchSubject) -> tuple[Any, ...]:
        imei = "".join(ch for ch in str(subject.imei or "") if ch.isdigit())
        lat = round(float(subject.latitude), 4) if subject.latitude is not None else None
        lng = round(float(subject.longitude), 4) if subject.longitude is not None else None
        return (
            normalize_cnic(subject.cnic) or "",
            normalize_mobile(subject.mobile) or "",
            imei,
            lat,
            lng,
        )
