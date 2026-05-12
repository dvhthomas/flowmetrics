from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from .cluster import cluster_activity


@dataclass(frozen=True)
class PullRequestEvents:
    number: int
    title: str
    created_at: datetime
    merged_at: datetime | None
    activity: list[datetime] = field(default_factory=list)
    is_bot: bool = False
    author_login: str | None = None


@dataclass(frozen=True)
class FlowEfficiency:
    pr_number: int
    title: str
    created_at: datetime
    merged_at: datetime
    cycle_time: timedelta
    active_time: timedelta
    efficiency: float
    is_bot: bool = False
    author_login: str | None = None


@dataclass(frozen=True)
class WindowResult:
    pr_count: int
    portfolio_efficiency: float  # sum(active) / sum(cycle) — Vacanti's recipe
    mean_efficiency: float  # average of per-PR ratios (less useful)
    median_efficiency: float
    total_cycle: timedelta
    total_active: timedelta
    per_pr: list[FlowEfficiency]
    bot_pr_count: int = 0

    @property
    def human_pr_count(self) -> int:
        return self.pr_count - self.bot_pr_count


def compute_pr_flow(
    pr: PullRequestEvents,
    *,
    gap: timedelta,
    min_cluster: timedelta,
) -> FlowEfficiency:
    if pr.merged_at is None:
        raise ValueError(f"PR #{pr.number} is not merged; cannot compute flow")

    cycle = pr.merged_at - pr.created_at

    events = {pr.created_at, pr.merged_at}
    for t in pr.activity:
        if pr.created_at <= t <= pr.merged_at:
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
        pr_number=pr.number,
        title=pr.title,
        created_at=pr.created_at,
        merged_at=pr.merged_at,
        cycle_time=cycle,
        active_time=active,
        efficiency=efficiency,
        is_bot=pr.is_bot,
        author_login=pr.author_login,
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
    )
