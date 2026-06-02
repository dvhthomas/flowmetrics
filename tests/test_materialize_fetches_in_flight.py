"""Materialize must capture in-flight items, not just completed ones.

Aging WIP needs items with `completed_at IS NULL` to plot. The
fetch primitive `fetch_in_flight(asof)` exists on the source
abstraction (and is implemented by the GitHub adapter), but
`materialize.materialize()` only called `fetch_completed_in_window`
in slice 1 — so the warehouse only ever knew about completed
work, and the aging chart was always empty even after a fresh
import.

These tests pin the new behavior: materialize calls BOTH fetch
primitives, writes the union to Parquet, and the resulting view
contains rows with `completed_at IS NULL`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from flowmetrics.compute import WorkItem
from flowmetrics.workflow import Contract
from flowmetrics.materialize import materialize


def _make_item(item_id, *, created_at, completed_at):
    """Minimal WorkItem for the materialize pipeline."""
    return WorkItem(
        item_id=item_id,
        title=f"item {item_id}",
        url=f"https://github.com/x/y/pull/{item_id.lstrip('#')}",
        author_login="alice",
        is_bot=False,
        created_at=created_at,
        completed_at=completed_at,
    )


class TestMaterializeFetchesInFlight:
    def test_both_fetch_methods_are_called(self, tmp_path: Path):
        """Pin the call shape: materialize hits BOTH fetch_completed
        and fetch_in_flight on every run."""
        contract = Contract(
            name="demo",
            source="github",
            repo="x/y",
            start=date(2026, 5, 4),
            stop=date(2026, 5, 19),
        )

        mock_source = MagicMock()
        mock_source.fetch_completed_in_window.return_value = []
        mock_source.fetch_in_flight.return_value = []

        with patch(
            "flowmetrics.materialize.make_github_source",
            return_value=mock_source,
        ):
            materialize(
                contract=contract,
                data_dir=tmp_path / "data",
                cache_dir=tmp_path / "cache",
                offline=False,
            )

        mock_source.fetch_completed_in_window.assert_called_once_with(
            contract.start, contract.stop
        )
        mock_source.fetch_in_flight.assert_called_once()

    def test_in_flight_items_appear_in_warehouse(self, tmp_path: Path):
        """End-to-end: after materialize, the work_items Parquet
        contains rows where `completed_at IS NULL` (in-flight)."""
        contract = Contract(
            name="demo",
            source="github",
            repo="x/y",
            start=date(2026, 5, 4),
            stop=date(2026, 5, 19),
        )

        completed_item = _make_item(
            "#1",
            created_at=datetime(2026, 5, 5, 10, 0, tzinfo=UTC),
            completed_at=datetime(2026, 5, 6, 12, 0, tzinfo=UTC),
        )
        in_flight_item = _make_item(
            "#2",
            created_at=datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
            completed_at=None,
        )

        mock_source = MagicMock()
        mock_source.fetch_completed_in_window.return_value = [completed_item]
        mock_source.fetch_in_flight.return_value = [in_flight_item]

        with patch(
            "flowmetrics.materialize.make_github_source",
            return_value=mock_source,
        ):
            materialize(
                contract=contract,
                data_dir=tmp_path / "data",
                cache_dir=tmp_path / "cache",
                offline=False,
            )

        # Read back the Parquet and assert both rows landed.
        from flowmetrics.app import open_warehouse

        con = open_warehouse(tmp_path / "data")
        try:
            rows = con.execute(
                "SELECT item_id, completed_at FROM work_items "
                "WHERE contract_id = 'demo' ORDER BY item_id"
            ).fetchall()
        finally:
            con.close()

        assert len(rows) == 2, f"both items must land in warehouse; got {rows}"
        # The in-flight item has completed_at NULL.
        by_id = {r[0]: r[1] for r in rows}
        assert by_id["#1"] is not None
        assert by_id["#2"] is None, (
            "in-flight item must persist with completed_at NULL — that's "
            "the signal aging-WIP queries depend on"
        )

    def test_in_flight_items_do_not_have_cycle_time(self, tmp_path: Path):
        """Sanity: in-flight items have no completion → no cycle
        time. The `cycle_time_days` column must be NULL for them
        (matters for downstream percentile filters that look for
        IS NOT NULL)."""
        contract = Contract(
            name="demo",
            source="github",
            repo="x/y",
            start=date(2026, 5, 4),
            stop=date(2026, 5, 19),
        )
        in_flight_item = _make_item(
            "#1",
            created_at=datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
            completed_at=None,
        )
        mock_source = MagicMock()
        mock_source.fetch_completed_in_window.return_value = []
        mock_source.fetch_in_flight.return_value = [in_flight_item]
        with patch(
            "flowmetrics.materialize.make_github_source",
            return_value=mock_source,
        ):
            materialize(
                contract=contract,
                data_dir=tmp_path / "data",
                cache_dir=tmp_path / "cache",
                offline=False,
            )

        from flowmetrics.app import open_warehouse

        con = open_warehouse(tmp_path / "data")
        try:
            row = con.execute(
                "SELECT cycle_time_days FROM work_items "
                "WHERE contract_id = 'demo' AND item_id = '#1'"
            ).fetchone()
        finally:
            con.close()

        assert row[0] is None
