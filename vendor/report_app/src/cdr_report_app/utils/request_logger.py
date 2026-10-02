"""Readable audit logging for inbound API requests and outbound provider calls.

Request logs are appended to ``logs/YYYY-MM-DD_api_requests.txt`` and error
logs are appended to ``logs/YYYY-MM-DD_error_logs.txt`` unless overridden by
``CDR_REQUEST_LOG_DIR`` or ``CDR_ERROR_LOG_DIR``. No information is redacted:
API keys, auth tokens, cookies, passwords, headers, params, and payloads are
written verbatim because these files are intended for deep local debugging of
CDR generation.
"""

from __future__ import annotations

from contextvars import ContextVar
import json
import logging
import os
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qsl, urlsplit

try:
    from dotenv import dotenv_values
except ModuleNotFoundError:  # pragma: no cover - keeps local utility tests working without installed deps.
    dotenv_values = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
request_log_id: ContextVar[str | None] = ContextVar("request_log_id", default=None)

_SEP_HEAVY = "=" * 80
_SEP_LIGHT = "-" * 60
_WRITE_LOCK = threading.Lock()
_TOKEN_KEY_PARTS = ("token", "auth", "api-key", "api_key", "apikey", "key", "secret", "cookie", "password")
_AUDIT_LOG_PATTERNS = ("*_api_requests.txt", "*_error_logs.txt", "api_requests.txt", "error_logs.txt", "*.log*")


def _reinit_write_lock_after_fork() -> None:
    """Reset _WRITE_LOCK in the child after fork.

    If the parent held _WRITE_LOCK at fork time (mid audit-write), the child
    inherits the locked state but no thread alive to release it, deadlocking
    any subsequent audit write. A fresh Lock per child avoids that.
    """
    global _WRITE_LOCK
    _WRITE_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reinit_write_lock_after_fork)


def _section(title: str) -> str:
    return f"\n{_SEP_LIGHT}\n  {title}\n{_SEP_LIGHT}"


def _kv(key: str, value: Any, indent: int = 2) -> str:
    return f"{' ' * indent}{key}: {value}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today_prefix() -> str:
    return date.today().isoformat()


def get_env_setting(name: str, default: str | None = None) -> str | None:
    """Read a setting from real env first, then common project .env locations."""
    value = os.environ.get(name)
    if value:
        return value
    candidates = [
        Path.cwd() / ".env",
        Path.cwd() / "CDR-Analysis" / "Report_App" / ".env",
        Path(__file__).resolve().parents[3] / ".env",
    ]
    for env_path in candidates:
        if not env_path.exists():
            continue
        if dotenv_values is None:
            env_value = _read_simple_env_value(env_path, name)
        else:
            env_value = dotenv_values(env_path).get(name)
        if env_value:
            return str(env_value)
    return default


def _read_simple_env_value(env_path: Path, name: str) -> str | None:
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == name:
                return value.strip().strip("\"'")
    except OSError:
        return None
    return None


def _retention_days() -> int:
    return max(1, int(get_env_setting("CDR_LOG_RETENTION_DAYS", "7") or "7"))


def _jsonish(value: Any, *, max_chars: int = 12000) -> str:
    try:
        if isinstance(value, (bytes, bytearray)):
            text = value.decode("utf-8", errors="replace")
        elif isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    if len(text) > max_chars:
        return text[:max_chars] + f"\n  [... truncated; original length={len(text)} chars]"
    return text


def _safe_request_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): _safe_request_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_request_value(v) for v in value]
    if hasattr(value, "name"):
        return {"file_like": getattr(value, "name", "unknown")}
    return str(value)


def _token_lines(source: Any, prefix: str = "") -> list[str]:
    lines: list[str] = []
    if isinstance(source, dict):
        for key, value in source.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if any(part in key_text.lower() for part in _TOKEN_KEY_PARTS):
                lines.append(_kv(path, _safe_request_value(value), indent=4))
            lines.extend(_token_lines(value, path))
    elif isinstance(source, (list, tuple)):
        for index, item in enumerate(source):
            lines.extend(_token_lines(item, f"{prefix}[{index}]"))
    return lines


def _log_dir(env_name: str) -> Path:
    log_dir = get_env_setting(env_name) or get_env_setting("CDR_REPORT_LOG_DIR") or "logs"
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    return Path(log_dir)


def cleanup_old_audit_logs(*log_dirs: Path | str) -> int:
    """Delete audit/error/app logs older than ``CDR_LOG_RETENTION_DAYS``."""
    cutoff = time.time() - (_retention_days() * 86400)
    removed = 0
    for raw_dir in log_dirs or (_log_dir("CDR_REQUEST_LOG_DIR"), _log_dir("CDR_ERROR_LOG_DIR")):
        log_dir = Path(raw_dir)
        if not log_dir.exists():
            continue
        seen: set[Path] = set()
        for pattern in _AUDIT_LOG_PATTERNS:
            for path in log_dir.glob(pattern):
                if path in seen:
                    continue
                seen.add(path)
                try:
                    if path.is_file() and path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    pass
    return removed


