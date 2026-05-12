from datetime import UTC, datetime, timedelta

import pytest

from flowmetrics.compute import (
    PullRequestEvents,
    StatusInterval,
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
        item_id=f"#{number}",
        title=f"PR #{number}",
        created_at=created,
        merged_at=merged,
        activity=activity or [],
    )


class TestComputePrFlow:
    def test_unmerged_pr_is_rejected(self):
        pr = PullRequestEvents(
            item_id="#1",
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


class TestObservedStatuses:
    """When items carry status_intervals, the WindowResult must expose
    the union of all statuses seen across the window. Agents and users
    look at this to tune --active-statuses for their workflow."""

    def test_aggregate_unions_statuses_across_items(self):
        prs = [
            PullRequestEvents(
                item_id="X-1", title="t1",
                created_at=ts(2026, 5, 4, 9, 0),
                merged_at=ts(2026, 5, 5, 9, 0),
                status_intervals=[
                    StatusInterval(ts(2026, 5, 4, 9, 0), ts(2026, 5, 4, 12, 0), "Open"),
                    StatusInterval(ts(2026, 5, 4, 12, 0), ts(2026, 5, 5, 9, 0), "In Progress"),
                ],
            ),
            PullRequestEvents(
                item_id="X-2", title="t2",
                created_at=ts(2026, 5, 4, 9, 0),
                merged_at=ts(2026, 5, 5, 9, 0),
                status_intervals=[
                    StatusInterval(ts(2026, 5, 4, 9, 0), ts(2026, 5, 4, 12, 0), "Open"),
                    StatusInterval(ts(2026, 5, 4, 12, 0), ts(2026, 5, 5, 9, 0), "Patch Available"),
                ],
            ),
        ]
        per_pr = [
            compute_pr_flow(
                p, gap=GAP, min_cluster=MIN_CLUSTER,
                active_statuses=frozenset({"In Progress"}),
            )
            for p in prs
        ]
        result = aggregate(per_pr)
        assert result.observed_statuses == ["In Progress", "Open", "Patch Available"]

    def test_empty_when_no_status_data(self):
        # GitHub-style items (no status_intervals) → empty observed_statuses
        prs = [make_pr(number=1)]
        per_pr = [compute_pr_flow(p, gap=GAP, min_cluster=MIN_CLUSTER) for p in prs]
        result = aggregate(per_pr)
        assert result.observed_statuses == []


class TestStatusDurationActiveTime:
    """When a WorkItem carries `status_intervals`, active time becomes the
    sum of durations spent in user-mapped active statuses. This is Vacanti's
    canonical Jira model: measured time-in-status, not inferred event clusters.
    """

    def test_active_time_is_sum_of_in_progress_intervals(self):
        # Issue created Mon 9am, In Progress Tue 9am, Resolved Fri 9am.
        # 1 day Open, 3 days In Progress. Active=3d, cycle=4d, FE=75%.
        item = PullRequestEvents(
            item_id="BIGTOP-1",
            title="example",
            created_at=ts(2026, 5, 4, 9, 0),
            merged_at=ts(2026, 5, 8, 9, 0),
            status_intervals=[
                StatusInterval(ts(2026, 5, 4, 9, 0), ts(2026, 5, 5, 9, 0), "Open"),
                StatusInterval(ts(2026, 5, 5, 9, 0), ts(2026, 5, 8, 9, 0), "In Progress"),
            ],
        )
        result = compute_pr_flow(
            item, gap=GAP, min_cluster=MIN_CLUSTER,
            active_statuses=frozenset({"In Progress"}),
        )
        assert result.cycle_time == timedelta(days=4)
        assert result.active_time == timedelta(days=3)
        assert result.efficiency == pytest.approx(0.75)

    def test_zero_active_time_when_no_active_status_visited(self):
        # All time in "Open" then "In Review" — neither marked active.
        item = PullRequestEvents(
            item_id="BIGTOP-2",
            title="never active",
            created_at=ts(2026, 5, 4, 9, 0),
            merged_at=ts(2026, 5, 8, 9, 0),
            status_intervals=[
                StatusInterval(ts(2026, 5, 4, 9, 0), ts(2026, 5, 6, 9, 0), "Open"),
                StatusInterval(ts(2026, 5, 6, 9, 0), ts(2026, 5, 8, 9, 0), "In Review"),
            ],
        )
        result = compute_pr_flow(
            item, gap=GAP, min_cluster=MIN_CLUSTER,
            active_statuses=frozenset({"In Progress"}),
        )
        assert result.active_time == timedelta(0)
        assert result.efficiency == pytest.approx(0.0)

    def test_multiple_active_statuses_summed(self):
        # Both "In Progress" and "In Development" treated as active.
        item = PullRequestEvents(
            item_id="X-3", title="multi active",
            created_at=ts(2026, 5, 4, 9, 0),
            merged_at=ts(2026, 5, 8, 9, 0),
            status_intervals=[
                StatusInterval(ts(2026, 5, 4, 9, 0), ts(2026, 5, 5, 9, 0), "In Progress"),
                StatusInterval(ts(2026, 5, 5, 9, 0), ts(2026, 5, 6, 9, 0), "Code Review"),
                StatusInterval(ts(2026, 5, 6, 9, 0), ts(2026, 5, 8, 9, 0), "In Development"),
            ],
        )
        result = compute_pr_flow(
            item, gap=GAP, min_cluster=MIN_CLUSTER,
            active_statuses=frozenset({"In Progress", "In Development"}),
        )
        # 1 day In Progress + 2 days In Development = 3 days active
        assert result.active_time == timedelta(days=3)

    def test_event_clustering_path_used_when_no_status_intervals(self):
        # Empty status_intervals (GitHub case) falls through to existing
        # event-clustering path.
        item = make_pr(
            created=ts(2026, 5, 4, 9, 0),
            merged=ts(2026, 5, 8, 9, 0),
        )
        assert item.status_intervals == []  # default empty
        result = compute_pr_flow(
            item, gap=GAP, min_cluster=MIN_CLUSTER,
            active_statuses=frozenset({"In Progress"}),
        )
        # Same numbers as test_quiet_pr_has_low_flowmetrics: 1h active over 96h
        assert result.cycle_time == timedelta(hours=96)
        assert result.active_time == timedelta(hours=1)


class TestServiceLayerPassesActiveStatuses:
    """flowmetrics_for_window must thread active_statuses through to compute."""

    def test_active_statuses_changes_active_time_for_jira_like_items(self, tmp_path):
        from datetime import date as _date

        from flowmetrics import flowmetrics_for_window
        from flowmetrics.compute import WorkItem

        class _FakeSource:
            label = "fake"

            def fetch_completed_in_window(self, start, stop):
                # One Jira-like item: 1d Open + 3d In Progress + done at day 4.
                return [
                    WorkItem(
                        item_id="X-1", title="t",
                        created_at=ts(2026, 5, 4, 9, 0),
                        merged_at=ts(2026, 5, 8, 9, 0),
                        status_intervals=[
                            StatusInterval(
                                ts(2026, 5, 4, 9, 0),
                                ts(2026, 5, 5, 9, 0), "Open",
                            ),
                            StatusInterval(
                                ts(2026, 5, 5, 9, 0),
                                ts(2026, 5, 8, 9, 0), "In Progress",
                            ),
                        ],
                    ),
                ]

        # With In Progress mapped active → 75%
        r = flowmetrics_for_window(
            _FakeSource(), _date(2026, 5, 1), _date(2026, 5, 30),
            active_statuses=frozenset({"In Progress"}),
        )
        assert r.portfolio_efficiency == pytest.approx(0.75)

        # With no status mapped active → 0%
        r2 = flowmetrics_for_window(
            _FakeSource(), _date(2026, 5, 1), _date(2026, 5, 30),
            active_statuses=frozenset({"Not A Real Status"}),
        )
        assert r2.portfolio_efficiency == pytest.approx(0.0)


class TestAggregate:
    def test_empty_input(self):
        result = aggregate([])
        assert result.pr_count == 0
        assert result.median_efficiency == 0.0
        assert result.portfolio_efficiency == 0.0

    def test_aggregate_counts_bot_prs(self):
        prs = [
            PullRequestEvents(
                item_id="#1",
                title="human",
                created_at=ts(2026, 5, 5, 9, 0),
                merged_at=ts(2026, 5, 5, 17, 0),
                activity=[],
                is_bot=False,
            ),
            PullRequestEvents(
                item_id="#2",
                title="bump",
                created_at=ts(2026, 5, 5, 9, 0),
                merged_at=ts(2026, 5, 5, 9, 5),
                activity=[],
                is_bot=True,
            ),
            PullRequestEvents(
                item_id="#3",
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
