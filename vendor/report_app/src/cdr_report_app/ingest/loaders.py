"""File readers for CDR ingestion."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def read_raw_cdr_file(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls", ".xlsb", ".xlsm"}:
        try:
            engine = None
            if suffix == ".xls":
                engine = "xlrd"
            elif suffix == ".xlsb":
                engine = "pyxlsb"
            else:
                engine = "openpyxl"
            df = pd.read_excel(path, header=None, dtype=str, engine=engine, na_filter=False)
        except Exception:
            # Fallback for HTML exported files masquerading as .xls
            try:
                df_list = pd.read_html(path)
                df = (df_list[0] if df_list else pd.DataFrame()).fillna("")
            except Exception:
                raise ValueError(f"Could not parse {suffix} file: it may be corrupted or in an unsupported format.")
    elif suffix in {".csv", ".tsv", ".txt"}:
        df = pd.read_csv(path, header=None, dtype=str, sep=None, engine="python", na_filter=False)
    else:
        raise ValueError(f"Unsupported input type: {path.suffix}")
    return df
