"""Component tests for `flowmetrics.web.components.work_items_table`.

The table is the second composable component in Slice 2. Same
pattern as cycle_time: a pure render function reads from DuckDB and
returns a typed payload that a Jinja partial renders.

Interaction (filter by title, sort by column) is client-side JS for
v1 — data volumes are small (≤ a few hundred rows per contract per
window) and snappy local sort beats round-tripping through HTMX.
The tests assert the contract at the data and the spec level; the
e2e file tests the in-browser interactivity.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.web.components.work_items_table import render

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
    tmp = Path(tempfile.mkdtemp())
    contracts_dir = tmp / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp / "data"
    (contracts_dir / "astral-uv-week.yaml").write_text(
        yaml.safe_dump(
            {
                "contract": {
                    "name": "astral-uv-week",
                    "source": "github",
                    "repo": "astral-sh/uv",
                    "start": "2026-05-04",
                    "stop": "2026-05-10",
                }
            }
        )
    )
    res = CliRunner().invoke(
        cli,
        [
            "materialise",
            "astral-uv-week",
            "--data-dir",
            str(data_dir),
            "--workflows-dir",
            str(contracts_dir),
            "--cache-dir",
            str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, res.output

    con = duckdb.connect(":memory:")
    glob = (data_dir / "work_items" / "**" / "*.parquet").as_posix()
    con.execute(
        f"CREATE VIEW work_items AS "
        f"SELECT * FROM read_parquet('{glob}', hive_partitioning = true)"
    )
    yield con
    con.close()


class TestWorkItemsTableShape:
    def test_renders_one_row_per_completed_item(self, warehouse):
        """`count` is the TOTAL matching rows (across pages);
        `len(rows)` is items on the current page (default 25)."""
        data = render(warehouse, "astral-uv-week")
        assert data.count == 43
        assert len(data.rows) == 25
        assert data.total_pages == 2
        # Page 2 has the remaining 18 rows.
        page2 = render(warehouse, "astral-uv-week", page=2)
        assert page2.count == 43
        assert len(page2.rows) == 18

    def test_row_fields_cover_identity_lifecycle_and_link(self, warehouse):
        data = render(warehouse, "astral-uv-week")
        first = data.rows[0]
        # Identity
        assert first.item_id
        assert first.title
        assert first.source in ("github", "jira")
        # Lifecycle — both endpoints. Start date is the column the
        # table will surface alongside the completion date.
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", first.created_at)
        assert first.created_at_display
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", first.completed_at)
        assert first.completed_at_display
        assert isinstance(first.cycle_time_days, float)
        # Optional: source URL for "open on GitHub/Jira"
        # (None acceptable; field present)
        assert first.url is None or first.url.startswith("http")

    def test_created_at_display_is_utc_anchored(self, warehouse):
        """Same TZ-safety contract as completed_at: the Started
        column must show the same UTC date regardless of viewer
        timezone."""
        data = render(warehouse, "astral-uv-week")
        for r in data.rows:
            assert re.match(
                r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}$", r.created_at_display
            ), (
                f"created_at_display must be '%b %d, %Y'; got "
                f"{r.created_at_display!r} on item {r.item_id!r}"
            )

    def test_created_at_is_never_after_completed_at(self, warehouse):
        """Internal-data invariant: start ≤ end. Catches a class of
        source-data corruption where the events table delivers a
        completion before the creation."""
        data = render(warehouse, "astral-uv-week")
        for r in data.rows:
            assert r.created_at <= r.completed_at, (
                f"item {r.item_id!r}: created_at {r.created_at!r} "
                f"is after completed_at {r.completed_at!r}"
            )

    def test_completed_on_filter_narrows_to_one_calendar_date(self, warehouse):
        """The throughput chart's click-handler will pass a single
        UTC date here to filter the table to items that completed on
        that exact date. The fixture's May 04 has 19 completions —
        passing completed_on='2026-05-04' must return exactly those."""
        data = render(
            warehouse, "astral-uv-week", completed_on="2026-05-04"
        )
        assert data.count == 19, (
            f"completed_on='2026-05-04' should return the 19 items "
            f"completed on May 4; got {data.count}"
        )
        for r in data.rows:
            assert r.completed_at == "2026-05-04", (
                f"every row's completed_at must match the filter; "
                f"got {r.completed_at!r} for {r.item_id!r}"
            )

    def test_completed_on_filter_with_no_matches_returns_zero_rows(
        self, warehouse
    ):
        data = render(
            warehouse, "astral-uv-week", completed_on="2099-12-31"
        )
        assert data.rows == ()
        assert data.count == 0

    def test_view_window_filters_table_to_completed_in_range(
        self, warehouse
    ):
        """The detail-page table must respect the same view
        window the chart does — otherwise the table shows rows
        the chart's date range excludes (confusing: "no data
        points but a full table"). `view` clamps completed_at
        to the inclusive [from_, to] range."""
        from datetime import date
        from flowmetrics.windows import Window
        # Fixture completions span May 4-10, 2026. A window of
        # just May 4 should match the 19 May-4 completions.
        data = render(
            warehouse, "astral-uv-week",
            view=Window(from_=date(2026, 5, 4), to=date(2026, 5, 4)),
        )
        assert data.count == 19, (
            f"view window May 4-4 should match the 19 May-4 "
            f"completions; got {data.count}"
        )
        # A window entirely outside the data → empty table.
        empty = render(
            warehouse, "astral-uv-week",
            view=Window(from_=date(2099, 1, 1), to=date(2099, 1, 31)),
        )
        assert empty.count == 0, (
            f"view window outside the data range must yield an "
            f"empty table; got {empty.count}"
        )

    def test_completed_on_filter_combines_with_q_filter(self, warehouse):
        """Filters compose: title contains 'q' AND completion date
        equals X. Used when a viewer drills into a specific day and
        then types a substring in the search box."""
        # Find a (date, substring) pair from the fixture that
        # narrows to ≥1 row but ≠ the full set.
        all_may4 = render(warehouse, "astral-uv-week", completed_on="2026-05-04")
        if not all_may4.rows:
            return  # defensive — fixture changed
        sample_title = all_may4.rows[0].title
        if len(sample_title) < 4:
            return  # defensive — pick a longer needle
        needle = sample_title[:4].lower()
        filtered = render(
            warehouse,
            "astral-uv-week",
            q=needle,
            completed_on="2026-05-04",
        )
        for r in filtered.rows:
            assert r.completed_at == "2026-05-04"
            assert needle in r.title.lower()

    def test_in_flight_at_filter_populates_age_days_field(self, warehouse):
        """When the table is scoped to in-flight items at an as-of
        date, each row carries `age_days = (asof - created_at) + 1`
        (Vacanti's CD - SD + 1 formula — same `+1` inclusive rule
        cycle time uses; a same-day item is 1d not 0d)."""
        data = render(
            warehouse, "astral-uv-week", in_flight_at="2026-05-06"
        )
        if not data.rows:
            return
        for r in data.rows:
            assert r.age_days is not None, (
                f"in_flight_at filter must populate age_days; got "
                f"None for {r.item_id!r}"
            )
            # Minimum legal value is 1d per Vacanti.
            assert r.age_days >= 1
            # Cross-check: re-compute from the row's created_at.
            from datetime import date as _d
            asof_d = _d.fromisoformat("2026-05-06")
            created_d = _d.fromisoformat(r.created_at)
            assert r.age_days == (asof_d - created_d).days + 1

    def test_age_days_is_none_when_in_flight_at_not_set(self, warehouse):
        """The Age column is meaningless without an asof context.
        Routes that aren't aging-scoped (cycle-time, throughput
        detail) leave the field None and the template doesn't
        render the column."""
        data = render(warehouse, "astral-uv-week")
        for r in data.rows:
            assert r.age_days is None

    def test_sort_by_created_at_orders_by_start_date(self, warehouse):
        """The new column needs a sort affordance. Pass
        sort='created_at' and the rows come back ordered by start
        date — defaults to descending (most-recently-started first)
        like the other date column."""
        data = render(warehouse, "astral-uv-week", sort="created_at")
        starts = [r.created_at for r in data.rows]
        assert starts == sorted(starts, reverse=True), (
            f"sort=created_at must order rows by start date desc; "
            f"got {starts}"
        )

    def test_completed_at_display_is_utc_anchored(self, warehouse):
        """Same TZ-safety contract as the cycle-time chart: the
        date the table shows must not shift by browser TZ. Display
        string comes from `flowmetrics.utc_dates`."""
        data = render(warehouse, "astral-uv-week")
        for r in data.rows:
            assert re.match(
                r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}$", r.completed_at_display
            ), (
                f"completed_at_display must be the UTC display form "
                f"'%b %d, %Y'; got {r.completed_at_display!r} on "
                f"item {r.item_id!r}"
            )

    def test_rows_default_ordered_by_completed_at_desc(self, warehouse):
        """Most-recent first is the natural default — users
        scanning the table want yesterday's work first."""
        data = render(warehouse, "astral-uv-week")
        dates = [r.completed_at for r in data.rows]
        assert dates == sorted(dates, reverse=True), (
            "table default sort must be completed_at descending"
        )
