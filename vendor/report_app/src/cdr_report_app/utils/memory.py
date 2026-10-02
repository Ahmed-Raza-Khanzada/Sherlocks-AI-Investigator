"""Memory release helpers for long-running worker processes.

glibc's malloc does not return freed arenas to the OS, so RSS stays high
after pandas/numpy/matplotlib work even though Python has released every
object. We force it with malloc_trim(0) after each job.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import gc
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_libc = None
_malloc_trim_available = False
_PROC_STATUS = Path("/proc/self/status")


def _load_libc() -> None:
    global _libc, _malloc_trim_available
    if _libc is not None:
        return
    try:
        libc_path = ctypes.util.find_library("c")
        if libc_path:
            _libc = ctypes.CDLL(libc_path)
            if hasattr(_libc, "malloc_trim"):
                _libc.malloc_trim.argtypes = [ctypes.c_size_t]
                _libc.malloc_trim.restype = ctypes.c_int
                _malloc_trim_available = True
    except Exception as exc:
        logger.debug("malloc_trim unavailable: %s", exc)


def release_memory() -> None:
    """Force a full GC sweep and ask glibc to return freed pages to the OS."""
    try:
        import matplotlib.pyplot as plt

        plt.close("all")
    except Exception:
        pass
    gc.collect()
    gc.collect()
    _load_libc()
    if _malloc_trim_available and _libc is not None:
        try:
            _libc.malloc_trim(0)
        except Exception as exc:
            logger.debug("malloc_trim call failed: %s", exc)


def read_memory_snapshot() -> dict[str, int]:
    snapshot = {"rss_kb": 0, "peak_rss_kb": 0}
    try:
        if not _PROC_STATUS.exists():
            return snapshot
        for line in _PROC_STATUS.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VmRSS:"):
                snapshot["rss_kb"] = int("".join(ch for ch in line if ch.isdigit()) or "0")
            elif line.startswith("VmHWM:"):
                snapshot["peak_rss_kb"] = int("".join(ch for ch in line if ch.isdigit()) or "0")
    except Exception as exc:
        logger.debug("Failed to read memory snapshot: %s", exc)
    return snapshot


def log_memory_snapshot(stage: str, *, logger_instance: logging.Logger | None = None, level: int = logging.INFO) -> dict[str, int]:
    target_logger = logger_instance or logger
    snapshot = read_memory_snapshot()
    rss_mb = snapshot["rss_kb"] / 1024
    peak_mb = snapshot["peak_rss_kb"] / 1024
    target_logger.log(level, "Memory snapshot | stage=%s rss_mb=%.2f peak_rss_mb=%.2f", stage, rss_mb, peak_mb)
    return snapshot
