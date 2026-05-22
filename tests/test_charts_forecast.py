"""Layer 2 (chart model) — tests for `flowmetrics.charts.forecast`.

The Monte Carlo primitives (`monte_carlo_when_done`,
`monte_carlo_how_many`, `build_histogram`, `forward_percentile`,
`backward_percentile`) are tested in `test_forecast.py` against the
CLI compute layer. These tests pin the model-layer wrappers: the
shape of the payload, the empty-state, and the percentile
convention (date-side forward vs. count-side backward).
"""

from __future__ import annotations

from datetime import date, datetime

from flowmetrics.charts.forecast import (
    HowManyModel,
    WhenDoneModel,
    build_how_many_model,
    build_when_done_model,
)
from flowmetrics.warehouse.queries import CompletedItem
from flowmetrics.windows import Window


def _items(n: int, *, base: date = date(2026, 1, 1)) -> list[CompletedItem]:
    """N items, one completion per day for `n` days starting at `base`."""
    return [
        CompletedItem(
            item_id=f"#{i}", title=f"t{i}", url=None,
            completed_at=datetime(base.year, base.month, base.day) + (
                datetime(2026, 1, 1) - datetime(2026, 1, 1)
            ),  # placeholder — see below
            cycle_time_days=1.0,
        )
        for i in range(n)
    ]


def _completed(n: int, when: date) -> CompletedItem:
    return CompletedItem(
        item_id=f"#{n}", title=f"t{n}", url=None,
        completed_at=datetime(when.year, when.month, when.day, 12),
        cycle_time_days=1.0,
    )


def _busy_history(days: int = 30) -> list[CompletedItem]:
    """One completion per day across `days` days — a stable
    throughput history the Monte Carlo can sample from."""
    from datetime import timedelta
    return [
        _completed(i, date(2026, 1, 1) + timedelta(days=i))
        for i in range(days)
    ]


class TestWhenDone:
    def test_empty_history_yields_empty_model(self):
        m = build_when_done_model(
            [], backlog=5, start_date=date(2026, 2, 1),
        )
        assert m.is_empty
        assert m.headline == "No throughput data yet."

    def test_resolves_histogram_and_percentiles_in_order(self):
        m = build_when_done_model(
            _busy_history(30), backlog=10,
            start_date=date(2026, 2, 1), runs=2_000, seed=0,
        )
        assert not m.is_empty
        assert m.p50_iso <= m.p85_iso <= m.p95_iso

    def test_percentile_rows_carry_display_strings(self):
        m = build_when_done_model(
            _busy_history(30), backlog=10,
            start_date=date(2026, 2, 1), runs=1_000, seed=0,
        )
        rows = m.percentile_rows
        assert [r["label"] for r in rows] == ["P50", "P85", "P95"]
        assert all(r["value_display"] for r in rows)

    def test_reference_window_clamps_the_sample(self):
        # 30 days of history; reference to 5 days → sample size 5.
        m = build_when_done_model(
            _busy_history(30), backlog=2,
            start_date=date(2026, 2, 1), runs=500, seed=0,
            reference=Window(from_=date(2026, 1, 1), to=date(2026, 1, 5)),
        )
        assert m.daily_throughput_n_days == 5


class TestHowMany:
    def test_empty_history_yields_empty_model(self):
        m = build_how_many_model(
            [], start_date=date(2026, 2, 1), end_date=date(2026, 2, 10),
        )
        assert m.is_empty

    def test_inverted_percentiles_p95_le_p85_le_p50(self):
        """For COUNTS the convention is inverted: P95 is the
        high-confidence floor (lower count), P50 is the median
        (higher count)."""
        m = build_how_many_model(
            _busy_history(30),
            start_date=date(2026, 2, 1), end_date=date(2026, 2, 10),
            runs=2_000, seed=0,
        )
        assert m.p95 <= m.p85 <= m.p50

    def test_percentile_rows_are_high_confidence_floors(self):
        m = build_how_many_model(
            _busy_history(30),
            start_date=date(2026, 2, 1), end_date=date(2026, 2, 5),
            runs=1_000, seed=0,
        )
        rows = m.percentile_rows
        # "≥ N items" framing.
        assert all("≥" in r["value_display"] for r in rows)

    def test_degenerate_window_yields_empty_model(self):
        m = build_how_many_model(
            _busy_history(10),
            start_date=date(2026, 3, 10), end_date=date(2026, 3, 1),
        )
        assert m.is_empty
