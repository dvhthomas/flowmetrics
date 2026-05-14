"""Vacanti's Aging Work In Progress chart.

Plots in-flight items (entered but not exited the workflow) by their
current workflow state (x-axis) and elapsed age in days (y-axis).
Percentile lines drawn from completed-item cycle times serve as
checkpoints: if an item ages past P85, it's likely to miss the forecast.

This module computes only the data. The chart is built by the renderer.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
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
    # Optional deep-link to the underlying issue/PR — populated by the
    # caller via the `url_for` argument to `compute_aging`. Used by the
    # interactive HTML chart's `href` channel; None disables click-through.
    pr_url: str | None = None


def compute_aging(
    items: Sequence[WorkItem],
    *,
    asof: date,
    url_for: Callable[[str], str | None] | None = None,
    max_age_days: int | None = None,
) -> list[AgingItem]:
    """Build aging data points for in-flight items.

    Completed items (those with `merged_at`) are excluded — by definition
    they have a Cycle Time, not an Age. Current state is read from the
    last status_interval; if there are none, falls back to ``"Unknown"``.

    `url_for(item_id) -> str | None` lets the caller plug in a backend-
    specific URL builder (GitHub vs Jira) without `compute_aging`
    knowing about the source.

    `max_age_days` is opt-in: when set, items with `age_days >
    max_age_days` are dropped from the result. Default (None) keeps
    every in-flight item per Vacanti.
    """
    out: list[AgingItem] = []
    for item in items:
        if item.merged_at is not None:
            continue
        age_days = (asof - item.created_at.date()).days
        if max_age_days is not None and age_days > max_age_days:
            continue
        current = item.status_intervals[-1].status if item.status_intervals else "Unknown"
        out.append(
            AgingItem(
                item_id=item.item_id,
                title=item.title,
                current_state=current,
                age_days=age_days,
                pr_url=url_for(item.item_id) if url_for is not None else None,
            )
        )
    return out


def compute_aging_distribution(
    items: Sequence[AgingItem],
    cycle_time_percentiles: dict[int, float],
) -> list[dict]:
    """Band the in-flight age distribution against the percentile-line
    thresholds. Returns five bands (Below P50, P50-P70, P70-P85,
    P85-P95, Above P95), each with `count` and `share` (fraction of
    items).

    The diagnostic that makes the survivorship-bias case legible: a
    healthy chart has most items below P85; a chart dominated by the
    Above-P95 band means the in-flight distribution doesn't resemble
    recent completers.

    Boundary convention matches the interpretation layer's `>= P95`:
    an item exactly at P95 belongs to Above P95. Within-range items
    use `lower <= age < upper`.
    """
    p50 = cycle_time_percentiles.get(50, 0.0)
    p70 = cycle_time_percentiles.get(70, 0.0)
    p85 = cycle_time_percentiles.get(85, 0.0)
    p95 = cycle_time_percentiles.get(95, 0.0)
    bands: list[dict] = [
        {"label": "Below P50", "lower": None, "upper": p50, "count": 0},
        {"label": "P50–P70",   "lower": p50,  "upper": p70, "count": 0},
        {"label": "P70–P85",   "lower": p70,  "upper": p85, "count": 0},
        {"label": "P85–P95",   "lower": p85,  "upper": p95, "count": 0},
        {"label": "Above P95", "lower": p95,  "upper": None, "count": 0},
    ]
    for it in items:
        age = it.age_days
        # Walk from the top: Above-P95 wins ties at the P95 boundary
        # (matches interpret_aging's `age_days >= p95` rule).
        if age >= p95:
            bands[4]["count"] += 1
        elif age >= p85:
            bands[3]["count"] += 1
        elif age >= p70:
            bands[2]["count"] += 1
        elif age >= p50:
            bands[1]["count"] += 1
        else:
            bands[0]["count"] += 1
    total = len(items)
    for b in bands:
        b["share"] = b["count"] / total if total > 0 else 0.0
    return bands


def per_state_diagnostic(
    *,
    items: Sequence[AgingItem],
    workflow: Sequence[str],
    percentiles: dict[int, float],
) -> list[dict]:
    """Per-state aging breakdown — the bottleneck diagnostic.

    For each state in workflow order, returns a dict with:
    `state`, `count`, `median_age_days`, `oldest_age_days`,
    `past_p85`, `past_p95`. Empty states return zeros / None so the
    table preserves column membership.
    """
    p50 = percentiles.get(50, 0.0)
    p85 = percentiles.get(85, 0.0)
    p95 = percentiles.get(95, 0.0)
    by_state: dict[str, list[int]] = {state: [] for state in workflow}
    for it in items:
        if it.current_state in by_state:
            by_state[it.current_state].append(it.age_days)

    rows: list[dict] = []
    for state in workflow:
        ages = by_state[state]
        if ages:
            ages_sorted = sorted(ages)
            median = ages_sorted[len(ages_sorted) // 2]
            # "At risk" cohort = items between P50 and P85: by Vacanti's
            # conditional-probability math, their risk of missing the
            # 85th-percentile forecast has at least doubled from 15% to
            # 30%. Naming the count surfaces who needs a conversation
            # NOW, before they've crossed the forecast line.
            at_risk = sum(
                1 for a in ages
                if p50 > 0 and p85 > 0 and a >= p50 and a < p85
            )
            row = {
                "state": state,
                "count": len(ages),
                "median_age_days": median,
                "oldest_age_days": max(ages),
                "at_risk_p50_to_p85": at_risk,
                "past_p85": sum(1 for a in ages if p85 > 0 and a >= p85),
                "past_p95": sum(1 for a in ages if p95 > 0 and a >= p95),
            }
        else:
            row = {
                "state": state,
                "count": 0,
                "median_age_days": None,
                "oldest_age_days": None,
                "at_risk_p50_to_p85": 0,
                "past_p85": 0,
                "past_p95": 0,
            }
        rows.append(row)
    return rows


_GLOBAL_INTERVENTION_CAP = 15
_DEFAULT_PER_STATE = 3


def top_interventions(
    *,
    items: Sequence[AgingItem],
    workflow: Sequence[str],
    percentiles: dict[int, float],
    per_state_n: int = _DEFAULT_PER_STATE,
) -> list[dict]:
    """Next-actions list. Top `per_state_n` oldest-past-P85 PRs in each
    workflow state, ordered rightmost-first (states closer to done =
    higher leverage). Global cap is 15.

    Default is 3 per state: meaningfully more than one (which can miss
    most of the stuck work when one column carries the lion's share)
    while still scannable in a standup.

    Returns an empty list when no items are past P85 (healthy pipeline).
    Items whose `current_state` is not in `workflow` are skipped — their
    position can't be ordered.
    """
    p85 = percentiles.get(85, 0.0)
    if p85 <= 0:
        return []

    state_index = {state: idx for idx, state in enumerate(workflow)}
    by_state: dict[str, list[AgingItem]] = {state: [] for state in workflow}
    for it in items:
        if it.current_state not in state_index:
            continue
        if it.age_days < p85:
            continue
        by_state[it.current_state].append(it)

    # Within each state, oldest first. Rightmost states win priority
    # in the final list ordering.
    out: list[dict] = []
    for state in sorted(workflow, key=lambda s: state_index[s], reverse=True):
        bucket = sorted(by_state[state], key=lambda i: i.age_days, reverse=True)
        for it in bucket[:per_state_n]:
            out.append(
                {
                    "item_id": it.item_id,
                    "title": it.title,
                    "current_state": it.current_state,
                    "age_days": it.age_days,
                    "pr_url": it.pr_url,
                }
            )
            if len(out) >= _GLOBAL_INTERVENTION_CAP:
                return out
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
