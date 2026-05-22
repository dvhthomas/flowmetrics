"""Layer 2 (chart model) — tests for `flowmetrics.charts.cycle_time`.

`build_cycle_time_model` is pure: a list of `CompletedItem` rows
+ a view `Window` in, a fully-resolved `CycleTimeModel` out. No
DuckDB, no Vega. Every chart decision — percentiles, the cap
control, tick density, the empty-state headline, the padded
x-domain — is decided here and asserted here. The tests construct
typed rows directly: no warehouse fixture, milliseconds per test.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from flowmetrics.charts.cycle_time import (
    CapControl,
    TickPolicy,
    build_cycle_time_model,
)
from flowmetrics.warehouse.queries import CompletedItem
from flowmetrics.windows import Window


def _item(n: int, completed: date, cycle: float, *, url: str | None = None):
    return CompletedItem(
        item_id=f"#{n}",
        title=f"item {n}",
        url=url,
        completed_at=datetime(completed.year, completed.month, completed.day, 12),
        cycle_time_days=cycle,
    )


def _run(n_days: int, cycle: float = 3.0):
    """A model over `n_days`+1 completed items, one per day."""
    items = [
        _item(i, date(2025, 1, 1) + timedelta(days=i), cycle)
        for i in range(n_days + 1)
    ]
    return build_cycle_time_model(items, view=None)


class TestShape:
    def test_item_count_and_points(self):
        items = [_item(1, date(2026, 1, 1), 2.0), _item(2, date(2026, 1, 2), 4.0)]
        m = build_cycle_time_model(items, view=None)
        assert m.item_count == 2
        assert len(m.points) == 2

    def test_points_keep_oldest_first_order(self):
        items = [_item(1, date(2026, 1, 1), 2.0), _item(2, date(2026, 1, 9), 4.0)]
        m = build_cycle_time_model(items, view=None)
        assert [p.item_id for p in m.points] == ["#1", "#2"]

    def test_point_carries_iso_and_display_dates(self):
        m = build_cycle_time_model([_item(1, date(2026, 1, 4), 2.0)], view=None)
        p = m.points[0]
        assert p.completed_at == "2026-01-04"
        assert p.completed_at_display == "Jan 04, 2026"


class TestPercentiles:
    def test_ordered_p50_le_p85_le_p95(self):
        m = _run(40)
        assert m.p50 <= m.p85 <= m.p95

    def test_linear_interpolation_matches_percentile_cont(self):
        # cycle times 1..10 — DuckDB percentile_cont reference values.
        items = [
            _item(i, date(2026, 1, 1) + timedelta(days=i - 1), float(i))
            for i in range(1, 11)
        ]
        m = build_cycle_time_model(items, view=None)
        assert m.p50 == 5.5                    # 1 + 0.50*(10-1)
        assert m.p85 == pytest.approx(8.65)    # 1 + 0.85*9
        assert m.p95 == pytest.approx(9.55)    # 1 + 0.95*9

    def test_percentiles_shift_when_the_window_narrows(self):
        """The percentile sample is the windowed items — narrowing
        the view changes the lines."""
        items = [
            _item(i, date(2026, 1, 1) + timedelta(days=i - 1), float(i))
            for i in range(1, 11)
        ]
        wide = build_cycle_time_model(items, view=None)
        narrow = build_cycle_time_model(
            items, view=Window(from_=date(2026, 1, 1), to=date(2026, 1, 5)),
        )
        assert narrow.p95 < wide.p95


class TestWindowing:
    def test_items_outside_the_view_are_excluded(self):
        items = [_item(1, date(2026, 1, 1), 1.0), _item(2, date(2026, 6, 1), 2.0)]
        m = build_cycle_time_model(
            items, view=Window(from_=date(2026, 5, 1), to=date(2026, 7, 1)),
        )
        assert [p.item_id for p in m.points] == ["#2"]

    def test_window_endpoints_are_inclusive(self):
        items = [_item(1, date(2026, 5, 1), 1.0), _item(2, date(2026, 5, 10), 2.0)]
        m = build_cycle_time_model(
            items, view=Window(from_=date(2026, 5, 1), to=date(2026, 5, 10)),
        )
        assert m.item_count == 2


class TestEmptyState:
    def test_empty_warehouse_headline_points_at_materialise(self):
        m = build_cycle_time_model([], view=None)
        assert m.item_count == 0
        assert "materialise" in m.headline.lower()

    def test_window_with_data_elsewhere_says_widen(self):
        items = [_item(1, date(2026, 1, 1), 1.0)]
        m = build_cycle_time_model(
            items, view=Window(from_=date(2026, 6, 1), to=date(2026, 7, 1)),
        )
        assert m.item_count == 0
        assert "warehouse covers" in m.headline.lower()
        assert "materialise" not in m.headline.lower()


class TestHeadline:
    def test_headline_names_all_three_percentiles(self):
        m = _run(5)
        assert "P50" in m.headline
        assert "P85" in m.headline
        assert "P95" in m.headline

    def test_headline_counts_items(self):
        m = _run(4)  # 5 items
        assert "5 items" in m.headline


class TestCapControl:
    def test_cap_floors_at_p95_and_ceilings_at_the_max(self):
        items = [
            _item(i, date(2026, 1, 1) + timedelta(days=i), float(i))
            for i in range(1, 21)
        ] + [_item(99, date(2026, 3, 1), 500.0)]
        m = build_cycle_time_model(items, view=None)
        assert m.cap is not None
        assert m.cap.floor == math.ceil(m.p95)
        assert m.cap.ceiling == 500
        assert m.cap.default == m.cap.ceiling  # opens showing all

    def test_no_cap_for_a_single_item(self):
        m = build_cycle_time_model([_item(1, date(2026, 1, 1), 5.0)], view=None)
        assert m.cap is None

    def test_no_cap_when_floor_meets_ceiling(self):
        # identical cycle times → p95 == max → nothing to crop.
        m = _run(5, cycle=5.0)
        assert m.cap is None


class TestTickPolicy:
    def test_short_span_is_daily(self):
        assert _run(20).ticks == TickPolicy("day", 1)

    def test_30_day_span_is_still_daily(self):
        assert _run(30).ticks == TickPolicy("day", 1)

    def test_31_day_span_steps_to_weekly(self):
        assert _run(31).ticks == TickPolicy("week", 1)

    def test_quarter_span_is_weekly(self):
        assert _run(90).ticks == TickPolicy("week", 1)

    def test_211_day_span_steps_to_monthly(self):
        assert _run(211).ticks == TickPolicy("month", 1)

    def test_multi_month_span_is_monthly(self):
        assert _run(420).ticks == TickPolicy("month", 1)

    def test_multi_year_span_steps_to_quarterly(self):
        assert _run(1200).ticks == TickPolicy("month", 3)


class TestXDomain:
    def test_domain_pads_one_day_each_side(self):
        items = [_item(1, date(2026, 1, 5), 1.0), _item(2, date(2026, 1, 10), 2.0)]
        m = build_cycle_time_model(items, view=None)
        assert m.x_domain == ("2026-01-04", "2026-01-11")

    def test_no_domain_when_empty(self):
        m = build_cycle_time_model([], view=None)
        assert m.x_domain is None
