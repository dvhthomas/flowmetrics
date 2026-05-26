"""Cumulative Flow Diagram (CFD) construction.

This module is the CLI surface for the CFD math. The actual
cumulative-by-stage algorithm lives in `flowmetrics.charts.cfd`
and is shared with the web layer; this module only adapts CLI
inputs (`WorkItem` lists) into the warehouse `StageEntry` rows
the shared primitive consumes, and adapts the daily counts back
into the `CfdPoint` shape the CLI renderers expect.

For each sample date T and each workflow state S, plot the
cumulative count of items that have entered S or any later
workflow state by T. Stacking those counts yields the six standard
CFD properties:

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
from datetime import date, timedelta

from .charts.cfd import cumulative_arrivals_by_stage
from .compute import WorkItem
from .warehouse.queries import StageEntry


@dataclass(frozen=True)
class CfdPoint:
    sampled_on: date
    counts_by_state: dict[str, int] = field(default_factory=dict)


def workitem_stage_entries(
    items: Sequence[WorkItem], workflow: Sequence[str],
) -> list[StageEntry]:
    """Flatten `WorkItem`s into the same `StageEntry` rows the
    warehouse query emits.

    Three sources of evidence per item, all collapsed to the
    EARLIEST date per (item, stage):

      1. Each `status_intervals` row whose status appears in the
         workflow — direct evidence the item visited that stage.
      2. `created_at` seeds the first workflow stage (a GitHub PR
         that has only review-decision intervals is still "Open"
         from creation).
      3. `completed_at` seeds the terminal stage (an item that
         merged without an explicit Done interval still reached
         the terminal state).
    """
    workflow_set = set(workflow)
    earliest: dict[tuple[str, str], date] = {}

    def _record(item_id: str, stage: str, d: date) -> None:
        key = (item_id, stage)
        existing = earliest.get(key)
        if existing is None or d < existing:
            earliest[key] = d

    for item in items:
        for iv in item.status_intervals:
            if iv.status in workflow_set:
                _record(item.item_id, iv.status, iv.start.date())
        if item.created_at is not None:
            _record(item.item_id, workflow[0], item.created_at.date())
        if item.completed_at is not None:
            _record(item.item_id, workflow[-1], item.completed_at.date())
    return [
        StageEntry(item_id=item_id, stage=stage, entered_date=d)
        for (item_id, stage), d in earliest.items()
    ]


def build_cfd(
    items: Sequence[WorkItem],
    *,
    workflow: Sequence[str],
    start: date,
    stop: date,
    interval: timedelta,
) -> list[CfdPoint]:
    """Build a CFD for `items` sampled from `start` to `stop` (inclusive).

    `workflow` lists states in flow order, earliest to latest. The
    top line is the workflow[0] count (cumulative arrivals); the
    bottom is workflow[-1] (cumulative departures). The math is
    delegated to `flowmetrics.charts.cfd.cumulative_arrivals_by_stage`
    — shared with the web layer.
    """
    if not workflow:
        raise ValueError("workflow must contain at least one state")
    if stop < start:
        raise ValueError(f"stop ({stop}) precedes start ({start})")

    step = timedelta(days=max(1, interval.days))
    sample_dates: list[date] = []
    cur = start
    while cur <= stop:
        sample_dates.append(cur)
        cur += step

    entries = workitem_stage_entries(items, workflow)
    counts_per_date = cumulative_arrivals_by_stage(
        entries, tuple(workflow), sample_dates=sample_dates,
    )
    return [
        CfdPoint(sampled_on=d, counts_by_state=dict(c))
        for d, c in zip(sample_dates, counts_per_date, strict=True)
    ]
