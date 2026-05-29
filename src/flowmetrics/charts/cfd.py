"""Layer 2 — the Cumulative Flow Diagram (CFD) chart model.

`build_cfd_model` turns first-entry rows + a stage order + a view
window into a `CfdModel`: every chart decision resolved, including
the visual-window clamping and the y-floor crop bounds. Pure
Python — no DuckDB, no Vega.

The CFD invariants this preserves:

  #1  Top line = cumulative arrivals; bottom = cumulative departures.
  #2  No line decreases over time.
  #3  count(earlier) >= count(later) for every date and every
      adjacent pair (so the bands never cross).

`infer_stage_order` is the pairwise-precedence resolver used when
no contract YAML pins the workflow.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from ..utc_dates import to_utc_display_date
from ..warehouse.queries import StageEntry
from ..windows import Window
from .primitives import RangeControl


@dataclass(frozen=True)
class CfdDailyPoint:
    """Cumulative arrivals at each stage as of `date_iso`."""

    date_iso: str
    date_display: str
    counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CfdModel:
    """Fully-resolved CFD chart. The template and the Vega view
    read these fields; neither re-derives anything."""

    daily: tuple[CfdDailyPoint, ...]
    stages: tuple[str, ...]
    headline: str
    first_date_iso: str | None
    last_date_iso: str | None
    # The y-floor "crop base" slider — runs 0..left-edge-carry-in,
    # opens at 0. Absent when the window's left edge has no inert
    # carry-in to crop (the cumulative starts at 0 there).
    crop: RangeControl | None

    @property
    def is_empty(self) -> bool:
        return not self.daily


@dataclass(frozen=True)
class CfdDayMetrics:
    """The basic flow numbers for one day, derived from the CFD's
    cumulative counts. Surfaced on the CFD hover panel."""

    date_iso: str
    date_display: str
    wip_by_stage: dict[str, int]      # band heights, per stage
    total_wip: int                    # cumulative arrivals - departures
    arrivals: int                     # entered the system that day
    departures: int                   # completed that day
    throughput: float                 # avg departures/day, to date
    avg_cycle_time: float | None      # Little's Law: WIP / throughput


def daily_flow_metrics(model: CfdModel) -> tuple[CfdDayMetrics, ...]:
    """Per-day flow metrics from a `CfdModel`'s cumulative counts.

    `counts[stage]` is the cumulative arrivals that have reached
    `stage` (or later) by that day, so for stages in workflow order:
      - top line  = counts[first stage] = cumulative arrivals,
      - bottom    = counts[last stage]  = cumulative departures,
      - band[s]   = counts[s] - counts[next(s)] = WIP in stage s.

    Arrivals/departures are daily deltas of those cumulatives (the
    first day reports the cumulative-to-date, i.e. window carry-in +
    that day). Throughput is the to-date average departures/day (the
    slope of the bottom line); average cycle time is Little's Law,
    WIP / throughput.
    """
    if not model.daily or not model.stages:
        return ()
    stages = model.stages
    first, last = stages[0], stages[-1]
    out: list[CfdDayMetrics] = []
    prev_arrivals = prev_departures = 0
    for i, d in enumerate(model.daily):
        cum_arrivals = d.counts.get(first, 0)
        cum_departures = d.counts.get(last, 0)
        wip_by_stage: dict[str, int] = {}
        for j, s in enumerate(stages):
            cur = d.counts.get(s, 0)
            nxt = d.counts.get(stages[j + 1], 0) if j < len(stages) - 1 else 0
            wip_by_stage[s] = max(0, cur - nxt)
        total_wip = max(0, cum_arrivals - cum_departures)
        arrivals = cum_arrivals - prev_arrivals if i > 0 else cum_arrivals
        departures = (
            cum_departures - prev_departures if i > 0 else cum_departures
        )
        throughput = cum_departures / (i + 1)
        avg_cycle = (total_wip / throughput) if throughput > 0 else None
        out.append(CfdDayMetrics(
            date_iso=d.date_iso,
            date_display=d.date_display,
            wip_by_stage=wip_by_stage,
            total_wip=total_wip,
            arrivals=arrivals,
            departures=departures,
            throughput=throughput,
            avg_cycle_time=avg_cycle,
        ))
        prev_arrivals, prev_departures = cum_arrivals, cum_departures
    return tuple(out)


def infer_stage_order(
    pairs: list[tuple[str, str, int]], all_stages: list[str],
) -> tuple[str, ...]:
    """Pairwise-precedence ordering. Each stage's net `precedes`
    count (precedes − preceded-by) drives the sort: higher = earlier
    in workflow. Alphabetical tiebreak for stages that never
    co-occur. Robust against items skipping an early stage."""
    if not all_stages:
        return ()
    precedes: dict[str, int] = {s: 0 for s in all_stages}
    for earlier, later, cnt in pairs:
        precedes[earlier] = precedes.get(earlier, 0) + cnt
        precedes[later] = precedes.get(later, 0) - cnt
    return tuple(sorted(all_stages, key=lambda s: (-precedes[s], s)))


def cumulative_arrivals_by_stage(
    entries: list[StageEntry],
    stages: tuple[str, ...],
    *,
    sample_dates: list[date],
) -> list[dict[str, int]]:
    """For each date in `sample_dates`, the cumulative count of
    items that have entered each stage OR any later stage by that
    date. Returns one dict per sample date, keyed by stage.

    The CFD core — both `build_cfd_model` (the web layer)
    and `flowmetrics.cfd.build_cfd` (the CLI surface) call into
    this. Items that skipped an early stage are propagated backward
    so the bands never cross (#3 in the CFD invariants).
    """
    if not stages or not entries or not sample_dates:
        return [{s: 0 for s in stages} for _ in sample_dates]
    reached = _reached_dates(entries, stages)
    for s in reached:
        reached[s].sort()
    return [
        {stage: bisect_right(reached[stage], d) for stage in stages}
        for d in sample_dates
    ]


def _reached_dates(
    entries: list[StageEntry], stages: tuple[str, ...],
) -> dict[str, list[date]]:
    """For each stage, the dates per item at which the item reached
    that stage OR any later stage.

    Items that skipped an early stage propagate a later-stage entry
    backward to set a 'reached' date for every earlier stage too —
    without this expansion the bands cross (count(later) > count
    (earlier)) when an item enters mid-workflow.
    """
    per_item: dict[str, dict[str, date]] = {}
    for e in entries:
        per_item.setdefault(e.item_id, {})[e.stage] = e.entered_date
    reached: dict[str, list[date]] = {s: [] for s in stages}
    for item_entries in per_item.values():
        running: date | None = None
        for stage in reversed(stages):
            own = item_entries.get(stage)
            if own is not None and (running is None or own < running):
                running = own
            if running is not None:
                reached[stage].append(running)
    return reached


def _empty(headline: str = "No transitions yet.") -> CfdModel:
    return CfdModel(
        daily=(),
        stages=(),
        headline=headline,
        first_date_iso=None,
        last_date_iso=None,
        crop=None,
    )


def _display(d: date) -> str:
    return to_utc_display_date(datetime(d.year, d.month, d.day, tzinfo=UTC))


def build_cfd_model(
    entries: list[StageEntry],
    stages: tuple[str, ...],
    *,
    view: Window | None = None,
) -> CfdModel:
    """Build the CFD model from first-stage-entry rows.

    `view` is a VISUAL window — it clamps the x-axis viewport to
    its intersection with the observed data span. The cumulative
    math stays full-history, so the carry-in at the window's left
    edge is the true running total.
    """
    if not stages or not entries:
        return _empty()

    all_dates = [e.entered_date for e in entries]
    data_min, data_max = min(all_dates), max(all_dates)
    if view is not None:
        first_date = max(view.from_, data_min)
        last_date = min(view.to, data_max)
    else:
        first_date, last_date = data_min, data_max
    if first_date > last_date:
        return _empty("No transitions in the selected period.")

    sample_dates: list[date] = []
    cur = first_date
    while cur <= last_date:
        sample_dates.append(cur)
        cur += timedelta(days=1)
    counts_per_date = cumulative_arrivals_by_stage(
        entries, stages, sample_dates=sample_dates,
    )
    daily: list[CfdDailyPoint] = [
        CfdDailyPoint(
            date_iso=d.isoformat(),
            date_display=_display(d),
            counts=counts,
        )
        for d, counts in zip(sample_dates, counts_per_date, strict=True)
    ]

    terminal = stages[-1]
    departures = daily[-1].counts[terminal]
    # Distinct items the workflow has touched. count[stages[0]] would
    # undercount items that skipped the first stage (e.g. a PR opened
    # straight into Awaiting Review).
    total_distinct = len({e.item_id for e in entries})
    in_flight = total_distinct - departures
    headline = (
        f"{total_distinct} item{'' if total_distinct == 1 else 's'} touched · "
        f"{departures} departed · {in_flight} in the system · "
        f"{len(daily)} day{'' if len(daily) == 1 else 's'} "
        f"({_display(first_date)} – {_display(last_date)})"
    )

    base_carry_in = daily[0].counts[terminal]
    crop = (
        RangeControl(floor=0, ceiling=base_carry_in, default=0)
        if base_carry_in > 1 else None
    )

    return CfdModel(
        daily=tuple(daily),
        stages=stages,
        headline=headline,
        first_date_iso=first_date.isoformat(),
        last_date_iso=last_date.isoformat(),
        crop=crop,
    )
