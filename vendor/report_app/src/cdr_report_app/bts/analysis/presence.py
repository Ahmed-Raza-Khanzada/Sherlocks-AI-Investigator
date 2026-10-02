"""Party presence analytics: A-party, B-party, B-as-A, window-only."""

from __future__ import annotations

import pandas as pd

from cdr_report_app.bts.models import APartyRow, BAsARow, BPartyRow, WindowOnlyEntry


def _top_str_value(series: pd.Series) -> str | None:
    """Most common non-empty string value in a series, or None."""
    s = series.dropna().astype(str).str.strip()
    s = s[~s.isin({"", "<NA>", "nan", "None"})]
    if s.empty:
        return None
    return str(s.mode().iloc[0])


def a_party_list(window_df: pd.DataFrame, top: int = 0) -> list[APartyRow]:
    """Summarise A-party activity within a window slice."""
    df = window_df.dropna(subset=["A_PARTY"])
    if df.empty:
        return []

    grp = df.groupby("A_PARTY", observed=True)
    counts = grp["CALL_TIME"].count().rename("call_count")
    total_dur = grp.apply(lambda g: int(pd.to_numeric(g["DURATION_SEC"], errors="coerce").fillna(0).sum()), include_groups=False).rename("total_duration_sec")
    first_seen = grp["CALL_TIME"].min().rename("first_seen")
    last_seen = grp["CALL_TIME"].max().rename("last_seen")

    direction_in = (
        df[df["DIRECTION"] == "IN"].groupby("A_PARTY", observed=True)["CALL_TIME"].count().rename("direction_in")
    )
    direction_out = (
        df[df["DIRECTION"] == "OUT"].groupby("A_PARTY", observed=True)["CALL_TIME"].count().rename("direction_out")
    )

    has_cell = "CELL_ID" in df.columns
    has_lac = "LAC_ID" in df.columns
    top_cell_s = grp["CELL_ID"].agg(_top_str_value).rename("top_cell") if has_cell else None
    top_lac_s = grp["LAC_ID"].agg(_top_str_value).rename("top_lac") if has_lac else None

    numeric_parts = [counts, total_dur, first_seen, last_seen, direction_in, direction_out]
    summary = pd.concat(numeric_parts, axis=1).fillna(0)

    # Attach string columns without fillna (Arrow-backed strings reject fillna(0))
    if top_cell_s is not None:
        summary = summary.join(top_cell_s.to_frame(), how="left")
    if top_lac_s is not None:
        summary = summary.join(top_lac_s.to_frame(), how="left")

    summary = summary.sort_values("call_count", ascending=False)
    if top:
        summary = summary.head(top)

    result = []
    for msisdn, row in summary.iterrows():
        def _safe_str(v) -> str | None:
            if v is None:
                return None
            try:
                if pd.isna(v):
                    return None
            except (TypeError, ValueError):
                pass
            s = str(v).strip()
            return None if not s or s in {"<NA>", "nan", "None", "0"} else s

        result.append(
            APartyRow(
                msisdn=str(msisdn),
                call_count=int(row["call_count"]),
                total_duration_sec=int(row["total_duration_sec"]),
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
                direction_in=int(row["direction_in"]),
                direction_out=int(row["direction_out"]),
                top_cell=_safe_str(row.get("top_cell")),
                top_lac=_safe_str(row.get("top_lac")),
            )
        )
    return result


