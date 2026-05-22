"""Component tests for `flowmetrics.web.components.cycle_time`.

The render function reads work_items Parquet via DuckDB and returns a
typed CycleTimeData payload. These tests assert the contract: data
shape the chart relies on, and — critically — that the chart's
positions match the chart's tooltips.

Slice 2 + bug found in browser:
  - A PR completed at "2026-05-06T18:30:00" was being plotted between
    the May 07 and May 08 tick labels, while the tooltip rounded the
    timestamp to "May 06, 2026". The chart misrepresented the data.

The fix is to make `completed_at` in the Vega-Lite data a *date*
(YYYY-MM-DD), not a datetime. Same date in the tooltip => same
position on the x-axis. These tests pin that contract.
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.web.components.cycle_time import render

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
    """Materialise the pinned fixture data into a tmp warehouse, then
    open a DuckDB connection registered against it.

    Mirrors the production runtime path: `flow materialise` writes
    Parquet, app.py reads via `read_parquet(... hive_partitioning=true)`.
    """
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


class TestCycleTimeRenderDataShape:
    def test_returns_one_point_per_completed_pr(self, warehouse):
        data = render(warehouse, "astral-uv-week")
        # The pinned fixture has 43 merged PRs in the window.
        assert data.item_count == 43
        assert len(data.points) == 43

    def test_empty_window_with_data_elsewhere_says_widen_not_no_data(
        self, warehouse
    ):
        """A view window outside the warehouse's data range is a
        FILTER artefact — the data exists, just not here. The
        headline must say so ("warehouse covers X – Y, widen the
        view"), NOT "no completed items" (which reads as a
        materialise gap and sends the operator looking for the
        wrong fix)."""
        from datetime import date
        from flowmetrics.windows import Window
        # Fixture data is May 2026; this window is years later.
        data = render(
            warehouse, "astral-uv-week",
            view=Window(from_=date(2030, 1, 1), to=date(2030, 1, 31)),
        )
        assert data.item_count == 0
        assert "warehouse covers" in data.headline.lower(), (
            f"empty-window headline must name the covered range; "
            f"got {data.headline!r}"
        )
        assert "widen" in data.headline.lower()
        # Must NOT imply the warehouse is empty.
        assert "no data materialised" not in data.headline.lower()

    def test_truly_empty_warehouse_says_materialise(self):
        """An empty warehouse (nothing pulled yet) gets the
        'run flow materialise' message — distinct from the
        window-too-narrow case above."""
        import duckdb
        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE
            )"""
        )
        data = render(con, "empty-contract")
        assert data.item_count == 0
        assert "materialise" in data.headline.lower(), (
            f"empty-warehouse headline must point at materialise; "
            f"got {data.headline!r}"
        )

    def test_percentiles_are_populated_and_p50_lt_p85(self, warehouse):
        data = render(warehouse, "astral-uv-week")
        assert data.p50 >= 0
        assert data.p50 <= data.p85, "P50 must be ≤ P85"

    def test_p95_is_populated_and_at_least_p85(self, warehouse):
        """P95 is Vacanti's high-stakes commitment threshold; the
        chart shows it alongside P50 and P85. Must be ≥ P85 by
        definition (a higher percentile of the same distribution)."""
        data = render(warehouse, "astral-uv-week")
        assert data.p95 is not None
        assert data.p85 <= data.p95, "P85 must be ≤ P95"

    def test_headline_names_p95_alongside_p50_and_p85(self, warehouse):
        data = render(warehouse, "astral-uv-week")
        assert "P95" in data.headline, (
            f"headline must include P95; got {data.headline!r}"
        )

    def test_chart_renders_p95_reference_line_with_value(self, warehouse):
        """Spec-level: the rule + text reference layers include a
        P95 entry (in addition to P50 and P85)."""
        data = render(warehouse, "astral-uv-week")
        spec = json.loads(data.vega_spec_json())
        # The reference layers (rule + text) share the same 2- or
        # 3-row dataset. After this fix the dataset has three rows
        # for P50, P85, P95.
        # Layer 1 is scatter; layer 2 is rule; layer 3 is text.
        rule_layer = spec["layer"][1]
        ref_rows = rule_layer["data"]["values"]
        pcts = {row["pct"] for row in ref_rows}
        assert pcts == {"P50", "P85", "P95"}, (
            f"reference layers must carry P50, P85, P95; got {pcts}"
        )
        # The label for the new P95 row must include the numeric
        # value so the user can read it off the chart without a
        # legend.
        p95_row = next(r for r in ref_rows if r["pct"] == "P95")
        assert f"{data.p95:.1f}" in p95_row["label"], (
            f"P95 reference label must include the numeric value; "
            f"got {p95_row['label']!r}"
        )