def get_request_log_path() -> Path:
    """Return today's date-prefixed request audit log path."""
    return _log_dir("CDR_REQUEST_LOG_DIR") / f"{_today_prefix()}_api_requests.txt"


def get_error_log_path() -> Path:
    """Return today's date-prefixed readable error log path."""
    return _log_dir("CDR_ERROR_LOG_DIR") / f"{_today_prefix()}_error_logs.txt"


def ensure_request_log_file() -> Path:
    """Create today's request audit log if missing and return its path."""
    log_path = get_request_log_path()
    log_path.touch(exist_ok=True)
    return log_path


def ensure_error_log_file() -> Path:
    """Create today's readable error log if missing and return its path."""
    log_path = get_error_log_path()
    log_path.touch(exist_ok=True)
    return log_path


def new_request_log_id() -> str:
    return f"req-{int(time.time() * 1000)}-{threading.get_ident()}"


def _append_to_log(text: str, *, error: bool = False) -> Path:
    log_path = get_error_log_path() if error else get_request_log_path()
    with _WRITE_LOCK:
        # Keep creation and write in the same critical section so deleting the
        # log directory during runtime does not break concurrent writers.
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(text)
        except FileNotFoundError:
            # If the directory/file is removed between mkdir and open by
            # another process, recreate once and retry.
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(text)
    return log_path


def format_request_details(
    method: str,
    path: str,
    full_url: str,
    query_params: dict[str, Any],
    headers: dict[str, str],
    client_ip: Optional[str] = None,
    client_port: Optional[int] = None,
    body: Optional[str] = None,
    body_type: Optional[str] = None,
    form_fields: Optional[dict[str, str]] = None,
    uploaded_files: Optional[list[dict[str, Any]]] = None,
    request_json_parsed: Optional[Any] = None,
    request_id: Optional[str] = None,
    response_status: Optional[int] = None,
    response_headers: Optional[dict[str, str]] = None,
    duration_ms: Optional[float] = None,
    error: Optional[str] = None,
) -> str:
    timestamp = _now()
    lines: list[str] = []

    lines.append(_SEP_HEAVY)
    lines.append(f"  INBOUND API REQUEST  |  {timestamp}")
    lines.append(_SEP_HEAVY)

    lines.append(_section("REQUEST INFO"))
    if request_id:
        lines.append(_kv("Request ID", request_id))
    lines.append(_kv("Timestamp", timestamp))
    lines.append(_kv("Method", method))
    lines.append(_kv("Path", path))
    lines.append(_kv("Full URL", full_url))
    if query_params:
        lines.append(_kv("Query String", ""))
        for key, value in query_params.items():
            lines.append(_kv(key, value, indent=4))

    lines.append(_section("CLIENT"))
    lines.append(_kv("IP Address", client_ip or "unknown"))
    lines.append(_kv("Port", client_port or "unknown"))

    auth_headers = {
        key: value
        for key, value in headers.items()
        if any(part in key.lower() for part in _TOKEN_KEY_PARTS) or key.lower() == "authorization"
    }
    lines.append(_section("AUTHENTICATION & SECURITY HEADERS"))
    if auth_headers:
        for key, value in auth_headers.items():
            lines.append(_kv(key, value))
            if key.lower() == "authorization" and str(value).lower().startswith("bearer "):
                lines.append(_kv("Bearer token", str(value).split(" ", 1)[1]))
    else:
        lines.append("  (none found)")

    lines.append(_section("ALL HEADERS"))
    for key, value in headers.items():
        lines.append(_kv(key, value))

    if uploaded_files:
        lines.append(_section(f"UPLOADED FILES ({len(uploaded_files)})"))
        for index, uploaded_file in enumerate(uploaded_files, 1):
            lines.append(f"  File {index}:")
            lines.append(_kv("field_name", uploaded_file.get("field_name", "unknown"), indent=4))
            lines.append(_kv("filename", uploaded_file.get("filename", "unknown"), indent=4))
            lines.append(_kv("content_type", uploaded_file.get("content_type", "unknown"), indent=4))
            size = uploaded_file.get("size")
            lines.append(_kv("size", f"{size} bytes" if size is not None else "unknown", indent=4))

    if form_fields:
        lines.append(_section("FORM FIELDS (non-file)"))
        lines.append(_jsonish(form_fields))

    if request_json_parsed is not None:
        lines.append(_section("PARSED request_json PAYLOAD"))
        lines.append(_jsonish(request_json_parsed))

    if body and body_type not in {"multipart/form-data"}:
        lines.append(_section(f"REQUEST BODY [{body_type or 'raw'}]"))
        if body_type == "application/json":
            try:
                lines.append(_jsonish(json.loads(body)))
            except Exception:
                lines.append(_jsonish(body))
        else:
            lines.append(_jsonish(body))

    lines.append(_section("RESPONSE"))
    if response_status is not None:
        lines.append(_kv("Status", response_status))
    if duration_ms is not None:
        lines.append(_kv("Duration", f"{duration_ms:.2f} ms"))
    if error:
        lines.append(_kv("Error", error))
    if response_headers:
        lines.append(_kv("Response Headers", ""))
        lines.append(_jsonish(response_headers))

    lines.append(_SEP_HEAVY)
    lines.append("")
    return "\n".join(lines)


