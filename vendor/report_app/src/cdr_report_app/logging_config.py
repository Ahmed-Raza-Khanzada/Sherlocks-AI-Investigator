"""Application logging helpers."""

from __future__ import annotations

import logging
import os
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from cdr_report_app.utils.request_logger import (
    cleanup_old_audit_logs,
    ensure_error_log_file,
    get_env_setting,
    log_error_event,
)


def _log_retention_days() -> int:
    return max(1, int(get_env_setting("CDR_LOG_RETENTION_DAYS", "7") or "7"))


class ReadableErrorLogHandler(logging.Handler):
    """Write ERROR+ records to the daily human-readable error log."""

    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self._cdr_error_audit = True

    def emit(self, record: logging.LogRecord) -> None:
        try:
            traceback_text = None
            if record.exc_info:
                traceback_text = self.formatter.formatException(record.exc_info) if self.formatter else None
            log_error_event(
                title="Application logger error",
                severity=record.levelname,
                source=record.name,
                message=record.getMessage(),
                details={
                    "module": record.module,
                    "function": record.funcName,
                    "line": record.lineno,
                    "pathname": record.pathname,
                },
                traceback_text=traceback_text,
            )
        except Exception:
            self.handleError(record)


def _cleanup_old_logs(log_dir: Path) -> None:
    """Delete log files older than the configured retention period."""
    retention = _log_retention_days() * 86400
    cutoff = time.time() - retention
    removed = 0
    patterns = ("*.log*", "*_api_requests.txt", "*_error_logs.txt", "api_requests.txt", "error_logs.txt")
    seen: set[Path] = set()
    for pattern in patterns:
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
    removed += cleanup_old_audit_logs(log_dir)
    if removed:
        logging.getLogger(__name__).info(
            "Log cleanup: removed %d file(s) older than %d days", removed, _log_retention_days()
        )


def _setup_readable_error_log(log_dir: Path) -> Path:
    os.environ.setdefault("CDR_ERROR_LOG_DIR", get_env_setting("CDR_ERROR_LOG_DIR", str(log_dir)) or str(log_dir))
    error_log_path = ensure_error_log_file()
    root = logging.getLogger()
    if not any(getattr(handler, "_cdr_error_audit", False) for handler in root.handlers):
        error_handler = ReadableErrorLogHandler()
        error_handler.setFormatter(logging.Formatter())
        root.addHandler(error_handler)
    return error_log_path


def setup_logging(
    level: str = "INFO",
    log_dir: Path | str = Path("logs"),
    log_to_file: bool = True,
) -> Path | None:
    resolved_level = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(resolved_level)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if not any(getattr(handler, "_cdr_console", False) for handler in root.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(resolved_level)
        console_handler.setFormatter(formatter)
        console_handler._cdr_console = True  # type: ignore[attr-defined]
        root.addHandler(console_handler)
    else:
        for handler in root.handlers:
            if getattr(handler, "_cdr_console", False):
                handler.setLevel(resolved_level)

    log_path: Path | None = None
    if log_to_file:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("CDR_REQUEST_LOG_DIR", get_env_setting("CDR_REQUEST_LOG_DIR", str(log_dir)) or str(log_dir))
        os.environ.setdefault("CDR_ERROR_LOG_DIR", get_env_setting("CDR_ERROR_LOG_DIR", str(log_dir)) or str(log_dir))

        # One log file per day named YYYY-MM-DD.log
        # At midnight the current file is renamed to YYYY-MM-DD.log.<date suffix>
        # and a fresh file is opened for the new day.
        from datetime import date
        log_path = log_dir / f"{date.today().isoformat()}.log"

        existing = next((h for h in root.handlers if getattr(h, "_cdr_file", False)), None)
        if existing is None:
            file_handler = TimedRotatingFileHandler(
                log_path,
                when="midnight",
                interval=1,
                backupCount=_log_retention_days(),
                encoding="utf-8",
                utc=False,
            )
            # Suffix format produces: 2026-04-24.log.2026-04-23 → rename to date-only on rotation
            file_handler.suffix = "%Y-%m-%d"
            file_handler.setLevel(resolved_level)
            file_handler.setFormatter(formatter)
            file_handler._cdr_file = True  # type: ignore[attr-defined]
            root.addHandler(file_handler)

            # Clean up any log files beyond retention on startup
            _cleanup_old_logs(log_dir)
        else:
            existing.setLevel(resolved_level)

        _setup_readable_error_log(log_dir)

    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_path