class TestCycleTimeChartHonestyContract:
    """Bug found in browser: chart positions points by datetime but
    labels them by date, so a same-date tooltip + a between-ticks dot
    misrepresent each other.

    Decision: the chart's unit is the calendar date (UTC). Both the
    tooltip's "Completed" value AND the dot's x-position derive from
    the same date string. Multiple PRs completing on the same date
    stack at the same x — that's honest; visual overlap reads as
    "more than one item completed on this day."
    """

    DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

    def test_completed_at_on_every_point_is_a_calendar_date(self, warehouse):
        """For every CycleTimePoint, `completed_at` is YYYY-MM-DD —
        no time component. This pins the bug fix: the position the
        chart uses must match the date the tooltip shows."""
        data = render(warehouse, "astral-uv-week")
        assert data.points  # sanity
        for p in data.points:
            assert self.DATE_ONLY.match(p.completed_at), (
                f"completed_at must be date-only (YYYY-MM-DD); got "
                f"{p.completed_at!r} on item {p.item_id!r}"
            )

    def test_completed_at_in_vega_spec_data_is_date_only(self, warehouse):
        """Same contract, asserted at the Vega-Lite spec level: the
        data array the chart actually plots has date-only strings.
        Belt-and-braces against a downstream serializer adding time
        back in."""
        data = render(warehouse, "astral-uv-week")
        spec = json.loads(data.vega_spec_json())
        # Locate the scatter layer's data values (first layer carries
        # them; reference layers have their own 2-row data).
        scatter_data = spec["layer"][0]["data"]["values"]
        assert scatter_data, "scatter layer is missing point data"
        for row in scatter_data:
            assert self.DATE_ONLY.match(row["completed_at"]), (
                f"completed_at in Vega data must be date-only; got "
                f"{row['completed_at']!r}"
            )

    def test_same_day_prs_share_completed_at_string(self, warehouse):
        """Honest stacking: two PRs that completed on the same
        calendar date must produce identical `completed_at` strings,
        so they sit at the same x position. The chart isn't claiming
        a finer-grained timing it cannot honestly draw."""
        data = render(warehouse, "astral-uv-week")
        completed_dates = [p.completed_at for p in data.points]
        # The pinned fixture's 43 PRs span ~7 days; some same-day
        # collisions are guaranteed.
        unique_dates = set(completed_dates)
        assert len(unique_dates) < len(completed_dates), (
            "expected at least one date with multiple completed PRs "
            "to exercise the stacking contract; got all-unique dates"
        )

    def test_completed_at_is_a_parseable_date(self, warehouse):
        """Any string that passes the regex must also parse via
        datetime.fromisoformat — guarding against e.g.
        '2026-13-99' regex matches that are not real dates."""
        data = render(warehouse, "astral-uv-week")
        for p in data.points:
            datetime.fromisoformat(p.completed_at + "T00:00:00+00:00").replace(
                tzinfo=UTC
            )


