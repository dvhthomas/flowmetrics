"""Layer 2 — the aging-WIP chart model.

`build_aging_model` turns an in-flight snapshot + completed-item
rows into an `AgingModel`: every chart decision resolved — the
per-item age, the WIP filter, the percentile thresholds and their
provenance, the empty-state classification, the cap control, the
per-state column order and WIP-count badges. Pure Python — no
DuckDB, no Vega.

Percentiles and the cap slider are shared primitives
(`flowmetrics.charts.primitives`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime

from ..utc_dates import attach_utc, to_utc_display_date
from ..warehouse.queries import CompletedItem, InFlightItem
from ..windows import Window
from .primitives import (
    Percentiles,
    RangeControl,
    percentiles_from,
    range_control,
)


@dataclass(frozen=True)
class AgingPoint:
    """One in-flight item at the asof date."""

    item_id: str
    title: str
    url: str | None
    current_state: str
    age_days: int


@dataclass(frozen=True)
class AgingModel:
    """Fully-resolved aging-WIP chart. The template and the Vega
    view read these fields; neither re-derives anything.

    `empty_state` is None when `items` is non-empty; otherwise one
    of `asof_after_coverage` / `asof_before_coverage` /
    `in_flight_never_captured` / `no_work_in_flight`.
    """

    items: tuple[AgingPoint, ...]
    count: int
    asof_iso: str
    asof_display: str
    headline: str
    empty_state: str | None
    percentiles: Percentiles
    coverage_earliest_display: str | None
    coverage_latest_display: str | None
    ordered_states: tuple[str, ...]
    wip_badges: tuple[tuple[str, int], ...]
    cap: RangeControl | None

    @property
    def is_empty(self) -> bool:
        return self.count == 0


def _utc_date(dt: datetime) -> date:
    return attach_utc(dt).date()


def _display(d: date) -> str:
    return to_utc_display_date(datetime(d.year, d.month, d.day, tzinfo=UTC))


def build_aging_model(
    in_flight: list[InFlightItem],
    completed: list[CompletedItem],
    *,
    asof: date,
    open_item_count: int,
    reference: Window | None = None,
    wip_states: frozenset[str] | None = None,
) -> AgingModel:
    """Resolve the aging-WIP chart model.

    `asof` is the in-flight snapshot date. Age is the
    CD - SD + 1 rule (a same-day item ages as 1d). `wip_states`, when
    given, keeps only items whose current state is a WIP state —
    backlog and done fall out. Percentile thresholds come from
    completed cycle times inside `reference` (full history when
    None) — the same sample the cycle-time chart uses.
    `open_item_count` is the count of work items the warehouse
    holds with no completion — it separates a never-captured
    snapshot from a genuinely empty one.
    """
    asof_display = _display(asof)

    # In-flight items, aged. The WIP filter drops non-WIP states.
    items: list[AgingPoint] = []
    for it in in_flight:
        if wip_states is not None and it.current_state not in wip_states:
            continue
        age = (asof - _utc_date(it.created_at)).days + 1
        items.append(
            AgingPoint(
                item_id=it.item_id,
                title=it.title or "",
                url=it.url,
                current_state=it.current_state,
                age_days=int(age),
            )
        )

    # Percentile sample — completed cycle times inside the
    # reference window (full history when no reference is set).
    sample: list[float] = []
    sample_dates: list[date] = []
    for c in completed:
        if c.cycle_time_days is None:
            continue
        cdate = _utc_date(c.completed_at)
        if reference is not None and not (
            reference.from_ <= cdate <= reference.to
        ):
            continue
        sample.append(c.cycle_time_days)
        sample_dates.append(cdate)
    percentiles = percentiles_from(sample)
    sample_dates.sort()

    # Coverage — the completion span the warehouse holds (all
    # completed items, ignoring the reference window).
    completion_dates = sorted(_utc_date(c.completed_at) for c in completed)
    earliest_data = completion_dates[0] if completion_dates else None
    latest_data = completion_dates[-1] if completion_dates else None

    ordered_states: list[str] = []
    for i in items:
        if i.current_state not in ordered_states:
            ordered_states.append(i.current_state)
    counts: dict[str, int] = {}
    for i in items:
        counts[i.current_state] = counts.get(i.current_state, 0) + 1

    return AgingModel(
        items=tuple(items),
        count=len(items),
        asof_iso=asof.isoformat(),
        asof_display=asof_display,
        headline=_headline(items, asof_display, percentiles, sample_dates),
        empty_state=_empty_state(
            items, asof, earliest_data, latest_data, open_item_count
        ),
        percentiles=percentiles,
        coverage_earliest_display=(
            _display(earliest_data) if earliest_data else None
        ),
        coverage_latest_display=(
            _display(latest_data) if latest_data else None
        ),
        ordered_states=tuple(ordered_states),
        wip_badges=tuple((s, counts[s]) for s in ordered_states),
        cap=_cap(items, percentiles),
    )


def _headline(
    items: list[AgingPoint],
    asof_display: str,
    percentiles: Percentiles,
    sample_dates: list[date],
) -> str:
    count_part = (
        f"{len(items)} in-flight item{'' if len(items) == 1 else 's'} "
        f"as of {asof_display} (UTC)"
    )
    if percentiles.source_count > 0:
        window = (
            f"{_display(sample_dates[0])} – {_display(sample_dates[-1])}"
            if sample_dates
            else "no completed items yet"
        )
        n = percentiles.source_count
        pct_part = (
            f"P50 {percentiles.p50:.1f}d · P85 {percentiles.p85:.1f}d · "
            f"P95 {percentiles.p95:.1f}d from {n} completed "
            f"item{'' if n == 1 else 's'} ({window})"
        )
    else:
        # 0/0/0 percentiles would print "P50 0.0d" — a fake
        # threshold. Say plainly there is no sample.
        pct_part = (
            "no percentile thresholds — no completed items in the "
            "reference period"
        )
    return f"{count_part} · {pct_part}"


def _empty_state(
    items: list[AgingPoint],
    asof: date,
    earliest_data: date | None,
    latest_data: date | None,
    open_item_count: int,
) -> str | None:
    """Action-first classification of an empty chart — what the
    operator must do to get an answer."""
    if items:
        return None
    if latest_data is not None and asof > latest_data:
        return "asof_after_coverage"
    if earliest_data is not None and asof < earliest_data:
        return "asof_before_coverage"
    if open_item_count == 0:
        return "in_flight_never_captured"
    return "no_work_in_flight"


def _cap(
    items: list[AgingPoint], percentiles: Percentiles
) -> RangeControl | None:
    """Cap slider — floors at the P95 commitment line, or, when
    there is no percentile line (no completed sample), at the P95
    of the in-flight ages themselves."""
    ages = [i.age_days for i in items]
    if percentiles.p95 > 0:
        floor: float = percentiles.p95
    elif ages:
        ordered = sorted(ages)
        idx = min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)
        floor = ordered[idx]
    else:
        floor = 0.0
    return range_control(floor, ages)
