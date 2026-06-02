"""`flow materialize` must anchor stored timestamps to UTC.

DuckDB's default session timezone is the system timezone. When the
INSERT path receives a tz-aware datetime and the target column is
plain `TIMESTAMP` (no TZ), DuckDB converts via session-TZ first and
then drops the offset — so the same source data on a US/Pacific host
and on a UTC CI runner ended up with different wall-time values in
the same Parquet column. That made `CAST(completed_at AS DATE)`
bucket items into different calendar days depending on where the
materialize ran. The downstream symptoms: throughput counts, chart
buckets, and the work-items-table filter all disagree across hosts.

The rule (see [[feedback_flowmetrics_anchor_is_authoritative]]): the
anchor is authoritative and TZ-independent. These tests pin the
invariant.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from flowmetrics.materialize import _write_work_items_parquet
from flowmetrics.workflow import Workflow


class _StubItem:
    """Minimal WorkItem shape that `_work_item_row` reads."""

    def __init__(
        self,
        item_id: str,
        created_at: datetime,
        completed_at: datetime | None,
    ):
        self.item_id = item_id
        self.title = "stub"
        self.url = None
        self.author_login = "stub"
        self.is_bot = False
        self.created_at = created_at
        self.completed_at = completed_at


def _workflow() -> Workflow:
    return Workflow(
        name="stub",
        source="github",
        repo="org/repo",
    )


def test_completed_at_stored_as_utc_walltime():
    """An item completed just past midnight UTC must read back with
    that UTC wall-time, regardless of the session TZ used to write
    it. Without the fix, MDT (UTC-6) would silently shift the stored
    value six hours back and the date-bucket would slide to the
    previous calendar day."""
    out_path = Path(tempfile.mkdtemp()) / "items.parquet"
    completed = datetime(2026, 5, 5, 1, 30, 0, tzinfo=UTC)
    item = _StubItem(
        item_id="#1",
        created_at=datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC),
        completed_at=completed,
    )

    _write_work_items_parquet(
        items=[item],
        workflow=_workflow(),
        run_id="run1",
        materialized_at=datetime(2026, 5, 5, 2, 0, 0, tzinfo=UTC),
        out_path=out_path,
    )

    # Force the READ session TZ off UTC so we'd notice if the read
    # path were doing its own conversion. Stored value should still
    # be the original UTC wall-time.
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='America/Los_Angeles'")
    row = con.execute(
        "SELECT completed_at, CAST(completed_at AS DATE) "
        f"FROM read_parquet('{out_path}')"
    ).fetchone()
    con.close()

    stored, bucket = row
    # Stored as a naive timestamp matching the UTC wall-time.
    assert stored == datetime(2026, 5, 5, 1, 30, 0), (
        f"completed_at must be stored as UTC wall-time; got {stored}"
    )
    assert bucket.isoformat() == "2026-05-05", (
        f"date bucket must follow UTC wall-time; got {bucket}"
    )