def log_request(
    method: str,
    path: str,
    full_url: str,
    query_params: dict[str, Any],
    headers: dict[str, str],
    client_ip: Optional[str] = None,
    client_port: Optional[int] = None,
    body: Optional[str] = None,
    body_type: Optional[str] = None,
    form_fields: Optional[dict[str, str]] = None,
    uploaded_files: Optional[list[dict[str, Any]]] = None,
    request_json_parsed: Optional[Any] = None,
    request_id: Optional[str] = None,
    response_status: Optional[int] = None,
    response_headers: Optional[dict[str, str]] = None,
    duration_ms: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    """Append a full inbound request record to the API request log file."""
    try:
        text = format_request_details(
            method=method,
            path=path,
            full_url=full_url,
            query_params=query_params,
            headers=headers,
            client_ip=client_ip,
            client_port=client_port,
            body=body,
            body_type=body_type,
            form_fields=form_fields,
            uploaded_files=uploaded_files,
            request_json_parsed=request_json_parsed,
            request_id=request_id,
            response_status=response_status,
            response_headers=response_headers,
            duration_ms=duration_ms,
            error=error,
        )
        log_path = _append_to_log(text)
        logger.debug("Request logged -> %s", log_path)
        if error or (response_status is not None and response_status >= 400):
            log_error_event(
                title="Inbound API request failed",
                severity="ERROR" if response_status is None or response_status >= 500 else "WARNING",
                request_id=request_id,
                source="api.middleware",
                message=error or f"HTTP status {response_status}",
                details={
                    "method": method,
                    "path": path,
                    "full_url": full_url,
                    "response_status": response_status,
                    "duration_ms": duration_ms,
                    "client_ip": client_ip,
                    "client_port": client_port,
                },
            )
    except Exception as exc:
        logger.warning("Failed to log request: %s", exc)


def format_error_event(
    *,
    title: str,
    severity: str = "ERROR",
    source: str | None = None,
    message: str | None = None,
    request_id: str | None = None,
    details: Any = None,
    traceback_text: str | None = None,
) -> str:
    timestamp = _now()
    lines: list[str] = []

    lines.append(_SEP_HEAVY)
    lines.append(f"  {severity.upper()} LOG  |  {timestamp}")
    lines.append(_SEP_HEAVY)
    lines.append(_section("ERROR INFO"))
    lines.append(_kv("Timestamp", timestamp))
    lines.append(_kv("Title", title))
    lines.append(_kv("Severity", severity.upper()))
    if source:
        lines.append(_kv("Source", source))
    current_request_id = request_id or request_log_id.get()
    if current_request_id:
        lines.append(_kv("Request ID", current_request_id))
    if message:
        lines.append(_kv("Message", message))
    if details is not None:
        lines.append(_section("DETAILS"))
        lines.append(_jsonish(_safe_request_value(details), max_chars=16000))
    if traceback_text:
        lines.append(_section("TRACEBACK"))
        lines.append(_jsonish(traceback_text, max_chars=20000))

    lines.append(_SEP_HEAVY)
    lines.append("")
    return "\n".join(lines)


def log_error_event(
    *,
    title: str,
    severity: str = "ERROR",
    source: str | None = None,
    message: str | None = None,
    request_id: str | None = None,
    details: Any = None,
    traceback_text: str | None = None,
) -> None:
    """Append one readable error event to today's error log file."""
    try:
        text = format_error_event(
            title=title,
            severity=severity,
            source=source,
            message=message,
            request_id=request_id,
            details=details,
            traceback_text=traceback_text,
        )
        _append_to_log(text, error=True)
    except Exception as exc:
        logger.warning("Failed to log error event: %s", exc)


def format_external_api_call(
    *,
    method: str,
    url: str | None,
    timeout: Any = None,
    request_kwargs: Optional[dict[str, Any]] = None,
    response_status: Optional[int] = None,
    response_headers: Optional[dict[str, str]] = None,
    response_body: Any = None,
    response_bytes: Optional[int] = None,
    duration_ms: Optional[float] = None,
    error: Optional[str] = None,
    skipped_reason: Optional[str] = None,
) -> str:
    timestamp = _now()
    kwargs = request_kwargs or {}
    headers = _safe_request_value(kwargs.get("headers") or {})
    params = _safe_request_value(kwargs.get("params") or {})
    json_payload = _safe_request_value(kwargs.get("json"))
    data_payload = _safe_request_value(kwargs.get("data"))
    files_payload = _safe_request_value(kwargs.get("files"))
    cookies_payload = _safe_request_value(kwargs.get("cookies"))

    lines: list[str] = []
    lines.append(_SEP_HEAVY)
    lines.append(f"  OUTBOUND EXTERNAL API CALL  |  {timestamp}")
    lines.append(_SEP_HEAVY)

    lines.append(_section("CALL INFO"))
    current_request_id = request_log_id.get()
    if current_request_id:
        lines.append(_kv("Inbound Request ID", current_request_id))
    lines.append(_kv("Timestamp", timestamp))
    lines.append(_kv("Method", method.upper()))
    lines.append(_kv("URL", url or "not configured"))
    if timeout is not None:
        lines.append(_kv("Timeout", timeout))
    if duration_ms is not None:
        lines.append(_kv("Duration", f"{duration_ms:.2f} ms"))
    if skipped_reason:
        lines.append(_kv("Skipped", skipped_reason))
    if error:
        lines.append(_kv("Error", error))

    if url:
        query_from_url = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        if query_from_url:
            lines.append(_section("URL QUERY PARAMETERS"))
            lines.append(_jsonish(query_from_url))

    token_matches: list[str] = []
    for source_name, source_value in {
        "headers": headers,
        "params": params,
        "json": json_payload,
        "data": data_payload,
        "cookies": cookies_payload,
    }.items():
        token_matches.extend(_token_lines(source_value, source_name))
    lines.append(_section("TOKENS / AUTH VALUES FOUND"))
    lines.extend(token_matches or ["  (none found)"])

    if headers:
        lines.append(_section("REQUEST HEADERS"))
        lines.append(_jsonish(headers))
    if cookies_payload:
        lines.append(_section("REQUEST COOKIES"))
        lines.append(_jsonish(cookies_payload))
    if params:
        lines.append(_section("REQUEST PARAMS"))
        lines.append(_jsonish(params))
    if json_payload is not None:
        lines.append(_section("REQUEST JSON PAYLOAD"))
        lines.append(_jsonish(json_payload))
    if data_payload is not None:
        lines.append(_section("REQUEST FORM / DATA PAYLOAD"))
        lines.append(_jsonish(data_payload))
    if files_payload is not None:
        lines.append(_section("REQUEST FILES"))
        lines.append(_jsonish(files_payload))

    lines.append(_section("RESPONSE"))
    if response_status is not None:
        lines.append(_kv("Status", response_status))
    if response_headers:
        lines.append(_kv("Headers", ""))
        lines.append(_jsonish(response_headers))
    if response_bytes is not None:
        lines.append(_kv("Body Bytes", response_bytes))
    if response_body is not None:
        lines.append(_section("RESPONSE BODY"))
        lines.append(_jsonish(response_body))

    lines.append(_SEP_HEAVY)
    lines.append("")
    return "\n".join(lines)


def log_external_api_call(**kwargs: Any) -> None:
    """Append one outbound external API call to the request log file."""
    try:
        text = format_external_api_call(**kwargs)
        log_path = _append_to_log(text)
        logger.debug("External API call logged -> %s", log_path)
        response_status = kwargs.get("response_status")
        error = kwargs.get("error")
        skipped_reason = kwargs.get("skipped_reason")
        if error or skipped_reason or (isinstance(response_status, int) and response_status >= 400):
            log_error_event(
                title="Outbound external API call failed",
                severity="ERROR",
                source="integrations.http",
                message=error or skipped_reason or f"HTTP status {response_status}",
                details={
                    "method": kwargs.get("method"),
                    "url": kwargs.get("url"),
                    "timeout": kwargs.get("timeout"),
                    "response_status": response_status,
                    "duration_ms": kwargs.get("duration_ms"),
                    "request_kwargs": _safe_request_value(kwargs.get("request_kwargs") or {}),
                    "response_headers": _safe_request_value(kwargs.get("response_headers") or {}),
                    "response_body": kwargs.get("response_body"),
                    "response_bytes": kwargs.get("response_bytes"),
                },
            )
    except Exception as exc:
        logger.warning("Failed to log external API call: %s", exc)
