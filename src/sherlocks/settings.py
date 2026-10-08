"""Configuration for Sherlocks.

Layered the same way ``cdr_report_app.settings`` does it, so operators only have to
learn one pattern: Pydantic defaults, then ``configs/default.yaml``, then a ``.env``
file, then real process environment variables. Later layers win.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field, field_validator

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent


class AppSettings(BaseModel):
    name: str = "Sherlocks"
    environment: str = "development"
    output_dir: str = "output"
    log_dir: str = "logs"
    log_level: str = "INFO"
    # cdr_report_app ships no pyproject, so it cannot be pip-installed. Sherlocks reuses
    # its PDF engine (Urdu glyph shaping, bundled fonts) and the Demo data source from a
    # copy kept in this project, vendor/report_app/src - nothing outside the project.
    report_app_src: str | None = str(PROJECT_ROOT / "vendor" / "report_app" / "src")


class DatabaseSettings(BaseModel):
    url: str = "postgresql://cdr_user:cdr_pass@localhost:5432/cdr_jobs"
    # Sherlocks owns a dedicated Postgres schema rather than a dedicated database, so
    # it can be deployed without CREATEDB privileges. Point ``url`` at a database of
    # its own later and this still works unchanged.
    schema_name: str = "sherlocks"
    pool_size: int = 5
    max_overflow: int = 10
    echo: bool = False

    @field_validator("url")
    @classmethod
    def _pin_driver(cls, value: str) -> str:
        """Name the driver Sherlocks ships (psycopg2). A bare ``postgresql://`` means
        psycopg2 in SQLAlchemy 2.0 but psycopg 3 from 2.1, which is not installed - so
        the same URL would work in one build and crash the next."""
        for bare in ("postgresql://", "postgres://"):
            if value.startswith(bare):
                return "postgresql+psycopg2://" + value[len(bare):]
        return value


class OllamaSettings(BaseModel):
    """Local LLM. Nothing here ever leaves the network."""

    base_url: str = "http://localhost:11434"
    # Reasoning model: planning an OSINT run, reading noisy results.
    reasoning_model: str = "qwen3.5:4b"
    # Bulk model: high-volume structured extraction. Same model until a larger one
    # is pulled; split so the two can diverge without touching call sites.
    bulk_model: str = "qwen3.5:4b"
    timeout_seconds: int = 180
    # A small model fails schema validation regularly. Retrying is normal, not exceptional.
    max_attempts: int = 3
    temperature: float = 0.0
    num_ctx: int = 8192
    enabled: bool = True

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.base_url)


class LlmSettings(BaseModel):
    """Which model the AI steps use.

    ``openai`` = any OpenAI-compatible server (vLLM, LM Studio, llama.cpp) - in this
    deployment the Qwen 27B vLLM server on the office LAN. ``ollama`` = the local Ollama
    in ``ollama:``. Either way the model sits on the local network; there is no cloud
    path, and adding one would break the constraint the design rests on.
    """

    provider: Literal["openai", "ollama"] = "ollama"
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = "EMPTY"
    timeout_seconds: int = 60
    max_attempts: int = 2
    temperature: float = 0.0
    max_tokens: int = 800
    enabled: bool = True
    # The served model is a vision-language model (the office Qwen 27B is): it reads
    # scanned report pages - Urdu and English - so no separate OCR engine is needed.
    vision: bool = True

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.base_url and self.model)


class OsintSettings(BaseModel):
    """OpenOSINT bridge.

    Disabled by default: these tools reach out to the public internet on behalf of an
    investigation, which is a policy decision, not a default.
    """

    enabled: bool = False
    # Hard allowlist. A tool absent from this list can never be run, whatever an
    # agent decides it wants.
    allowed_tools: list[str] = Field(
        default_factory=lambda: [
            "search_phone",
            "search_username",
            "search_email",
            "search_github",
            "search_paste",
            "search_breach",
            "search_social",
            "search_apify",
            "search_socialcrawl",
            "search_pipl",
            "search_pipl_email",
            "search_fullcontact",
            "search_fullcontact_phone",
            "search_hunter",
            "search_enformion",
            "search_footprint",
            "search_dorks_live",
            "generate_dorks",
            "scrape_url",
        ]
    )
    per_tool_timeout_seconds: int = 120
    delay_between_calls_seconds: float = 1.0
    max_tools_per_run: int = 12
    # Bright Data backs the only tools that meaningfully find a Pakistani citizen's
    # public footprint. Without it those tools are skipped, not failed.
    brightdata_api_key: str | None = None
    # Free name search (DuckDuckGo, then Bing) for social profiles when there is no Bright
    # Data key. Sends the person's name and city to those engines - a policy choice, on by
    # default because the analysts asked for name-based profile search; turn off in .env.
    free_web_search: bool = True
    web_search_delay_seconds: float = 3.0
    web_search_queries_per_person: int = 3
    brightdata_serp_zone: str | None = None
    brightdata_unlocker_zone: str | None = None
    hibp_api_key: str | None = None
    github_token: str | None = None
    ipinfo_token: str | None = None
    # Apify scraping platform (paid). Actor + input field are configurable because
    # each Apify actor takes a different input shape.
    apify_token: str | None = None
    apify_actor: str = "apify/instagram-scraper"
    apify_input_field: str = "usernames"
    # Paid person-data APIs (reference/OSINT_APIS.md). Blank = that tool is skipped.
    socialcrawl_api_key: str | None = None
    # Platforms SocialCrawl checks per username; each one costs credits.
    socialcrawl_platforms: list[str] = Field(
        default_factory=lambda: ["instagram", "facebook", "tiktok", "twitter", "youtube", "linkedin"])
    pipl_api_key: str | None = None
    fullcontact_api_key: str | None = None
    hunter_api_key: str | None = None
    enformion_api_key: str | None = None       # EnformionGO profile password (galaxy-ap-password)
    enformion_api_profile: str | None = None   # EnformionGO profile name (galaxy-ap-name)
    # Every email found (records or OSINT) is searched online, up to this many per person.
    email_pivots: int = 3
    # No email known: find the person by name on Pipl / Hunter, kept only when the
    # records corroborate the match.
    name_match: bool = True

    @property
    def brightdata_ready(self) -> bool:
        return bool(self.brightdata_api_key and self.brightdata_serp_zone)


class LinkGraphSettings(BaseModel):
    """The person link graph.

    ``backend`` defaults to ``demo``: a first click in a fresh deployment should not fire
    twenty logged queries at live police systems. Set ``live`` deliberately.
    """

    backend: Literal["live", "demo", "ems"] = "demo"
    # Let the portal pick demo or live per run. Turn off to pin a deployment to one.
    allow_backend_override: bool = True
    # Report_App's .env (provider URLs, keys, Shield login). None = Report_App's own
    # discovery, which finds <Report_App>/.env next to its configs.
    report_app_env: str | None = None
    default_depth: int = 2
    max_depth: int = 4
    default_max_persons: int = 25
    hard_max_persons: int = 100
    # Systems queried concurrently for one person, and people searched concurrently.
    parallel_systems: int = 6
    parallel_persons: int = 2
    # A person's SIMs are ALL searched through the phone-driven systems (a licence, a
    # hotel stay, or a different registered owner can sit on any number). This caps how
    # many numbers per person so the live-query fan-out stays bounded; raise for full
    # coverage of heavy SIM-holders. 1 = first number only.
    max_numbers_per_person: int = 8
    seed_phone_limit: int = 3  # deprecated; kept so old configs still validate
    include_fir_rosters: bool = True
    max_firs_per_person: int = 6
    cache_ttl_hours: int = 24
    # A phone/CNIC held by more than this many people is a shared/office line; its
    # shared-identifier links are dropped so it cannot fabricate a hairball.
    shared_identifier_max: int = 6
    # Weak links below this combined score are not drawn.
    weak_min_score: float = 0.35
    # LLM address normalisation for borderline address pairs; capped per run.
    ai_address_calls_per_run: int = 40
    # LLM review of candidate person pairs the rules could not settle; capped per run.
    ai_pair_calls_per_run: int = 30
    image_dir: str = "output/images"


class ApiSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 7401
    prefix: str = "/sindhpolice-sherlocks"
    api_key: str | None = None
    cors_origins: str = "*"
    max_concurrent_jobs: int = 4


class AuthSettings(BaseModel):
    """Portal sign-in. Credentials live in .env, never in the repo or the YAML."""

    enabled: bool = True
    username: str = "Admin"
    password: str | None = None          # SHERLOCKS_AUTH_PASSWORD
    password_sha256: str | None = None   # or a hash, if you prefer not to store the text
    session_hours: int = 12
    secret: str | None = None            # signs session tokens; random per boot if unset


class ChatSettings(BaseModel):
    """How long each step of a chat reply may take (seconds), and how big the context
    pack may be. A step over budget falls back (templates, the board, rules only) and
    what finishes late is sent as a follow-up message - partial beats late."""

    reply_budget_s: float = 40.0      # the whole reply, never longer
    draft_budget_s: float = 10.0      # O1 understanding + drafting (one model call)
    tools_budget_s: float = 10.0      # toolbox / live calls, together, in parallel
    view_budget_s: float = 10.0       # Sherlock's quick view (one model call, no tools)
    investigate_budget_s: float = 25.0  # the investigator's tool loop in a reply
    check_budget_s: float = 6.0       # the Answer checker's model check
    revise_budget_s: float = 6.0      # O1 fixing a draft once
    pack_chars: int = 14000           # the context pack, all parts together
    recent_turns: int = 4             # chat turns given word for word; older ones summarised


class EvidenceSettings(BaseModel):
    """Case evidence read while a graph builds: the FIR document of every FIR found
    (PSRMS file report), the forensic lab reports filed against it (DNA, chemical,
    FSL, medico-legal), and the CRO dossier PDF of every criminal record number.

    Each document is fetched once per run (and cached like any provider answer), read
    into facts that quote it, and kept with the graph as the case file the chat and
    the case report cite."""

    enabled: bool = True
    # Per run. Each FIR document, lab report and CRO dossier is one upstream query.
    max_documents: int = 40
    lab_reports: bool = True
    cro_dossiers: bool = True
    # Read scanned pages (lab reports, dossiers) with the vision model (llm.vision).
    ocr: bool = True
    # The AI reader writes facts from narratives, case diaries and report text, each
    # with a verbatim quote that is checked against the document.
    ai_reader: bool = True
    ai_reader_calls_per_run: int = 30
    # Write the case report (assessments, linkages) as soon as the graph finishes or is stopped.
    auto_report: bool = True
    # The chat may call police systems itself (FIR document, lab reports, CRO dossier,
    # one system lookup) to check a lead - at most this many live calls per question.
    chat_live_calls: int = 6
    # Live calls one case may make in all, and per hour (every agent, every team). Over the
    # limit the calls stop and the officer is told; near the hourly limit admins are warned.
    case_live_calls: int = 120
    case_live_calls_per_hour: int = 40
    # Live calls the Sherlock team may make in one background run.
    background_live_calls: int = 4
    # Background runs of the Sherlock team per case (the report and the officer can always
    # ask for one more).
    sherlock_runs_per_case: int = 20
    # On a report download, how long Sherlock may take to bring his assessment up to date
    # first; past it the report is built from his last assessment and says "as of".
    report_wait_s: float = 90.0
    # Uploads (image, PDF, Word, Excel) per file.
    max_upload_mb: int = 25
    # Send an uploaded CDR / BTS file to the CDR server for its analysis (its report also
    # looks up top contacts in the police systems - live queries). false = own analysis only.
    cdr_auto_analyze: bool = True
    # "Near the incident": towers within this distance of the pinned point.
    cdr_radius_km: float = 2.0
    # Map tiles for pinning the incident ({z}/{x}/{y}); a tile server on the police network
    # when the officers' browsers have no internet.
    map_tiles: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


class Settings(BaseModel):
    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    osint: OsintSettings = Field(default_factory=OsintSettings)
    linkgraph: LinkGraphSettings = Field(default_factory=LinkGraphSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    evidence: EvidenceSettings = Field(default_factory=EvidenceSettings)
    chat: ChatSettings = Field(default_factory=ChatSettings)
    config_path: str | None = None
    env_file: str | None = None

    @property
    def output_path(self) -> Path:
        path = Path(self.app.output_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if value is None:
            continue
        if isinstance(value, dict):
            # Recurse even when the base has no such section, so unset (None) env
            # overrides inside it are dropped instead of overwriting model defaults.
            base_section = merged.get(key)
            merged[key] = _deep_merge(base_section if isinstance(base_section, dict) else {}, value)
        else:
            merged[key] = value
    return merged


def discover_env_file(explicit: str | None = None) -> Path | None:
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.exists() else None
    for candidate in (PROJECT_ROOT / ".env", Path.cwd() / ".env"):
        if candidate.exists():
            return candidate
    return None


def _as_bool(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _as_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


def _env_overrides(env: dict[str, str]) -> dict[str, Any]:
    """Map flat environment keys onto the nested settings tree."""

    def get(key: str) -> str | None:
        raw = env.get(key)
        return raw.strip() if isinstance(raw, str) and raw.strip() else None

    return {
        "app": {
            "environment": get("SHERLOCKS_ENV"),
            "output_dir": get("SHERLOCKS_OUTPUT_DIR"),
            "log_dir": get("SHERLOCKS_LOG_DIR"),
            "log_level": get("SHERLOCKS_LOG_LEVEL"),
            "report_app_src": get("SHERLOCKS_REPORT_APP_SRC"),
        },
        "database": {
            "url": get("SHERLOCKS_DATABASE_URL") or get("DATABASE_URL"),
            "schema_name": get("SHERLOCKS_DB_SCHEMA"),
            "echo": _as_bool(get("SHERLOCKS_DB_ECHO")),
        },
        "ollama": {
            "base_url": get("OLLAMA_BASE_URL"),
            "reasoning_model": get("SHERLOCKS_REASONING_MODEL"),
            "bulk_model": get("SHERLOCKS_BULK_MODEL"),
            "timeout_seconds": _as_int(get("SHERLOCKS_OLLAMA_TIMEOUT")),
            "max_attempts": _as_int(get("SHERLOCKS_OLLAMA_MAX_ATTEMPTS")),
            "temperature": _as_float(get("SHERLOCKS_OLLAMA_TEMPERATURE")),
            "num_ctx": _as_int(get("SHERLOCKS_OLLAMA_NUM_CTX")),
            "enabled": _as_bool(get("SHERLOCKS_OLLAMA_ENABLED")),
        },
        "llm": {
            "provider": get("SHERLOCKS_LLM_PROVIDER"),
            "base_url": get("SHERLOCKS_LLM_BASE_URL"),
            "model": get("SHERLOCKS_LLM_MODEL"),
            "api_key": get("SHERLOCKS_LLM_API_KEY"),
            "timeout_seconds": _as_int(get("SHERLOCKS_LLM_TIMEOUT")),
            "enabled": _as_bool(get("SHERLOCKS_LLM_ENABLED")),
            "vision": _as_bool(get("SHERLOCKS_LLM_VISION")),
        },
        "evidence": {
            "enabled": _as_bool(get("SHERLOCKS_EVIDENCE_ENABLED")),
            "max_documents": _as_int(get("SHERLOCKS_EVIDENCE_MAX_DOCUMENTS")),
            "lab_reports": _as_bool(get("SHERLOCKS_EVIDENCE_LAB_REPORTS")),
            "cro_dossiers": _as_bool(get("SHERLOCKS_EVIDENCE_CRO_DOSSIERS")),
            "ocr": _as_bool(get("SHERLOCKS_EVIDENCE_OCR")),
            "ai_reader": _as_bool(get("SHERLOCKS_EVIDENCE_AI_READER")),
            "auto_report": _as_bool(get("SHERLOCKS_EVIDENCE_AUTO_REPORT")),
            "chat_live_calls": _as_int(get("SHERLOCKS_EVIDENCE_CHAT_LIVE_CALLS")),
            "max_upload_mb": _as_int(get("SHERLOCKS_UPLOAD_MAX_MB")),
            "cdr_auto_analyze": _as_bool(get("SHERLOCKS_CDR_AUTO_ANALYZE")),
            "cdr_radius_km": _as_float(get("SHERLOCKS_CDR_RADIUS_KM")),
            "map_tiles": get("SHERLOCKS_MAP_TILES"),
        },
        "osint": {
            "enabled": _as_bool(get("SHERLOCKS_OSINT_ENABLED")),
            "allowed_tools": _as_list(get("SHERLOCKS_OSINT_ALLOWED_TOOLS")),
            "per_tool_timeout_seconds": _as_int(get("SHERLOCKS_OSINT_TOOL_TIMEOUT")),
            "delay_between_calls_seconds": _as_float(get("SHERLOCKS_OSINT_DELAY")),
            "max_tools_per_run": _as_int(get("SHERLOCKS_OSINT_MAX_TOOLS")),
            "brightdata_api_key": get("BRIGHTDATA_API_KEY"),
            "free_web_search": _as_bool(get("SHERLOCKS_OSINT_FREE_WEB_SEARCH")),
            "web_search_delay_seconds": _as_float(get("SHERLOCKS_OSINT_WEB_SEARCH_DELAY")),
            "web_search_queries_per_person": _as_int(get("SHERLOCKS_OSINT_WEB_SEARCH_QUERIES")),
            "brightdata_serp_zone": get("BRIGHTDATA_SERP_ZONE"),
            "brightdata_unlocker_zone": get("BRIGHTDATA_UNLOCKER_ZONE"),
            "hibp_api_key": get("HIBP_API_KEY"),
            "github_token": get("GITHUB_TOKEN"),
            "ipinfo_token": get("IPINFO_TOKEN"),
            "apify_token": get("APIFY_TOKEN"),
            "apify_actor": get("SHERLOCKS_OSINT_APIFY_ACTOR"),
            "apify_input_field": get("SHERLOCKS_OSINT_APIFY_INPUT_FIELD"),
            "socialcrawl_api_key": get("SOCIALCRAWL_API_KEY"),
            "socialcrawl_platforms": _as_list(get("SHERLOCKS_OSINT_SOCIALCRAWL_PLATFORMS")),
            "pipl_api_key": get("PIPL_API_KEY"),
            "fullcontact_api_key": get("FULLCONTACT_API_KEY"),
            "hunter_api_key": get("HUNTER_API_KEY"),
            "enformion_api_key": get("ENFORMION_API_KEY"),
            "enformion_api_profile": get("ENFORMION_API_PROFILE"),
            "email_pivots": _as_int(get("SHERLOCKS_OSINT_EMAIL_PIVOTS")),
            "name_match": _as_bool(get("SHERLOCKS_OSINT_NAME_MATCH")),
        },
        "linkgraph": {
            "backend": get("SHERLOCKS_LINKGRAPH_BACKEND"),
            "allow_backend_override": _as_bool(get("SHERLOCKS_LINKGRAPH_ALLOW_OVERRIDE")),
            "report_app_env": get("SHERLOCKS_REPORT_APP_ENV"),
            "max_depth": _as_int(get("SHERLOCKS_LINKGRAPH_MAX_DEPTH")),
            "hard_max_persons": _as_int(get("SHERLOCKS_LINKGRAPH_MAX_PERSONS")),
            "cache_ttl_hours": _as_int(get("SHERLOCKS_LINKGRAPH_CACHE_HOURS")),
            "parallel_systems": _as_int(get("SHERLOCKS_LINKGRAPH_PARALLEL_SYSTEMS")),
            "max_numbers_per_person": _as_int(get("SHERLOCKS_LINKGRAPH_MAX_NUMBERS")),
        },
        "api": {
            "host": get("SHERLOCKS_API_HOST"),
            "port": _as_int(get("SHERLOCKS_API_PORT")),
            "prefix": get("SHERLOCKS_API_PREFIX"),
            "api_key": get("SHERLOCKS_API_KEY"),
            "cors_origins": get("SHERLOCKS_CORS_ORIGINS"),
            "max_concurrent_jobs": _as_int(get("SHERLOCKS_MAX_CONCURRENT_JOBS")),
        },
        "auth": {
            "enabled": _as_bool(get("SHERLOCKS_AUTH_ENABLED")),
            "username": get("SHERLOCKS_AUTH_USERNAME"),
            "password": get("SHERLOCKS_AUTH_PASSWORD"),
            "password_sha256": get("SHERLOCKS_AUTH_PASSWORD_SHA256"),
            "session_hours": _as_int(get("SHERLOCKS_AUTH_SESSION_HOURS")),
            "secret": get("SHERLOCKS_AUTH_SECRET"),
        },
    }


def load_settings(config_path: str | None = None, env_file: str | None = None) -> Settings:
    resolved_config = Path(config_path or os.environ.get("SHERLOCKS_CONFIG") or PROJECT_ROOT / "configs" / "default.yaml")
    yaml_data = _read_yaml(resolved_config)

    resolved_env = discover_env_file(env_file or os.environ.get("SHERLOCKS_ENV_FILE"))
    # Read the .env without injecting it into os.environ - the process environment
    # stays the caller's, and real env vars still win over the file.
    env_map: dict[str, str] = {}
    if resolved_env:
        env_map.update({k: v for k, v in dotenv_values(resolved_env).items() if v is not None})

    merged = _deep_merge(yaml_data, _env_overrides({**env_map, **os.environ}))
    merged["config_path"] = str(resolved_config) if resolved_config.exists() else None
    merged["env_file"] = str(resolved_env) if resolved_env else None
    return Settings.model_validate(merged)
