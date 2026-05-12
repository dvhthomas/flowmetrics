"""Vacanti's Aging Work In Progress chart (WWIBD pp. 50-51).

Plots in-flight items (entered but not exited the workflow) by their
current workflow state (x-axis) and elapsed age in days (y-axis).
Percentile lines drawn from completed-item cycle times serve as
checkpoints: if an item ages past P85, it's likely to miss the forecast.

This module computes only the data. The chart is built by the renderer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from .compute import FlowEfficiency, WorkItem
from .percentiles import CANONICAL_PERCENTILES, chart_percentiles


@dataclass(frozen=True)
class AgingItem:
    item_id: str
    title: str
    current_state: str
    age_days: int


def compute_aging(items: Sequence[WorkItem], *, asof: date) -> list[AgingItem]:
    """Build aging data points for in-flight items.

    Completed items (those with `merged_at`) are excluded — by definition
    they have a Cycle Time, not an Age. Current state is read from the
    last status_interval; if there are none, falls back to ``"Unknown"``.
    """
    out: list[AgingItem] = []
    for item in items:
        if item.merged_at is not None:
            continue
        current = item.status_intervals[-1].status if item.status_intervals else "Unknown"
        age_days = (asof - item.created_at.date()).days
        out.append(
            AgingItem(
                item_id=item.item_id,
                title=item.title,
                current_state=current,
                age_days=age_days,
            )
        )
    return out


def cycle_time_percentiles(
    completed: Sequence[FlowEfficiency],
) -> dict[int, float]:
    """50/70/85/95 percentiles of cycle time across completed items, in days.

    Same distribution as Vacanti's Scatterplot — the Aging chart's
    percentile lines are reference checkpoints drawn from this.
    """
    if not completed:
        return {p: 0.0 for p in CANONICAL_PERCENTILES}
    days = [item.cycle_time.total_seconds() / 86400 for item in completed]
    return chart_percentiles(days)
