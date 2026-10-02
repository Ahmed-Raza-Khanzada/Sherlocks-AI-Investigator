"""Excel workbook renderer for BTS analysis reports."""

from __future__ import annotations

import gc
import io
import logging
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

from cdr_report_app.bts.models import BtsAnalysis, BtsAnalysisRequest

logger = logging.getLogger(__name__)

# Reuse same styling constants as multi_excel_renderer
HEADER_FILL = PatternFill(start_color="29417A", end_color="29417A", fill_type="solid")
HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center", wrap_text=True)
DATA_FONT = Font(name="Calibri", size=10)
DATA_ALIGNMENT = Alignment(vertical="top", wrap_text=True)
THIN_BORDER = Border(
    left=Side(style="thin", color="CCCCCC"),
    right=Side(style="thin", color="CCCCCC"),
    top=Side(style="thin", color="CCCCCC"),
    bottom=Side(style="thin", color="CCCCCC"),
)
EVEN_FILL = PatternFill(start_color="F2F6FC", end_color="F2F6FC", fill_type="solid")
ODD_FILL = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
SPIKE_FILL = PatternFill(start_color="FFE0E0", end_color="FFE0E0", fill_type="solid")

# Soft per-tower palette (cycles if >6 towers)
_TOWER_PALETTE = [
    "DAEEF3",  # light blue
    "E2EFDA",  # light green
    "FDE9D9",  # light peach
    "E6E0EC",  # light lavender
    "FFF2CC",  # light yellow
    "FCE4D6",  # light salmon
]


def _tower_fill(tower_idx: int) -> PatternFill:
    color = _TOWER_PALETTE[tower_idx % len(_TOWER_PALETTE)]
    return PatternFill(start_color=color, end_color=color, fill_type="solid")


def _style_header(ws, headers: list[str], widths: list[int] | None = None, start_row: int = 1) -> None:
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=start_row, column=col_idx, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGNMENT
        cell.border = THIN_BORDER
        if widths and col_idx <= len(widths):
            ws.column_dimensions[get_column_letter(col_idx)].width = widths[col_idx - 1]
    ws.freeze_panes = f"A{start_row + 1}"


def _write_row(ws, row_idx: int, values: list, fill: PatternFill | None = None) -> None:
    row_fill = fill or (EVEN_FILL if row_idx % 2 == 0 else ODD_FILL)
    for col_idx, value in enumerate(values, 1):
        cell = ws.cell(row=row_idx, column=col_idx, value=value)
        cell.font = DATA_FONT
        cell.alignment = DATA_ALIGNMENT
        cell.fill = row_fill
        cell.border = THIN_BORDER


def render_bts_excel(
    analysis: BtsAnalysis,
    request: BtsAnalysisRequest,
    bts_df=None,
    output_path: Path | None = None,
    single_bts: bool = True,
) -> bytes:
    """Generate a BTS analysis Excel workbook.

    *bts_df* is the window-range-only calls DataFrame; used for the Raw Calls sheet.
    *single_bts*: omit Window Key and BTS columns when only one tower.
    """
    logger.info("BTS Excel rendering started")
    wb = Workbook()
    wb.remove(wb.active)

    # Build spec_id → tower color fill for multi-BTS coloring
    all_specs = list(dict.fromkeys(wr.key.spec_id for wr in analysis.window_results))
    tower_fills = {sid: _tower_fill(i) for i, sid in enumerate(all_specs)}
    multiple_bts = len(all_specs) >= 2

    _write_summary_sheet(wb, analysis, request)
    _write_windows_overview_sheet(wb, analysis, tower_fills)

    if request.include_b_as_a:
        _write_b_as_a_sheet(wb, analysis, single_bts, tower_fills)

    if request.include_window_only:
        _write_window_only_sheet(wb, analysis, single_bts, tower_fills)

    if request.include_cross_bts_common and multiple_bts:
        _write_cross_bts_sheet(wb, analysis)

    if bts_df is not None and not bts_df.empty:
        _write_raw_calls_sheet(wb, bts_df)

    buffer = io.BytesIO()
    wb.save(buffer)
    content = buffer.getvalue()
    buffer.close()
    del wb
    gc.collect()

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(content)

    logger.info("BTS Excel rendering completed | size=%d bytes", len(content))
    return content


# ---------------------------------------------------------------------------
#  Sheet writers
# ---------------------------------------------------------------------------

