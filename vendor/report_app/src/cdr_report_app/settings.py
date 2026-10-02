"""Application settings and config loading."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import dotenv_values
from pydantic import BaseModel, Field
import yaml


class AppSettings(BaseModel):
    name: str = "CDR Report App"
    environment: str = "development"
    output_dir: Path = Path("output")
    log_dir: Path = Path("logs")
    log_level: str = "INFO"
    log_to_file: bool = True
    request_timeout_seconds: int = 30


class ReportSettings(BaseModel):
    title: str = "Single CDR Analysis Report"
    language: str = "roman_urdu"
    top_contacts_count: int = 5
    top_locations_count: int = 5
    top_short_codes_count: int = 5
    late_night_start: int = 23
    late_night_end: int = 5
    short_call_threshold: int = 10
    burst_window_minutes: int = 30
    burst_threshold: int = 5
    new_number_lookback_days: int = 7
    multi_timeline_events_per_pair: int | None = None
    # Crime-proximity tower analysis thresholds (all configurable)
    crime_proximity_radius_meters: int = 1000   # tower must be within this distance of crime location
    crime_before_window_minutes: int = 60        # how far before crime time to look
    crime_during_tolerance_minutes: int = 15     # ± around crime time = "during" window
    crime_after_window_minutes: int = 60         # how far after crime time to look


class SectionSettings(BaseModel):
    cdr_maloomat: bool = True
    jurm_ki_maloomat: bool = True
    rozana_ki_sargarmi: bool = True
    zyada_waqt_guzarne_wali_jagahein: bool = True
    lambe_qayam_ki_jaga: bool = True
    heatmap: bool = True
    jurm_ke_din_movement_path: bool = True
    map: bool = True
    burst_activity: bool = True
    short_codes: bool = True
    imei_imsi: bool = True
    top_contacts: bool = True
    db_verification: bool = True
    top_contacts_history: bool = True


class AttachmentSettings(BaseModel):
    embed_cro_pdf: bool = True
    embed_fir_reports: bool = True
    preserve_external_pdf_pages: bool = True
    prefer_rendered_fir_pdf: bool = True
    include_custom_fir_summary: bool = False


class CrimeSettings(BaseModel):
    fir_no: str | None = None
    police_station: str | None = None
    sections_of_law: str | None = None
    crime_date: str | None = None
    crime_time: str | None = None
    crime_place: str | None = None
    crime_lat: str | None = None
    crime_lng: str | None = None


def _assemble_url(base: str | None, path: str | None) -> str | None:
    if not base or not path:
        return None
    return base.rstrip("/") + "/" + path.lstrip("/")


class ShieldConfig(BaseModel):
    """Shared Bearer-token auth config for SPAG-protected services."""
    base_url: str | None = None
    email: str | None = None
    password: str | None = None
    cookie: str | None = None

    @property
    def login_url(self) -> str | None:
        if not self.base_url:
            return None
        parsed = urlparse(self.base_url)
        host_only = f"{parsed.scheme}://{parsed.netloc}"
        return _assemble_url(host_only, "/auth/login")

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.email and self.password)


class CallerIdSettings(BaseModel):
    """Truecaller-style caller-id lookup (rate-limited, multi-key)."""
    enabled: bool = False
    base_url: str | None = None
    api_keys: list[str] = Field(default_factory=list)
    delay_seconds: float = 1.5
    timeout_seconds: int = 20
    lookup_top_contacts: int = 5

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.base_url and self.api_keys)


class ProviderConfig(BaseModel):
    enabled: bool = True
    base_url: str | None = None
    api_key: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ProviderSettings(BaseModel):
    simsdb: ProviderConfig = Field(default_factory=ProviderConfig)
    subscriber: ProviderConfig = Field(default_factory=ProviderConfig)
    prvs: ProviderConfig = Field(default_factory=ProviderConfig)
    cro: ProviderConfig = Field(default_factory=ProviderConfig)
    psrms: ProviderConfig = Field(default_factory=ProviderConfig)
    watchlist: ProviderConfig = Field(default_factory=ProviderConfig)
    nearest_ps: ProviderConfig = Field(default_factory=ProviderConfig)
    nadra: ProviderConfig = Field(default_factory=ProviderConfig)
    cfms: ProviderConfig = Field(default_factory=ProviderConfig)
    hotel_eye: ProviderConfig = Field(default_factory=ProviderConfig)
    sbvs: ProviderConfig = Field(default_factory=ProviderConfig)
    pfc: ProviderConfig = Field(default_factory=ProviderConfig)
    hrmis: ProviderConfig = Field(default_factory=ProviderConfig)
    igp_cms: ProviderConfig = Field(default_factory=ProviderConfig)
    imei: ProviderConfig = Field(default_factory=ProviderConfig)
    evs: ProviderConfig = Field(default_factory=ProviderConfig)
    hope: ProviderConfig = Field(default_factory=ProviderConfig)
    dls: ProviderConfig = Field(default_factory=ProviderConfig)
    tracs: ProviderConfig = Field(default_factory=ProviderConfig)
    old_tenant: ProviderConfig = Field(default_factory=ProviderConfig)
    trust: ProviderConfig = Field(default_factory=ProviderConfig)
    milap: ProviderConfig = Field(default_factory=ProviderConfig)


class Settings(BaseModel):
    app: AppSettings = Field(default_factory=AppSettings)
    report: ReportSettings = Field(default_factory=ReportSettings)
    sections: SectionSettings = Field(default_factory=SectionSettings)
    attachments: AttachmentSettings = Field(default_factory=AttachmentSettings)
    crime: CrimeSettings = Field(default_factory=CrimeSettings)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    caller_id: CallerIdSettings = Field(default_factory=CallerIdSettings)
    shield: ShieldConfig = Field(default_factory=ShieldConfig)
    config_path: Path | None = None
    env_file: Path | None = None


PROVIDER_REQUIREMENTS: dict[str, list[str]] = {
    "simsdb": ["base_url", "api_key"],
    "subscriber": ["api_key", "extra.cookie", "extra.mobile_url", "extra.cnic_url"],
    "prvs": ["api_key", "extra.mobile_url", "extra.cnic_url"],
    "cro": ["base_url", "api_key"],
    "psrms": ["base_url", "api_key", "extra.personsearch_cookie", "extra.fir_report_url", "extra.fir_cookie"],
    "watchlist": ["base_url", "api_key"],
    "nearest_ps": ["base_url"],
    "nadra": ["extra.verify_url", "extra.archive_url", "extra.autokenn", "extra.secrt"],
    "cfms": ["base_url"],
    "hotel_eye": ["api_key", "extra.guest_url", "extra.simple_url"],
    "sbvs": ["api_key", "extra.cnic_url", "extra.mobile_url"],
    "pfc": ["base_url", "api_key"],
    "hrmis": ["base_url", "api_key", "extra.auth_token"],
    "igp_cms": ["base_url", "api_key"],
    "imei": ["base_url"],
    "evs": ["base_url", "extra.i_key", "extra.j_key", "extra.api_token"],
    "hope": ["extra.employee_url", "extra.i_key", "extra.j_key", "extra.api_token"],
    "dls": ["base_url", "extra.login_url", "extra.username", "extra.password", "extra.seed_token"],
    "tracs": ["base_url", "extra.auth_token"],
    "old_tenant": ["base_url", "api_key"],
    "trust": ["base_url", "api_key"],
    "milap": ["base_url", "api_key"],
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def discover_env_file(project_root: Path) -> Path | None:
    root_env = project_root / ".env"
    if root_env.exists():
        return root_env

    legacy_env = project_root / "venv" / ".env"
    if legacy_env.exists():
        return legacy_env

    return None


def _provider_overrides(env_map: dict[str, str]) -> dict[str, Any]:
    shield_base = env_map.get("SHIELD_BASE_URL")

    def _url(path_key: str, legacy_url_key: str | None = None) -> str | None:
        """Assemble SHIELD-prefixed URL from {path_key}; fall back to full {legacy_url_key}."""
        path = env_map.get(path_key)
        if path and shield_base:
            return _assemble_url(shield_base, path)
        return env_map.get(legacy_url_key) if legacy_url_key else None

    return {
        "shield": {
            "base_url": shield_base,
            "email": env_map.get("SHIELD_EMAIL"),
            "password": env_map.get("SHIELD_PASSWORD"),
            "cookie": env_map.get("SHIELD_COOKIE"),
        },
        "providers": {
            "cro": {
                "base_url": _url("CRO_PATH", "CRO_API_URL"),
                "api_key": env_map.get("CRO_API_KEY"),
                "extra": {
                    "report_api_key": env_map.get("CRO_REPORT_API_KEY"),
                    "report_url": env_map.get("CRO_REPORT_URL", "https://safe.sindhpolice.gov.pk/api/cro-report-pdf"),
                },
            },
            "psrms": {
                "base_url": _url("PSRMS_PERSONSEARCH_PATH", "PSRMS_PERSONSEARCH_URL")
                            or env_map.get("PSRMS_API_URL"),
                "api_key": env_map.get("PSRMS_API_KEY"),
                "extra": {
                    "fir_report_url": _url("PSRMS_FIR_PATH", "PSRMS_FIR_REPORT_URL"),
                    "personsearch_cookie": env_map.get("PSRMS_PERSONSEARCH_COOKIE") or env_map.get("PSRMS_COOKIE"),
                    "fir_cookie": env_map.get("PSRMS_FIR_COOKIE") or env_map.get("PSRMS_COOKIE"),
                    "chrome_bin": env_map.get("CHROME_BIN"),
                },
            },
            "simsdb": {
                "base_url": env_map.get("SIMSDB_API_URL"),
                "api_key": env_map.get("SIMSDB_APPKEY"),
                "extra": {
                    "user_agent": env_map.get("SIMSDB_USER_AGENT"),
                },
            },
            "subscriber": {
                "api_key": env_map.get("SUBSCRIBER_API_KEY"),
                "extra": {
                    "cookie": env_map.get("SUBSCRIBER_COOKIE"),
                    "cnic_url": _url("SUBSCRIBER_CNIC_PATH", "SUBSCRIBER_CNIC_URL"),
                    "mobile_url": _url("SUBSCRIBER_MOBILE_PATH", "SUBSCRIBER_MOBILE_URL"),
                },
            },
            "prvs": {
                "api_key": env_map.get("PRVS_API_KEY"),
                "extra": {
                    "cnic_url": _url("PRVS_CNIC_PATH", "PRVS_CNIC_URL"),
                    "mobile_url": _url("PRVS_MOBILE_PATH", "PRVS_MOBILE_URL"),
                    "cnic_password": env_map.get("PRVS_CNIC_PASSWORD"),
                    "mobile_password": env_map.get("PRVS_MOBILE_PASSWORD"),
                },
            },
            "watchlist": {
                "base_url": _url("WATCHLIST_PATH", "WATCHLIST_API_URL"),
                "api_key": env_map.get("WATCHLIST_API_KEY"),
            },
            "nearest_ps": {
                "base_url": _url("NEAREST_PS_PATH", "NEAREST_PS_API_URL"),
            },
            "nadra": {
                "extra": {
                    "verify_url": env_map.get("NADRA_VERIFY_URL"),
                    "archive_url": env_map.get("NADRA_ARCHIVE_URL"),
                    "autokenn": env_map.get("NADRA_AUTOKENN"),
                    "secrt": env_map.get("NADRA_SECRT"),
                },
            },
            "cfms": {
                "base_url": _url("CFMS_PATH", "CFMS_API_URL"),
            },
            "hotel_eye": {
                "api_key": env_map.get("HOTEL_EYE_API_KEY"),
                "extra": {
                    "guest_url": _url("HOTEL_EYE_GUEST_PATH", "HOTEL_EYE_GUEST_URL"),
                    "simple_url": _url("HOTEL_EYE_SIMPLE_PATH", "HOTEL_EYE_SIMPLE_URL"),
                },
            },
            "sbvs": {
                "api_key": env_map.get("SBVS_API_KEY"),
                "extra": {
                    "cnic_url": _url("SBVS_CNIC_PATH", "SBVS_CNIC_URL"),
                    "mobile_url": _url("SBVS_MOBILE_PATH", "SBVS_MOBILE_URL"),
                },
            },
            "pfc": {
                "base_url": _url("PFC_PATH", "PFC_API_URL"),
                "api_key": env_map.get("PFC_API_KEY"),
            },
            "hrmis": {
                "base_url": _url("HRMIS_PATH", "HRMIS_API_URL"),
                "api_key": env_map.get("HRMIS_API_KEY"),
                "extra": {
                    "auth_token": env_map.get("HRMIS_AUTH_TOKEN"),
                },
            },
            "igp_cms": {
                "base_url": _url("IGP_CMS_PATH", "IGP_CMS_API_URL"),
                "api_key": env_map.get("IGP_CMS_API_KEY"),
            },
            "imei": {
                "base_url": env_map.get("IMEI_API_URL"),
            },
            "evs": {
                "base_url": _url("EVS_PATH", "EVS_API_URL"),
                "extra": {
                    "i_key": env_map.get("EVS_I_KEY"),
                    "j_key": env_map.get("EVS_J_KEY"),
                    "api_token": env_map.get("EVS_API_TOKEN"),
                },
            },
            "hope": {
                "extra": {
                    "employee_url": _url("HOPE_EMPLOYEE_PATH", "HOPE_EMPLOYEE_URL"),
                    "employer_url": _url("HOPE_EMPLOYER_PATH", "HOPE_EMPLOYER_URL"),
                    "i_key": env_map.get("HOPE_I_KEY"),
                    "j_key": env_map.get("HOPE_J_KEY"),
                    "api_token": env_map.get("HOPE_API_TOKEN"),
                },
            },
            "dls": {
                "base_url": _url("DLS_PATH", "DLS_API_URL"),
                "extra": {
                    "login_url": _url("DLS_LOGIN_PATH", "DLS_LOGIN_URL"),
                    "username": env_map.get("DLS_USERNAME"),
                    "password": env_map.get("DLS_PASSWORD"),
                    "seed_token": env_map.get("DLS_SEED_TOKEN"),
                },
            },
            "tracs": {
                "base_url": _url("TRACS_PATH", "TRACS_API_URL"),
                "extra": {
                    "auth_token": env_map.get("TRACS_AUTH_TOKEN"),
                },
            },
            "old_tenant": {
                "base_url": _url("OLD_TENANT_PATH", "OLD_TENANT_API_URL"),
                "api_key": env_map.get("OLD_TENANT_API_KEY"),
            },
            "trust": {
                "base_url": _url("TRUST_PATH", "TRUST_API_URL"),
                "api_key": env_map.get("TRUST_API_KEY"),
            },
            "milap": {
                "base_url": _url("MILAP_LOST_RECORDS_PATH", "MILAP_LOST_RECORDS_URL")
                            or _url("MILAP_LOST_PERSONS_PATH", "MILAP_LOST_PERSONS_URL"),
                "api_key": env_map.get("MILAP_API_KEY"),
                "extra": {
                    "lost_persons_url": _url("MILAP_LOST_PERSONS_PATH", "MILAP_LOST_PERSONS_URL"),
                    "lost_records_url": _url("MILAP_LOST_RECORDS_PATH", "MILAP_LOST_RECORDS_URL"),
                },
            },
        }
    }


def load_settings(
    config_path: Path | None = None,
    env_file: Path | None = None,
    output_override: Path | None = None,
) -> Settings:
    project_root = Path(__file__).resolve().parents[2]
    selected_config = config_path or Path(os.environ.get("CDR_REPORT_CONFIG", "configs/default.yaml"))
    if not selected_config.is_absolute():
        selected_config = project_root / selected_config

    selected_env = env_file or discover_env_file(project_root)
    env_map: dict[str, str] = {}
    if selected_env and selected_env.exists():
        env_map = {
            key: value
            for key, value in dotenv_values(selected_env).items()
            if value is not None
        }

    yaml_data = _read_yaml(selected_config)
    merged = _deep_merge(yaml_data, _provider_overrides({**env_map, **os.environ}))

    if "app" not in merged:
        merged["app"] = {}

    merged["app"]["environment"] = os.environ.get(
        "CDR_REPORT_ENV",
        env_map.get("CDR_REPORT_ENV", merged["app"].get("environment", "development")),
    )
    merged["app"]["output_dir"] = str(
        output_override
        or os.environ.get("CDR_REPORT_OUTPUT_DIR")
        or env_map.get("CDR_REPORT_OUTPUT_DIR")
        or merged["app"].get("output_dir", "output")
    )
    merged["app"]["log_dir"] = str(
        os.environ.get("CDR_REPORT_LOG_DIR")
        or env_map.get("CDR_REPORT_LOG_DIR")
        or merged["app"].get("log_dir", "logs")
    )
    merged["app"]["log_level"] = str(
        os.environ.get("CDR_REPORT_LOG_LEVEL")
        or env_map.get("CDR_REPORT_LOG_LEVEL")
        or merged["app"].get("log_level", "INFO")
    )
    merged["app"]["log_to_file"] = str(
        os.environ.get("CDR_REPORT_LOG_TO_FILE")
        or env_map.get("CDR_REPORT_LOG_TO_FILE")
        or merged["app"].get("log_to_file", "true")
    ).strip().lower() not in {"0", "false", "no", "off"}
    merged["app"]["request_timeout_seconds"] = int(
        os.environ.get("CDR_REPORT_HTTP_TIMEOUT")
        or env_map.get("CDR_REPORT_HTTP_TIMEOUT")
        or merged["app"].get("request_timeout_seconds", 30)
    )

    env_lookup = {**env_map, **os.environ}
    merged["caller_id"] = _caller_id_overrides(env_lookup, merged.get("caller_id", {}))

    settings = Settings.model_validate(merged)
    settings.config_path = selected_config
    settings.env_file = selected_env
    return settings


def _caller_id_overrides(env_map: dict[str, str], base: dict[str, Any]) -> dict[str, Any]:
    def _flag(value: object, default: bool) -> bool:
        if value is None:
            return default
        return str(value).strip().lower() not in {"0", "false", "no", "off", ""}

    keys_raw = env_map.get("CALLER_ID_API_KEYS") or ""
    api_keys = [part.strip() for part in keys_raw.split(",") if part.strip()] or base.get("api_keys", [])

    return {
        "enabled": _flag(env_map.get("CALLER_ID_ENABLED"), base.get("enabled", False)),
        "base_url": env_map.get("CALLER_ID_BASE_URL") or base.get("base_url"),
        "api_keys": api_keys,
        "delay_seconds": float(env_map.get("CALLER_ID_DELAY_SECONDS") or base.get("delay_seconds", 1.5)),
        "timeout_seconds": int(env_map.get("CALLER_ID_TIMEOUT_SECONDS") or base.get("timeout_seconds", 20)),
        "lookup_top_contacts": int(env_map.get("CALLER_ID_TOP_CONTACTS") or base.get("lookup_top_contacts", 5)),
    }


def _resolve_provider_field(provider_cfg: ProviderConfig, field_path: str) -> Any:
    current: Any = provider_cfg
    for part in field_path.split("."):
        if isinstance(current, BaseModel):
            current = getattr(current, part, None)
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def provider_readiness(settings: Settings) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for provider_name, required_fields in PROVIDER_REQUIREMENTS.items():
        provider_cfg = getattr(settings.providers, provider_name)
        missing = []
        for field in required_fields:
            value = _resolve_provider_field(provider_cfg, field)
            if not value:
                missing.append(field)
        rows.append(
            {
                "provider": provider_name,
                "enabled": provider_cfg.enabled,
                "ready": provider_cfg.enabled and not missing,
                "missing": missing,
            }
        )
    return rows
