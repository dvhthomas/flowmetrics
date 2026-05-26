"""Stale-item filter — drops items whose most recent event is
older than a threshold.

OSS pipelines accumulate hundreds of in-flight items with zero
recent activity: external PRs no maintainer touched, abandoned
issues, drive-by suggestions. They aren't part of the team's
real flow but they dominate every chart — this isn't a "deep tail"
to investigate, it's noise to filter out.

`filter_stale(items, asof, days)` keeps only items whose last
observed event is within `days` of `asof`. "Last observed event"
considers the union of:
  - created_at
  - completed_at (when set)
  - max activity timestamp
  - max status_intervals interval.end

When `days is None`, the filter is a no-op (returns the input
unchanged) — this is the default behavior, so existing callers
who don't pass the flag see no change.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

from .compute import WorkItem


def _last_event(item: WorkItem) -> datetime:
    """Most recent REAL event timestamp associated with this item.

    Deliberately omits `status_intervals[-1].end` because in-flight
    sources (fetch_open_prs etc.) synthesize that boundary to
    extend to `asof` — using it would make every in-flight item
    look "active today" regardless of when actual work happened.

    The interior interval boundaries (everything except the very
    last interval's end) reflect real state changes and ARE
    counted via `iv.start` on the next interval — captured here
    by including `iv.start` for every interval beyond the first.
    """
    candidates: list[datetime] = [item.created_at]
    if item.completed_at is not None:
        candidates.append(item.completed_at)
    if item.activity:
        candidates.append(max(item.activity))
    # Interval-start timestamps mark genuine state transitions
    # (label added, status changed, etc.) — these are real events
    # even when the SYNTHETIC last `interval.end` extends to asof.
    if len(item.status_intervals) > 1:
        candidates.append(max(iv.start for iv in item.status_intervals))
    return max(candidates)


def filter_stale(
    items: Iterable[WorkItem], *, asof: date, days: int | None,
) -> list[WorkItem]:
    """Return items whose `_last_event` is within `days` of `asof`.

    `asof` is interpreted as end-of-day so an item with a same-day
    timestamp survives any positive threshold. `days=None` disables
    the filter.
    """
    if days is None:
        return list(items)
    asof_dt = datetime.combine(asof, datetime.max.time()).replace(tzinfo=UTC)
    cutoff = asof_dt - timedelta(days=days)
    return [item for item in items if _last_event(item) >= cutoff]
