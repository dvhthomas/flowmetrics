"""Stream-native consumers for the remaining flow-metric reports.

All four reports (CFD, Scatterplot, Throughput, Flow Efficiency)
implemented as pure functions over `Stream`. Same numeric outputs
as the legacy WorkItem-based modules; different inputs.

Living here in one module rather than alongside each legacy
module to keep the canonical-stream surface area easy to scan.
Once the legacy WorkItem-based reports are retired, these become
the canonical reports.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .stream import Stream

# ----------------------------------------------------------------------
# CFD - daily counts per stage over a date window.
# ----------------------------------------------------------------------


def cfd_daily_counts(stream: Stream, *, start: date, stop: date) -> list[dict]:
    """For each day in [start, stop], count how many items are in
    each stage of the workflow.

    Returns one dict per day: {date, counts: {stage: int}}.
    """
    stages = stream.workflow.stages
    out: list[dict] = []
    cur = start
    while cur <= stop:
        counts = {s: 0 for s in stages}
        for item in stream:
            stage = stream.current_stage_at(item.item_id, cur)
            if stage is not None:
                counts[stage] += 1
        out.append({"date": cur, "counts": counts})
        cur += timedelta(days=1)
    return out


# ----------------------------------------------------------------------
# Scatterplot - cycle time per completed item.
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class StreamScatterPoint:
    item_id: str
    title: str
    completed_at: datetime
    cycle_time_days: float
    url: str | None


def scatterplot_points(stream: Stream) -> list[StreamScatterPoint]:
    out: list[StreamScatterPoint] = []
    for item in stream:
        if item.completed_at is None:
            continue
        days = (item.completed_at - item.created_at).total_seconds() / 86400
        out.append(
            StreamScatterPoint(
                item_id=item.item_id,
                title=item.title,
                completed_at=item.completed_at,
                cycle_time_days=days,
                url=item.url,
            )
        )
    return out


# ----------------------------------------------------------------------
# Throughput - completions-per-day in a window.
# ----------------------------------------------------------------------


def throughput_per_day(stream: Stream, *, start: date, stop: date) -> list[dict]:
    """Daily count of items whose completed_at falls on each date in
    [start, stop]. The bedrock of the how-many forecast.
    """
    counts: dict[date, int] = {}
    cur = start
    while cur <= stop:
        counts[cur] = 0
        cur += timedelta(days=1)
    for item in stream:
        if item.completed_at is None:
            continue
        d = item.completed_at.date()
        if start <= d <= stop:
            counts[d] += 1
    return [{"date": d, "completed": n} for d, n in sorted(counts.items())]


# ----------------------------------------------------------------------
# Flow Efficiency - active time over cycle time, per completed item.
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class StreamFlowEfficiency:
    item_id: str
    title: str
    cycle_time: timedelta
    active_time: timedelta
    efficiency: float
    url: str | None


def flow_efficiency_per_item(stream: Stream) -> list[StreamFlowEfficiency]:
    """For each completed item, sum the time spent in stages that
    belong to `workflow.wip_set` ("active" time) and divide by total
    cycle time.

    Exit-of-stage is the next transition's `entered_at` for the
    same item; the terminal transition has no exit (the item
    completed). Stages outside the WIP set don't accrue active
    time even if visited.
    """
    wip = stream.workflow.wip_set
    out: list[StreamFlowEfficiency] = []
    for item in stream:
        if item.completed_at is None:
            continue
        txs = list(stream.transitions_for(item.item_id))
        if not txs:
            continue
        active = timedelta()
        for i, t in enumerate(txs):
            if t.stage not in wip:
                continue
            end = txs[i + 1].entered_at if i + 1 < len(txs) else item.completed_at
            active += end - t.entered_at
        cycle = item.completed_at - item.created_at
        eff = (
            active.total_seconds() / cycle.total_seconds()
            if cycle.total_seconds() > 0
            else 1.0
        )
        out.append(
            StreamFlowEfficiency(
                item_id=item.item_id,
                title=item.title,
                cycle_time=cycle,
                active_time=active,
                efficiency=eff,
                url=item.url,
            )
        )
    return out
