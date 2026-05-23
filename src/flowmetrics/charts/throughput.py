"""Layer 2 — the throughput chart model.

`build_throughput_model` turns completed-item rows + a view window
into a `ThroughputModel`: the daily series (zero-completion days
included, per Vacanti), weekday/weekend classification,
warehouse-vs-missing coverage tagging, and the headline. Pure
Python — no DuckDB, no Vega.

Reference: Vacanti, *Actionable Agile Metrics for Predictability*,
10th Anniversary Edition, pp. 61–63.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from ..throughput import daily_counts
from ..utc_dates import attach_utc, to_utc_display_date, to_utc_iso_date
from ..warehouse.queries import CompletedItem
from ..windows import Window
from .primitives import Percentiles, percentiles_from


@dataclass(frozen=True)
class DailyThroughput:
    """One day of the throughput series.

    A zero count on a `warehouse` day is a TRUE zero (no completions
    that day); a "zero" on a `missing` day is a GAP — no data for
    that date, backfill-able from the system of record. The view
    paints them differently.
    """

    date_iso: str          # YYYY-MM-DD (UTC)
    date_display: str      # "May 04, 2026" — pre-formatted for tooltips
    count: int
    day_type: Literal["weekday", "weekend", "holiday"]
    data_coverage: Literal["warehouse", "missing", "stale"] = "warehouse"


@dataclass(frozen=True)
class ThroughputReference:
    """Empirical P50/P85 of the daily throughput counts — the
    "throughput reference band" the chart draws as horizontal
    rule marks. Both variants are pre-computed so the view can
    toggle include-/exclude-weekends without re-querying.

    `weekdays_only` is None when the window has no warehouse-covered
    weekday (a Sat-only span, for example).
    """

    include_weekends: Percentiles
    weekdays_only: Percentiles | None


@dataclass(frozen=True)
class ThroughputModel:
    """Fully-resolved throughput chart. The template and the Vega
    view read these fields; neither re-derives anything."""

    daily: tuple[DailyThroughput, ...]
    headline: str
    reference: ThroughputReference | None = None

    @property
    def is_empty(self) -> bool:
        return not self.daily


def _utc_date(dt: datetime) -> date:
    return attach_utc(dt).date()


def build_throughput_model(
    items: list[CompletedItem], *, view: Window | None = None,
) -> ThroughputModel:
    """Resolve the throughput model.

    `view` clamps the x-axis (and the headline's window) to the
    inclusive range; days outside the warehouse's completion span
    are tagged `missing` so a "zero" gap is visually distinct from
    a real zero-completion day. When `view` is None the window is
    data-derived (first completion → last completion).
    """
    if not items:
        return ThroughputModel(
            daily=(),
            headline="No completed items in this window.",
        )

    completion_dates = [_utc_date(it.completed_at) for it in items]
    # The warehouse's completion span — drives the
    # warehouse-vs-missing coverage tag for each day.
    warehouse_start = min(completion_dates)
    warehouse_stop = max(completion_dates)

    if view is not None:
        windowed = [d for d in completion_dates if view.from_ <= d <= view.to]
    else:
        windowed = completion_dates

    if not windowed:
        return ThroughputModel(
            daily=(),
            headline="No completed items in this window.",
        )

    # Window span: chosen Period when a view is set (Vacanti — the
    # rate divides over the PERIOD, not the observed-completion
    # span); data-derived otherwise.
    if view is not None:
        first_d, last_d = view.from_, view.to
    else:
        first_d, last_d = min(windowed), max(windowed)

    # Shared per-day count helper — same primitive the forecast's
    # daily-throughput sample uses.
    counts_per_day = daily_counts(windowed, first_d, last_d)

    daily: list[DailyThroughput] = []
    for i, count in enumerate(counts_per_day):
        cur = first_d + timedelta(days=i)
        coverage: Literal["warehouse", "missing", "stale"] = (
            "warehouse" if warehouse_start <= cur <= warehouse_stop else "missing"
        )
        day_type: Literal["weekday", "weekend", "holiday"] = (
            "weekend" if cur.weekday() >= 5 else "weekday"
        )
        anchored = datetime.combine(cur, time.min, tzinfo=UTC)
        daily.append(
            DailyThroughput(
                date_iso=to_utc_iso_date(anchored),
                date_display=to_utc_display_date(anchored),
                count=count,
                day_type=day_type,
                data_coverage=coverage,
            )
        )

    total = sum(d.count for d in daily)
    span_days = len(daily)
    # Average divides by COVERED days only — days the warehouse
    # actually has data for. Missing days aren't "zero items
    # completed," they're "we didn't observe this," so averaging
    # them in would understate the rate.
    covered_days = sum(1 for d in daily if d.data_coverage == "warehouse")
    avg = total / covered_days if covered_days else 0.0
    if covered_days == span_days:
        headline = (
            f"{total} items over {span_days} day"
            f"{'' if span_days == 1 else 's'} · {avg:.1f}/day"
        )
    else:
        headline = (
            f"{total} items · {avg:.1f}/day over {covered_days} day"
            f"{'' if covered_days == 1 else 's'} with data "
            f"({span_days}-day window)"
        )

    # Reference band: empirical P50/P85 of the daily counts, drawn
    # from COVERED days only — a `missing` day is no-data, not zero
    # throughput, so including it would deflate the percentiles.
    # Both variants are precomputed so the view can toggle
    # include-/exclude-weekends without round-tripping to the model.
    covered_counts = [d.count for d in daily if d.data_coverage == "warehouse"]
    weekday_counts = [
        d.count for d in daily
        if d.data_coverage == "warehouse" and d.day_type == "weekday"
    ]
    reference = ThroughputReference(
        include_weekends=percentiles_from(covered_counts),
        weekdays_only=(
            percentiles_from(weekday_counts) if weekday_counts else None
        ),
    ) if covered_counts else None

    return ThroughputModel(
        daily=tuple(daily), headline=headline, reference=reference,
    )
