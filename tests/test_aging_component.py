"""Component tests for `flowmetrics.web.components.aging`.

Vacanti's Aging Work In Progress chart: in-flight items only,
plotted by current workflow state (x-axis) and elapsed age in
days (y-axis). Percentile lines drawn from completed-item cycle
times serve as checkpoints — once an item ages past the
commitment threshold (P85), it's likely to miss the forecast.

The component takes a required `asof` UTC date. Aging is a "right
now" metric: the caller pins `asof` to the in-flight snapshot date
(the latest materialise), not a scrollable view anchor.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.web.components.aging import render
from flowmetrics.windows import Window

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
    for kind in ("work_items", "transitions"):
        glob = (data_dir / kind / "**" / "*.parquet").as_posix()
        con.execute(
            f"CREATE VIEW {kind} AS "
            f"SELECT * FROM read_parquet('{glob}', hive_partitioning = true)"
        )
    yield con
    con.close()


# A historical asof inside the fixture window — items completed on
# or after this date are "in flight" at this asof, with age =
# (asof - created_at.date()).days. May 06 is mid-week; items
# completed May 07–10 will appear as in-flight here.
_DEMO_ASOF = date(2026, 5, 6)


class TestAgingShape:
    def test_items_completed_after_asof_appear_as_in_flight(self, warehouse):
        """Aging includes items that started ≤ asof but didn't
        complete until after asof — those are the in-flight set
        from the asof's point of view."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        # Cross-check against the warehouse directly.
        in_flight_n = warehouse.execute(
            "SELECT count(*) FROM work_items "
            "WHERE contract_id = 'astral-uv-week' "
            "  AND created_at IS NOT NULL "
            "  AND CAST(created_at AS DATE) <= CAST(? AS DATE) "
            "  AND (completed_at IS NULL "
            "       OR CAST(completed_at AS DATE) > CAST(? AS DATE))",
            [_DEMO_ASOF, _DEMO_ASOF],
        ).fetchone()[0]
        assert in_flight_n > 0, "fixture sanity: should have ≥1 in-flight @ May 06"
        assert data.count == in_flight_n
        assert len(data.items) == in_flight_n

    def test_items_completed_by_asof_are_excluded(self, warehouse):
        """An item with completed_at ≤ asof is NOT in-flight at
        asof. Aging is about open work, not history."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        # Sample check: a known PR completed May 04 must not appear.
        completed_early = warehouse.execute(
            "SELECT item_id FROM work_items "
            "WHERE contract_id = 'astral-uv-week' "
            "  AND CAST(completed_at AS DATE) <= CAST(? AS DATE) "
            "LIMIT 1",
            [_DEMO_ASOF],
        ).fetchone()
        assert completed_early is not None
        ids = {i.item_id for i in data.items}
        assert completed_early[0] not in ids, (
            f"item {completed_early[0]!r} was completed by {_DEMO_ASOF} "
            f"and must not appear in aging"
        )

    def test_age_days_follows_vacanti_formula(self, warehouse):
        """Per Vacanti (Actionable Agile Metrics, 10th Anniversary
        Edition, p. 60): Age = CD - SD + 1. Same `+1` inclusive
        rule as cycle time — a work item created today and aged
        today is 1d, never 0d ("you will never have a work item
        that has an Age of zero days").

        Computed at query/view time, not materialise time, because
        asof is a runtime parameter. Materialise can't pre-compute
        age for an arbitrary future asof."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        for item in data.items:
            created = warehouse.execute(
                "SELECT created_at FROM work_items "
                "WHERE contract_id = 'astral-uv-week' AND item_id = ?",
                [item.item_id],
            ).fetchone()[0]
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            expected = (_DEMO_ASOF - created.date()).days + 1
            assert item.age_days == expected, (
                f"item {item.item_id!r}: age_days={item.age_days} "
                f"expected={expected} (CD - SD + 1)"
            )

    def test_age_days_is_never_zero_for_same_day_items(self):
        """The +1 in the Vacanti formula exists precisely so a
        same-day item reports 1d, not 0. Synthetic test pins this
        edge case."""
        from datetime import datetime as _dt

        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE,
                materialised_at TIMESTAMP
            )"""
        )
        con.execute(
            """CREATE TABLE transitions (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                entered_at TIMESTAMP, stage VARCHAR, signal VARCHAR
            )"""
        )
        # Item created May 6 09:00 UTC, in-flight at May 6 23:00.
        # Calendar day distance = 0; +1 = 1d.
        con.execute(
            "INSERT INTO work_items VALUES "
            "('c', 'github', '#1', 't', NULL, ?, NULL, NULL, ?)",
            [_dt(2026, 5, 6, 9, 0), _dt(2026, 5, 7, 0, 0)],
        )
        data = render(con, "c", asof=date(2026, 5, 6))
        assert data.count == 1
        assert data.items[0].age_days == 1, (
            f"same-day item must report age 1d (Vacanti +1); got "
            f"{data.items[0].age_days}"
        )

    def test_current_state_is_last_transition_at_or_before_asof(self, warehouse):
        """The state shown on the x-axis is the last stage the
        item ENTERED at or before asof. Transitions after asof
        haven't happened yet from the asof point of view."""
        asof = _DEMO_ASOF
        data = render(warehouse, "astral-uv-week", asof=asof)
        # Sample assertion against the transitions Parquet.
        if not data.items:
            return
        item = data.items[0]
        expected = warehouse.execute(
            "SELECT stage FROM transitions "
            "WHERE contract_id = 'astral-uv-week' AND item_id = ? "
            "  AND CAST(entered_at AS DATE) <= CAST(? AS DATE) "
            "ORDER BY entered_at DESC LIMIT 1",
            [item.item_id, asof],
        ).fetchone()
        assert expected is not None
        assert item.current_state == expected[0], (
            f"current_state for {item.item_id!r}: got "
            f"{item.current_state!r} expected {expected[0]!r}"
        )

    def test_asof_is_required_and_echoed_back(self, warehouse):
        """`asof` is supplied by the caller (the one window
        model) — the component never invents a date. It's echoed
        back so the view can name it."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        assert data.asof_iso == _DEMO_ASOF.isoformat()

    def test_item_carries_identity_and_url(self, warehouse):
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        if not data.items:
            return
        first = data.items[0]
        assert first.item_id
        assert first.title
        assert first.url is None or first.url.startswith("http")

    def test_percentile_thresholds_come_from_completed_cycle_times(
        self, warehouse
    ):
        """Aging uses the SAME percentile thresholds the cycle-time
        chart shows — they're the commitment lines an aging item is
        checked against. Pull from completed cycle_time_days in the
        warehouse."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        row = warehouse.execute(
            "SELECT percentile_cont(0.50) WITHIN GROUP (ORDER BY cycle_time_days), "
            "       percentile_cont(0.85) WITHIN GROUP (ORDER BY cycle_time_days), "
            "       percentile_cont(0.95) WITHIN GROUP (ORDER BY cycle_time_days) "
            "FROM work_items "
            "WHERE contract_id = 'astral-uv-week' "
            "  AND cycle_time_days IS NOT NULL"
        ).fetchone()
        # Allow tiny float drift across reads.
        for got, want in zip([data.percentiles.p50, data.percentiles.p85, data.percentiles.p95], row):
            assert abs(got - want) < 1e-9, (
                f"percentile drift: got {got!r} want {want!r}"
            )

    def test_headline_summarizes_item_count_and_asof(self, warehouse):
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        assert str(data.count) in data.headline, (
            f"headline must include the item count; got {data.headline!r}"
        )
        # Headline names the asof date in human form ("May 06, 2026").
        assert re.search(
            r"[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}", data.headline
        ), (
            f"headline must include a human-formatted asof date; "
            f"got {data.headline!r}"
        )

    def test_in_flight_items_means_empty_state_is_none(self, warehouse):
        """When there ARE in-flight items, no empty-state framing
        is needed — the chart renders normally."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        assert data.count > 0
        assert data.empty_state is None, (
            f"non-empty render must carry empty_state=None; got "
            f"{data.empty_state!r}"
        )

    def test_asof_past_latest_coverage_offers_to_import_forward(
        self, warehouse
    ):
        """`asof` past the latest data the warehouse has on hand.
        The UI offers a backfill command spanning the gap, so the
        operator sees what range to fetch rather than a vague
        'outside window' message."""
        data = render(
            warehouse,
            "astral-uv-week",
            asof=date(2026, 5, 19),
        )
        assert data.count == 0
        assert data.empty_state == "asof_after_coverage", (
            f"expected 'asof_after_coverage'; got {data.empty_state!r}"
        )
        # The coverage bounds are surfaced so the UI can name the
        # exact gap in the import-command suggestion.
        assert data.coverage.latest_iso is not None
        assert data.coverage.latest_display is not None

    def test_asof_before_earliest_coverage_offers_to_import_backward(
        self, warehouse
    ):
        """Symmetric to the forward case — asof predates everything
        the warehouse has on hand, so the action is "import data
        backwards"."""
        data = render(
            warehouse,
            "astral-uv-week",
            asof=date(2025, 12, 1),
        )
        assert data.count == 0
        assert data.empty_state == "asof_before_coverage"
        assert data.coverage.earliest_iso is not None
        assert data.coverage.earliest_display is not None

    def test_asof_in_coverage_warehouse_only_has_completed_is_never_captured(self):
        """Coverage of completed data spans asof, but the warehouse
        has NEVER captured an in-flight snapshot. This is the
        common state right after the first materialise run (which
        only fetched merged PRs). Distinguished from the real
        'no work' state because importing WILL help — there's a
        snapshot we haven't taken yet."""
        from datetime import datetime as _dt

        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE,
                materialised_at TIMESTAMP
            )"""
        )
        con.execute(
            """CREATE TABLE transitions (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                entered_at TIMESTAMP, stage VARCHAR, signal VARCHAR
            )"""
        )
        # Two completed items bracketing asof. NO in-flight rows
        # anywhere in the warehouse.
        con.executemany(
            "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("c", "github", "#1", "t", None,
                 _dt(2026, 5, 4, 9, 0), _dt(2026, 5, 4, 12, 0),
                 1.0, _dt(2026, 5, 10, 0, 0)),
                ("c", "github", "#2", "t", None,
                 _dt(2026, 5, 8, 9, 0), _dt(2026, 5, 8, 12, 0),
                 1.0, _dt(2026, 5, 10, 0, 0)),
            ],
        )

        data = render(con, "c", asof=date(2026, 5, 6))
        assert data.count == 0
        assert data.empty_state == "in_flight_never_captured"
        # Coverage names the gap-free range so the UI can show
        # "warehouse covers May 4 – May 8".
        assert data.coverage.earliest_iso == "2026-05-04"
        assert data.coverage.latest_iso == "2026-05-08"

    def test_in_flight_never_captured_state_when_no_open_rows_exist(self):
        """User-reported bug: after clicking import, the button
        disappears because empty_state flips from
        'asof_after_coverage' to 'no_work_in_flight' — but the
        warehouse has only completed items; in-flight has never
        been captured. That looks like "real empty" but really
        means "the snapshot of open work doesn't exist yet."
        Distinguish them so the operator still gets a button to
        fetch fresh in-flight data."""
        from datetime import datetime as _dt

        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE,
                materialised_at TIMESTAMP
            )"""
        )
        con.execute(
            """CREATE TABLE transitions (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                entered_at TIMESTAMP, stage VARCHAR, signal VARCHAR
            )"""
        )
        # Only completed items — no row has completed_at IS NULL.
        con.execute(
            "INSERT INTO work_items VALUES "
            "('c', 'github', '#1', 't', NULL, "
            " ?, ?, 1.0, ?)",
            [
                _dt(2026, 5, 4, 9, 0),
                _dt(2026, 5, 19, 12, 0),
                _dt(2026, 5, 19, 14, 0),
            ],
        )

        data = render(con, "c", asof=date(2026, 5, 19))
        assert data.count == 0
        assert data.empty_state == "in_flight_never_captured", (
            f"warehouse with only completed rows must mark in-flight as "
            f"never captured; got {data.empty_state!r}"
        )

    def test_no_work_in_flight_only_when_in_flight_rows_actually_exist(self):
        """The 'real empty' state only applies when the warehouse
        HAS captured in-flight items at some point — just none
        match the as-of. With actual NULL-completed_at rows
        present elsewhere, the absence is meaningful."""
        from datetime import datetime as _dt

        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE,
                materialised_at TIMESTAMP
            )"""
        )
        con.execute(
            """CREATE TABLE transitions (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                entered_at TIMESTAMP, stage VARCHAR, signal VARCHAR
            )"""
        )
        # Completed items span May 04 → May 10 (warehouse has
        # coverage through asof=May 06). PLUS a separate in-flight
        # item created May 18 — proves the warehouse HAS captured
        # in-flight rows at some point, just none at asof=May 06.
        con.executemany(
            "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)",
            [
                ("c", "github", "#1", "t", None,
                 _dt(2026, 5, 4, 9, 0), _dt(2026, 5, 4, 12, 0),
                 1.0, _dt(2026, 5, 19, 14, 0)),
                ("c", "github", "#2", "t", None,
                 _dt(2026, 5, 10, 9, 0), _dt(2026, 5, 10, 12, 0),
                 1.0, _dt(2026, 5, 19, 14, 0)),
                ("c", "github", "#3", "t", None,
                 _dt(2026, 5, 18, 9, 0), None,
                 None, _dt(2026, 5, 19, 14, 0)),
            ],
        )

        # Asof MAY 06 — between #1 and #2, before #3.
        data = render(con, "c", asof=date(2026, 5, 6))
        assert data.count == 0
        assert data.empty_state == "no_work_in_flight", (
            f"warehouse with in-flight rows that just don't match "
            f"asof should be the real 'no work' state; got "
            f"{data.empty_state!r}"
        )

    def test_percentile_source_smell_when_inflight_dwarfs_history(
        self, warehouse
    ):
        """Smell signal: when in-flight ages span far longer than
        the historical sample driving the percentiles, the P-line
        thresholds are statistically shaky. We flag it so the
        operator sees the warning before drawing conclusions.

        The fixture's percentile sample is 7 days (May 4–10). The
        historical demo asof produces 5 in-flight items aged 0–6
        days, ratio ~0.9x — no smell. Below the test seeds a
        synthetic warehouse with ages dramatically larger to
        exercise the smell branch."""
        # Build a synthetic warehouse: 5 completed items in a
        # 3-day window AND 1 in-flight item created 30 days ago.
        # Ratio of in-flight age (30d) to percentile window (3d)
        # = 10x → smell triggered.
        from datetime import datetime as _dt

        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE,
                materialised_at TIMESTAMP
            )"""
        )
        con.execute(
            """CREATE TABLE transitions (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                entered_at TIMESTAMP, stage VARCHAR, signal VARCHAR
            )"""
        )
        rows = []
        # 5 completed items, all on consecutive days, cycle=1d
        for i in range(5):
            rows.append((
                "c", "github", f"#c{i}", "t", None,
                _dt(2026, 5, 4 + (i % 3), 9, 0),
                _dt(2026, 5, 4 + (i % 3), 12, 0),
                1.0,
                _dt(2026, 6, 4, 12, 0),
            ))
        # One in-flight item created 30 days before asof
        rows.append((
            "c", "github", "#open", "t", None,
            _dt(2026, 5, 5, 9, 0),
            None, None,
            _dt(2026, 6, 4, 12, 0),
        ))
        con.executemany(
            "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )

        data = render(con, "c", asof=date(2026, 6, 4))
        # The item created May 5, asof June 4 → age = 30 days.
        # Pct source window: May 4–6 = 3 days. Ratio ≈ 10x → smell.
        assert data.count == 1
        assert data.percentiles.smell is True, (
            f"smell must trigger when in-flight ages dwarf the "
            f"percentile sample window; got "
            f"percentile_source_smell={data.percentiles.smell!r} "
            f"with in-flight max age = 30d, sample = 3d"
        )
        # A descriptive sentence the UI can render verbatim.
        assert data.percentiles.smell_text
        assert "wider" in data.percentiles.smell_text.lower() or (
            "broaden" in data.percentiles.smell_text.lower()
        )

    def test_percentile_source_no_smell_when_history_is_proportional(
        self, warehouse
    ):
        """When in-flight ages are within the same order of
        magnitude as the historical sample window, the percentile
        baseline is fine — no smell."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        # Fixture: in-flight ages 0–6, sample window 7. Ratio < 3x.
        assert data.percentiles.smell is False

    def test_payload_names_the_percentile_source(self, warehouse):
        """User-pinned: surface the inputs to the percentile lines.
        A chart showing 30-day in-flight ages with thresholds drawn
        from only 7 days of historical completions is a smell —
        the operator should see both numbers and reach the
        conclusion themselves."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        # How many completed items were percentiles drawn from?
        assert data.percentiles.source_count > 0, (
            f"percentile_source_count must be set; got "
            f"{data.percentiles.source_count!r}"
        )
        # Human-readable window — "May 04 – May 10" or similar.
        assert data.percentiles.source_window_display, (
            f"percentile_source_window_display must be set; got "
            f"{data.percentiles.source_window_display!r}"
        )

    def test_headline_names_the_percentile_source(self, warehouse):
        """The metric-summary headline (the line that sits above
        the chart) must mention how many completed items the
        percentiles came from. That's the operator's signal that
        '9d P95 from 7 days of data is shaky.'"""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        text = data.headline.lower()
        assert "from" in text or "based on" in text, (
            f"headline must name the percentile source; got "
            f"{data.headline!r}"
        )
        assert str(data.percentiles.source_count) in data.headline

    def test_chart_spec_threshold_rules_render_on_top_of_dots(
        self, warehouse
    ):
        """The rule lines were visually obscured by the in-flight
        dot cloud. Their layer must paint AFTER the points so they
        stay visible — Vega-Lite layer order = paint order."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        spec = json.loads(data.vega_spec_json())
        layers = spec["layer"]
        # Find each layer's primary mark type.
        def _mark(layer):
            m = layer.get("mark")
            if isinstance(m, str):
                return m
            if isinstance(m, dict):
                return m.get("type")
            return None
        marks_in_order = [_mark(layer) for layer in layers]
        # Some rule (or text-label) layer must follow the point
        # layer — Vega-Lite paints layers in the order listed.
        try:
            point_idx = marks_in_order.index("point")
        except ValueError:  # pragma: no cover — pinned by another test
            pytest.fail(f"no point layer; marks = {marks_in_order}")
        post_point = marks_in_order[point_idx + 1 :]
        assert "rule" in post_point, (
            f"a rule layer must paint AFTER the in-flight points so "
            f"the threshold lines remain visible. Marks in order: "
            f"{marks_in_order}"
        )

    def test_states_wip_filter_excludes_non_wip_items(
        self, warehouse
    ):
        """Items currently in a state outside `states.wip` are
        excluded from Aging WIP — backlog and done both fall
        out. Per Vacanti, the chart is WIP-only."""
        from flowmetrics.contract import WorkflowStates
        states = WorkflowStates(wip=("Draft",))
        data = render(
            warehouse, "astral-uv-week",
            asof=_DEMO_ASOF,
            states=states,
        )
        for item in data.items:
            assert item.current_state == "Draft", (
                f"item {item.item_id!r} has current_state="
                f"{item.current_state!r}, not in states.wip"
            )

    def test_aging_preserves_raw_state_names_on_chart(
        self, warehouse
    ):
        """Raw state names (Changes Suggested, Awaiting Feedback)
        survive the filter so operators can see where work is
        stuck. The wip list is a filter, not a renamer."""
        from flowmetrics.contract import WorkflowStates
        states = WorkflowStates(
            wip=("Draft", "Awaiting Review", "Changes Requested", "Approved"),
        )
        data = render(
            warehouse, "astral-uv-week",
            asof=_DEMO_ASOF,
            states=states,
        )
        valid = set(states.wip)
        for item in data.items:
            assert item.current_state in valid

    def test_y_axis_floors_at_zero(self, warehouse):
        """The dot layer's y scale is floored at 0 (age can't go
        negative); the domainMax auto-fits whatever survives the
        cap filter, so the visible dots fill the plot."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        spec = json.loads(data.vega_spec_json())
        if not data.items:
            pytest.skip("no in-flight items in fixture")
        point_layer = next(
            lyr for lyr in spec["layer"]
            if isinstance(lyr.get("mark"), dict)
            and lyr["mark"].get("type") == "point"
        )
        assert point_layer["encoding"]["y"]["scale"]["domainMin"] == 0

    def test_chart_uses_canonical_xoffset_random_pattern(self, warehouse):
        """The aging chart's jitter follows Vega-Lite's canonical
        `point_offset_random` pattern: a quantitative xOffset
        field with NO explicit pixel range. Vega-Lite then auto-
        fits the offset to the band's width at render time, so
        dots fill whatever band the layout produces — robust
        across viewport sizes without brittle pixel numbers
        baked into the spec.

        Reference: https://vega.github.io/vega-lite/examples/point_offset_random.html
        """
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        spec = json.loads(data.vega_spec_json())

        # Find xOffset on the point layer.
        offsets_seen: list = []

        def _walk(node):
            if isinstance(node, dict):
                if "xOffset" in node and isinstance(node["xOffset"], dict):
                    offsets_seen.append(node["xOffset"])
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)

        _walk(spec)
        assert offsets_seen, "xOffset not present on point layer"
        off = offsets_seen[0]
        assert off.get("type") == "quantitative", (
            f"xOffset must be quantitative for Vega's band-auto-fit "
            f"to apply; got type={off.get('type')!r}"
        )
        # The canonical pattern omits scale.range. Pixel ranges
        # bake assumptions about band width into the spec; the
        # whole point of canonical is to let Vega derive the
        # range from the band's actual width at render time.
        scale = off.get("scale", {})
        assert "range" not in scale, (
            f"xOffset.scale.range must NOT be hard-coded — that "
            f"breaks the canonical auto-fit pattern. Got scale={scale!r}"
        )

    def test_x_scale_is_band_so_labels_center_in_their_band(
        self, warehouse
    ):
        """User-reported: the category labels appear AT a tick mark
        instead of centered IN the band. Vega-Lite's default for a
        nominal x with xOffset is a `point` scale (each category =
        single tick, no band); switching to `band` puts ticks at
        band centers and gives each category a visible width the
        dots can spread across.

        Pin scale.type = 'band' on the point layer's x channel."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        spec = json.loads(data.vega_spec_json())
        # Find the in-flight point layer (the one that carries
        # the `params: [aging_zoom]` zoom binding).
        point_layer = None
        for layer in spec["layer"]:
            if isinstance(layer.get("params"), list) and any(
                p.get("name") == "aging_zoom" for p in layer["params"]
            ):
                point_layer = layer
                break
        assert point_layer is not None, "point layer not found"
        x = point_layer["encoding"]["x"]
        assert isinstance(x.get("scale"), dict), (
            f"x channel must declare scale.type='band'; got x={x!r}"
        )
        assert x["scale"].get("type") == "band", (
            f"x scale must be band so labels center in their band; "
            f"got type={x['scale'].get('type')!r}"
        )

    def test_chart_includes_per_column_count_badge(self, warehouse):
        """At-a-glance WIP count per state — a 'WIP N' text header
        pinned to the TOP of the chart (a fixed pixel y, not
        floating above each column's tallest dot), one label per
        state column."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        spec = json.loads(data.vega_spec_json())
        badge = None
        for lyr in spec["layer"]:
            m = lyr.get("mark")
            m_type = m.get("type") if isinstance(m, dict) else m
            if m_type != "text":
                continue
            vals = lyr.get("data", {}).get("values", [])
            if any("WIP" in str(v.get("label", "")) for v in vals):
                badge = lyr
                break
        assert badge is not None, (
            "aging spec must include a 'WIP N' per-state count "
            "header layer"
        )
        # Pinned to the top — a fixed pixel position, not a
        # data-driven y above the dots.
        assert badge["encoding"]["y"] == {"value": 0}, (
            f"count header must be top-pinned; got "
            f"{badge['encoding']['y']!r}"
        )
        # One 'WIP N' label per state column.
        labels = [v["label"] for v in badge["data"]["values"]]
        assert labels and all(lab.startswith("WIP ") for lab in labels)

    def test_dots_are_coloured_by_state_category(self, warehouse):
        """Each dot is coloured by its workflow state, so a viewer
        can tell which state category a point belongs to. The
        x-axis already labels the columns, so the colour carries
        no legend — it just reinforces the grouping."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        spec = json.loads(data.vega_spec_json())
        point_layer = next(
            lyr for lyr in spec["layer"]
            if isinstance(lyr.get("mark"), dict)
            and lyr["mark"].get("type") == "point"
        )
        color = point_layer["encoding"]["color"]
        assert color["field"] == "current_state"
        assert color.get("legend") is None

    def test_chart_spec_is_zoom_and_pan_able(self, warehouse):
        """Aging WIP needs zoom/pan so an operator can dig into
        the densest column (typically 'Awaiting Review') without
        losing the percentile-reference context. Vega-Lite's
        interval-select bound to scales is the idiomatic way."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        spec = json.loads(data.vega_spec_json())

        # The params block can live at top-level OR on a sub-layer.
        # Either way it must declare an interval-select bound to
        # scales (otherwise the chart is static).
        found = False

        def _walk(node):
            nonlocal found
            if isinstance(node, dict):
                params = node.get("params")
                if isinstance(params, list):
                    for p in params:
                        if (
                            isinstance(p, dict)
                            and isinstance(p.get("select"), dict)
                            and p["select"].get("type") == "interval"
                            and p.get("bind") == "scales"
                        ):
                            found = True
                            return
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)

        _walk(spec)
        assert found, (
            f"aging spec must declare an interval-select param "
            f"bound to scales (zoom + pan); none found in spec"
        )

    def test_chart_spec_uses_point_marks_for_in_flight_items(self, warehouse):
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        spec = json.loads(data.vega_spec_json())
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
        # Point + rule layers (rule for percentile thresholds).
        assert "point" in marks, (
            f"aging spec must include point marks; got {marks}"
        )
        assert "rule" in marks, (
            f"aging spec must include rule marks for percentile "
            f"thresholds; got {marks}"
        )


