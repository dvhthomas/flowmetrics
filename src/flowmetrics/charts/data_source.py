"""Layer 2 — the Data Source coverage model.

`build_data_source_model` turns per-day work-item-creation counts
into a `DataSourceModel`: every day in the (capped) span, the
log-bucket coverage level for each, the Monday-anchored week and
weekday for the calendar layout, the month tick list, and the
headline. Pure Python — no DuckDB, no Vega.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from ..utc_dates import to_utc_display_date

# The chart shows at most this many daily cells — the most recent
# stretch of the data (~26 week columns in the calendar grid).
MAX_DAYS = 180

_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _coverage_level(records: int, max_records: int) -> str:
    """Bucket a day's record count into a heat-map level. `"None"`
    = nothing created that day; otherwise Low / Medium / High by
    thirds of a LOG scale — log so one spike day doesn't flatten
    every other day into the bottom bucket."""
    if records <= 0:
        return "None"
    if max_records <= 1:
        return "Low"
    t = math.log(records) / math.log(max_records)  # 0 … 1
    if t <= 1 / 3:
        return "Low"
    if t <= 2 / 3:
        return "Medium"
    return "High"


@dataclass(frozen=True)
class DayBucket:
    """One day on the coverage chart, including its calendar
    coordinates (Monday-anchored week + weekday name) and the
    pre-bucketed level."""

    day_iso: str       # "2025-04-05"
    day_display: str   # "Apr 05, 2025"
    weekday: str       # "Mon" … "Sun"
    week_iso: str      # Monday-anchored week start, ISO
    records: int
    level: str         # "None" | "Low" | "Medium" | "High"


@dataclass(frozen=True)
class DataSourceModel:
    """Fully-resolved Data Source coverage payload."""

    days: tuple[DayBucket, ...]
    week_starts: tuple[str, ...]  # in calendar order, deduplicated
    month_ticks: tuple[str, ...]  # week_iso values to show on axis
    total_records: int
    latest_display: str | None
    headline: str

    @property
    def is_empty(self) -> bool:
        return not self.days


def _display(d: date) -> str:
    return to_utc_display_date(datetime(d.year, d.month, d.day, tzinfo=UTC))


def build_data_source_model(
    per_day: list[tuple[date, int]],
) -> DataSourceModel:
    """Resolve the Data Source model from per-day creation counts.

    Empty input → an empty-state model with a "no work items yet"
    headline. Otherwise the span is the most recent `MAX_DAYS` of
    data (zero-filled), and each day is bucketed by log-thirds of
    the maximum daily count.
    """
    if not per_day:
        return DataSourceModel(
            days=(),
            week_starts=(),
            month_ticks=(),
            total_records=0,
            latest_display=None,
            headline=(
                "No work items in the warehouse yet — use Backfill "
                "below to fetch data."
            ),
        )

    counts_by_date = {d: c for d, c in per_day}
    earliest = min(counts_by_date)
    latest = max(counts_by_date)
    total = sum(counts_by_date.values())

    # Cap the span at the most recent MAX_DAYS days so a long-lived
    # workflow doesn't blow the cell count up.
    span_start = max(earliest, latest - timedelta(days=MAX_DAYS - 1))
    max_records = max(counts_by_date.values()) if counts_by_date else 1

    days: list[DayBucket] = []
    week_starts: list[str] = []
    seen_weeks: set[str] = set()
    month_ticks: list[str] = []
    seen_months: set[str] = set()
    cur = span_start
    while cur <= latest:
        records = counts_by_date.get(cur, 0)
        # Monday-anchored week — the cell's x column.
        week_start = (cur - timedelta(days=cur.weekday())).isoformat()
        if week_start not in seen_weeks:
            seen_weeks.add(week_start)
            week_starts.append(week_start)
            # One x-axis label per month: the first week column
            # whose Monday lands in a new calendar month.
            ym = week_start[:7]
            if ym not in seen_months:
                seen_months.add(ym)
                month_ticks.append(week_start)
        days.append(
            DayBucket(
                day_iso=cur.isoformat(),
                day_display=_display(cur),
                weekday=_WEEKDAY_NAMES[cur.weekday()],
                week_iso=week_start,
                records=records,
                level=_coverage_level(records, max_records),
            )
        )
        cur += timedelta(days=1)

    earliest_display = _display(earliest)
    latest_display = _display(latest)
    headline = (
        f"{total} work item{'' if total == 1 else 's'} in the "
        f"warehouse · earliest work item {earliest_display}, "
        f"most recent work item {latest_display}"
    )
    return DataSourceModel(
        days=tuple(days),
        week_starts=tuple(week_starts),
        month_ticks=tuple(month_ticks),
        total_records=total,
        latest_display=latest_display,
        headline=headline,
    )
