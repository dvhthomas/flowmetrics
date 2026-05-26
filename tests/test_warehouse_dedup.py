"""Warehouse read-side deduplication: items in multiple snapshots
collapse to the latest row.

The materialise step writes a fresh Parquet file per ETL-run day
(`data/work_items/contract_id={name}/year={Y}/month={M}/day={D}/`).
Cross-day re-runs accumulate snapshots, so a single work item can
appear in N partitions — N+1 if we run today and N days have
already passed since first materialise. Each snapshot captures
the state of that item at the ETL time: in-flight on day 1
(completed_at NULL), completed on day 5 (completed_at filled in),
etc.

Without dedup, every query joining `work_items` would see N rows
per item, breaking counts and percentile calculations. The
`_open_warehouse` helper in `app.py` registers a VIEW that picks
the LATEST snapshot per `(contract_id, source, item_id)` so all
component renderers see one canonical row per item — the current
state — regardless of how many ETL runs accumulated on disk.

History remains queryable from the raw parquet glob if a future
caller (e.g. "what did aging look like at X-snapshot?") needs it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import pytest


@pytest.fixture
def two_snapshot_warehouse(tmp_path: Path) -> Path:
    """Build a warehouse with TWO daily snapshots for the same item.

    The first snapshot (May 04) captures the item in-flight
    (completed_at NULL). The second snapshot (May 06) captures
    the same item as completed.
    """
    contract = "demo"
    base = tmp_path / "work_items" / f"contract_id={contract}"
    snap1 = base / "year=2026" / "month=05" / "day=04" / "items.parquet"
    snap2 = base / "year=2026" / "month=05" / "day=06" / "items.parquet"
    snap1.parent.mkdir(parents=True, exist_ok=True)
    snap2.parent.mkdir(parents=True, exist_ok=True)

    def _write(path: Path, completed_at, mat_at, cycle):
        con = duckdb.connect()
        con.execute(
            """CREATE TEMP TABLE wi (
                source VARCHAR, repo VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR, author VARCHAR, is_bot BOOLEAN,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE, contract_id VARCHAR,
                materialised_at TIMESTAMP, run_id VARCHAR
            )"""
        )
        con.execute(
            "INSERT INTO wi VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                "github", "demo/repo", "#1", "demo item", None,
                "alice", False,
                datetime(2026, 5, 4, 9, 0),
                completed_at,
                cycle,
                contract,
                mat_at,
                "rid",
            ],
        )
        p = str(path).replace("'", "''")
        con.execute(f"COPY wi TO '{p}' (FORMAT PARQUET)")
        con.close()

    # Snapshot 1: in-flight on day 04 (materialised May 04 14:00).
    _write(snap1, None, datetime(2026, 5, 4, 14, 0), None)
    # Snapshot 2: completed on day 06 (materialised May 06 14:00).
    _write(snap2, datetime(2026, 5, 5, 15, 0), datetime(2026, 5, 6, 14, 0), 2.0)

    return tmp_path


def _open_warehouse_via_app(data_dir: Path) -> duckdb.DuckDBPyConnection:
    """Exercise the production code path so this test pins the
    runtime behavior, not a parallel implementation."""
    from flowmetrics.app import create_app

    contracts_dir = data_dir.parent / "contracts"
    contracts_dir.mkdir(exist_ok=True)
    # Ensure the app's import side-effects fire (templates, helpers).
    _ = create_app(data_dir=data_dir, contracts_dir=contracts_dir)
    from flowmetrics.app import open_warehouse_for_test

    return open_warehouse_for_test(data_dir)


class TestWorkItemsDedup:
    def test_same_item_in_two_snapshots_appears_once_in_view(
        self, two_snapshot_warehouse: Path
    ):
        """The same item exists in May 04 (in-flight) and May 06
        (completed) snapshots. The `work_items` view must return
        exactly ONE row for that item — the May 06 snapshot."""
        con = _open_warehouse_via_app(two_snapshot_warehouse)
        try:
            rows = con.execute(
                "SELECT item_id, completed_at, cycle_time_days "
                "FROM work_items WHERE item_id = '#1'"
            ).fetchall()
            assert len(rows) == 1, (
                f"expected 1 deduplicated row; got {len(rows)}: {rows}"
            )
            _item_id, completed_at, cycle = rows[0]
            assert completed_at is not None, (
                "latest snapshot must have completed_at filled in"
            )
            assert cycle == 2.0, (
                f"latest snapshot's cycle_time_days should be 2.0; "
                f"got {cycle}"
            )
        finally:
            con.close()

    def test_dedup_picks_latest_by_materialised_at(
        self, two_snapshot_warehouse: Path
    ):
        """The chosen row is the LATEST `materialised_at`, not the
        latest by some other column. Pin this so partition-order
        accidents don't pick the wrong row."""
        con = _open_warehouse_via_app(two_snapshot_warehouse)
        try:
            row = con.execute(
                "SELECT materialised_at FROM work_items "
                "WHERE item_id = '#1'"
            ).fetchone()
            # materialised_at on the May 06 snapshot was 14:00 UTC.
            assert row[0].strftime("%Y-%m-%d") == "2026-05-06"
        finally:
            con.close()

    def test_raw_parquet_glob_still_shows_both_snapshots(
        self, two_snapshot_warehouse: Path
    ):
        """The dedup is a read-side VIEW concern; the on-disk
        parquet keeps every snapshot. Verified so future "what did
        aging look like at snapshot X" work has the raw data
        available."""
        con = duckdb.connect()
        glob = (
            two_snapshot_warehouse / "work_items" / "**" / "*.parquet"
        ).as_posix()
        rows = con.execute(
            f"SELECT count(*) FROM read_parquet('{glob}', "
            f"hive_partitioning = true) WHERE item_id = '#1'"
        ).fetchone()
        con.close()
        assert rows[0] == 2, (
            f"raw parquet must preserve both snapshots; got "
            f"{rows[0]} rows for #1"
        )