def _marks(spec: dict) -> list[str]:
    """All mark types in a Vega-Lite spec, recursively."""
    out: list[str] = []

    def _walk(node):
        if isinstance(node, dict):
            m = node.get("mark")
            if isinstance(m, str):
                out.append(m)
            elif isinstance(m, dict) and "type" in m:
                out.append(m["type"])
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(spec)
    return out


class TestEmptyReferenceWindow:
    """When the reference window captures zero completed items the
    percentiles collapse to 0/0/0. Drawing "P50 0.0d" threshold
    lines and a "NNN× wider" smell callout against an empty
    sample is misinformation — the component must say so honestly
    instead. (Surfaced by a user who picked a reference period
    that landed entirely outside the data: the aging headline
    read "P50 0.0d ... from 0 completed items" beside a smell
    callout claiming the percentiles were "340× too narrow".)"""

    def _warehouse_with_old_inflight_and_no_recent_completions(self):
        from datetime import datetime as _dt

        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE,
                materialised_at TIMESTAMP
            )"""
        )
        con.execute(
            """CREATE TABLE transitions (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                entered_at TIMESTAMP, stage VARCHAR, signal VARCHAR
            )"""
        )
        con.executemany(
            "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)",
            [
                # An ancient in-flight item — created Jan 2024,
                # still open. Gives the chart a non-empty body.
                ("c", "github", "#open", "ancient", None,
                 _dt(2024, 1, 1, 9, 0), None, None,
                 _dt(2026, 5, 10, 12, 0)),
                # A completed item — but it completed in Jan 2024,
                # OUTSIDE any May-2026 reference window.
                ("c", "github", "#done", "old", None,
                 _dt(2024, 1, 1, 9, 0), _dt(2024, 1, 2, 9, 0), 1.0,
                 _dt(2026, 5, 10, 12, 0)),
            ],
        )
        return con

    _REF = Window(from_=date(2026, 5, 5), to=date(2026, 5, 10))
    _ASOF = date(2026, 5, 10)

    def test_old_inflight_item_still_renders(self):
        """Sanity: the in-flight item shows even though the
        reference window has no completions."""
        con = self._warehouse_with_old_inflight_and_no_recent_completions()
        data = render(con, "c", asof=self._ASOF, reference=self._REF)
        assert data.count == 1
        assert data.percentiles.source_count == 0

    def test_zero_completions_in_reference_yields_no_smell(self):
        """No smell when there is no historical sample to compare
        against — a "NNN× wider" callout against an empty window
        is noise, not signal."""
        con = self._warehouse_with_old_inflight_and_no_recent_completions()
        data = render(con, "c", asof=self._ASOF, reference=self._REF)
        assert data.percentiles.smell is False, (
            "smell must not fire when the reference window has "
            "zero completed items"
        )

    def test_zero_completions_headline_is_honest(self):
        """The headline must name the empty reference plainly,
        never print "P50 0.0d" as if it were a real threshold."""
        con = self._warehouse_with_old_inflight_and_no_recent_completions()
        data = render(con, "c", asof=self._ASOF, reference=self._REF)
        assert "0.0d" not in data.headline, (
            f"headline must not print a 0.0d percentile for an "
            f"empty reference sample; got {data.headline!r}"
        )
        assert "no completed items" in data.headline.lower(), (
            f"headline must name the empty reference explicitly; "
            f"got {data.headline!r}"
        )

    def test_zero_completions_omits_percentile_rule_layer(self):
        """Percentile rule lines must be omitted entirely — three
        dashed rules stacked on y=0 read as a real threshold."""
        con = self._warehouse_with_old_inflight_and_no_recent_completions()
        data = render(con, "c", asof=self._ASOF, reference=self._REF)
        spec = json.loads(data.vega_spec_json())
        assert "rule" not in _marks(spec), (
            f"percentile rule lines must be omitted when there are "
            f"no completions to draw them from; marks: {_marks(spec)}"
        )
        # The dots are still drawn — this is a populated chart.
        assert "point" in _marks(spec)


class TestPinnedToSnapshot:
    """Aging is a "right now" metric: the caller pins `asof` to the
    in-flight snapshot date. An ancient still-open item is reported
    AT the snapshot date — never projected into some future range
    the user scrolled to (the old phantom "318 in-flight as of
    <future>" bug, fixed by removing the scrollable `view`)."""

    def _warehouse(self):
        from datetime import datetime as _dt

        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE, materialised_at TIMESTAMP
            )"""
        )
        con.execute(
            """CREATE TABLE transitions (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                entered_at TIMESTAMP, stage VARCHAR, signal VARCHAR
            )"""
        )
        con.executemany(
            "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)",
            [
                # An ancient still-open item — in flight at the snapshot.
                ("c", "github", "#open", "ancient", None,
                 _dt(2024, 1, 1, 9, 0), None, None, _dt(2025, 1, 20)),
                # A completed item — coverage is Jan 15, 2025 only.
                ("c", "github", "#done", "t", None,
                 _dt(2024, 1, 1, 9, 0), _dt(2025, 1, 15, 9, 0), 1.0,
                 _dt(2025, 1, 20)),
            ],
        )
        return con

    def test_old_open_item_is_in_flight_at_the_snapshot_date(self):
        """`render` takes no `view` — the caller passes the snapshot
        date as `asof` and the ancient open item is counted there."""
        data = render(
            self._warehouse(), "c",
            asof=date(2025, 1, 20),  # the snapshot date
        )
        assert data.count == 1  # the ancient open item, in-flight
        assert data.empty_state is None

    def test_render_rejects_a_view_argument(self):
        """The scrollable `view` parameter is gone — aging no longer
        follows the Period picker."""
        with pytest.raises(TypeError):
            render(
                self._warehouse(), "c",
                asof=date(2025, 1, 20),
                view=Window(from_=date(2025, 1, 1), to=date(2025, 1, 20)),
            )


class TestAgingCapSlider:
    """The aging chart's y-axis cap — a range slider that clips the
    age axis to the readable bulk so a few very-old items don't
    squash it. Runs from ~P95 of the in-flight ages up to the
    oldest item; the percentile threshold lines are unaffected."""

    def _warehouse(self):
        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE, materialised_at TIMESTAMP)"""
        )
        con.execute(
            """CREATE TABLE transitions (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                entered_at TIMESTAMP, stage VARCHAR, signal VARCHAR)"""
        )
        snap = datetime(2026, 6, 1)
        # 20 items aged 1-20 days, plus one ancient at 600 days.
        rows = [
            ("c", "github", f"#{age}", f"item {age}", None,
             snap - timedelta(days=age - 1), None, None, snap)
            for age in [*range(1, 21), 600]
        ]
        con.executemany(
            "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)", rows
        )
        return con

    def _warehouse_with_completions(self):
        """Same in-flight items as `_warehouse`, plus completed
        items whose cycle times drive the P95 reference line."""
        con = self._warehouse()
        snap = datetime(2026, 6, 1)
        completed = [
            ("c", "github", f"done-{n}", f"done {n}", None,
             datetime(2026, 1, 1), datetime(2026, 5, 1), 200.0, snap)
            for n in range(10)
        ]
        con.executemany(
            "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)", completed
        )
        return con

    def test_cap_floor_is_the_p95_commitment_line(self):
        """The cap slider's floor is the P95 reference line — the
        dashed commitment threshold drawn from completed cycle
        times — NOT the P95 of the in-flight ages. Cropping down
        to it focuses the view on items at or below the
        high-stakes threshold."""
        con = self._warehouse_with_completions()
        data = render(con, "c", asof=date(2026, 6, 1))
        spec = json.loads(data.vega_spec_json())
        cap = next(
            p for p in spec.get("params", [])
            if isinstance(p.get("bind"), dict)
            and p["bind"].get("input") == "range"
        )
        assert data.percentiles.p95 > 0
        assert cap["bind"]["min"] == math.ceil(data.percentiles.p95), (
            f"cap floor must be the P95 reference line "
            f"({data.percentiles.p95}); got {cap['bind']['min']}"
        )

    def test_chart_has_a_y_cap_range_control(self):
        con = self._warehouse()
        data = render(con, "c", asof=date(2026, 6, 1))
        spec = json.loads(data.vega_spec_json())
        # Top-level value param — a layer-scoped one would be out
        # of scope for the shared y scale's domainMax expr.
        caps = [
            p for p in spec.get("params", [])
            if isinstance(p.get("bind"), dict)
            and p["bind"].get("input") == "range"
        ]
        assert caps, "expected a top-level range-input y-cap param"
        cap = caps[0]
        assert cap["bind"]["max"] >= 600  # reaches the oldest item
        assert cap["bind"]["min"] < cap["bind"]["max"]
        # Default = max → opens showing every in-flight item.
        assert cap["value"] == cap["bind"]["max"]
        # The dot layer FILTERS by the cap; the y-axis auto-scales.
        point_layer = next(
            lyr for lyr in spec["layer"]
            if isinstance(lyr.get("mark"), dict)
            and lyr["mark"].get("type") == "point"
        )
        filters = [
            t.get("filter") for t in point_layer.get("transform", [])
        ]
        assert any(cap["name"] in str(f) for f in filters), (
            f"dot transform must filter by {cap['name']!r}; got {filters}"
        )
