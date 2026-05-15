"""Vacanti's six CFD properties become tests.

Property #1 — top line = cumulative arrivals, bottom = departures.
Property #2 — no line ever decreases.
Property #3 — vertical distance between two lines = WIP in that band.
Property #4 — horizontal distance between two lines ≈ avg cycle time.
Property #5 — past data only (enforced by what we fetch; not testable here).
Property #6 — slope between intervals = avg arrival rate at that band.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from flowmetrics.cfd import CfdPoint, build_cfd
from flowmetrics.compute import StatusInterval, WorkItem


def ts(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


def _item(
    *,
    item_id: str,
    created: datetime,
    merged: datetime | None,
    intervals: list[tuple[str, datetime, datetime]],
) -> WorkItem:
    return WorkItem(
        item_id=item_id,
        title=f"t-{item_id}",
        created_at=created,
        merged_at=merged,
        status_intervals=[StatusInterval(s, e, name) for name, s, e in intervals],
    )


class TestCfdBasicShape:
    def test_returns_one_point_per_interval(self):
        # 5-day window, daily interval → 5 points (inclusive of both endpoints)
        items: list[WorkItem] = []
        points = build_cfd(
            items,
            workflow=["Open", "Done"],
            start=date(2026, 5, 1),
            stop=date(2026, 5, 5),
            interval=timedelta(days=1),
        )
        assert len(points) == 5
        for p in points:
            assert isinstance(p, CfdPoint)
            assert set(p.counts_by_state.keys()) == {"Open", "Done"}

    def test_each_point_emits_all_workflow_states(self):
        items: list[WorkItem] = []
        points = build_cfd(
            items,
            workflow=["Open", "In Progress", "Done"],
            start=date(2026, 5, 1),
            stop=date(2026, 5, 3),
            interval=timedelta(days=1),
        )
        for p in points:
            assert list(p.counts_by_state.keys()) == [
                "Open", "In Progress", "Done",
            ]


class TestVacantiProperty1ArrivalsAndDepartures:
    """Top line = cumulative arrivals; bottom = cumulative departures."""

    def test_top_line_at_end_equals_total_items_arrived(self):
        items = [
            _item(
                item_id=f"X-{i}",
                created=ts(2026, 5, 1),
                merged=ts(2026, 5, 5),
                intervals=[("Open", ts(2026, 5, 1), ts(2026, 5, 5))],
            )
            for i in range(3)
        ]
        points = build_cfd(
            items, workflow=["Open", "Done"],
            start=date(2026, 5, 1), stop=date(2026, 5, 6),
            interval=timedelta(days=1),
        )
        # At the final sample, all 3 items have arrived (entered Open)
        assert points[-1].counts_by_state["Open"] == 3

    def test_bottom_line_at_end_equals_total_items_departed(self):
        # 2 items merged before stop, 1 still in flight
        items = [
            _item(
                item_id="X-1",
                created=ts(2026, 5, 1),
                merged=ts(2026, 5, 3),
                intervals=[("Open", ts(2026, 5, 1), ts(2026, 5, 3))],
            ),
            _item(
                item_id="X-2",
                created=ts(2026, 5, 1),
                merged=ts(2026, 5, 4),
                intervals=[("Open", ts(2026, 5, 1), ts(2026, 5, 4))],
            ),
            _item(
                item_id="X-3",
                created=ts(2026, 5, 1),
                merged=None,  # still in flight
                intervals=[("Open", ts(2026, 5, 1), ts(2026, 5, 6))],
            ),
        ]
        points = build_cfd(
            items, workflow=["Open", "Done"],
            start=date(2026, 5, 1), stop=date(2026, 5, 6),
            interval=timedelta(days=1),
        )
        # At end of window: 2 departures, 1 still in Open
        assert points[-1].counts_by_state["Done"] == 2
        assert points[-1].counts_by_state["Open"] == 3


class TestVacantiProperty2NoLineDecreases:
    """No line on a CFD can ever decrease (cumulative)."""

    def test_every_line_is_monotonic_non_decreasing(self):
        # Mixed flow: arrivals and departures interleaved
        items = [
            _item(
                item_id=f"X-{i}",
                created=ts(2026, 5, i),
                merged=ts(2026, 5, i + 1),
                intervals=[
                    ("Open", ts(2026, 5, i), ts(2026, 5, i) + timedelta(hours=6)),
                    ("In Progress", ts(2026, 5, i) + timedelta(hours=6), ts(2026, 5, i + 1)),
                ],
            )
            for i in range(1, 4)
        ]
        points = build_cfd(
            items, workflow=["Open", "In Progress", "Done"],
            start=date(2026, 5, 1), stop=date(2026, 5, 6),
            interval=timedelta(days=1),
        )
        for state in ["Open", "In Progress", "Done"]:
            values = [p.counts_by_state[state] for p in points]
            assert values == sorted(values), (
                f"state {state!r} decreased: {values}"
            )


class TestVacantiProperty3VerticalDistanceIsWip:
    """At any time T, line_for(state) - line_for(next_state) = items
    currently in `state`."""

    def test_vertical_distance_matches_wip(self):
        # Two items both enter In Progress by 5/3 but neither is Done yet
        items = [
            _item(
                item_id=f"X-{i}",
                created=ts(2026, 5, 1),
                merged=None,
                intervals=[
                    ("Open", ts(2026, 5, 1), ts(2026, 5, 2)),
                    ("In Progress", ts(2026, 5, 2), ts(2026, 5, 10)),
                ],
            )
            for i in range(2)
        ]
        points = build_cfd(
            items, workflow=["Open", "In Progress", "Done"],
            start=date(2026, 5, 5), stop=date(2026, 5, 5),
            interval=timedelta(days=1),
        )
        p = points[0]
        # WIP in In Progress at 5/5 = (entered In Progress) - (entered Done) = 2 - 0 = 2
        assert p.counts_by_state["In Progress"] - p.counts_by_state["Done"] == 2


class TestVacantiProperty6SlopeIsArrivalRate:
    """Slope = avg arrival rate. Difference between two consecutive
    samples on a line = items that entered that state in the interval."""

    def test_slope_equals_count_of_new_arrivals_in_interval(self):
        # 1 arrival per day on days 1, 2, 3
        items = [
            _item(
                item_id=f"X-{i}",
                created=ts(2026, 5, i),
                merged=None,
                intervals=[("Open", ts(2026, 5, i), ts(2026, 5, i) + timedelta(days=5))],
            )
            for i in range(1, 4)
        ]
        points = build_cfd(
            items, workflow=["Open", "Done"],
            start=date(2026, 5, 1), stop=date(2026, 5, 4),
            interval=timedelta(days=1),
        )
        opens = [p.counts_by_state["Open"] for p in points]
        # Days 5/1, 5/2, 5/3, 5/4 → cumulative arrivals 1, 2, 3, 3
        assert opens == [1, 2, 3, 3]
        # Slope between consecutive points = 1 per day
        for i in range(1, 3):
            assert opens[i] - opens[i - 1] == 1


class TestGithubLikeItems:
    """Items without status_intervals (typical GitHub PRs) must still
    produce a valid two-state CFD: top line tracks `created_at`, bottom
    line tracks `merged_at`. Earlier bug: both lines collapsed to
    merged_at because merged_at was used as evidence for every
    state-or-later, including the first state."""

    def test_open_line_tracks_created_at_not_merged_at(self):
        # Three PRs, each created 2 days before merge
        items = [
            WorkItem(
                item_id=f"#{i}",
                title=f"PR {i}",
                created_at=ts(2026, 5, 1),
                merged_at=ts(2026, 5, 3),
                status_intervals=[],  # GitHub: no workflow history
            )
            for i in range(3)
        ]
        # At 5/2 (between created and merged): Open=3, Merged=0
        points = build_cfd(
            items, workflow=["Open", "Merged"],
            start=date(2026, 5, 2), stop=date(2026, 5, 2),
            interval=timedelta(days=1),
        )
        assert points[0].counts_by_state["Open"] == 3
        assert points[0].counts_by_state["Merged"] == 0

    def test_at_merge_date_both_lines_match(self):
        items = [
            WorkItem(
                item_id="#1", title="P", created_at=ts(2026, 5, 1),
                merged_at=ts(2026, 5, 3), status_intervals=[],
            )
        ]
        points = build_cfd(
            items, workflow=["Open", "Merged"],
            start=date(2026, 5, 3), stop=date(2026, 5, 3),
            interval=timedelta(days=1),
        )
        # After merge: arrivals=1, departures=1 → WIP=0
        assert points[0].counts_by_state["Open"] == 1
        assert points[0].counts_by_state["Merged"] == 1


class TestGithubOpenPrWithReviewDecisionIntervals:
    """fetch_in_flight on GitHub populates status_intervals from
    review-decision states (Draft / Awaiting Review / Changes
    Requested / Approved) — NOT from the user's --workflow. When the
    user runs `flow cfd --workflow 'Open,Merged'`, those intervals
    don't match the workflow at all. The chart must still place the
    item correctly: an item that exists in the system on date T is
    in the first workflow step (Open) at T regardless of whether
    any of its intervals say so.

    Regression scenario: an open PR with intervals=[Awaiting Review]
    and merged_at=None used to render zero across the CFD because
    `_entry_date` only fell back to created_at when intervals were
    empty.
    """

    def test_open_pr_with_non_workflow_intervals_still_shows_in_first_state(self):
        # PR opened 4/15, currently in 'Awaiting Review' (a review-
        # decision state, not in the user's --workflow). Workflow is
        # Open / Merged.
        items = [
            WorkItem(
                item_id="#42",
                title="open PR",
                created_at=ts(2026, 4, 15),
                merged_at=None,
                status_intervals=[StatusInterval(
                    ts(2026, 4, 15), None, "Awaiting Review"
                )],
            )
        ]
        # At 4/20 (between open and never-merged): should show in Open.
        points = build_cfd(
            items, workflow=["Open", "Merged"],
            start=date(2026, 4, 20), stop=date(2026, 4, 20),
            interval=timedelta(days=1),
        )
        assert points[0].counts_by_state["Open"] == 1
        assert points[0].counts_by_state["Merged"] == 0

    def test_merged_pr_with_review_decision_intervals_open_at_created_not_merged(self):
        """Symmetric case: a merged PR that carried review-decision
        intervals. The Open line should still rise on created_at, NOT
        on merged_at."""
        items = [
            WorkItem(
                item_id="#42",
                title="merged PR",
                created_at=ts(2026, 4, 15),
                merged_at=ts(2026, 4, 20),
                status_intervals=[
                    StatusInterval(ts(2026, 4, 15), ts(2026, 4, 18), "Awaiting Review"),
                    StatusInterval(ts(2026, 4, 18), ts(2026, 4, 20), "Approved"),
                ],
            )
        ]
        # At 4/17 (between creation and merge): Open=1, Merged=0.
        points = build_cfd(
            items, workflow=["Open", "Merged"],
            start=date(2026, 4, 17), stop=date(2026, 4, 17),
            interval=timedelta(days=1),
        )
        assert points[0].counts_by_state["Open"] == 1
        assert points[0].counts_by_state["Merged"] == 0


class TestEmptyInputs:
    def test_no_items_yields_all_zero_points(self):
        points = build_cfd(
            [], workflow=["Open", "Done"],
            start=date(2026, 5, 1), stop=date(2026, 5, 3),
            interval=timedelta(days=1),
        )
        for p in points:
            assert all(c == 0 for c in p.counts_by_state.values())

    def test_empty_workflow_raises(self):
        with pytest.raises(ValueError):
            build_cfd(
                [], workflow=[],
                start=date(2026, 5, 1), stop=date(2026, 5, 3),
                interval=timedelta(days=1),
            )

    def test_inverted_window_raises(self):
        with pytest.raises(ValueError):
            build_cfd(
                [], workflow=["A"],
                start=date(2026, 5, 5), stop=date(2026, 5, 1),
                interval=timedelta(days=1),
            )
