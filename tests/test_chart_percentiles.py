"""Behavioural spec for the chart-percentile helper.

Every chart in the report annotates its data with Vacanti's 50/70/85/95
percentile lines. The helper turns a sequence of values into a dict of
{percentile: value} so the chart code can draw lines at those positions.
"""

from __future__ import annotations

import pytest

from flowmetrics.percentiles import chart_percentiles


class TestChartPercentiles:
    def test_returns_all_four_canonical_levels(self):
        result = chart_percentiles([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        assert set(result.keys()) == {50, 70, 85, 95}

    def test_50th_is_median_for_simple_set(self):
        # 10 values 1..10. The 50th percentile via the cumulative
        # method is the value at index ceil(0.5 * 10) - 1 = 4, i.e. 5.
        result = chart_percentiles([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        assert result[50] == 5

    def test_95th_for_skewed_distribution(self):
        # Right-skewed: 9 ones and a 100. 95th percentile reaches the 100.
        values = [1] * 9 + [100]
        result = chart_percentiles(values)
        assert result[95] == 100

    def test_85th_for_uniform_distribution(self):
        values = list(range(1, 101))  # 1..100
        # The 85th percentile of 1..100 = the 85th value when sorted = 85
        result = chart_percentiles(values)
        assert result[85] == 85

    def test_monotonic_increasing(self):
        # P50 ≤ P70 ≤ P85 ≤ P95 for any input
        values = [3, 1, 4, 1, 5, 9, 2, 6, 5, 3, 5]
        r = chart_percentiles(values)
        assert r[50] <= r[70] <= r[85] <= r[95]

    def test_empty_input_raises(self):
        with pytest.raises(ValueError):
            chart_percentiles([])

    def test_single_value(self):
        result = chart_percentiles([42])
        assert result[50] == 42
        assert result[95] == 42
