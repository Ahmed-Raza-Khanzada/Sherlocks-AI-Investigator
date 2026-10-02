"""BTS report generation orchestrator.

Full pipeline: ingest → early window pre-filter → analyse → render.
"""

from __future__ import annotations

import gc
import logging
from pathlib import Path

import pandas as pd

from cdr_report_app.bts.analysis.bursts import burst_callers
from cdr_report_app.bts.analysis.cross import cross_bts_common, _window_key_id
from cdr_report_app.bts.analysis.density import hourly_density
from cdr_report_app.bts.analysis.imei import imei_anomalies
from cdr_report_app.bts.analysis.movement import movement_feasibility
from cdr_report_app.bts.analysis.presence import a_party_list, b_party_list, b_as_a, window_only_presence
from cdr_report_app.bts.analysis.top_facts import compute_top_facts
from cdr_report_app.bts.analysis.windowing import make_window_key, slice_window
from cdr_report_app.bts.ingest.detect import classify_bts_source
from cdr_report_app.bts.ingest.readers import read_bts_group
from cdr_report_app.bts.models import (
    BtsAnalysis,
    BtsAnalysisRequest,
    BtsWindowResult,
)
from cdr_report_app.bts.rendering.excel import render_bts_excel
from cdr_report_app.bts.rendering.pdf import render_bts_pdf
from cdr_report_app.settings import Settings

logger = logging.getLogger(__name__)


