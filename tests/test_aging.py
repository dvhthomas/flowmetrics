"""Vacanti's Aging Work In Progress chart (WWIBD pp. 50-51).

Aging plots in-flight items by their current workflow state (x-axis,
ordered earliest → latest) against their age in days (y-axis). Percentile
checkpoint lines are drawn from completed items' cycle times — same
distribution shown on the Scatterplot.

Properties under test:
1. Only in-flight items (not yet exited the workflow) appear.
2. Age is days elapsed since the item entered the workflow.
3. Current state = the workflow state the item is sitting in today.
4. Percentile checkpoint lines come from completed-item cycle times.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from flowmetrics.aging import AgingItem, compute_aging, cycle_time_percentiles
from flowmetrics.compute import FlowEfficiency, StatusInterval, WorkItem


def ts(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, tzinfo=UTC)


def _in_flight(
    item_id: str,
    *,
    created: datetime,
    intervals: list[tuple[str, datetime, datetime]],
) -> WorkItem:
    return WorkItem(
        item_id=item_id,
        title=f"t-{item_id}",
        created_at=created,
        merged_at=None,  # in flight
        status_intervals=[StatusInterval(s, e, name) for name, s, e in intervals],
    )


def _completed(
    item_id: str,
    *,
    created: datetime,
    merged: datetime,
    intervals: list[tuple[str, datetime, datetime]] | None = None,
) -> WorkItem:
    return WorkItem(
        item_id=item_id,
        title=f"t-{item_id}",
        created_at=created,
        merged_at=merged,
        status_intervals=[
            StatusInterval(s, e, name) for name, s, e in (intervals or [])
        ],
    )


class TestComputeAgingFiltering:
    def test_completed_items_are_excluded(self):
        items = [
            _completed("DONE-1", created=ts(2026, 4, 1), merged=ts(2026, 4, 10)),
            _in_flight(
                "OPEN-1",
                created=ts(2026, 4, 20),
                intervals=[("In Progress", ts(2026, 4, 20), ts(2026, 5, 1))],
            ),
        ]
        out = compute_aging(items, asof=date(2026, 5, 12))
        ids = {p.item_id for p in out}
        assert ids == {"OPEN-1"}
        assert "DONE-1" not in ids


class TestAgeComputation:
    def test_age_days_is_today_minus_created_date(self):
        items = [
            _in_flight(
                "X-1",
                created=ts(2026, 5, 1),  # 11 days before asof
                intervals=[("Open", ts(2026, 5, 1), ts(2026, 5, 12))],
            )
        ]
        out = compute_aging(items, asof=date(2026, 5, 12))
        assert out[0].age_days == 11

    def test_age_is_zero_for_just_created_item(self):
        items = [
            _in_flight(
                "X-2",
                created=ts(2026, 5, 12),
                intervals=[("Open", ts(2026, 5, 12), ts(2026, 5, 12))],
            )
        ]
        out = compute_aging(items, asof=date(2026, 5, 12))
        assert out[0].age_days == 0


class TestCurrentStateDerivation:
    def test_current_state_is_last_interval_status(self):
        items = [
            _in_flight(
                "X-1",
                created=ts(2026, 5, 1),
                intervals=[
                    ("Open", ts(2026, 5, 1), ts(2026, 5, 3)),
                    ("In Progress", ts(2026, 5, 3), ts(2026, 5, 12)),
                ],
            )
        ]
        out = compute_aging(items, asof=date(2026, 5, 12))
        assert out[0].current_state == "In Progress"

    def test_no_intervals_falls_back_to_unknown(self):
        items = [
            _in_flight("X-1", created=ts(2026, 5, 1), intervals=[]),
        ]
        out = compute_aging(items, asof=date(2026, 5, 12))
        assert out[0].current_state == "Unknown"


class TestCycleTimePercentiles:
    """The Aging chart overlays percentile lines computed from completed
    items' cycle times. Same distribution as the Scatterplot — used as
    checkpoints to see if in-flight items are aging past historic norms."""

    def test_returns_p50_p70_p85_p95_in_days(self):
        # Cycle times: 1, 2, 3, ..., 10 days
        completed = [
            FlowEfficiency(
                item_id=f"#{i}",
                title=f"PR {i}",
                created_at=ts(2026, 4, 1),
                merged_at=ts(2026, 4, 1) + timedelta(days=i),
                cycle_time=timedelta(days=i),
                active_time=timedelta(days=i),
                efficiency=1.0,
            )
            for i in range(1, 11)
        ]
        pct = cycle_time_percentiles(completed)
        assert set(pct.keys()) == {50, 70, 85, 95}
        # Monotonically non-decreasing across percentiles
        assert pct[50] <= pct[70] <= pct[85] <= pct[95]
        # All values in days, positive
        for v in pct.values():
            assert v > 0

    def test_empty_input_yields_zero_percentiles(self):
        pct = cycle_time_percentiles([])
        assert pct == {50: 0.0, 70: 0.0, 85: 0.0, 95: 0.0}


class TestAgingItemFields:
    def test_carries_title_and_id(self):
        items = [
            _in_flight(
                "BIGTOP-42",
                created=ts(2026, 5, 1),
                intervals=[("In Progress", ts(2026, 5, 1), ts(2026, 5, 12))],
            )
        ]
        out = compute_aging(items, asof=date(2026, 5, 12))
        assert isinstance(out[0], AgingItem)
        assert out[0].item_id == "BIGTOP-42"
        assert out[0].title == "t-BIGTOP-42"


class TestEmptyInputs:
    def test_no_items_returns_empty(self):
        out = compute_aging([], asof=date(2026, 5, 12))
        assert out == []
