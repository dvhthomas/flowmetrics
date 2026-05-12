"""Tests for Monte Carlo forecasting (both Vacanti scenarios).

Scenario A — "When will it be done?": given N items, simulate completion
dates. Percentiles read forward: 85% confidence ⇒ later date.

Scenario B — "How many items by date?": given a target date, simulate how
many items finish. Percentiles read backward: 85% confidence ⇒ fewer items.
"""

from datetime import date, timedelta
from random import Random

import pytest

from flowmetrics.forecast import (
    backward_percentile,
    build_histogram,
    forward_percentile,
    monte_carlo_how_many,
    monte_carlo_when_done,
    render_histogram,
)


class TestMonteCarloWhenDone:
    def test_deterministic_for_same_seed(self):
        samples = [0, 1, 2, 3, 4]
        a = monte_carlo_when_done(
            samples, items=50, start_date=date(2026, 1, 1), runs=100, rng=Random(42)
        )
        b = monte_carlo_when_done(
            samples, items=50, start_date=date(2026, 1, 1), runs=100, rng=Random(42)
        )
        assert a == b

    def test_constant_throughput_yields_exact_date(self):
        # 1 item/day → 10 items finishes on day 10 (start + 9d)
        results = monte_carlo_when_done(
            [1], items=10, start_date=date(2026, 1, 1), runs=20, rng=Random(0)
        )
        assert len(results) == 20
        assert all(r == date(2026, 1, 10) for r in results)

    def test_higher_throughput_finishes_sooner(self):
        start = date(2026, 1, 1)
        fast = monte_carlo_when_done([5], 100, start, runs=10, rng=Random(0))
        slow = monte_carlo_when_done([1], 100, start, runs=10, rng=Random(0))
        assert max(fast) < min(slow)

    def test_zero_throughput_days_allowed(self):
        samples = [0, 0, 0, 0, 2]
        results = monte_carlo_when_done(samples, 20, date(2026, 1, 1), runs=50, rng=Random(1))
        assert len(results) == 50
        assert all(isinstance(r, date) for r in results)

    def test_all_zero_throughput_raises(self):
        with pytest.raises(ValueError):
            monte_carlo_when_done([0, 0, 0], 5, date(2026, 1, 1), runs=10, rng=Random(0))

    def test_empty_samples_raises(self):
        with pytest.raises(ValueError):
            monte_carlo_when_done([], 5, date(2026, 1, 1), runs=10, rng=Random(0))

    def test_invalid_items_or_runs(self):
        with pytest.raises(ValueError):
            monte_carlo_when_done([1], 0, date(2026, 1, 1), runs=10, rng=Random(0))
        with pytest.raises(ValueError):
            monte_carlo_when_done([1], 5, date(2026, 1, 1), runs=0, rng=Random(0))


class TestMonteCarloHowMany:
    def test_deterministic_for_same_seed(self):
        samples = [0, 1, 2, 3, 4]
        a = monte_carlo_how_many(
            samples,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 14),
            runs=100,
            rng=Random(42),
        )
        b = monte_carlo_how_many(
            samples,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 14),
            runs=100,
            rng=Random(42),
        )
        assert a == b

    def test_constant_throughput_yields_exact_count(self):
        # 3 items/day, 10 days inclusive → 30 items every run
        results = monte_carlo_how_many(
            [3],
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 10),
            runs=50,
            rng=Random(0),
        )
        assert all(r == 30 for r in results)

    def test_longer_window_produces_more_items(self):
        samples = [1, 2, 3]
        short = monte_carlo_how_many(
            samples,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 7),
            runs=200,
            rng=Random(0),
        )
        long = monte_carlo_how_many(
            samples,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 28),
            runs=200,
            rng=Random(0),
        )
        assert sum(long) / len(long) > sum(short) / len(short)

    def test_invalid_window(self):
        with pytest.raises(ValueError):
            monte_carlo_how_many(
                [1],
                start_date=date(2026, 1, 10),
                end_date=date(2026, 1, 1),
                runs=10,
                rng=Random(0),
            )


class TestBuildHistogram:
    def test_counts_dates(self):
        results = [
            date(2026, 1, 1),
            date(2026, 1, 1),
            date(2026, 1, 3),
        ]
        hist = build_histogram(results)
        assert hist.counts[date(2026, 1, 1)] == 2
        assert hist.counts[date(2026, 1, 3)] == 1
        assert hist.total == 3

    def test_counts_ints(self):
        results = [5, 5, 7, 9, 9, 9]
        hist = build_histogram(results)
        assert hist.counts[5] == 2
        assert hist.counts[9] == 3
        assert hist.total == 6
        assert hist.sorted_keys == [5, 7, 9]

    def test_empty_results_raises(self):
        with pytest.raises(ValueError):
            build_histogram([])