def generate_bts_report(
    settings: Settings,
    temp_paths: list[Path],
    original_filenames: list[str],
    request: BtsAnalysisRequest,
    output_dir: Path | None = None,
) -> tuple[bytes, bytes, BtsAnalysis]:
    """Full BTS pipeline: ingest → analyse → render PDF + Excel.

    Returns *(pdf_bytes, excel_bytes, analysis)*.
    """
    if output_dir is None:
        output_dir = settings.app.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Map original filename → temp path (positional: upload order is preserved)
    name_to_path: dict[str, Path] = dict(zip(original_filenames, temp_paths))

    # Auto-generate specs when caller sent no request_json / empty specs list.
    # classify_bts_source detects providers by content so randomised temp-path
    # names are not a problem; we then reverse-map back to original filenames.
    if not request.specs:
        logger.info("No specs provided — auto-detecting specs from %d uploaded files", len(temp_paths))
        path_to_name: dict[Path, str] = {v: k for k, v in name_to_path.items()}
        auto_groups = classify_bts_source(temp_paths)
        if not auto_groups:
            raise ValueError("No BTS files could be ingested — check provider support and file formats.")
        from cdr_report_app.bts.models import BtsFileSpec
        request = request.model_copy(update={"specs": [
            BtsFileSpec(
                spec_id=f"auto_{idx}",
                filenames=[path_to_name.get(p, p.name) for p in grp.paths],
                provider=grp.provider,
                bts_id=grp.bts_id,
                label=grp.label,
                windows=[],
            )
            for idx, grp in enumerate(auto_groups, start=1)
        ]})
        logger.info("Auto-generated %d spec(s): %s", len(request.specs),
                    [(s.spec_id, s.provider, s.filenames) for s in request.specs])

    # --- Step 1: Ingest each spec using its own temp paths ---
    frames: list[pd.DataFrame] = []
    file_info: list[dict] = []

    for spec_idx, spec in enumerate(request.specs, start=1):
        if not spec.spec_id:
            spec.spec_id = f"spec_{spec_idx}"
        spec_paths = [name_to_path[fn] for fn in spec.filenames if fn in name_to_path]
        if not spec_paths:
            logger.warning("No temp paths found for spec %s (filenames=%s)", spec.spec_id, spec.filenames)
            continue

        # Use spec.provider when already known (e.g. from the inspect endpoint).
        # classify_bts_source detects Telenor by content (INBOUND_OUTBOUND_IND +
        # Site_Id columns), so temp-path randomised names are not a problem.
        provider = spec.provider
        bts_id = spec.bts_id

        if not provider:
            groups = classify_bts_source(spec_paths)
            if not groups:
                logger.warning("Could not classify files for spec %s", spec.spec_id)
                continue
            group = groups[0]
            provider = group.provider
            bts_id = bts_id or group.bts_id

        try:
            df = read_bts_group(
                provider,
                spec_paths,
                spec_id=spec.spec_id,
                bts_id=bts_id,
            )

            # Auto-pick BTS_ID from file data (e.g. Telenor Site_Id) is disabled.
            # Tower labels are always "Tower N" unless the user sets them explicitly
            # via the UI (spec.bts_id already set before this loop).
            # Uncomment the block below to re-enable file-based inference:
            # if "BTS_ID" in df.columns and not df["BTS_ID"].dropna().empty:
            #     non_empty = df["BTS_ID"].astype("string").str.strip()
            #     non_empty = non_empty[non_empty != ""]
            #     if not non_empty.empty:
            #         bts_id = bts_id or str(non_empty.mode().iloc[0])
            if not bts_id:
                bts_id = f"Tower {spec_idx}"
            df["BTS_ID"] = pd.Series([bts_id] * len(df), dtype="string")
            spec.bts_id = bts_id

            frames.append(df)
            file_info.append({
                "provider": provider,
                "filename": ", ".join(spec.filenames),
                "bts_id": bts_id,
                "row_count": len(df),
            })
            logger.info(
                "Ingested BTS spec | spec=%s provider=%s rows=%d",
                spec.spec_id, provider, len(df),
            )
        except NotImplementedError as exc:
            logger.warning("Skipping unimplemented provider for spec %s: %s", spec.spec_id, exc)
        except Exception as exc:
            logger.error("Failed to ingest spec %s: %s", spec.spec_id, exc, exc_info=True)

    # If spec filenames don't match any uploaded file (e.g. Postman request_json
    # has stale/wrong filenames), fall back to content-based auto-detection on
    # all temp paths so the analysis still runs.
    if not frames:
        logger.warning(
            "Spec filenames matched no uploaded file — falling back to auto-detection on %d path(s)",
            len(temp_paths),
        )
        auto_groups = classify_bts_source(temp_paths)
        if not auto_groups:
            raise ValueError("No BTS files could be ingested — check provider support and file formats.")
        from cdr_report_app.bts.models import BtsFileSpec
        path_to_name: dict[Path, str] = {v: k for k, v in name_to_path.items()}
        new_specs = []
        for grp_idx, grp in enumerate(auto_groups, start=1):
            spec_id = f"fallback_{grp_idx}"
            bts_id = grp.bts_id or f"Tower {grp_idx}"
            try:
                df = read_bts_group(grp.provider, grp.paths, spec_id=spec_id, bts_id=bts_id)
                df["BTS_ID"] = pd.Series([bts_id] * len(df), dtype="string")
                filenames = [path_to_name.get(p, p.name) for p in grp.paths]
                frames.append(df)
                file_info.append({
                    "provider": grp.provider,
                    "filename": ", ".join(filenames),
                    "bts_id": bts_id,
                    "row_count": len(df),
                })
                new_specs.append(BtsFileSpec(
                    spec_id=spec_id,
                    filenames=filenames,
                    provider=grp.provider,
                    bts_id=bts_id,
                    label=grp.label,
                    windows=[],
                ))
                logger.info("Fallback auto-detect | spec=%s provider=%s bts_id=%s rows=%d", spec_id, grp.provider, bts_id, len(df))
            except Exception as exc:
                logger.error("Fallback ingest failed for group %s: %s", grp_idx, exc, exc_info=True)
        if not frames:
            raise ValueError("No BTS files could be ingested — check provider support and file formats.")
        request = request.model_copy(update={"specs": new_specs})

    bts_df = pd.concat(frames, ignore_index=True)
    del frames
    gc.collect()

    logger.info("BTS concat complete | total_rows=%d  unique_specs=%s", len(bts_df), bts_df["SPEC_ID"].unique().tolist())

    # --- Step 2: Auto-fill missing windows (full file extent per spec) ---
    # If a spec has no windows, treat the entire file as one window covering
    # its min→max CALL_TIME. This lets investigators upload a file and run
    # the analysis without first picking a time range.
    from cdr_report_app.bts.models import BtsTimeWindow
    for spec in request.specs:
        if not spec.windows:
            spec_rows = bts_df[bts_df["SPEC_ID"] == spec.spec_id]
            if spec_rows.empty:
                continue
            t_min = pd.Timestamp(spec_rows["CALL_TIME"].min())
            t_max = pd.Timestamp(spec_rows["CALL_TIME"].max())
            spec.windows = [BtsTimeWindow(start=t_min.to_pydatetime(), end=t_max.to_pydatetime(), label="Full file")]
            logger.info("Auto-window for spec %s | %s → %s (no windows specified)", spec.spec_id, t_min, t_max)

    # Total records across entire ingested file(s), pre-window — for exec summary.
    total_records_all = int(len(bts_df))
    raw_bts_df = bts_df.copy()

    # --- Step 3: Early window pre-filter ---
    all_windows = [w for spec in request.specs for w in spec.windows]
    if all_windows:
        w_min = min(pd.Timestamp(w.start) for w in all_windows)
        w_max = max(pd.Timestamp(w.end) for w in all_windows)
        before = len(bts_df)
        bts_df = bts_df[
            (bts_df["CALL_TIME"] >= w_min) & (bts_df["CALL_TIME"] <= w_max)
        ].copy()
        gc.collect()
        logger.info("Window pre-filter | rows %d → %d  (window %s → %s)", before, len(bts_df), w_min, w_max)

    # --- Step 4: Sort once ---
    bts_df = bts_df.sort_values(["SPEC_ID", "CALL_TIME"]).reset_index(drop=True)

    # --- Step 5: Run analysis per window ---
    window_results: list[BtsWindowResult] = []
    keys = []
    per_window_a: dict[str, set[str]] = {}
    per_window_counts: dict[str, dict[str, int]] = {}
    # spec_id → (bts_id, filenames) for PDF tower summary
    spec_tower_map: dict[str, tuple[str, str]] = {}

    for spec in request.specs:
        bts_id = spec.bts_id
        spec_tower_map[spec.spec_id] = (bts_id or spec.spec_id, ", ".join(spec.filenames))

        # Use raw (pre-filter) data for window-only exclusivity — covers the full
        # file period so numbers active outside the window are not missed.
        raw_spec_df = raw_bts_df[
            (raw_bts_df["SPEC_ID"] == spec.spec_id) &
            (raw_bts_df["DIRECTION"].isin(["IN", "OUT"]))
        ]

        for window in spec.windows:
            wdf_full = slice_window(bts_df, spec.spec_id, window, bts_id)
            # Exclude DATA (internet/GPRS) rows from analytics; they remain in raw export.
            wdf = wdf_full[wdf_full["DIRECTION"].isin(["IN", "OUT"])].copy()
            logger.info("Window slice | spec=%s window=%s→%s rows=%d (calls=%d)", spec.spec_id, window.start, window.end, len(wdf_full), len(wdf))
            key = make_window_key(spec.spec_id, bts_id, spec.label, window)
            keys.append(key)
            wkey_id = _window_key_id(key)
            per_window_a[wkey_id] = set(wdf["A_PARTY"].dropna().astype(str).unique())
            # Per-MSISDN call counts for cross-BTS enrichment
            if not wdf.empty and "A_PARTY" in wdf.columns:
                per_window_counts[wkey_id] = (
                    wdf.groupby("A_PARTY", observed=True)["CALL_TIME"].count().to_dict()
                )

            wr = BtsWindowResult(
                key=key,
                total_rows=len(wdf),
                a_party=a_party_list(wdf),
                b_party=b_party_list(wdf),
            )

            if request.include_b_as_a:
                wr.b_as_a = b_as_a(wdf)

            if request.include_window_only and not raw_spec_df.empty:
                wr.window_only = window_only_presence(wdf, raw_spec_df)

            if request.include_hourly_density:
                wr.hourly_density = hourly_density(wdf, key)

            if request.include_imei_anomalies:
                wr.imei_anomalies = imei_anomalies(wdf)

            if request.include_bursts:
                wr.burst_callers = burst_callers(
                    wdf,
                    threshold=request.burst_threshold_calls,
                    window_minutes=request.burst_window_minutes,
                )

            if request.include_top_facts:
                wr.top_facts = compute_top_facts(wdf)

            window_results.append(wr)

    # --- Step 5: Cross-BTS and movement ---
    # Cross-BTS only when 2+ distinct specs (different towers); multiple windows
    # on the same tower don't qualify.
    distinct_specs = {k.spec_id for k in keys}
    cross = []
    if request.include_cross_bts_common and len(distinct_specs) >= 2:
        cross = cross_bts_common(per_window_a, keys, top=0, per_window_counts=per_window_counts)

    mv = []
    if request.include_movement and len(distinct_specs) >= 2:
        mv = movement_feasibility(bts_df, keys)

    analysis = BtsAnalysis(
        window_results=window_results,
        cross_bts_common=cross,
        movement_feasibility=mv,
        total_bts_records=total_records_all,
    )

    # Raw calls sheet: filter each spec by its own windows (a global [w_min, w_max]
    # would leak rows from other specs' wider windows into this spec's narrower one).
    if all_windows:
        calls_only = raw_bts_df[raw_bts_df["DIRECTION"].isin(["IN", "OUT"])]
        per_spec_frames = []
        for spec in request.specs:
            if not spec.windows:
                continue
            spec_rows = calls_only[calls_only["SPEC_ID"] == spec.spec_id]
            if spec_rows.empty:
                continue
            mask = pd.Series(False, index=spec_rows.index)
            for w in spec.windows:
                mask |= (
                    (spec_rows["CALL_TIME"] >= pd.Timestamp(w.start))
                    & (spec_rows["CALL_TIME"] <= pd.Timestamp(w.end))
                )
            per_spec_frames.append(spec_rows[mask])
        raw_calls_df = (
            pd.concat(per_spec_frames, ignore_index=False).copy()
            if per_spec_frames else calls_only.iloc[0:0].copy()
        )
    else:
        raw_calls_df = raw_bts_df[raw_bts_df["DIRECTION"].isin(["IN", "OUT"])].copy()

    # --- Step 6: Render ---
    logger.info("BTS rendering started")
    single_bts = len(distinct_specs) == 1
    pdf_bytes = render_bts_pdf(analysis, request, file_info=file_info, spec_tower_map=spec_tower_map)
    excel_bytes = render_bts_excel(analysis, request, bts_df=raw_calls_df, single_bts=single_bts)

    del bts_df, raw_bts_df, raw_calls_df
    gc.collect()

    return pdf_bytes, excel_bytes, analysis
