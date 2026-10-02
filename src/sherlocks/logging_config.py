"""Logging setup. Console always; a dated file when a log directory is configured."""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

from sherlocks.settings import PROJECT_ROOT, Settings

_CONFIGURED = False
_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def configure_logging(settings: Settings, *, force: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    level = getattr(logging, settings.app.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(console)

    log_dir = Path(settings.app.log_dir)
    if not log_dir.is_absolute():
        log_dir = PROJECT_ROOT / log_dir
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            log_dir / f"{date.today().isoformat()}_sherlocks.log", encoding="utf-8"  # noqa: DTZ011 - log files are named by local date, deliberately
        )
        file_handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(file_handler)
    except OSError:
        # A read-only or missing log directory must not stop the service booting.
        root.warning("Could not open log directory %s - logging to console only", log_dir)

    # These are chatty at DEBUG and drown out our own lines.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # fpdf2 subsets fonts on every render and narrates each table it prunes - roughly
    # 400 INFO lines per PDF, which buries everything else.
    logging.getLogger("fontTools").setLevel(logging.WARNING)
    logging.getLogger("fpdf").setLevel(logging.WARNING)
    _CONFIGURED = True
