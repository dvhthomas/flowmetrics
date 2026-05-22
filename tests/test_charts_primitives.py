"""Layer 2 — tests for the shared chart primitives.

`percentile_cont`, `Percentiles` and `RangeControl` are the chart
decisions that recur across more than one chart. Defined once,
tested once, pure functions — no DuckDB, no Vega.
"""

from __future__ import annotations

import pytest

from flowmetrics.charts.primitives import (
    Percentiles,
    RangeControl,
    percentile_cont,
    percentiles_from,
    range_control,
)


class TestPercentileCont:
    def test_empty_sample_is_zero(self):
        assert percentile_cont([], 0.5) == 0.0

    def test_single_value(self):
        assert percentile_cont([7.0], 0.95) == 7.0

    def test_linear_interpolation_matches_duckdb_percentile_cont(self):
        vals = [float(i) for i in range(1, 11)]  # 1..10
        assert percentile_cont(vals, 0.50) == 5.5
        assert percentile_cont(vals, 0.85) == pytest.approx(8.65)
        assert percentile_cont(vals, 0.95) == pytest.approx(9.55)

    def test_unsorted_input_is_sorted_first(self):
        assert percentile_cont([10, 1, 5, 3, 8], 0.5) == 5


class TestPercentilesFrom:
    def test_resolves_all_three_plus_the_count(self):
        p = percentiles_from([float(i) for i in range(1, 11)])
        assert p.p50 == 5.5
        assert p.source_count == 10

    def test_ordered_p50_le_p85_le_p95(self):
        p = percentiles_from([float(i) for i in range(1, 21)])
        assert p.p50 <= p.p85 <= p.p95

    def test_empty_sample(self):
        p = percentiles_from([])
        assert p == Percentiles(p50=0.0, p85=0.0, p95=0.0, source_count=0)


class TestRangeControl:
    def test_runs_floor_to_max_opening_at_the_ceiling(self):
        rc = range_control(10.0, [1, 2, 3, 500])
        assert rc == RangeControl(floor=10, ceiling=500, default=500)

    def test_floor_is_rounded_up(self):
        rc = range_control(9.2, [1, 2, 500])
        assert rc is not None
        assert rc.floor == 10

    def test_absent_for_fewer_than_two_values(self):
        assert range_control(5.0, [100]) is None

    def test_absent_when_the_floor_meets_the_ceiling(self):
        # ceil(floor) >= ceil(max) → nothing to crop.
        assert range_control(50.0, [10, 20, 30]) is None

    def test_absent_when_the_floor_is_above_the_ceiling(self):
        assert range_control(999.0, [1, 2, 3]) is None