class TestForwardPercentile:
    """Used for when-will-it-be-done (date axis).

    Smallest x such that P(outcome <= x) >= p%. As p increases, x grows.
    """

    def test_median_returns_first_date_at_50pct_cumulative(self):
        hist = build_histogram([date(2026, 1, 1)] * 5 + [date(2026, 1, 2)] * 5)
        assert forward_percentile(hist, 50) == date(2026, 1, 1)
        assert forward_percentile(hist, 85) == date(2026, 1, 2)

    def test_skewed_distribution_monotonic_in_p(self):
        results = [date(2026, 1, 1)] * 100 + [date(2026, 1, 5)] * 50 + [date(2026, 1, 20)] * 10
        hist = build_histogram(results)
        assert (
            forward_percentile(hist, 50)
            <= forward_percentile(hist, 70)
            <= forward_percentile(hist, 85)
            <= forward_percentile(hist, 95)
        )

    def test_bounds(self):
        hist = build_histogram([date(2026, 1, 1)])
        with pytest.raises(ValueError):
            forward_percentile(hist, 0)
        with pytest.raises(ValueError):
            forward_percentile(hist, 101)


class TestBackwardPercentile:
    """Used for how-many-by-date (items axis).

    Largest x such that P(outcome >= x) >= p%. As p increases, x shrinks —
    "more confidence = commit to fewer items".
    """

    def test_basic_skewed_distribution(self):
        # 10 runs produced 20 items, 50 runs produced 15, 100 runs produced 10
        results = [20] * 10 + [15] * 50 + [10] * 100
        hist = build_histogram(results)
        # P(>= 10) = 160/160 = 100% — so 100th percentile is 10
        # P(>= 15) = 60/160 = 37.5% — so 30% confidence is 15
        # P(>= 20) = 10/160 = 6.25%
        assert backward_percentile(hist, 100) == 10
        assert backward_percentile(hist, 30) == 15
        # 85% confidence → must be at most 10 (since P(>=10)=100%)
        assert backward_percentile(hist, 85) == 10

    def test_confidence_inversely_monotonic_in_items(self):
        results = [10] * 100 + [12] * 50 + [15] * 10
        hist = build_histogram(results)
        p50 = backward_percentile(hist, 50)
        p85 = backward_percentile(hist, 85)
        p95 = backward_percentile(hist, 95)
        # Higher confidence ⇒ smaller (or equal) commitment number
        assert p95 <= p85 <= p50

    def test_bounds(self):
        hist = build_histogram([5])
        with pytest.raises(ValueError):
            backward_percentile(hist, 0)
        with pytest.raises(ValueError):
            backward_percentile(hist, 101)


class TestRenderHistogram:
    def test_renders_each_outcome_on_its_own_line(self):
        hist = build_histogram([5, 5, 7, 9, 9, 9])
        out = render_histogram(hist, label="items")
        lines = out.splitlines()
        # Header + 3 distinct outcomes
        assert len(lines) == 4
        assert "items" in lines[0]
        assert "5" in lines[1]
        assert "7" in lines[2]
        assert "9" in lines[3]
        # Tallest bar belongs to the most-frequent outcome (9 → count 3)
        bar_5 = lines[1].count("#")
        bar_9 = lines[3].count("#")
        assert bar_9 > bar_5


class TestSimulationConvergence:
    """Vacanti: ~1k runs gives the shape, ~10k stabilises."""

    def test_when_done_p50_recovers_known_median(self):
        # Uniform throughput on {2..6}, mean 4, backlog 100 → ~25 days
        results = monte_carlo_when_done(
            [2, 3, 4, 5, 6], 100, date(2026, 1, 1), runs=10_000, rng=Random(12345)
        )
        hist = build_histogram(results)
        expected = date(2026, 1, 1) + timedelta(days=24)
        assert abs((forward_percentile(hist, 50) - expected).days) <= 2

    def test_how_many_p50_recovers_known_median(self):
        # Uniform throughput on {2..6}, mean 4, 25-day window → ~100 items
        results = monte_carlo_how_many(
            [2, 3, 4, 5, 6],
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 25),
            runs=10_000,
            rng=Random(12345),
        )
        hist = build_histogram(results)
        # 25 days × 4 items/day = 100 items expected; median should be near
        assert abs(forward_percentile(hist, 50) - 100) <= 3
