from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta

Interval = tuple[datetime, datetime]


def cluster_activity(events: Iterable[datetime], *, gap: timedelta) -> list[Interval]:
    """Group activity timestamps into intervals.

    Consecutive events whose neighbour-to-neighbour spacing is <= `gap`
    collapse into a single (start, end) interval. A single isolated event
    becomes a zero-duration point interval (t, t); the caller decides
    whether to apply a minimum-interval floor.
    """
    if gap <= timedelta(0):
        raise ValueError("gap must be positive")

    sorted_events = sorted(events)
    if not sorted_events:
        return []

    clusters: list[Interval] = []
    cluster_start = sorted_events[0]
    prev = sorted_events[0]
    for event in sorted_events[1:]:
        if event - prev <= gap:
            prev = event
            continue
        clusters.append((cluster_start, prev))
        cluster_start = event
        prev = event
    clusters.append((cluster_start, prev))
    return clusters