def _write_summary_sheet(wb: Workbook, analysis: BtsAnalysis, request: BtsAnalysisRequest) -> None:
    ws = wb.create_sheet("Summary")
    ws.sheet_properties.tabColor = "29417A"

    title = ws.cell(row=1, column=1, value="BTS Analysis Report — Summary")
    title.font = Font(name="Calibri", bold=True, size=14, color="29417A")
    ws.merge_cells("A1:D1")

    metrics = [
        ("Total Windows Analyzed", len(analysis.window_results)),
        ("Total BTS Records", analysis.total_bts_records),
        ("Total Calls in Windows", sum(w.total_rows for w in analysis.window_results)),
        ("Cross-BTS Common Numbers", len(analysis.cross_bts_common)),
        ("Unique A-Party Numbers", sum(len(w.a_party) for w in analysis.window_results)),
        ("Unique B-Party Numbers", sum(len(w.b_party) for w in analysis.window_results)),
    ]

    row = 3
    for label, value in metrics:
        ws.cell(row=row, column=1, value=label).font = Font(name="Calibri", bold=True, size=11)
        ws.cell(row=row, column=2, value=str(value)).font = DATA_FONT
        row += 1

    ws.column_dimensions["A"].width = 40
    ws.column_dimensions["B"].width = 25


def _write_windows_overview_sheet(wb: Workbook, analysis: BtsAnalysis, tower_fills: dict) -> None:
    ws = wb.create_sheet("Windows_Overview")
    ws.sheet_properties.tabColor = "4472C4"

    headers = ["#", "BTS ID", "Start", "End", "Total Rows",
               "A-Party Count", "B-Party Count", "B-as-A Count", "Window-Only"]
    widths = [5, 14, 22, 22, 12, 14, 14, 14, 14]
    _style_header(ws, headers, widths)

    for idx, wr in enumerate(analysis.window_results, start=1):
        k = wr.key
        fill = tower_fills.get(k.spec_id)
        _write_row(ws, idx + 1, [
            idx,
            k.bts_id or "-",
            k.window_start.strftime("%Y-%m-%d %H:%M:%S"),
            k.window_end.strftime("%Y-%m-%d %H:%M:%S"),
            wr.total_rows,
            len(wr.a_party),
            len(wr.b_party),
            len(wr.b_as_a),
            len(wr.window_only),
        ], fill=fill)


def _write_b_as_a_sheet(wb: Workbook, analysis: BtsAnalysis, single_bts: bool = True, tower_fills: dict | None = None) -> None:
    ws = wb.create_sheet("B_as_A")
    ws.sheet_properties.tabColor = "FFC000"

    headers = ["#"]
    widths = [5]
    if not single_bts:
        headers += ["Window Key", "BTS"]
        widths += [30, 12]
    headers += ["A-Party", "B-Party Also At Tower", "Total Contacts At Tower"]
    widths += [18, 18, 20]
    _style_header(ws, headers, widths)

    row_idx = 2
    counter = 1
    for wr in analysis.window_results:
        k = wr.key
        wkey = _wlabel(k)
        fill = (tower_fills or {}).get(k.spec_id) if not single_bts else None
        for ba in wr.b_as_a:
            for i, b_party in enumerate(ba.b_parties_at_tower):
                row = []
                if i == 0:
                    row.append(counter)
                    if not single_bts:
                        row += [wkey, k.bts_id or "-"]
                    row += [ba.a_party, b_party, ba.contact_count]
                    counter += 1
                else:
                    row.append("")
                    if not single_bts:
                        row += ["", ""]
                    row += ["", b_party, ""]
                _write_row(ws, row_idx, row, fill=fill)
                row_idx += 1


def _write_window_only_sheet(wb: Workbook, analysis: BtsAnalysis, single_bts: bool = True, tower_fills: dict | None = None) -> None:
    ws = wb.create_sheet("Window_Only_Presence")
    ws.sheet_properties.tabColor = "A9D18E"

    headers = ["#"]
    widths = [5]
    if not single_bts:
        headers += ["Window Key", "BTS"]
        widths += [30, 12]
    headers += ["MSISDN", "Calls in Window", "B-Party Contact"]
    widths += [18, 16, 18]
    _style_header(ws, headers, widths)

    row_idx = 2
    counter = 1
    for wr in analysis.window_results:
        k = wr.key
        wkey = _wlabel(k)
        fill = (tower_fills or {}).get(k.spec_id) if not single_bts else None
        for wo in wr.window_only:
            b_list = wo.b_parties if wo.b_parties else [""]
            for i, b_party in enumerate(b_list):
                row = []
                if i == 0:
                    row.append(counter)
                    if not single_bts:
                        row += [wkey, k.bts_id or "-"]
                    row += [wo.msisdn, wo.call_count, b_party]
                    counter += 1
                else:
                    row.append("")
                    if not single_bts:
                        row += ["", ""]
                    row += ["", "", b_party]
                _write_row(ws, row_idx, row, fill=fill)
                row_idx += 1