def b_party_list(window_df: pd.DataFrame, top: int = 0) -> list[BPartyRow]:
    """Summarise B-party (call destination/source partner) activity."""
    df = window_df.dropna(subset=["B_PARTY"])
    if df.empty:
        return []

    grp = df.groupby("B_PARTY", observed=True)
    counts = grp["CALL_TIME"].count().rename("call_count")
    total_dur = grp.apply(lambda g: int(pd.to_numeric(g["DURATION_SEC"], errors="coerce").fillna(0).sum()), include_groups=False).rename("total_duration_sec")
    first_seen = grp["CALL_TIME"].min().rename("first_seen")
    last_seen = grp["CALL_TIME"].max().rename("last_seen")

    has_cell = "CELL_ID" in df.columns
    has_lac = "LAC_ID" in df.columns
    top_cell_s = grp["CELL_ID"].agg(_top_str_value).rename("top_cell") if has_cell else None
    top_lac_s = grp["LAC_ID"].agg(_top_str_value).rename("top_lac") if has_lac else None

    numeric_parts = [counts, total_dur, first_seen, last_seen]
    summary = pd.concat(numeric_parts, axis=1).fillna(0)

    # Attach string columns without fillna (Arrow-backed strings reject fillna(0))
    if top_cell_s is not None:
        summary = summary.join(top_cell_s.to_frame(), how="left")
    if top_lac_s is not None:
        summary = summary.join(top_lac_s.to_frame(), how="left")

    summary = summary.sort_values("call_count", ascending=False)
    if top:
        summary = summary.head(top)

    result = []
    for msisdn, row in summary.iterrows():
        def _safe_str(v) -> str | None:
            if v is None:
                return None
            try:
                if pd.isna(v):
                    return None
            except (TypeError, ValueError):
                pass
            s = str(v).strip()
            return None if not s or s in {"<NA>", "nan", "None", "0"} else s

        result.append(
            BPartyRow(
                msisdn=str(msisdn),
                call_count=int(row["call_count"]),
                total_duration_sec=int(row["total_duration_sec"]),
                first_seen=row["first_seen"],
                last_seen=row["last_seen"],
                top_cell=_safe_str(row.get("top_cell")),
                top_lac=_safe_str(row.get("top_lac")),
            )
        )
    return result


def b_as_a(window_df: pd.DataFrame) -> list[BAsARow]:
    """For each A-party, find which of their B-party contacts are also A-party at this tower.

    A B-party appearing as A-party means they were physically present at the tower
    (making/receiving calls logged here), not just a remote contact.
    """
    a_set = set(window_df["A_PARTY"].dropna().astype(str).unique())
    df = window_df.dropna(subset=["A_PARTY", "B_PARTY"])
    if df.empty or not a_set:
        return []

    result = []
    for a_party, grp in df.groupby("A_PARTY", observed=True):
        raw_b = grp["B_PARTY"].astype(str).str.strip().unique()
        b_at_tower = sorted(
            b for b in raw_b
            if b and b not in {"<NA>", "nan", "None"} and b in a_set
        )
        if b_at_tower:
            result.append(BAsARow(
                a_party=str(a_party),
                b_parties_at_tower=b_at_tower,
                contact_count=len(b_at_tower),
            ))
    result.sort(key=lambda r: r.contact_count, reverse=True)
    return result


def window_only_presence(window_df: pd.DataFrame, full_spec_df: pd.DataFrame) -> list[WindowOnlyEntry]:
    """A-party numbers active only in the window — absent in the full spec file outside it.

    *full_spec_df* must be the raw (pre-filter) spec data so the exclusivity check
    covers the entire file period, not just the window range.
    """
    w_nums = set(window_df["A_PARTY"].dropna().astype(str).unique())
    if not w_nums:
        return []

    w_start = window_df["CALL_TIME"].min()
    w_end = window_df["CALL_TIME"].max()
    outside = full_spec_df[
        (full_spec_df["CALL_TIME"] < w_start) | (full_spec_df["CALL_TIME"] > w_end)
    ]
    outside_nums = set(outside["A_PARTY"].dropna().astype(str).unique())

    exclusive = w_nums - outside_nums
    if not exclusive:
        return []

    df_excl = window_df[window_df["A_PARTY"].isin(exclusive)]
    result = []
    for msisdn, grp in df_excl.groupby("A_PARTY", observed=True):
        if "B_PARTY" in grp.columns:
            raw_b = grp["B_PARTY"].dropna().astype(str).str.strip().unique()
            b_parties = sorted(b for b in raw_b if b and b not in {"<NA>", "nan", "None"})
        else:
            b_parties = []
        result.append(WindowOnlyEntry(
            msisdn=str(msisdn),
            call_count=int(len(grp)),
            b_parties=b_parties,
        ))
    result.sort(key=lambda r: r.call_count, reverse=True)
    return result
