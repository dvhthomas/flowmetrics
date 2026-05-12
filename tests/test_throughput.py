"""Tests for converting PR records into daily throughput samples."""

from datetime import UTC, date, datetime

from flowmetrics.compute import PullRequestEvents
from flowmetrics.throughput import daily_throughput


def pr(number: int, merged_at: datetime) -> PullRequestEvents:
    return PullRequestEvents(
        number=number,
        title=f"PR {number}",
        created_at=merged_at,  # not used by daily_throughput
        merged_at=merged_at,
        activity=[],
    )


def dt(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestDailyThroughput:
    def test_counts_per_day_including_zero_days(self):
        prs = [
            pr(1, dt(2026, 5, 4, 10, 0)),
            pr(2, dt(2026, 5, 4, 15, 0)),
            pr(3, dt(2026, 5, 6, 9, 0)),
        ]
        samples = daily_throughput(prs, date(2026, 5, 4), date(2026, 5, 7))
        # Mon=2, Tue=0, Wed=1, Thu=0
        assert samples == [2, 0, 1, 0]

    def test_empty_window_returns_zeros(self):
        samples = daily_throughput([], date(2026, 5, 4), date(2026, 5, 10))
        assert samples == [0] * 7

    def test_prs_outside_window_are_ignored(self):
        prs = [
            pr(1, dt(2026, 5, 3, 23, 0)),  # day before window
            pr(2, dt(2026, 5, 5, 10, 0)),  # inside
            pr(3, dt(2026, 5, 11, 1, 0)),  # day after window
        ]
        samples = daily_throughput(prs, date(2026, 5, 4), date(2026, 5, 10))
        assert sum(samples) == 1
        # Day index 1 (Tue 5/5) should have the one merge
        assert samples[1] == 1

    def test_single_day_window(self):
        prs = [pr(1, dt(2026, 5, 4, 10, 0))]
        assert daily_throughput(prs, date(2026, 5, 4), date(2026, 5, 4)) == [1]

    def test_unmerged_prs_skipped(self):
        unmerged = PullRequestEvents(
            number=99,
            title="open",
            created_at=dt(2026, 5, 4, 9, 0),
            merged_at=None,
            activity=[],
        )
        samples = daily_throughput(
            [unmerged, pr(1, dt(2026, 5, 4, 10, 0))],
            date(2026, 5, 4),
            date(2026, 5, 5),
        )
        assert samples == [1, 0]