def _write_cross_bts_sheet(wb: Workbook, analysis: BtsAnalysis) -> None:
    ws = wb.create_sheet("Cross_BTS_Common")
    ws.sheet_properties.tabColor = "C00000"

    common = analysis.cross_bts_common
    if not common:
        ws.cell(row=1, column=1, value="کوئی مشترکہ نمبر نہیں ملا — No common numbers found across tower windows.").font = DATA_FONT
        ws.column_dimensions["A"].width = 70
        return

    # Dynamic per-tower columns
    all_towers = sorted({t for cc in common for t in cc.towers})
    headers = ["#", "MSISDN", "Tower Count", "Total Calls"] + [f"Calls ({t})" for t in all_towers]
    widths = [5, 18, 12, 12] + [16] * len(all_towers)
    _style_header(ws, headers, widths)

    for idx, cc in enumerate(common, start=1):
        total_calls = sum(cc.tower_call_counts.values())
        row = [idx, cc.msisdn, cc.tower_count, total_calls]
        for t in all_towers:
            row.append(cc.tower_call_counts.get(t, 0))
        _write_row(ws, idx + 1, row)


def _write_raw_calls_sheet(wb: Workbook, bts_df) -> None:
    import pandas as pd
    from cdr_report_app.services.tac_lookup import lookup_by_imei

    RAW_CAP = 50_000
    ws = wb.create_sheet("Raw_Calls")
    ws.sheet_properties.tabColor = "808080"

    df = bts_df.copy()

    direction_to_type = {"IN": "INCOMING", "OUT": "OUTGOING", "DATA": "INTERNET"}
    if "DIRECTION" in df.columns:
        df["CALL_TYPE"] = df["DIRECTION"].astype("string").map(direction_to_type).fillna("UNKNOWN")
    else:
        df["CALL_TYPE"] = "UNKNOWN"

    if "IMEI" in df.columns:
        unique_imeis = df["IMEI"].dropna().astype(str).str.strip()
        unique_imeis = unique_imeis[unique_imeis != ""].unique()
        tac_map: dict[str, tuple[str, str]] = {}
        for imei in unique_imeis:
            res = lookup_by_imei(imei)
            if res:
                tac_map[imei] = res
        def _tac_lookup_field(x, idx: int) -> str:
            try:
                if x is None or (hasattr(x, '__class__') and type(x).__name__ == 'NAType'):
                    return ""
                import pandas as pd
                if pd.isna(x):
                    return ""
            except (TypeError, ValueError):
                pass
            s = str(x).strip()
            if not s or s in {"<NA>", "nan", "None"}:
                return ""
            res = tac_map.get(s)
            return res[idx] if res else ""

        df["IMEI_BRAND"] = df["IMEI"].astype("string").map(lambda x: _tac_lookup_field(x, 0))
        df["IMEI_MODEL"] = df["IMEI"].astype("string").map(lambda x: _tac_lookup_field(x, 1))
    else:
        df["IMEI_BRAND"] = ""
        df["IMEI_MODEL"] = ""

    cols = ["BTS_ID", "A_PARTY", "B_PARTY", "CALL_TYPE", "DIRECTION", "CALL_TIME",
            "DURATION_SEC", "IMEI", "IMEI_BRAND", "IMEI_MODEL",
            "CELL_ID", "LAC_ID", "LAT", "LNG", "LOCATION", "PROVIDER"]
    available = [c for c in cols if c in df.columns]
    widths_map = {"BTS_ID": 12, "A_PARTY": 16, "B_PARTY": 16, "CALL_TYPE": 12, "DIRECTION": 10,
                  "CALL_TIME": 20, "DURATION_SEC": 14, "IMEI": 18, "IMEI_BRAND": 16, "IMEI_MODEL": 28,
                  "CELL_ID": 12, "LAC_ID": 10, "LAT": 10, "LNG": 10, "LOCATION": 30, "PROVIDER": 12}
    widths = [widths_map.get(c, 14) for c in available]
    _style_header(ws, available, widths)

    sample = df[available].head(RAW_CAP)
    if len(df) > RAW_CAP:
        logger.warning("Raw Calls sheet capped at %d rows (total=%d)", RAW_CAP, len(df))

    def _fmt(v):
        if v is None:
            return ""
        try:
            if pd.isna(v):
                return ""
        except (TypeError, ValueError):
            pass
        text = str(v)
        return "" if text in {"<NA>", "nan", "NaT", "None"} else text

    for row_idx, (_, row) in enumerate(sample.iterrows(), start=2):
        _write_row(ws, row_idx, [_fmt(v) for v in row.tolist()])


def _wlabel(k) -> str:
    if k.window_label:
        return k.window_label
    return f"{k.window_start.strftime('%H:%M:%S')}–{k.window_end.strftime('%H:%M:%S')}"
