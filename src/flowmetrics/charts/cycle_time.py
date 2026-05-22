"""Layer 2 — the cycle-time chart model.

`build_cycle_time_model` turns raw `CompletedItem` rows + a view
window into a `CycleTimeModel`: every chart decision resolved,
nothing left for the view to decide. Pure Python — no DuckDB, no
Vega.

The cap control, the tick policy and the linear-interpolation
percentile live HERE for now. When the aging chart needs them too
(refactor Slice 2) they move to `flowmetrics.charts.primitives`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..utc_dates import attach_utc, to_utc_display_date, to_utc_iso_date
from ..warehouse.queries import CompletedItem
from ..windows import Window


@dataclass(frozen=True)
class CyclePoint:
    """One completed item, positioned on the scatter. Dates are
    pre-formatted UTC strings — the view never re-formats them
    (Vega's temporal formatter would shift them to browser-local)."""

    item_id: str
    title: str
    url: str | None
    completed_at: str          # ISO date, UTC
    completed_at_display: str  # "Jan 04, 2026", UTC
    cycle_time_days: float


@dataclass(frozen=True)
class TickPolicy:
    """X-axis tick/gridline interval. Span-adaptive so a multi-month
    window doesn't hatch the plot with one gridline per day."""

    interval: str  # "day" | "week" | "month"
    step: int


@dataclass(frozen=True)
class CapControl:
    """A y-axis cap slider. The view FILTERS out items above the
    slider value and lets the axis re-scale. Runs `floor`..`ceiling`
    and opens at `default`."""

    floor: int
    ceiling: int
    default: int


@dataclass(frozen=True)
class CycleTimeModel:
    """Fully-resolved cycle-time chart. The template and the Vega
    view read these fields; neither re-derives anything."""

    item_count: int
    points: tuple[CyclePoint, ...]
    p50: float
    p85: float
    p95: float
    headline: str
    ticks: TickPolicy
    x_domain: tuple[str, str] | None
    cap: CapControl | None

    @property
    def is_empty(self) -> bool:
        return self.item_count == 0


def _percentile_cont(values: list[float], p: float) -> float:
    """Linear-interpolation percentile — matches DuckDB
    `percentile_cont`. Empty sample → 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = p * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    return ordered[lo] + (rank - lo) * (ordered[hi] - ordered[lo])


def _tick_policy(span_days: int) -> TickPolicy:
    """Tick interval scales with the window span — a fixed daily
    interval hatches a multi-month view into a grey wash."""
    if span_days <= 30:
        return TickPolicy("day", 1)
    if span_days <= 210:
        return TickPolicy("week", 1)
    if span_days <= 1095:
        return TickPolicy("month", 1)
    return TickPolicy("month", 3)


def _utc_iso(dt: datetime) -> str:
    return to_utc_iso_date(attach_utc(dt))


def _coverage_display(d: date) -> str:
    return to_utc_display_date(attach_utc(datetime.combine(d, time.min)))


def build_cycle_time_model(
    items: list[CompletedItem], *, view: Window | None
) -> CycleTimeModel:
    """Resolve the cycle-time chart model from completed-item rows.

    `view` clamps the scatter to items completed inside the
    inclusive window; the P50/P85/P95 lines are the empirical
    percentiles of those SAME items — the lines summarise the dots
    on screen. When `view` is None the full history is used.
    """
    windowed: list[tuple[CompletedItem, str]] = []
    for it in items:
        iso = _utc_iso(it.completed_at)
        if view is not None:
            d = date.fromisoformat(iso)
            if not (view.from_ <= d <= view.to):
                continue
        windowed.append((it, iso))

    if not windowed:
        return _empty_model(items)

    points = tuple(
        CyclePoint(
            item_id=it.item_id,
            title=it.title or "",
            url=it.url,
            completed_at=iso,
            completed_at_display=to_utc_display_date(attach_utc(it.completed_at)),
            cycle_time_days=it.cycle_time_days or 0.0,
        )
        for it, iso in windowed
    )

    # Percentiles sample the windowed items' cycle times — the
    # lines summarise the dots on screen. Items with no recorded
    # cycle time are still plotted (at 0) but not sampled.
    cycle_sample = [
        it.cycle_time_days
        for it, _ in windowed
        if it.cycle_time_days is not None
    ]
    p50 = _percentile_cont(cycle_sample, 0.50)
    p85 = _percentile_cont(cycle_sample, 0.85)
    p95 = _percentile_cont(cycle_sample, 0.95)

    headline = (
        f"{len(points)} items completed · "
        f"P50 {p50:.1f}d · P85 {p85:.1f}d · P95 {p95:.1f}d"
    )

    point_dates = sorted({p.completed_at for p in points})
    first = date.fromisoformat(point_dates[0])
    last = date.fromisoformat(point_dates[-1])
    # Pad the domain one day each side so first/last-day dots (and
    # their forward jitter) aren't half-clipped at the plot edges.
    x_domain = (
        (first - timedelta(days=1)).isoformat(),
        (last + timedelta(days=1)).isoformat(),
    )

    return CycleTimeModel(
        item_count=len(points),
        points=points,
        p50=p50,
        p85=p85,
        p95=p95,
        headline=headline,
        ticks=_tick_policy((last - first).days),
        x_domain=x_domain,
        cap=_cap_control(points, p95),
    )


def _cap_control(
    points: tuple[CyclePoint, ...], p95: float
) -> CapControl | None:
    """The y-cap slider runs from the P95 line up to the slowest
    item. Absent when there's nothing to crop — fewer than two
    items, or P95 already at the maximum."""
    cycle_vals = sorted(p.cycle_time_days for p in points)
    if len(cycle_vals) < 2:
        return None
    floor = math.ceil(p95)
    ceiling = math.ceil(cycle_vals[-1])
    if floor >= ceiling:
        return None
    return CapControl(floor=floor, ceiling=ceiling, default=ceiling)


def _empty_model(all_items: list[CompletedItem]) -> CycleTimeModel:
    """Empty-state model. Distinguishes 'nothing materialised at
    all' from 'nothing in this window' — the operator needs the
    right fix (backfill vs. widen the view)."""
    if not all_items:
        headline = (
            "No data materialised yet — open the Data Source "
            "page to fetch completions from the source system."
        )
    else:
        dates = sorted(
            date.fromisoformat(_utc_iso(i.completed_at)) for i in all_items
        )
        headline = (
            "No completed items in this window. The warehouse "
            f"covers {_coverage_display(dates[0])} – "
            f"{_coverage_display(dates[-1])} "
            f"({len(all_items)} completed items) — widen the "
            "view window to see them."
        )
    return CycleTimeModel(
        item_count=0,
        points=(),
        p50=0.0,
        p85=0.0,
        p95=0.0,
        headline=headline,
        ticks=TickPolicy("day", 1),
        x_domain=None,
        cap=None,
    )
