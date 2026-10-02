"""Access to cdr_report_app's PDF engine.

``ReportPdf`` is 540 lines of Urdu glyph shaping, bidi handling, font fallback and
bundled TTFs. Reimplementing it would mean re-solving problems that were already solved
in production, so Sherlocks imports it instead.

The awkward part: cdr_report_app ships no ``pyproject.toml`` or ``setup.py``, so it
cannot be pip-installed. ``app.report_app_src`` names its ``src`` directory and this
module puts it on ``sys.path`` on first use. When the package is importable already the
setting is ignored.
"""

from __future__ import annotations

import logging
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from sherlocks.settings import Settings, load_settings

logger = logging.getLogger(__name__)


class ReportAppUnavailable(RuntimeError):
    """Raised when cdr_report_app cannot be located."""


@lru_cache(maxsize=1)
def _ensure_on_path(src: str | None) -> None:
    if not src:
        return
    path = Path(src).expanduser().resolve()
    if not path.is_dir():
        logger.warning("app.report_app_src does not exist: %s", path)
        return
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
        logger.debug("Added cdr_report_app source path %s", path)


def load_report_pdf(settings: Settings | None = None) -> Any:
    """Return the ``ReportPdf`` class, or raise with an actionable message."""
    settings = settings or load_settings()
    _ensure_on_path(settings.app.report_app_src)
    try:
        from cdr_report_app.rendering.pdf.builder import ReportPdf
    except ImportError as exc:  # pragma: no cover - environment specific
        raise ReportAppUnavailable(
            "cdr_report_app is not importable. Set app.report_app_src (or "
            "SHERLOCKS_REPORT_APP_SRC) to the Report_App 'src' directory."
        ) from exc
    return ReportPdf


def report_app_available(settings: Settings | None = None) -> bool:
    try:
        load_report_pdf(settings)
    except ReportAppUnavailable:
        return False
    return True
