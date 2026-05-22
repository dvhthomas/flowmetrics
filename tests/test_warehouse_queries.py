"""Layer 1 (data access) — tests for `flowmetrics.warehouse.queries`.

These functions are pure SQL: a DuckDB connection in, raw typed
rows out. No windowing, no percentiles, no chart decisions — the
chart-model layer (`flowmetrics.charts`) does the deciding; this
layer only fetches. The tests build a tiny in-memory `work_items`
table directly — no warehouse fixture, no CLI.
"""

from __future__ import annotations

from datetime import date, datetime

import duckdb

from flowmetrics.warehouse.queries import (
    CompletedItem,
    InFlightItem,
    completed_items,
    count_open_items,
    in_flight_snapshot,
)


def _warehouse() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(
        """CREATE TABLE work_items (
            contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
            title VARCHAR, url VARCHAR,
            created_at TIMESTAMP, completed_at TIMESTAMP,
            cycle_time_days DOUBLE
        )"""
    )
    rows = [
        # contract "c" — three completed, completion-ascending
        ("c", "github", "#1", "first", "http://x/1",
         datetime(2026, 1, 1), datetime(2026, 1, 4), 3.0),
        ("c", "github", "#2", "second", None,
         datetime(2026, 1, 2), datetime(2026, 1, 9), 7.0),
        ("c", "github", "#3", "third", "http://x/3",
         datetime(2026, 1, 1), datetime(2026, 2, 1), 31.0),
        # contract "c" — two in flight (completed_at NULL)
        ("c", "github", "#4", "open", None,
         datetime(2026, 1, 5), None, None),
        ("c", "github", "#5", "open2", None,
         datetime(2026, 1, 6), None, None),
        # a different contract — must be excluded
        ("other", "github", "#9", "elsewhere", None,
         datetime(2026, 1, 1), datetime(2026, 1, 2), 1.0),
    ]
    con.executemany("INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?)", rows)
    return con


class TestCompletedItems:
    def test_returns_completed_items_for_the_contract(self):
        items = completed_items(_warehouse(), "c")
        assert {i.item_id for i in items} == {"#1", "#2", "#3"}

    def test_excludes_in_flight_items(self):
        items = completed_items(_warehouse(), "c")
        assert "#4" not in {i.item_id for i in items}
        assert all(i.completed_at is not None for i in items)

    def test_excludes_other_contracts(self):
        items = completed_items(_warehouse(), "c")
        assert "#9" not in {i.item_id for i in items}

    def test_maps_every_column_onto_the_row_type(self):
        items = completed_items(_warehouse(), "c")
        first = next(i for i in items if i.item_id == "#1")
        assert first == CompletedItem(
            item_id="#1", title="first", url="http://x/1",
            completed_at=datetime(2026, 1, 4), cycle_time_days=3.0,
        )

    def test_null_url_survives_as_none(self):
        items = completed_items(_warehouse(), "c")
        second = next(i for i in items if i.item_id == "#2")
        assert second.url is None

    def test_rows_are_ordered_oldest_completion_first(self):
        items = completed_items(_warehouse(), "c")
        assert items == sorted(items, key=lambda i: i.completed_at)

    def test_unknown_contract_returns_empty(self):
        assert completed_items(_warehouse(), "nope") == []


def _warehouse_with_transitions() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(
        """CREATE TABLE work_items (
            contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
            title VARCHAR, url VARCHAR,
            created_at TIMESTAMP, completed_at TIMESTAMP,
            cycle_time_days DOUBLE)"""
    )
    con.execute(
        """CREATE TABLE transitions (
            contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
            entered_at TIMESTAMP, stage VARCHAR, signal VARCHAR)"""
    )
    con.executemany(
        "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?)",
        [
            # open at 2026-02-01: created before, not completed
            ("c", "github", "#1", "one", None,
             datetime(2026, 1, 1), None, None),
            # open: created before, completed AFTER the snapshot
            ("c", "github", "#2", "two", None,
             datetime(2026, 1, 5), datetime(2026, 3, 1), None),
            # closed: completed before the snapshot
            ("c", "github", "#3", "three", None,
             datetime(2026, 1, 2), datetime(2026, 1, 20), 18.0),
            # not yet created at the snapshot
            ("c", "github", "#4", "four", None,
             datetime(2026, 2, 15), None, None),
            # open, never transitioned
            ("c", "github", "#5", "five", None,
             datetime(2026, 1, 10), None, None),
        ],
    )
    con.executemany(
        "INSERT INTO transitions VALUES (?,?,?,?,?,?)",
        [
            ("c", "github", "#1", datetime(2026, 1, 2), "Draft", "open"),
            ("c", "github", "#1", datetime(2026, 1, 10), "Review", "ready"),
            # a transition AFTER the snapshot — must be ignored
            ("c", "github", "#1", datetime(2026, 3, 5), "Merged", "merge"),
            ("c", "github", "#2", datetime(2026, 1, 6), "Draft", "open"),
        ],
    )
    return con


def _by_id(items: list[InFlightItem], item_id: str) -> InFlightItem:
    return next(i for i in items if i.item_id == item_id)


class TestInFlightSnapshot:
    ASOF = date(2026, 2, 1)

    def test_includes_items_open_at_the_snapshot(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert {i.item_id for i in items} == {"#1", "#2", "#5"}

    def test_excludes_items_completed_by_the_snapshot(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert "#3" not in {i.item_id for i in items}

    def test_excludes_items_created_after_the_snapshot(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert "#4" not in {i.item_id for i in items}

    def test_item_completed_after_the_snapshot_is_still_in_flight(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert "#2" in {i.item_id for i in items}

    def test_current_state_is_the_latest_transition_at_or_before_asof(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert _by_id(items, "#1").current_state == "Review"

    def test_transitions_after_asof_do_not_set_the_state(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert _by_id(items, "#1").current_state != "Merged"

    def test_item_with_no_transitions_is_unknown(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert _by_id(items, "#5").current_state == "Unknown"

    def test_rows_are_ordered_by_creation(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert [i.item_id for i in items] == ["#1", "#2", "#5"]


class TestCountOpenItems:
    def test_counts_items_with_no_completion(self):
        # #1, #4, #5 have completed_at NULL.
        assert count_open_items(_warehouse_with_transitions(), "c") == 3

    def test_zero_for_an_unknown_contract(self):
        assert count_open_items(_warehouse_with_transitions(), "nope") == 0
