"""Layer 2 (chart model) — tests for `flowmetrics.charts.aging`.

`build_aging_model` is pure: an in-flight snapshot + completed-item
rows in, a fully-resolved `AgingModel` out. Every aging decision —
the per-item age, the WIP filter, percentile thresholds and their
provenance, the empty-state classification, the cap control, the
column order and WIP-count badges — is decided and asserted here,
with no DuckDB and no Vega.
"""

from __future__ import annotations

import math
from datetime import date, datetime

from flowmetrics.charts.aging import build_aging_model
from flowmetrics.charts.primitives import Percentiles
from flowmetrics.warehouse.queries import CompletedItem, InFlightItem
from flowmetrics.windows import Window

ASOF = date(2026, 6, 1)


def _inflight(n: int, created: date, state: str = "Review") -> InFlightItem:
    return InFlightItem(
        item_id=f"#{n}",
        title=f"item {n}",
        url=None,
        created_at=datetime(created.year, created.month, created.day, 12),
        current_state=state,
    )


def _completed(n: int, completed: date, cycle: float) -> CompletedItem:
    return CompletedItem(
        item_id=f"c{n}",
        title=f"c{n}",
        url=None,
        completed_at=datetime(completed.year, completed.month, completed.day, 12),
        cycle_time_days=cycle,
    )


def _build(in_flight=(), completed=(), *, asof=ASOF, open_item_count=5,
           reference=None, wip_states=None):
    return build_aging_model(
        list(in_flight), list(completed),
        asof=asof, open_item_count=open_item_count,
        reference=reference, wip_states=wip_states,
    )


class TestAge:
    def test_same_day_item_ages_as_one_day(self):
        # Vacanti's CD - SD + 1 — a same-day item ages as 1d.
        m = _build([_inflight(1, ASOF)])
        assert m.items[0].age_days == 1

    def test_age_is_asof_minus_start_plus_one(self):
        m = _build([_inflight(1, date(2026, 5, 23))])  # 9 days before asof
        assert m.items[0].age_days == 10


class TestWipFilter:
    def test_keeps_only_wip_states_when_given(self):
        m = _build(
            [_inflight(1, ASOF, "Review"), _inflight(2, ASOF, "Backlog")],
            wip_states=frozenset({"Review"}),
        )
        assert {i.item_id for i in m.items} == {"#1"}

    def test_no_filter_keeps_every_state(self):
        m = _build([_inflight(1, ASOF, "Review"), _inflight(2, ASOF, "Backlog")])
        assert m.count == 2


class TestPercentiles:
    def test_drawn_from_completed_cycle_times(self):
        completed = [_completed(i, date(2026, 5, 1), float(i)) for i in range(1, 11)]
        m = _build([_inflight(1, ASOF)], completed)
        assert m.percentiles.source_count == 10
        assert m.percentiles.p50 == 5.5

    def test_reference_window_filters_the_sample(self):
        completed = [
            _completed(1, date(2026, 1, 1), 1.0),
            _completed(2, date(2026, 5, 15), 9.0),
        ]
        m = _build(
            [_inflight(1, ASOF)], completed,
            reference=Window(from_=date(2026, 5, 1), to=date(2026, 5, 31)),
        )
        assert m.percentiles.source_count == 1

    def test_no_completed_items_yields_zero_percentiles(self):
        m = _build([_inflight(1, ASOF)], [])
        assert m.percentiles == Percentiles(p50=0.0, p85=0.0, p95=0.0, source_count=0)


class TestEmptyState:
    def test_non_empty_has_no_empty_state(self):
        assert _build([_inflight(1, ASOF)]).empty_state is None

    def test_asof_after_coverage(self):
        m = _build([], [_completed(1, date(2026, 5, 1), 3.0)])
        assert m.empty_state == "asof_after_coverage"

    def test_asof_before_coverage(self):
        m = _build([], [_completed(1, date(2026, 7, 1), 3.0)])
        assert m.empty_state == "asof_before_coverage"

    def test_in_flight_never_captured(self):
        # asof within coverage, but no open rows ever recorded.
        m = _build(
            [],
            [_completed(1, date(2026, 5, 1), 3.0),
             _completed(2, date(2026, 7, 1), 3.0)],
            open_item_count=0,
        )
        assert m.empty_state == "in_flight_never_captured"

    def test_no_work_in_flight(self):
        # asof within coverage, open rows exist — just none at asof.
        m = _build(
            [],
            [_completed(1, date(2026, 5, 1), 3.0),
             _completed(2, date(2026, 7, 1), 3.0)],
            open_item_count=4,
        )
        assert m.empty_state == "no_work_in_flight"


class TestHeadline:
    def test_counts_in_flight_items(self):
        m = _build(
            [_inflight(1, ASOF), _inflight(2, ASOF)],
            [_completed(1, date(2026, 5, 1), 3.0)],
        )
        assert "2 in-flight items as of" in m.headline

    def test_names_percentiles_and_their_source(self):
        m = _build(
            [_inflight(1, ASOF)],
            [_completed(i, date(2026, 5, 1), float(i)) for i in range(1, 6)],
        )
        assert "P95" in m.headline
        assert "from 5 completed items" in m.headline

    def test_no_completed_items_says_so_plainly(self):
        m = _build([_inflight(1, ASOF)], [])
        assert "no percentile thresholds" in m.headline


class TestCap:
    def _ancient_plus_bulk(self):
        items = [_inflight(i, date(2026, 5, 1)) for i in range(1, 20)]
        items.append(_inflight(99, date(2024, 1, 1)))  # one ancient outlier
        return items

    def test_cap_floors_at_the_p95_line(self):
        completed = [
            _completed(i, date(2026, 5, 1), float(i * 10)) for i in range(1, 11)
        ]
        m = _build(self._ancient_plus_bulk(), completed)
        assert m.cap is not None
        assert m.cap.floor == math.ceil(m.percentiles.p95)

    def test_cap_falls_back_to_p95_of_ages_without_a_percentile_line(self):
        m = _build(self._ancient_plus_bulk(), [])
        assert m.percentiles.p95 == 0
        assert m.cap is not None  # still resolved, from the ages

    def test_no_cap_for_a_single_item(self):
        assert _build([_inflight(1, ASOF)]).cap is None


class TestColumnsAndBadges:
    def test_ordered_states_is_first_appearance_order(self):
        m = _build([
            _inflight(1, ASOF, "Review"),
            _inflight(2, ASOF, "Draft"),
            _inflight(3, ASOF, "Review"),
        ])
        assert m.ordered_states == ("Review", "Draft")

    def test_wip_badges_count_items_per_state(self):
        m = _build([
            _inflight(1, ASOF, "Review"),
            _inflight(2, ASOF, "Draft"),
            _inflight(3, ASOF, "Review"),
        ])
        assert m.wip_badges == (("Review", 2), ("Draft", 1))


class TestCoverage:
    def test_coverage_displays_the_completion_span(self):
        m = _build(
            [_inflight(1, ASOF)],
            [_completed(1, date(2026, 4, 1), 3.0),
             _completed(2, date(2026, 5, 20), 3.0)],
        )
        assert m.coverage_earliest_display is not None
        assert m.coverage_latest_display is not None

    def test_no_coverage_when_no_completed_items(self):
        m = _build([_inflight(1, ASOF)], [])
        assert m.coverage_earliest_display is None
