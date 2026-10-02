"""Activity and heatmap analysis."""

from __future__ import annotations

import pandas as pd

from cdr_report_app.domain.analysis_models import ActivityBin, DailyActivityAnalysis, HeatmapAnalysis
from cdr_report_app.domain.cdr_models import CrimeContext


def build_daily_activity(df: pd.DataFrame, crime_context: CrimeContext) -> DailyActivityAnalysis:
    valid = df.dropna(subset=["_DT"])
    if valid.empty:
        return DailyActivityAnalysis()

    if crime_context.crime_date:
        crime_ts = pd.Timestamp(crime_context.crime_date).normalize()
        day_before = crime_ts - pd.Timedelta(days=1)
        day_after = crime_ts + pd.Timedelta(days=1)
        filtered = valid[(valid["_DT"] >= day_before) & (valid["_DT"] < day_after + pd.Timedelta(days=1))]
        bins: list[ActivityBin] = []
        palette = ["#4facfe", "#dc3545", "#43e97b"]
        labels = ["Day Before", "Crime Day", "Day After"]

        for idx, day in enumerate([day_before, crime_ts, day_after]):
            day_df = filtered[filtered["_DT"].dt.normalize() == day]
            hourly = day_df.groupby(day_df["_DT"].dt.hour).size()
            for hour in range(0, 24, 3):
                count = int(sum(hourly.get(h, 0) for h in range(hour, min(hour + 3, 24))))
                bins.append(
                    ActivityBin(
                        label=f"{hour:02d}-{min(hour + 3, 24):02d}h",
                        value=count,
                        color=palette[idx],
                        group=labels[idx],
                    )
                )

        busiest_hour = None
        busiest_count = None
        if not filtered.empty:
            hourly_all = filtered.groupby(filtered["_DT"].dt.hour).size()
            if not hourly_all.empty:
                busiest_hour = int(hourly_all.idxmax())
                busiest_count = int(hourly_all.max())

        return DailyActivityAnalysis(
            title_suffix=f"Around Crime Date: {crime_ts.date()}",
            bins=bins,
            busiest_hour=busiest_hour,
            busiest_hour_count=busiest_count,
        )

    daily = valid.groupby(valid["_DT"].dt.date).size()
    bins = [ActivityBin(label=str(day), value=int(count)) for day, count in daily.items()]
    return DailyActivityAnalysis(bins=bins)


def build_heatmap(df: pd.DataFrame) -> HeatmapAnalysis:
    valid = df.dropna(subset=["_DT"])
    if valid.empty:
        return HeatmapAnalysis()

    valid = valid.copy()
    valid["_HOUR"] = valid["_DT"].dt.hour
    valid["_DAY_NAME"] = valid["_DT"].dt.day_name()
    day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    pivot = valid.groupby(["_HOUR", "_DAY_NAME"]).size().unstack(fill_value=0)
    pivot = pivot.reindex(index=range(24), fill_value=0)
    pivot = pivot.reindex(columns=[day for day in day_order if day in pivot.columns], fill_value=0)

    return HeatmapAnalysis(
        day_labels=[day[:3].upper() for day in pivot.columns],
        hour_labels=[f"{hour:02d}:00" for hour in pivot.index],
        matrix=[[int(value) for value in row] for row in pivot.values.tolist()],
    )