class TestCycleTimeChartIntraDayJitter:
    """Bug-spec: when two PRs complete on the same calendar date, they
    visually stack at the same x position, which makes "five items on
    May 04" indistinguishable from "one item on May 04."

    Decision: each dot gets a random x offset within its date band.
    A dot whose completed_at is May 06 must render somewhere in
    [May 06, May 07) — never drifting past the May 07 tick. Vega-Lite
    handles this with a `calculate` transform using `random()`,
    keeping the data layer date-only (the truth) and the
    visualization layer jittered (the readability hint).
    """

    def test_spec_has_transform_jittering_x_within_one_day(self, warehouse):
        """The scatter layer's spec must include a transform that
        builds a new field by adding a random offset (within one
        day in ms) to the parsed completed_at timestamp. Asserted
        at the spec level so we don't depend on rendering
        randomness in the unit test."""
        data = render(warehouse, "astral-uv-week")
        spec = json.loads(data.vega_spec_json())
        scatter = spec["layer"][0]
        transforms = scatter.get("transform", [])
        # Look for a transform that creates a jittered field using
        # random() multiplied by one day in ms.
        ms_per_day = 86_400_000
        jittered_calcs = [
            t for t in transforms
            if "calculate" in t
            and "random()" in t["calculate"]
            and str(ms_per_day) in t["calculate"]
        ]
        assert jittered_calcs, (
            "scatter layer must have a `calculate` transform using "
            "`random()` * 86_400_000 ms to spread points within their "
            f"day band; found transforms: {transforms}"
        )

    def test_jitter_is_forward_into_the_date_column(self, warehouse):
        """User's column convention: a dot for "May DD" lives in
        [May DD tick, May DD+1 tick). The jitter must therefore be
        a FORWARD offset in [0, +1 day) — `random() * 86400000` —
        not centered, because centered (random() - 0.5) lets dots
        slip LEFT of their own tick into the previous date's
        column.

        Trade-off vs the earlier "off by one" perception: a
        high-jitter May DD dot can render visually close to the
        May DD+1 tick. The fix to THAT problem is making the
        tooltip's Completed value TZ-invariant + matching the
        column — both addressed separately. The jitter direction
        is determined by the column convention.
        """
        data = render(warehouse, "astral-uv-week")
        spec = json.loads(data.vega_spec_json())
        scatter = spec["layer"][0]
        transforms = scatter.get("transform", [])
        calc = next(
            (t for t in transforms
             if "calculate" in t and "random()" in t["calculate"]),
            None,
        )
        assert calc is not None
        expr = calc["calculate"].replace(" ", "")
        # Must use forward `random() * 86400000`; must NOT subtract
        # half a day or use the centered (random() - 0.5) form.
        assert "random()*86400000" in expr, (
            f"jitter must add `random() * 86400000` (forward) to the "
            f"tick timestamp; got {calc['calculate']!r}"
        )
        assert "(random()-0.5)" not in expr, (
            f"jitter must NOT use centered (random() - 0.5) — that "
            f"puts dots LEFT of their own tick. Got {calc['calculate']!r}"
        )
        assert "-43200000" not in expr, (
            f"jitter must NOT subtract half a day; that would centre it. "
            f"Got {calc['calculate']!r}"
        )

    def test_tooltip_completed_field_is_python_preformatted_nominal(
        self, warehouse
    ):
        """Bug-spec: when a tooltip's date field is `type: temporal`
        with a format string, Vega-Lite renders the value in
        browser-local time. A PR completed at UTC May 04 then shows
        "May 03" in a PT (UTC-7) browser. Different viewers see
        different dates for the same data — broken.

        Fix: pre-format the date string in Python (UTC), pass the
        already-formatted string as `type: nominal` to the tooltip.
        Vega renders the literal string regardless of TZ.
        """
        data = render(warehouse, "astral-uv-week")
        spec = json.loads(data.vega_spec_json())
        scatter = spec["layer"][0]
        tooltip = scatter.get("encoding", {}).get("tooltip", [])
        completed = next(
            (t for t in tooltip if t.get("title", "").startswith("Completed")),
            None,
        )
        assert completed is not None, (
            "tooltip must include a `Completed` row; got "
            f"{tooltip!r}"
        )
        assert completed.get("type") != "temporal", (
            "tooltip's Completed field must NOT be `type: temporal` — "
            "that triggers browser-local-time formatting. Use a "
            "Python-preformatted nominal field."
        )
        # And the field referenced must not be the raw date string
        # (which would get coerced anyway under default inference);
        # it must be a separate display field carrying the
        # already-formatted "%b %d, %Y" string from Python.
        assert completed.get("field") != "completed_at", (
            "tooltip should reference a separate display field "
            "(e.g. `completed_at_display`), not the raw date `completed_at`."
        )

    def test_x_axis_title_makes_utc_explicit(self, warehouse):
        """The reader of the chart must not have to guess whether
        the dates on the x-axis are UTC or their local TZ. The
        axis title says so explicitly: 'Completion date (UTC)'.

        Without this annotation, a user in PT looking at a "May 04"
        column might assume the dot represents work that closed at
        a May 04 *local* moment, when in fact it was May 04 UTC
        (which could be May 03 evening locally). The annotation
        is the visual companion to the tooltip's UTC display value.
        """
        data = render(warehouse, "astral-uv-week")
        spec = json.loads(data.vega_spec_json())
        scatter = spec["layer"][0]
        x_title = (
            scatter.get("encoding", {})
            .get("x", {})
            .get("axis", {})
            .get("title", "")
        )
        assert "UTC" in x_title, (
            f"x-axis title must annotate the timezone (UTC) so the "
            f"reader doesn't assume browser-local. Got {x_title!r}"
        )

    def test_each_point_has_a_preformatted_display_date(self, warehouse):
        """The CycleTimePoint dataclass must expose a field carrying
        the already-formatted "%b %d, %Y" display string so the
        tooltip can render it without Vega-Lite's TZ-aware
        formatter."""
        data = render(warehouse, "astral-uv-week")
        assert data.points
        for p in data.points:
            display = getattr(p, "completed_at_display", None)
            assert display, (
                f"CycleTimePoint must have a completed_at_display "
                f"field; got item {p.item_id!r} with attributes "
                f"{vars(p)}"
            )
            # Format "%b %d, %Y" — e.g. "May 04, 2026".
            import re

            assert re.match(
                r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}$", display
            ), (
                f"completed_at_display must be Python-pre-formatted "
                f"%b %d, %Y (e.g. 'May 04, 2026'); got {display!r}"
            )

    def test_scatter_layer_x_encoding_uses_jittered_field(self, warehouse):
        """The chart must plot dots by the jittered field, not the
        raw date — otherwise the transform exists but the chart
        ignores it."""
        data = render(warehouse, "astral-uv-week")
        spec = json.loads(data.vega_spec_json())
        scatter = spec["layer"][0]
        x_enc = scatter.get("encoding", {}).get("x", {})
        assert x_enc, "scatter layer must define an x encoding"
        # Find the field produced by the jitter transform.
        transforms = scatter.get("transform", [])
        jittered_field = next(
            (t.get("as") for t in transforms
             if "calculate" in t and "random()" in t["calculate"]),
            None,
        )
        assert jittered_field, "no jittered output field found"
        assert x_enc.get("field") == jittered_field, (
            f"scatter x encoding must use jittered field "
            f"{jittered_field!r}; got {x_enc.get('field')!r}"
        )

    def test_x_scale_domain_pads_one_day_each_side(self, warehouse):
        """Bug-spec: dots at the first and last dates of the data
        sit at x=0 and x=plot_width, so they render half-clipped at
        the chart edges. Fix: the x-scale domain is the data range
        padded by one day on each side. A May 04 dot sits one
        day-width in from the left axis; a May 10 dot sits one
        day-width in from the right.

        Asserted at the spec level so the test is fast and
        deterministic (no random-jitter dependency).
        """
        from datetime import date, timedelta

        data = render(warehouse, "astral-uv-week")
        spec = json.loads(data.vega_spec_json())
        scatter = spec["layer"][0]
        x_scale = scatter.get("encoding", {}).get("x", {}).get("scale", {})
        domain = x_scale.get("domain")
        assert domain and len(domain) == 2, (
            f"x scale must declare a 2-element domain; got {domain!r}"
        )
        # Pull the data's actual date range to compute the
        # expected padded domain.
        dates = sorted({p.completed_at for p in data.points})
        first = date.fromisoformat(dates[0])
        last = date.fromisoformat(dates[-1])
        expected_start = (first - timedelta(days=1)).isoformat()
        expected_end = (last + timedelta(days=1)).isoformat()
        assert domain[0] == expected_start, (
            f"x domain start must be one day before earliest data date "
            f"({expected_start}); got {domain[0]!r}"
        )
        assert domain[1] == expected_end, (
            f"x domain end must be one day after latest data date "
            f"({expected_end}); got {domain[1]!r}"
        )

    def test_tooltip_does_not_read_the_jittered_timestamp(self, warehouse):
        """Honesty contract: the tooltip's "Completed" value must
        NOT come from the jittered timestamp — otherwise hovering a
        May 04 dot could show "May 04, 1:23 AM" when the truth is
        just "May 04." Tooltip must read a date-derived field
        (either the raw `completed_at` or the Python-preformatted
        `completed_at_display`).
        """
        data = render(warehouse, "astral-uv-week")
        spec = json.loads(data.vega_spec_json())
        scatter = spec["layer"][0]
        tooltip = scatter.get("encoding", {}).get("tooltip", [])
        completed_field = next(
            (t for t in tooltip if "Completed" in t.get("title", "")),
            None,
        )
        assert completed_field is not None, "tooltip missing 'Completed' row"
        assert completed_field["field"] in {"completed_at", "completed_at_display"}, (
            f"tooltip 'Completed' must read a date-derived field, "
            f"not the jittered output. Got {completed_field['field']!r}"
        )
        assert "jittered" not in completed_field["field"], (
            f"tooltip must NOT read the jittered timestamp field "
            f"(it leaks sub-day randomness into the displayed value). "
            f"Got {completed_field['field']!r}"
        )


