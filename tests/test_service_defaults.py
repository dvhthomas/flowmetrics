"""Behavioural spec for service-level defaults.

GitHub uses UTC for `mergedAt`. Today is always partial — including it
in the training window biases the simulator low because the rest of
today's merges haven't happened yet. So the default end of the training
window is yesterday-in-UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from flowmetrics.compute import WorkItem
from flowmetrics.service import (
    DEFAULT_TRAINING_DAYS,
    default_history_end,
    default_history_start,
    fetch_items_active_in_window,
)


def _wi(item_id: str, *, created_at: datetime, merged_at: datetime | None = None) -> WorkItem:
    return WorkItem(
        item_id=item_id,
        title=item_id,
        created_at=created_at,
        merged_at=merged_at,
    )


class _FakeSource:
    """Test double mirroring the Source protocol's two CFD-relevant
    methods. Sources don't return overlapping ids between completed
    and in_flight in practice (a PR can't be both merged-in-window
    and still-open-at-window-end), so the dedup is defensive."""

    def __init__(self, completed: list[WorkItem], in_flight: list[WorkItem]):
        self._completed = completed
        self._in_flight = in_flight
        self.completed_calls: list[tuple[date, date]] = []
        self.in_flight_calls: list[date] = []

    def fetch_completed_in_window(self, start: date, stop: date) -> list[WorkItem]:
        self.completed_calls.append((start, stop))
        return list(self._completed)

    def fetch_in_flight(self, asof: date) -> list[WorkItem]:
        self.in_flight_calls.append(asof)
        return list(self._in_flight)


class TestFetchItemsActiveInWindow:
    """The CFD needs items ACTIVE during the window — not just items
    that completed in the window. Categories the new function covers:
      A. Merged inside the window (completed list).
      B. Still open at window end (in_flight at stop list).
      C. Opened before the window and merged inside it (overlap with A).

    Union of (A) and (B) gives the right population. Dedupe on
    `item_id` so any defensive double-list is collapsed."""

    def test_returns_union_of_completed_and_in_flight(self):
        completed = [_wi("#1", created_at=datetime(2026, 4, 10, tzinfo=UTC),
                         merged_at=datetime(2026, 4, 20, tzinfo=UTC))]
        in_flight = [
            _wi("#2", created_at=datetime(2026, 4, 12, tzinfo=UTC), merged_at=None),
            _wi("#3", created_at=datetime(2026, 4, 25, tzinfo=UTC), merged_at=None),
        ]
        src = _FakeSource(completed, in_flight)
        items = fetch_items_active_in_window(src, date(2026, 4, 15), date(2026, 5, 14))
        ids = {it.item_id for it in items}
        assert ids == {"#1", "#2", "#3"}

    def test_uses_window_start_and_stop_for_completed_query(self):
        src = _FakeSource([], [])
        fetch_items_active_in_window(src, date(2026, 4, 15), date(2026, 5, 14))
        assert src.completed_calls == [(date(2026, 4, 15), date(2026, 5, 14))]

    def test_uses_window_stop_as_in_flight_snapshot_date(self):
        """The in-flight query asks 'what was still open at stop?' so
        we see end-of-window WIP."""
        src = _FakeSource([], [])
        fetch_items_active_in_window(src, date(2026, 4, 15), date(2026, 5, 14))
        assert src.in_flight_calls == [date(2026, 5, 14)]

    def test_dedupes_when_item_appears_in_both_lists(self):
        same = _wi("#1", created_at=datetime(2026, 4, 10, tzinfo=UTC), merged_at=None)
        src = _FakeSource([same], [same])
        items = fetch_items_active_in_window(src, date(2026, 4, 15), date(2026, 5, 14))
        # Despite appearing in both lists, the unioned set has one row.
        ids = [it.item_id for it in items]
        assert ids.count("#1") == 1

    def test_returns_empty_when_both_sources_empty(self):
        src = _FakeSource([], [])
        items = fetch_items_active_in_window(src, date(2026, 4, 15), date(2026, 5, 14))
        assert items == []


class TestDefaultHistoryEnd:
    def test_returns_yesterday_in_utc(self):
        today_utc = datetime.now(UTC).date()
        assert default_history_end() == today_utc - timedelta(days=1)

    def test_is_a_date_not_a_datetime(self):
        result = default_history_end()
        assert isinstance(result, date)
        assert not isinstance(result, datetime)


class TestDefaultHistoryStart:
    def test_default_is_29_days_before_default_end(self):
        # 30-day inclusive window: default_end - 29 days
        end = default_history_end()
        assert default_history_start() == end - timedelta(days=DEFAULT_TRAINING_DAYS - 1)

    def test_relative_to_given_end_date(self):
        end = date(2026, 5, 10)
        assert default_history_start(end) == date(2026, 4, 11)
        # Inclusive: end - start + 1 == 30
        assert (end - default_history_start(end)).days + 1 == DEFAULT_TRAINING_DAYS
