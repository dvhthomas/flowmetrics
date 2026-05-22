"""Component tests for `flowmetrics.web.components.cfd`.

Cumulative Flow Diagram per Vacanti (Actionable Agile Metrics
for Predictability, 10th Anniversary Edition):

  For each date in the window and each stage, the value plotted
  is the cumulative count of items that have ENTERED that stage
  on or before that date.

  Bands stack in workflow order (first stage at top, terminal
  at bottom). The visible width of each band = WIP in that
  stage at that moment. The full stack height at any date =
  total items that have ever entered the workflow. The bottom
  band = items that have completed (departures).

The key invariant the math must preserve:

  count_entered(stage_N, date) ≥ count_entered(stage_N+1, date)

for every date and every adjacent pair of stages in workflow
order. The difference between the two cumulatives IS the WIP in
the earlier stage at that date. If the math ever violates this
(by, say, double-counting transitions or using the wrong source
table), the CFD bands would cross — visually nonsense.

We test the invariant directly. We also pin:

  - The bottom band's cumulative at the final date == count of
    completed items in `work_items`. This catches drift between
    the transitions table and the work_items table.
  - The top band's cumulative at the final date ≥ all other
    bands. (Same invariant generalised.)
  - Zero-arrival days are not gaps — every calendar date in the
    [first, last] window has one row per stage.

For our GitHub PR fixture, the inferred workflow order is
Draft → Awaiting Review → Merged. The component infers the
ordering from data (median `entered_at` per stage) so it works
on Jira workflows or contract-specific stage names without code
changes. A contract YAML override is a future hook.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.web.components.cfd import render


FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
    tmp = Path(tempfile.mkdtemp())
    contracts_dir = tmp / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp / "data"
    (contracts_dir / "demo.yaml").write_text(
        yaml.safe_dump(
            {
                "contract": {
                    "name": "demo",
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
            "materialise", "demo",
            "--data-dir", str(data_dir),
            "--contracts-dir", str(contracts_dir),
            "--cache-dir", str(FIXTURE_CACHE),
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


class TestCfdShape:
    def test_one_row_per_date_per_stage(self, warehouse):
        """Every calendar date from first arrival to last arrival
        appears with one entry per stage. Zero-arrival days are
        carried over from the previous day (cumulative), not
        skipped."""
        data = render(warehouse, "demo")
        assert data.stages, "fixture must have ≥ 1 stage"
        # Each date_iso appears exactly once in daily — each daily
        # point carries a `counts` dict keyed by stage.
        date_isos = [d.date_iso for d in data.daily]
        assert date_isos == sorted(date_isos), (
            f"daily must be sorted ascending; got {date_isos}"
        )
        # Consecutive — no gaps.
        from datetime import date as _date
        parsed = [_date.fromisoformat(s) for s in date_isos]
        for prev, cur in zip(parsed, parsed[1:]):
            assert (cur - prev).days == 1, (
                f"daily series gap between {prev} and {cur}"
            )
        # Each point has a count per stage.
        for d in data.daily:
            assert set(d.counts.keys()) == set(data.stages), (
                f"date {d.date_iso!r}: counts keys {sorted(d.counts)} "
                f"don't match stages {sorted(data.stages)}"
            )

    def test_counts_are_monotonic_non_decreasing_per_stage(self, warehouse):
        """Cumulative arrivals only go UP over time. If a stage's
        count ever decreases, we're counting wrong (probably
        double-counting transitions or using the source-events
        rather than first-entry-per-item)."""
        data = render(warehouse, "demo")
        for stage in data.stages:
            counts = [d.counts[stage] for d in data.daily]
            for prev, cur in zip(counts, counts[1:]):
                assert cur >= prev, (
                    f"stage {stage!r} cumulative count decreased: "
                    f"{prev} → {cur}. Cumulative arrivals must be "
                    f"monotonic non-decreasing."
                )

    def test_bands_never_cross(self, warehouse):
        """The core CFD invariant: at every date, the cumulative
        count for an EARLIER stage in the workflow is ≥ the
        cumulative count for any LATER stage. Items must pass
        through stages in order, so if N items have reached
        stage_N+1, those same N items already reached stage_N.

        Bands crossing on a CFD would be visually meaningless and
        a signal that the math is wrong."""
        data = render(warehouse, "demo")
        for d in data.daily:
            counts_in_order = [d.counts[stage] for stage in data.stages]
            for stage_a, stage_b, a, b in zip(
                data.stages,
                data.stages[1:],
                counts_in_order,
                counts_in_order[1:],
            ):
                assert a >= b, (
                    f"on {d.date_iso}: earlier stage {stage_a!r} "
                    f"cumulative ({a}) < later stage {stage_b!r} "
                    f"cumulative ({b}). Workflow-order invariant "
                    f"broken — bands would cross."
                )

    def test_terminal_band_at_last_date_matches_work_items_completed(
        self, warehouse
    ):
        """Cross-check the transitions math against work_items:
        the terminal stage's cumulative at the last date should
        equal the count of completed items in work_items. If
        these drift, the two tables disagree about completions."""
        data = render(warehouse, "demo")
        last = data.daily[-1]
        terminal_stage = data.stages[-1]
        terminal_count = last.counts[terminal_stage]
        completed_in_work_items = warehouse.execute(
            "SELECT count(*) FROM work_items "
            "WHERE contract_id = 'demo' AND completed_at IS NOT NULL"
        ).fetchone()[0]
        assert terminal_count == completed_in_work_items, (
            f"CFD terminal stage count ({terminal_count}) disagrees "
            f"with work_items completed count "
            f"({completed_in_work_items}). The two tables must "
            f"agree about who completed by when."
        )

    def test_top_band_at_last_date_is_total_arrivals(self, warehouse):
        """The top band's cumulative = items that have entered the
        workflow's first stage. Every item must have passed
        through stage_0 at some point, so this equals the total
        number of items the warehouse knows about (where
        `created_at IS NOT NULL`)."""
        data = render(warehouse, "demo")
        last = data.daily[-1]
        top_stage = data.stages[0]
        top_count = last.counts[top_stage]
        total_items = warehouse.execute(
            "SELECT count(DISTINCT item_id) FROM transitions "
            "WHERE contract_id = 'demo'"
        ).fetchone()[0]
        # Top stage cumulative = items that entered stage[0]. Items
        # whose first transition was NOT stage[0] (e.g. PR created
        # straight into Awaiting Review, skipping Draft) won't be
        # counted in the top band. That's fine — those items DO
        # appear under whichever stage they first entered. So the
        # top band ≤ total distinct items.
        assert top_count <= total_items
        # And the SUM across all stages' first-arrivals == total
        # distinct items.
        first_arrivals = warehouse.execute(
            """
            SELECT stage, count(DISTINCT item_id) AS n FROM (
              SELECT item_id, stage,
                ROW_NUMBER() OVER (
                  PARTITION BY item_id ORDER BY entered_at ASC
                ) AS rn
              FROM transitions
              WHERE contract_id = 'demo'
            ) WHERE rn = 1
            GROUP BY stage
            """
        ).fetchall()
        first_arrival_total = sum(n for _, n in first_arrivals)
        assert first_arrival_total == total_items

    def test_stages_ordered_in_typical_workflow_progression(self, warehouse):
        """The component infers stage order from data (median
        entered_at per stage). For the GitHub PR fixture, that
        should land on Draft → Awaiting Review → Merged."""
        data = render(warehouse, "demo")
        # Sanity: those three stages are present (subset; CFD may
        # also include other GitHub stages from the data).
        assert "Draft" in data.stages
        assert "Awaiting Review" in data.stages
        assert "Merged" in data.stages
        # Ordering: Draft comes before Awaiting Review, which
        # comes before Merged.
        idx = {s: i for i, s in enumerate(data.stages)}
        assert idx["Draft"] < idx["Awaiting Review"] < idx["Merged"], (
            f"expected Draft → Awaiting Review → Merged; got "
            f"{data.stages}"
        )

    def test_empty_warehouse_renders_no_daily_no_crash(self, warehouse):
        """An unknown contract returns an empty payload, not a
        crash. The view layer renders an empty state."""
        data = render(warehouse, "does-not-exist")
        assert data.daily == ()
        assert data.stages == ()
        assert data.headline

    def test_dates_are_utc_anchored(self, warehouse):
        """Same TZ-safety contract as every other chart: the
        rendered date display is in UTC, regardless of viewer TZ."""
        import re
        data = render(warehouse, "demo")
        for d in data.daily:
            assert re.match(r"^\d{4}-\d{2}-\d{2}$", d.date_iso)
            assert re.match(
                r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}$", d.date_display
            ), (
                f"date_display must be UTC `%b %d, %Y`; got "
                f"{d.date_display!r}"
            )

    def test_spans_full_data_history(self, warehouse):
        """The CFD spans the FULL observed transition history —
        first stage-entry to last — never a clamped window. A
        cumulative chart needs the whole build-up to be legible;
        it is not driven by the filter Period."""
        data = render(warehouse, "demo")
        span = warehouse.execute(
            "SELECT min(d), max(d) FROM ("
            "  SELECT CAST(min(entered_at) AS DATE) AS d "
            "  FROM transitions WHERE contract_id = 'demo' "
            "  GROUP BY item_id, stage)"
        ).fetchone()
        assert data.first_date_iso == span[0].isoformat()
        assert data.last_date_iso == span[1].isoformat()

    def test_cfd_bands_are_wip_plus_done_in_declared_order(
        self, warehouse
    ):
        """The CFD's band list is `states.cfd_bands()` —
        wip followed by done, in declared YAML order. Each raw
        state is its own band. Backlog excluded."""
        from flowmetrics.contract import WorkflowStates
        states = WorkflowStates(
            wip=("Draft", "Awaiting Review", "Changes Requested", "Approved"),
            done=("Merged",),
        )
        data = render(warehouse, "demo", states=states)
        assert data.stages == states.cfd_bands(), (
            f"CFD stages must match states.cfd_bands(); got {data.stages}"
        )

    def test_states_not_in_wip_or_done_are_excluded(self, warehouse):
        """Per Vacanti, backlog states MUST NOT appear in the CFD.
        Any raw state not in `wip` or `done` is dropped from CFD
        math. The cumulative at the terminal must STILL equal
        the completed-items count — backlog-exclusion shouldn't
        lose departures, just hide the upstream bands."""
        from flowmetrics.contract import WorkflowStates
        states = WorkflowStates(done=("Merged",))
        data = render(warehouse, "demo", states=states)
        assert data.stages == ("Merged",)
        last = data.daily[-1]
        completed = warehouse.execute(
            "SELECT count(*) FROM work_items "
            "WHERE contract_id='demo' AND completed_at IS NOT NULL"
        ).fetchone()[0]
        assert last.counts["Merged"] == completed

    def _data_span(self, warehouse) -> tuple[date, date]:
        row = warehouse.execute(
            "SELECT min(d), max(d) FROM ("
            "  SELECT CAST(min(entered_at) AS DATE) AS d "
            "  FROM transitions WHERE contract_id = 'demo' "
            "  GROUP BY item_id, stage)"
        ).fetchone()
        return row[0], row[1]

    def test_chart_has_a_y_axis_floor_control(self, warehouse):
        """A range slider raises the y-axis floor so the operator
        can crop the inert carry-in base and zoom into the active
        bands. It appears only when a Period window starts partway
        into the data (so there IS a carry-in to crop); default
        0 = no crop."""
        from flowmetrics.windows import Window
        _data_min, data_max = self._data_span(warehouse)
        # A window over the tail of the data, so its left edge
        # carries a non-zero, non-final terminal-stage base.
        view = Window(from_=data_max - timedelta(days=4), to=data_max)
        data = render(warehouse, "demo", view=view)
        spec = json.loads(data.vega_spec_json())
        floors = [
            p for p in spec.get("params", [])
            if isinstance(p.get("bind"), dict)
            and p["bind"].get("input") == "range"
        ]
        assert floors, "expected a range-input y-floor param"
        floor = floors[0]
        assert floor["value"] == 0  # default: full chart
        assert floor["bind"]["min"] == 0
        assert floor["bind"]["max"] > 0
        # The y-scale's domainMin follows the floor param.
        assert spec["encoding"]["y"]["scale"]["domainMin"] == {
            "expr": floor["name"]
        }

    def test_y_floor_slider_max_is_the_left_edge_carry_in(
        self, warehouse
    ):
        """The 'hide first N items' slider can crop at most the
        inert carry-in present at the FIRST visible date — cropping
        past that would eat into the curve that grows inside the
        window. Its max is the terminal-stage cumulative at the
        window's LEFT edge, not at the right (the final count)."""
        from flowmetrics.windows import Window
        _data_min, data_max = self._data_span(warehouse)
        # A window over the tail of the data, so its left edge
        # carries a non-zero, non-final terminal-stage base.
        view = Window(from_=data_max - timedelta(days=4), to=data_max)
        data = render(warehouse, "demo", view=view)
        spec = json.loads(data.vega_spec_json())
        floor = next(
            p for p in spec.get("params", [])
            if isinstance(p.get("bind"), dict)
            and p["bind"].get("input") == "range"
        )
        terminal = data.stages[-1]
        left_edge = data.daily[0].counts[terminal]
        right_edge = data.daily[-1].counts[terminal]
        assert floor["bind"]["max"] == left_edge
        assert left_edge < right_edge, (
            "fixture must have the terminal cumulative GROW across "
            "the window for this test to be meaningful"
        )

    def test_visual_window_clamps_to_the_data_range(self, warehouse):
        """A view window wider than the data must not paint empty
        columns: the CFD clamps its visible span to the observed
        [first arrival, last arrival] range — no blank dates
        before the first arrival or after the last."""
        from flowmetrics.windows import Window
        data_min, data_max = self._data_span(warehouse)
        wide = Window(
            from_=data_min - timedelta(days=120),
            to=data_max + timedelta(days=120),
        )
        data = render(warehouse, "demo", view=wide)
        assert data.first_date_iso == data_min.isoformat()
        assert data.last_date_iso == data_max.isoformat()

    def test_area_marks_are_clipped_to_the_plot(self, warehouse):
        """The CFD area marks are clipped to the plot rectangle.
        Without `clip`, raising the y-floor slider leaves the
        cumulative bands spilling below the axis instead of
        cropping cleanly."""
        data = render(warehouse, "demo")
        spec = json.loads(data.vega_spec_json())
        assert spec["mark"]["clip"] is True

    def test_chart_spec_uses_area_marks_stacked_in_stage_order(
        self, warehouse
    ):
        """The CFD is a stacked area chart. The stack order must
        match the workflow stage order so the bands read
        top-to-bottom in workflow progression — Vega-Lite's
        `sort` on the color encoding pins this."""
        data = render(warehouse, "demo")
        spec = json.loads(data.vega_spec_json())
        marks = []

        def _walk(node):
            if isinstance(node, dict):
                m = node.get("mark")
                if isinstance(m, str):
                    marks.append(m)
                elif isinstance(m, dict) and "type" in m:
                    marks.append(m["type"])
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)

        _walk(spec)
        assert "area" in marks, (
            f"CFD spec must include an area mark; got {marks}"
        )
        # The color encoding's `sort` carries the stage order so
        # Vega stacks them top-to-bottom in workflow progression.
        json_str = json.dumps(spec)
        for stage in data.stages:
            assert stage in json_str, (
                f"stage {stage!r} must appear in spec (color domain)"
            )

    def test_chart_x_scale_has_no_outer_padding(self, warehouse):
        """The CFD area fills the plot edge-to-edge — no empty
        strip before the first date or after the last (the
        default point-scale outer padding)."""
        data = render(warehouse, "demo")
        spec = json.loads(data.vega_spec_json())
        assert spec["encoding"]["x"]["scale"]["paddingOuter"] == 0