class TestCycleTimeCapSlider:
    """The y-axis cap: a range slider that clips the y domain so a
    few slow outliers don't squash the readable bulk. It runs from
    ~P95 up to the max cycle time; the percentile VALUES are
    untouched (computed server-side from the full data)."""

    def test_chart_has_a_y_cap_range_control(self, warehouse):
        data = render(warehouse, "astral-uv-week")
        spec = json.loads(data.vega_spec_json())
        scatter = next(
            lyr for lyr in spec["layer"]
            if (lyr["mark"].get("type")
                if isinstance(lyr["mark"], dict) else lyr["mark"]) == "point"
        )
        # The cap is a TOP-LEVEL value param — a layer-scoped param
        # is out of scope for the shared y scale's domainMax expr.
        caps = [
            p for p in spec.get("params", [])
            if isinstance(p.get("bind"), dict)
            and p["bind"].get("input") == "range"
        ]
        assert caps, "expected a top-level range-input y-cap param"
        cap = caps[0]
        # Slider runs from ~P95 (rounded up) to the max observation.
        assert cap["bind"]["min"] >= data.p95 - 0.5, (
            f"cap slider should start near P95 ({data.p95}); "
            f"min={cap['bind']['min']}"
        )
        assert cap["bind"]["max"] >= cap["bind"]["min"]
        # Default = max → opens showing all data; drag down to crop.
        assert cap["value"] == cap["bind"]["max"]
        # The scatter layer FILTERS dots by the cap (so the y-axis
        # auto-scales to what's shown) — it does NOT pin the domain.
        filters = [t.get("filter") for t in scatter.get("transform", [])]
        assert any(cap["name"] in str(f) for f in filters), (
            f"scatter transform must filter dots by {cap['name']!r}; "
            f"got {filters}"
        )

    def test_zoom_no_longer_binds_the_y_axis(self, warehouse):
        """The cap slider owns the y axis, so the wheel/drag zoom
        is bound to x only — binding y too would fight the cap."""
        data = render(warehouse, "astral-uv-week")
        spec = json.loads(data.vega_spec_json())
        scatter = next(
            lyr for lyr in spec["layer"]
            if (lyr["mark"].get("type")
                if isinstance(lyr["mark"], dict) else lyr["mark"]) == "point"
        )
        zoom = next(
            p for p in scatter["params"]
            if p.get("bind") == "scales"
        )
        assert zoom["select"]["encodings"] == ["x"]


