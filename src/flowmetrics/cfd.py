"""Cumulative Flow Diagram (CFD) construction per Vacanti.

For each sample date T and each workflow state S, count items that have
entered S or any later workflow state by T. Stacking those counts yields
Vacanti's six CFD properties:

  #1  Top line = cumulative arrivals; bottom = cumulative departures.
  #2  No line decreases (always cumulative).
  #3  Vertical distance between adjacent lines = WIP in that band.
  #4  Horizontal distance between two lines ≈ avg cycle time.
  #5  Past data only — no projections.
  #6  Slope between consecutive samples = avg arrival rate at that band.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .compute import WorkItem


@dataclass(frozen=True)
class CfdPoint:
    sampled_on: date
    counts_by_state: dict[str, int] = field(default_factory=dict)


def build_cfd(
    items: Sequence[WorkItem],
    *,
    workflow: Sequence[str],
    start: date,
    stop: date,
    interval: timedelta,
) -> list[CfdPoint]:
    """Build a CFD for `items` sampled from `start` to `stop` inclusive.

    `workflow` lists states in flow order, earliest to latest. The top
    line of the resulting CFD is the workflow[0] count (cumulative
    arrivals); the bottom is workflow[-1] (cumulative departures).
    """
    if not workflow:
        raise ValueError("workflow must contain at least one state")
    if stop < start:
        raise ValueError(f"stop ({stop}) precedes start ({start})")

    step = timedelta(days=max(1, interval.days))

    # Precompute each item's entry date into each workflow position. An
    # item's "entry date" at state[i] is the earliest date it visited
    # state[i] or any later workflow state — that's what makes the line
    # cumulative-by-state-or-later (Vacanti property #2).
    entry_by_item: list[list[date | None]] = [
        [_entry_date(item, workflow, i) for i in range(len(workflow))]
        for item in items
    ]

    points: list[CfdPoint] = []
    current = start
    while current <= stop:
        counts: dict[str, int] = {}
        for i, state in enumerate(workflow):
            n = 0
            for per_state in entry_by_item:
                d = per_state[i]
                if d is not None and d <= current:
                    n += 1
            counts[state] = n
        points.append(CfdPoint(sampled_on=current, counts_by_state=counts))
        current = current + step
    return points


def _entry_date(item: WorkItem, workflow: Sequence[str], idx: int) -> date | None:
    """Earliest date `item` entered workflow[idx] or any later state.

    Three independent sources of evidence:

      1. **status_intervals** whose status is in `workflow[idx:]` —
         direct evidence.
      2. **merged_at** when `workflow[-1]` is in `workflow[idx:]` —
         the item reached the terminal state at merge time, even
         without an explicit Done interval.
      3. **created_at** when `idx == 0` AND the item has no intervals
         — for sources without workflow history (GitHub PRs), the
         arrival date is implied by the item's existence. NOTE this
         only applies to the first-state line; without it, source #2
         would propagate merged_at backward and collapse the Open and
         Merged lines onto each other.

    The entry date is the *earliest* of all available evidence.
    """
    later: set[str] = set(workflow[idx:])
    candidates: list[datetime] = [
        iv.start for iv in item.status_intervals if iv.status in later
    ]
    if workflow[-1] in later and item.merged_at is not None:
        candidates.append(item.merged_at)
    if idx == 0 and not item.status_intervals:
        candidates.append(item.created_at)
    if not candidates:
        return None
    return min(candidates).date()
