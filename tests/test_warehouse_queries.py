"""Layer 1 (data access) — tests for `flowmetrics.warehouse.queries`.

These functions are pure SQL: a DuckDB connection in, raw typed
rows out. No windowing, no percentiles, no chart decisions — the
chart-model layer (`flowmetrics.charts`) does the deciding; this
layer only fetches. The tests build a tiny in-memory `work_items`
table directly — no warehouse fixture, no CLI.
"""

from __future__ import annotations

from datetime import datetime

import duckdb

from flowmetrics.warehouse.queries import CompletedItem, completed_items


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
