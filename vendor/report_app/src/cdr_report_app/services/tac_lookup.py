"""Local TAC (Type Allocation Code) lookup backed by an on-disk SQLite index.

On first use the Excel file is converted to a SQLite database next to it
(tac_full.db). Subsequent startups reuse the SQLite file directly — the full
250k rows never load into RAM. Only individual looked-up results are cached.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from functools import lru_cache
from pathlib import Path

from cdr_report_app.utils.memory import log_memory_snapshot

logger = logging.getLogger(__name__)

_DEFAULT_TAC_XLSX = Path(__file__).resolve().parents[4] / "tac_full.xlsx"


def _tac_paths() -> tuple[Path, Path]:
    configured = Path(os.environ.get("TAC_DB_PATH", str(_DEFAULT_TAC_XLSX)))
    if configured.suffix.lower() == ".db":
        db = configured
        xlsx = configured.with_suffix(".xlsx")
    else:
        xlsx = configured
        db = configured.with_suffix(".db")
    return xlsx, db


def _build_sqlite(xlsx: Path, db: Path) -> None:
    import pandas as pd

    logger.info("Building TAC SQLite index | source=%s target=%s", xlsx, db)
    log_memory_snapshot("tac_build:start", logger_instance=logger)
    df = pd.read_excel(xlsx, dtype={"TAC": str})
    df["TAC"] = df["TAC"].str.strip().str.zfill(8)
    df = df.dropna(subset=["TAC"])

    con = sqlite3.connect(db)
    try:
        con.execute("DROP TABLE IF EXISTS tac")
        con.execute("CREATE TABLE tac (tac TEXT PRIMARY KEY, brand TEXT, specs TEXT)")
        con.executemany(
            "INSERT OR REPLACE INTO tac VALUES (?, ?, ?)",
            (
                (row["TAC"], str(row.get("Brand") or "").strip(), str(row.get("SPECS") or "").strip())
                for _, row in df.iterrows()
            ),
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_tac ON tac(tac)")
        con.commit()
        logger.info("TAC SQLite index built | rows=%d", len(df))
    finally:
        con.close()
    del df
    log_memory_snapshot("tac_build:completed", logger_instance=logger)


def _get_db_path() -> Path | None:
    xlsx, db = _tac_paths()
    if not db.exists():
        if not xlsx.exists():
            logger.warning("TAC database not found at %s — IMEI local lookup disabled", xlsx)
            return None
        _build_sqlite(xlsx, db)
    return db


@lru_cache(maxsize=512)
def lookup_by_imei(imei: str) -> tuple[str, str] | None:
    """Return (brand, specs) for the given IMEI using the first 8 digits.

    Only the queried result is cached — the full dataset stays on disk.
    """
    digits = "".join(ch for ch in imei if ch.isdigit())
    if len(digits) < 8:
        return None

    tac = digits[:8]
    db_path = _get_db_path()
    if not db_path:
        return None

    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT brand, specs FROM tac WHERE tac = ?", (tac,)).fetchone()
    finally:
        con.close()

    if not row:
        return None
    brand, specs = row
    return (brand or "", specs or "") if (brand or specs) else None