class TestCycleTimeGridlineDensity:
    """The x-axis tick/gridline interval scales with the window
    span. A multi-month view must not draw a gridline every single
    day — that hatches the plot into an unreadable grey wash.
    Short windows keep daily ticks so Vega doesn't auto-pick a
    sub-day granularity that repeats the same '%b %d' label."""

    def _warehouse_spanning(self, days: int):
        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE
            )"""
        )
        base = datetime(2025, 1, 1)
        rows = [
            ("c", "github", f"#{n}", f"item {n}", None,
             base + timedelta(days=n - 3), base + timedelta(days=n), 3.0)
            for n in range(days + 1)
        ]
        con.executemany(
            "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?)", rows
        )
        return con

    def _x_tick_count(self, con):
        data = render(con, "c")
        spec = json.loads(data.vega_spec_json())
        scatter = next(
            lyr for lyr in spec["layer"]
            if (lyr["mark"].get("type")
                if isinstance(lyr["mark"], dict) else lyr["mark"]) == "point"
        )
        return scatter["encoding"]["x"]["axis"]["tickCount"]

    def test_short_window_keeps_daily_ticks(self):
        """A ≤ 1-month window keeps daily ticks — needed so Vega
        doesn't auto-pick a sub-day granularity that repeats the
        same '%b %d' label four times."""
        tc = self._x_tick_count(self._warehouse_spanning(20))
        assert tc == {"interval": "day", "step": 1}

    def test_quarter_window_steps_up_to_weekly_ticks(self):
        """A ~90-day span is too wide for daily gridlines (~90
        lines) — it steps up to a weekly interval."""
        tc = self._x_tick_count(self._warehouse_spanning(90))
        assert tc == {"interval": "week", "step": 1}

    def test_multi_month_window_steps_up_to_monthly_ticks(self):
        """A 14-month span must NOT use daily ticks — that draws
        ~400 gridlines (the reported bug). It steps up to a
        monthly interval, ~14 sane gridlines."""
        tc = self._x_tick_count(self._warehouse_spanning(420))
        assert tc == {"interval": "month", "step": 1}
