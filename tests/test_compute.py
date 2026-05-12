from datetime import UTC, datetime, timedelta

import pytest

from flowmetrics.compute import (
    PullRequestEvents,
    aggregate,
    compute_pr_flow,
)

GAP = timedelta(hours=4)
MIN_CLUSTER = timedelta(minutes=30)


def ts(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def make_pr(
    number: int = 1,
    created: datetime = ts(2026, 5, 5, 9, 0),
    merged: datetime = ts(2026, 5, 5, 17, 0),
    activity: list[datetime] | None = None,
) -> PullRequestEvents:
    return PullRequestEvents(
        number=number,
        title=f"PR #{number}",
        created_at=created,
        merged_at=merged,
        activity=activity or [],
    )


class TestComputePrFlow:
    def test_unmerged_pr_is_rejected(self):
        pr = PullRequestEvents(
            number=1,
            title="open",
            created_at=ts(2026, 5, 5, 9, 0),
            merged_at=None,
            activity=[],
        )
        with pytest.raises(ValueError):
            compute_pr_flow(pr, gap=GAP, min_cluster=MIN_CLUSTER)

    def test_quiet_pr_has_low_flowmetrics(self):
        # Open Mon 9am, merge Fri 9am (96 hours). No activity in between.
        # Open + merge each form a point cluster credited 30min → 1h active.
        # FE = 1h / 96h ≈ 0.0104
        pr = make_pr(
            created=ts(2026, 5, 4, 9, 0),
            merged=ts(2026, 5, 8, 9, 0),
        )
        result = compute_pr_flow(pr, gap=GAP, min_cluster=MIN_CLUSTER)
        assert result.cycle_time == timedelta(hours=96)
        assert result.active_time == timedelta(hours=1)
        assert result.efficiency == pytest.approx(1 / 96, abs=1e-4)

    def test_continuous_activity_yields_high_efficiency(self):
        # Open at 9am, activity every hour, merged at 5pm — single cluster
        created = ts(2026, 5, 5, 9, 0)
        merged = ts(2026, 5, 5, 17, 0)
        activity = [created + timedelta(hours=h) for h in range(1, 9)]
        pr = make_pr(created=created, merged=merged, activity=activity)
        result = compute_pr_flow(pr, gap=GAP, min_cluster=MIN_CLUSTER)
        assert result.cycle_time == timedelta(hours=8)
        # One cluster spans created → merged → 8h. Active equals cycle.
        assert result.active_time == timedelta(hours=8)
        assert result.efficiency == pytest.approx(1.0)

    def test_active_time_is_capped_at_cycle_time(self):
        # Minimum cluster floor must never push active > cycle
        created = ts(2026, 5, 5, 12, 0)
        merged = ts(2026, 5, 5, 12, 5)  # 5 minute cycle
        pr = make_pr(created=created, merged=merged)
        result = compute_pr_flow(pr, gap=GAP, min_cluster=MIN_CLUSTER)
        assert result.active_time <= result.cycle_time
        assert result.efficiency <= 1.0

    def test_two_activity_bursts_with_long_gap(self):
        # Burst 1: 9–10am Monday (1h)
        # Burst 2: 9–10am Wednesday (1h)
        # Cycle: Mon 9am → Wed 10am = 49h. Active: 2h. FE ≈ 2/49.
        created = ts(2026, 5, 4, 9, 0)
        merged = ts(2026, 5, 6, 10, 0)
        activity = [
            ts(2026, 5, 4, 9, 30),
            ts(2026, 5, 4, 10, 0),
            ts(2026, 5, 6, 9, 0),
            ts(2026, 5, 6, 9, 30),
        ]
        pr = make_pr(created=created, merged=merged, activity=activity)
        result = compute_pr_flow(pr, gap=GAP, min_cluster=MIN_CLUSTER)
        assert result.cycle_time == timedelta(hours=49)
        assert result.active_time == timedelta(hours=2)
        assert result.efficiency == pytest.approx(2 / 49, abs=1e-4)


class TestAggregate:
    def test_empty_input(self):
        result = aggregate([])
        assert result.pr_count == 0
        assert result.median_efficiency == 0.0
        assert result.portfolio_efficiency == 0.0

    def test_aggregate_counts_bot_prs(self):
        prs = [
            PullRequestEvents(
                number=1,
                title="human",
                created_at=ts(2026, 5, 5, 9, 0),
                merged_at=ts(2026, 5, 5, 17, 0),
                activity=[],
                is_bot=False,
            ),
            PullRequestEvents(
                number=2,
                title="bump",
                created_at=ts(2026, 5, 5, 9, 0),
                merged_at=ts(2026, 5, 5, 9, 5),
                activity=[],
                is_bot=True,
            ),
            PullRequestEvents(
                number=3,
                title="bump 2",
                created_at=ts(2026, 5, 5, 9, 0),
                merged_at=ts(2026, 5, 5, 9, 5),
                activity=[],
                is_bot=True,
            ),
        ]
        per_pr = [compute_pr_flow(p, gap=GAP, min_cluster=MIN_CLUSTER) for p in prs]
        result = aggregate(per_pr)
        assert result.bot_pr_count == 2
        assert result.human_pr_count == 1

    def test_aggregate_uses_portfolio_ratio_not_mean_of_ratios(self):
        # Two PRs: one short+busy, one long+quiet. Vacanti's recipe is
        # sum(active)/sum(cycle), not mean(FE) — this test pins that.
        per_pr = [
            compute_pr_flow(
                make_pr(
                    number=1,
                    created=ts(2026, 5, 5, 9, 0),
                    merged=ts(2026, 5, 5, 10, 0),  # 1h cycle, ~1h active
                    activity=[ts(2026, 5, 5, 9, 30)],
                ),
                gap=GAP,
                min_cluster=MIN_CLUSTER,
            ),
            compute_pr_flow(
                make_pr(
                    number=2,
                    created=ts(2026, 5, 4, 9, 0),
                    merged=ts(2026, 5, 8, 9, 0),  # 96h cycle, ~1h active
                ),
                gap=GAP,
                min_cluster=MIN_CLUSTER,
            ),
        ]
        result = aggregate(per_pr)
        assert result.pr_count == 2

        total_active = sum((p.active_time for p in per_pr), start=timedelta())
        total_cycle = sum((p.cycle_time for p in per_pr), start=timedelta())
        expected_portfolio = total_active.total_seconds() / total_cycle.total_seconds()
        assert result.portfolio_efficiency == pytest.approx(expected_portfolio)

        mean_of_ratios = sum(p.efficiency for p in per_pr) / 2
        assert result.mean_efficiency == pytest.approx(mean_of_ratios)
        # The two values must diverge — otherwise the test wouldn't pin
        # the right definition.
        assert result.portfolio_efficiency != pytest.approx(mean_of_ratios)
