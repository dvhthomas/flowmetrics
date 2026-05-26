from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from .cluster import cluster_activity


@dataclass(frozen=True)
class StatusInterval:
    """One uninterrupted span at a single workflow status.

    Populated by sources that have explicit named states (Jira). For
    sources like GitHub PRs that infer activity from event timestamps,
    `status_intervals` stays empty and the compute layer falls back to
    event clustering.
    """

    start: datetime
    end: datetime
    status: str


@dataclass(frozen=True)
class WorkItem:
    """One completed unit of work — a merged PR (GitHub), a resolved
    issue (Jira), etc. `item_id` is the source's display form:
    `"#42"` for GitHub PRs, `"BIGTOP-4525"` for Jira issues.
    """

    item_id: str
    title: str
    created_at: datetime
    completed_at: datetime | None
    activity: list[datetime] = field(default_factory=list)
    is_bot: bool = False
    author_login: str | None = None
    # Canonical Jira input. If non-empty AND active_statuses are
    # configured, compute_pr_flow uses status-duration math instead of
    # event clustering. See docs/METRICS.md.
    status_intervals: list[StatusInterval] = field(default_factory=list)
    # Canonical drill-down URL for this item, set by the source that
    # fetched it (since the source is the only thing that knows its
    # own URL convention). Renderers should consume `url` directly
    # rather than pattern-matching `item_id` to guess the source type.
    url: str | None = None


@dataclass(frozen=True)
class FlowEfficiency:
    item_id: str
    title: str
    created_at: datetime
    completed_at: datetime
    cycle_time: timedelta
    active_time: timedelta
    efficiency: float
    is_bot: bool = False
    author_login: str | None = None
    # Distinct named statuses this item visited. Empty for sources
    # without explicit workflow states (GitHub). Sourced from
    # `WorkItem.status_intervals` so renderers can surface a workflow
    # map without re-reading the upstream source.
    statuses_visited: tuple[str, ...] = ()
    # Canonical drill-down URL inherited from the source's WorkItem
    # (see `WorkItem.url`). Renderers use this directly instead of
    # pattern-matching `item_id` to construct one.
    url: str | None = None


@dataclass(frozen=True)
class WindowResult:
    pr_count: int
    portfolio_efficiency: float  # sum(active) / sum(cycle) — the headline number
    mean_efficiency: float  # average of per-PR ratios (less useful)
    median_efficiency: float
    total_cycle: timedelta
    total_active: timedelta
    per_pr: list[FlowEfficiency]
    bot_pr_count: int = 0
    # Sorted union of all named statuses observed across the window's
    # items. Empty for sources without workflow states. Lets users tune
    # `--active-statuses` to whatever their team's workflow actually uses.
    observed_statuses: list[str] = field(default_factory=list)

    @property
    def human_pr_count(self) -> int:
        return self.pr_count - self.bot_pr_count


def compute_pr_flow(
    pr: WorkItem,
    *,
    gap: timedelta,
    min_cluster: timedelta,
    active_statuses: frozenset[str] | None = None,
) -> FlowEfficiency:
    if pr.completed_at is None:
        raise ValueError(f"Item {pr.item_id} is not merged; cannot compute flow")

    cycle = pr.completed_at - pr.created_at

    # Status-duration path: used when the source provides explicit
    # named-status intervals (Jira) AND the caller has mapped some statuses
    # as active. This is the canonical Jira computation — measured,
    # not inferred.
    #
    # Discriminator between "Jira-style status_intervals" and "GitHub-PR-
    # lifecycle status_intervals": GitHub PRs ALSO carry rich `activity`
    # timestamps from their timeline events; Jira items don't. When
    # status_intervals exist but no interval matches active_statuses AND
    # activity is non-empty, we treat the item as GitHub-style and fall
    # through to event-clustering. This prevents PR-lifecycle stages
    # (Draft / Awaiting Review / Merged) from being silently scored 0%
    # against Jira-default active_statuses like 'In Progress'.
    has_matching_interval = (
        active_statuses is not None
        and any(iv.status in active_statuses for iv in pr.status_intervals)
    )
    use_status_duration_path = (
        pr.status_intervals
        and active_statuses
        and (has_matching_interval or not pr.activity)
    )
    if use_status_duration_path:
        raw_active = sum(
            (
                (interval.end - interval.start)
                for interval in pr.status_intervals
                if interval.status in active_statuses
            ),
            start=timedelta(),
        )
        active = min(raw_active, cycle)
    else:
        # Event-clustering path (GitHub: no explicit status, infer from
        # event timestamps).
        events = {pr.created_at, pr.completed_at}
        for t in pr.activity:
            if pr.created_at <= t <= pr.completed_at:
                events.add(t)
        clusters = cluster_activity(events, gap=gap)
        raw_active = sum(
            (max(end - start, min_cluster) for start, end in clusters),
            start=timedelta(),
        )
        active = min(raw_active, cycle)

    efficiency = (
        active.total_seconds() / cycle.total_seconds() if cycle.total_seconds() > 0 else 1.0
    )

    return FlowEfficiency(
        item_id=pr.item_id,
        title=pr.title,
        created_at=pr.created_at,
        completed_at=pr.completed_at,
        cycle_time=cycle,
        active_time=active,
        efficiency=efficiency,
        is_bot=pr.is_bot,
        author_login=pr.author_login,
        url=pr.url,
        statuses_visited=tuple(sorted({iv.status for iv in pr.status_intervals})),
    )


def aggregate(per_pr: Iterable[FlowEfficiency]) -> WindowResult:
    results = list(per_pr)
    if not results:
        return WindowResult(
            pr_count=0,
            portfolio_efficiency=0.0,
            mean_efficiency=0.0,
            median_efficiency=0.0,
            total_cycle=timedelta(),
            total_active=timedelta(),
            per_pr=[],
        )

    total_active = sum((r.active_time for r in results), start=timedelta())
    total_cycle = sum((r.cycle_time for r in results), start=timedelta())
    portfolio = (
        total_active.total_seconds() / total_cycle.total_seconds()
        if total_cycle.total_seconds() > 0
        else 0.0
    )

    ratios = [r.efficiency for r in results]
    return WindowResult(
        pr_count=len(results),
        portfolio_efficiency=portfolio,
        mean_efficiency=sum(ratios) / len(ratios),
        median_efficiency=median(ratios),
        total_cycle=total_cycle,
        total_active=total_active,
        per_pr=results,
        bot_pr_count=sum(1 for r in results if r.is_bot),
        observed_statuses=sorted({s for r in results for s in r.statuses_visited}),
    )
