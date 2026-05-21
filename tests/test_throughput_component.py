"""Component tests for `flowmetrics.web.components.throughput`.

Throughput per Vacanti (Actionable Agile Metrics for Predictability,
10th Anniversary Edition, pp. 61–63): enumerate every calendar
date from the earliest to the latest completion, then count items
that finished on each exact date. Days with zero completions are
included as zeros — they are real observations of "slow days"
that downstream Monte Carlo sampling needs to represent capacity
honestly.

The component mirrors cycle_time's shape: a render function reads
from DuckDB and returns a typed payload; a Jinja partial renders
the bar chart. Same TZ-safety contract (dates pre-formatted in
Python so Vega-Lite can't shift them by browser-local TZ).
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.web.components.throughput import render

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
            "--contracts-dir",
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


class TestThroughputShape:
    def test_one_row_per_enumerated_date(self, warehouse):
        """Vacanti: enumerate every calendar date from first to last
        completion. With the fixture's 7-day window the daily series
        should cover every date between the earliest and latest
        completion (inclusive), no gaps."""
        data = render(warehouse, "astral-uv-week")
        # Dates must be sorted ascending.
        dates = [d.date_iso for d in data.daily]
        assert dates == sorted(dates), (
            f"daily series must be sorted ascending; got {dates}"
        )
        # Consecutive — no missing days.
        from datetime import date

        parsed = [date.fromisoformat(d) for d in dates]
        for prev, cur in zip(parsed, parsed[1:]):
            assert (cur - prev).days == 1, (
                f"daily series has a gap between {prev} and {cur}; "
                f"every enumerated date must be present"
            )

    def test_zero_count_days_are_included(self):
        """A core Vacanti requirement: a day with zero completions
        appears as a zero. The fixture's 7-day window happens to
        have completions on every date, so this test builds a
        synthetic warehouse with a deliberate gap (May 04 and May
        07 have completions; May 05 and May 06 don't) and asserts
        the daily series includes those interior zeros."""
        from datetime import datetime, UTC

        con = duckdb.connect(":memory:")
        # Build a tiny work_items view in-memory with three items
        # spanning a 4-day range, with a gap in the middle.
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR,
                source VARCHAR,
                item_id VARCHAR,
                title VARCHAR,
                url VARCHAR,
                created_at TIMESTAMP,
                completed_at TIMESTAMP,
                cycle_time_days DOUBLE
            )"""
        )
        rows = [
            ("c", "github", "#1", "t", None,
             datetime(2026, 5, 4, 9, 0), datetime(2026, 5, 4, 12, 0), 1.0),
            ("c", "github", "#2", "t", None,
             datetime(2026, 5, 4, 10, 0), datetime(2026, 5, 4, 14, 0), 1.0),
            ("c", "github", "#3", "t", None,
             datetime(2026, 5, 7, 9, 0), datetime(2026, 5, 7, 15, 0), 1.0),
        ]
        con.executemany(
            "INSERT INTO work_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

        data = render(con, "c")
        date_to_count = {d.date_iso: d.count for d in data.daily}
        assert date_to_count == {
            "2026-05-04": 2,
            "2026-05-05": 0,  # the gap
            "2026-05-06": 0,  # the gap
            "2026-05-07": 1,
        }, (
            f"interior zero-count days must be included in the "
            f"daily series; got {date_to_count}"
        )

    def test_count_matches_items_completed_on_that_date(self, warehouse):
        """Sample check: pick the date with the most completions
        and confirm it matches a direct SQL query."""
        data = render(warehouse, "astral-uv-week")
        # Top day from the component.
        top_day = max(data.daily, key=lambda d: d.count)
        # Same query, computed independently from the warehouse.
        sql = (
            "SELECT count(*) FROM work_items "
            "WHERE contract_id = 'astral-uv-week' "
            "  AND completed_at IS NOT NULL "
            "  AND CAST(completed_at AS DATE) = CAST(? AS DATE)"
        )
        actual = warehouse.execute(sql, [top_day.date_iso]).fetchone()[0]
        assert top_day.count == actual, (
            f"throughput count for {top_day.date_iso} disagrees with "
            f"a direct warehouse query: component says {top_day.count}, "
            f"SQL says {actual}"
        )

    def test_total_count_matches_completed_items(self, warehouse):
        """Sum of daily counts == count of completed items in the
        contract (no double-counting, no drop)."""
        data = render(warehouse, "astral-uv-week")
        total_from_daily = sum(d.count for d in data.daily)
        total_completed = warehouse.execute(
            "SELECT count(*) FROM work_items "
            "WHERE contract_id = 'astral-uv-week' "
            "  AND completed_at IS NOT NULL"
        ).fetchone()[0]
        assert total_from_daily == total_completed

    def test_date_display_is_utc_anchored(self, warehouse):
        """Same TZ-safety contract as the cycle-time chart: every
        rendered date string is the UTC display form."""
        data = render(warehouse, "astral-uv-week")
        for d in data.daily:
            assert re.match(
                r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}$", d.date_display
            ), (
                f"date_display must be '%b %d, %Y'; got "
                f"{d.date_display!r}"
            )
            assert re.match(r"^\d{4}-\d{2}-\d{2}$", d.date_iso), (
                f"date_iso must be YYYY-MM-DD; got {d.date_iso!r}"
            )

    def test_headline_summarizes_total_and_window(self, warehouse):
        """The headline is the at-a-glance summary above the chart:
        total items + window length + items/day average."""
        data = render(warehouse, "astral-uv-week")
        assert "43" in data.headline, (
            f"headline must mention the total item count; "
            f"got {data.headline!r}"
        )
        # Mentions "day" somewhere (per day, day average, etc.)
        assert "day" in data.headline.lower(), (
            f"headline should describe the rate per day; "
            f"got {data.headline!r}"
        )

    def test_empty_warehouse_renders_no_daily_no_crash(self, warehouse):
        """An unknown contract returns empty daily + a defensive
        headline. The component should not raise."""
        data = render(warehouse, "does-not-exist")
        assert data.daily == ()
        assert data.headline  # non-empty defensive string

    def test_chart_spec_uses_bar_mark(self, warehouse):
        """The throughput chart is a bar chart, one bar per day."""
        data = render(warehouse, "astral-uv-week")
        spec = json.loads(data.vega_spec_json())
        # Find any layer with a bar mark — Vega-Lite spec may be
        # single-mark or layered. Bars are the canonical throughput
        # visualization (one bar = one day's count).
        marks_seen = []

        def _collect(node):
            if isinstance(node, dict):
                m = node.get("mark")
                if isinstance(m, str):
                    marks_seen.append(m)
                elif isinstance(m, dict) and "type" in m:
                    marks_seen.append(m["type"])
                for v in node.values():
                    _collect(v)
            elif isinstance(node, list):
                for v in node:
                    _collect(v)

        _collect(spec)
        assert "bar" in marks_seen, (
            f"throughput chart must include a bar mark; marks "
            f"seen: {marks_seen}"
        )

    def test_data_coverage_distinguishes_missing_from_zero(self):
        """`data_coverage` tags each day as `warehouse` (inside
        the materialise window — a 0 is a true zero) or
        `missing` (outside — no data, backfill-able). The
        chart renders these visually distinctly so operators
        don't mistake a gap for a quiet day."""
        from datetime import datetime, date

        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE
            )"""
        )
        # One completion on May 5 inside the warehouse window
        # (May 4-6). View is May 2-8: May 2/3 = missing (before
        # warehouse), May 4-6 = warehouse, May 7/8 = missing.
        con.execute(
            "INSERT INTO work_items VALUES "
            "('c', 'github', '#1', 't', NULL, "
            "'2026-05-05 09:00', '2026-05-05 12:00', 0.13)"
        )
        from flowmetrics.windows import Window
        data = render(
            con, "c",
            view=Window(from_=date(2026, 5, 2), to=date(2026, 5, 8)),
            warehouse_start=date(2026, 5, 4),
            warehouse_stop=date(2026, 5, 6),
        )
        by_iso = {d.date_iso: d.data_coverage for d in data.daily}
        assert by_iso == {
            "2026-05-02": "missing",
            "2026-05-03": "missing",
            "2026-05-04": "warehouse",
            "2026-05-05": "warehouse",
            "2026-05-06": "warehouse",
            "2026-05-07": "missing",
            "2026-05-08": "missing",
        }, f"coverage classification wrong; got {by_iso}"

    def test_headline_average_divides_by_covered_days_only(self):
        """The /day rate must divide by days the warehouse
        actually covers — NOT the full view span. A missing day
        is unobserved, not a zero-completion day; averaging it
        in understates the rate. View May 2-8 (7 days) over a
        warehouse window of May 4-6 (3 days): 1 completion ÷ 3
        covered days = 0.3/day, not ÷ 7."""
        from datetime import datetime, date

        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE
            )"""
        )
        con.execute(
            "INSERT INTO work_items VALUES "
            "('c', 'github', '#1', 't', NULL, "
            "'2026-05-05 09:00', '2026-05-05 12:00', 0.13)"
        )
        from flowmetrics.windows import Window
        data = render(
            con, "c",
            view=Window(from_=date(2026, 5, 2), to=date(2026, 5, 8)),
            warehouse_start=date(2026, 5, 4),
            warehouse_stop=date(2026, 5, 6),
        )
        # 1 item ÷ 3 covered days = 0.3/day. Headline names both
        # the covered-day count and the window span.
        assert "0.3/day" in data.headline, (
            f"average should divide by 3 covered days; got "
            f"{data.headline!r}"
        )
        assert "3 days with data" in data.headline
        assert "7-day window" in data.headline

    def test_each_row_flags_day_type_correctly(self):
        """Saturday + Sunday in UTC are weekend; Mon–Fri are
        weekday. `day_type` is a string-typed classification
        with room for future values (`holiday`).
        2026-05-02 = Sat, 2026-05-03 = Sun, 2026-05-04 = Mon."""
        from datetime import datetime

        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE
            )"""
        )
        # One completion per day from Sat 2026-05-02 through Mon 2026-05-04.
        rows = [
            ("c", "github", f"#{i}", "t", None,
             datetime(2026, 5, 2 + i, 9, 0),
             datetime(2026, 5, 2 + i, 12, 0),
             1.0)
            for i in range(3)
        ]
        con.executemany(
            "INSERT INTO work_items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        data = render(con, "c")
        by_iso = {d.date_iso: d.day_type for d in data.daily}
        assert by_iso == {
            "2026-05-02": "weekend",
            "2026-05-03": "weekend",
            "2026-05-04": "weekday",
        }, f"day_type classification is wrong; got {by_iso}"

    def test_chart_x_axis_orders_dates_ascending(self, warehouse):
        """User-reported bug: the throughput x-axis was rendering
        weekend dates first (May 09, May 10) then the rest (May 04..
        May 08) because the weekend-shade layer was being scanned
        before the bar layer to build the nominal-axis domain.

        Fix: pin the x-axis ordering explicitly via a sort array
        equal to the ascending date sequence from the data. This
        test asserts the spec carries that explicit order on every
        x encoding (both layers must agree).
        """
        data = render(warehouse, "astral-uv-week")
        expected_order = [d.date_iso for d in data.daily]
        spec = json.loads(data.vega_spec_json())

        # Walk every encoding.x in the spec; each one must carry
        # `sort: <expected_order>` (or `scale.domain: <expected_order>`)
        # so neither layer's data discovery can reorder the axis.
        x_encodings_seen = 0

        def _check_x(node):
            nonlocal x_encodings_seen
            if isinstance(node, dict):
                enc = node.get("encoding")
                if isinstance(enc, dict) and "x" in enc:
                    x = enc["x"]
                    if isinstance(x, dict) and x.get("field") == "date_iso":
                        x_encodings_seen += 1
                        # Either `sort` array OR `scale.domain` array
                        # equal to the expected ascending order.
                        sort_value = x.get("sort")
                        domain = (x.get("scale") or {}).get("domain")
                        pinned = sort_value if isinstance(sort_value, list) else domain
                        assert pinned == expected_order, (
                            f"x encoding must pin ascending date order; "
                            f"expected {expected_order}, got sort={sort_value!r} "
                            f"domain={domain!r}"
                        )
                for v in node.values():
                    _check_x(v)
            elif isinstance(node, list):
                for v in node:
                    _check_x(v)

        _check_x(spec)
        assert x_encodings_seen >= 2, (
            f"expected ≥ 2 x encodings (one per layer); got "
            f"{x_encodings_seen}. Layers must share the pinned axis."
        )

    def test_chart_spec_layers_weekend_shading(self, warehouse):
        """The throughput chart marks weekend columns with a faint
        background rect so weekend-vs-weekday is visually obvious.
        The fixture's window (May 4 Mon → May 10 Sun) contains
        weekend days, so the spec must include the shading layer."""
        data = render(warehouse, "astral-uv-week")
        spec = json.loads(data.vega_spec_json())
        # The spec must be a layered chart now; collect all marks.
        marks: list[str] = []

        def _collect(node):
            if isinstance(node, dict):
                m = node.get("mark")
                if isinstance(m, str):
                    marks.append(m)
                elif isinstance(m, dict) and "type" in m:
                    marks.append(m["type"])
                for v in node.values():
                    _collect(v)
            elif isinstance(node, list):
                for v in node:
                    _collect(v)

        _collect(spec)
        assert "rect" in marks, (
            f"throughput chart must include a rect mark for weekend "
            f"shading; marks seen: {marks}"
        )

    def test_chart_tooltip_date_is_nominal_not_temporal(self, warehouse):
        """TZ-safety regression net: tooltip date fields must be
        pre-formatted strings (type:nominal), not type:temporal.
        Vega-Lite's temporal formatter shifts UTC dates by browser
        TZ — exactly the bug the cycle-time chart was burned by."""
        data = render(warehouse, "astral-uv-week")
        spec_json = data.vega_spec_json()
        spec = json.loads(spec_json)

        offenders: list[dict] = []

        def _walk(node):
            if isinstance(node, dict):
                # If this is an encoding dict carrying tooltip, check it.
                tooltip = node.get("tooltip")
                if tooltip is not None:
                    items = tooltip if isinstance(tooltip, list) else [tooltip]
                    for t in items:
                        if (
                            isinstance(t, dict)
                            and t.get("type") == "temporal"
                            and "date" in str(t.get("field", "")).lower()
                        ):
                            offenders.append(t)
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)

        _walk(spec)
        assert not offenders, (
            f"throughput chart has type:temporal tooltip fields that "
            f"will TZ-shift in the browser. Use type:nominal with "
            f"pre-formatted date strings instead. Offenders: {offenders}"
        )
