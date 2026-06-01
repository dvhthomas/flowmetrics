"""Layer 3 — tests for the cycle-time view
(`flowmetrics.web.components.cycle_time`).

The chart DECISIONS — percentiles, cap bounds, tick density,
empty-states, headline — are tested at Layer 2 in
`test_charts_cycle_time.py`, with no warehouse and no Vega. This
file covers the view only:

  - `render()` wires query (Layer 1) → model (Layer 2);
  - `to_vega()` faithfully translates a model into a Vega-Lite
    spec — the model's numbers reach the spec, the spec is
    structurally sound.

So these tests assert what the chart is EXPECTED to show, via the
model, not the incidental shape of the spec dict.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from flowmetrics.charts.cycle_time import build_cycle_time_model
from flowmetrics.warehouse.queries import CompletedItem
from flowmetrics.web.components.cycle_time import render, to_vega
from flowmetrics.windows import Window


def _item(n: int, completed: date, cycle: float) -> CompletedItem:
    return CompletedItem(
        item_id=f"#{n}",
        title=f"item {n}",
        url=f"http://x/{n}",
        completed_at=datetime(completed.year, completed.month, completed.day, 12),
        cycle_time_days=cycle,
    )


def _model(n_items: int = 12):
    """A model with a clear outlier so a cap control is resolved."""
    items = [
        _item(i, date(2026, 1, 1) + timedelta(days=i), float(i + 1))
        for i in range(n_items)
    ]
    items.append(_item(999, date(2026, 1, 1) + timedelta(days=n_items + 5), 500.0))
    return build_cycle_time_model(items, view=None)


def _warehouse() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(
        """CREATE TABLE work_items (
            contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
            title VARCHAR, url VARCHAR,
            created_at TIMESTAMP, completed_at TIMESTAMP,
            cycle_time_days DOUBLE)"""
    )
    con.executemany(
        "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?)",
        [
            ("c", "github", f"#{i}", f"t{i}", None,
             datetime(2026, 1, 1), datetime(2026, 1, 1 + i), float(i))
            for i in range(1, 6)
        ],
    )
    return con


class TestRenderWiresQueryToModel:
    def test_render_returns_a_model_from_the_warehouse(self):
        model = render(_warehouse(), "c")
        assert model.item_count == 5
        assert {p.item_id for p in model.points} == {f"#{i}" for i in range(1, 6)}

    def test_render_passes_the_view_window_through(self):
        model = render(
            _warehouse(), "c",
            view=Window(from_=date(2026, 1, 1), to=date(2026, 1, 3)),
        )
        # completions land on Jan 2..6; only Jan 2 + 3 are in window.
        assert model.item_count == 2

    def test_unknown_contract_yields_an_empty_model(self):
        model = render(_warehouse(), "absent")
        assert model.is_empty


class TestRenderPercentileFilter:
    """The Percentile Filter slider is page-level — it has to
    narrow the chart's scatter as well as the table below it.
    `ptile_min` / `ptile_max` defaults of 0 / 100 are the no-op;
    any narrower bound drops dots whose cycle-time rank sits
    outside the range. The percentile reference lines stay
    computed from the FULL set so the bands don't move while
    the user drags."""

    def test_default_bounds_preserve_every_point(self):
        unbounded = render(_warehouse(), "c")
        defaulted = render(
            _warehouse(), "c", ptile_min=0, ptile_max=100,
        )
        assert defaulted.item_count == unbounded.item_count
        assert (
            {p.item_id for p in defaulted.points}
            == {p.item_id for p in unbounded.points}
        )

    def test_ptile_max_drops_upper_tail_points(self):
        # ptile_max=50: keep only the lower-half points. The
        # warehouse fixture has 5 items with distinct cycle times
        # (1d, 2d, 3d, 4d, 5d), so the bottom half = the smaller
        # three.
        full = render(_warehouse(), "c")
        narrowed = render(_warehouse(), "c", ptile_max=50)
        assert narrowed.item_count < full.item_count
        assert narrowed.item_count > 0
        max_kept = max(p.cycle_time_days for p in narrowed.points)
        min_dropped = min(
            p.cycle_time_days for p in full.points
            if p.item_id not in {q.item_id for q in narrowed.points}
        )
        assert max_kept <= min_dropped

    def test_ptile_min_drops_lower_tail_points(self):
        full = render(_warehouse(), "c")
        narrowed = render(_warehouse(), "c", ptile_min=85)
        assert narrowed.item_count < full.item_count
        assert narrowed.item_count > 0
        min_kept = min(p.cycle_time_days for p in narrowed.points)
        max_dropped = max(
            p.cycle_time_days for p in full.points
            if p.item_id not in {q.item_id for q in narrowed.points}
        )
        assert min_kept >= max_dropped

    def test_percentile_lines_stay_computed_from_the_full_sample(self):
        # The reference lines summarise the whole window, NOT just
        # the narrowed slice — otherwise the bands would shift
        # under the user's slider drag, which makes the chart
        # impossible to read.
        full = render(_warehouse(), "c")
        narrowed = render(
            _warehouse(), "c", ptile_min=0, ptile_max=50,
        )
        assert narrowed.percentiles == full.percentiles


class TestToVegaStructure:
    def test_spec_has_scatter_rule_and_text_layers(self):
        spec = to_vega(_model())
        marks = [
            lyr["mark"]["type"] if isinstance(lyr["mark"], dict) else lyr["mark"]
            for lyr in spec["layer"]
        ]
        assert marks == ["point", "rule", "text"]

    def test_background_is_transparent_so_the_page_shows_through(self):
        # Matches every other chart — the cream page color, not white.
        assert to_vega(_model())["background"] == "transparent"

    def test_points_reach_the_scatter_data(self):
        model = _model(n_items=7)
        scatter = to_vega(model)["layer"][0]
        assert len(scatter["data"]["values"]) == model.item_count

    def test_percentile_values_reach_the_reference_rows(self):
        model = _model()
        rows = to_vega(model)["layer"][1]["data"]["values"]
        pct = model.percentiles
        assert {r["pct"]: r["y"] for r in rows} == {
            "P50": pct.p50, "P85": pct.p85, "P95": pct.p95,
        }

    def test_tick_interval_reaches_the_x_axis(self):
        model = _model()
        axis = to_vega(model)["layer"][0]["encoding"]["x"]["axis"]
        assert axis["tickCount"] == {
            "interval": model.ticks.interval, "step": model.ticks.step,
        }

    def test_x_domain_reaches_the_scale(self):
        model = _model()
        scale = to_vega(model)["layer"][0]["encoding"]["x"]["scale"]
        assert scale["domain"] == list(model.x_domain)


class TestToVegaCapControl:
    def test_cap_becomes_a_param_and_a_filter_when_present(self):
        model = _model()
        assert model.cap is not None
        spec = to_vega(model)
        cap = next(
            p for p in spec["params"]
            if isinstance(p.get("bind"), dict)
            and p["bind"].get("input") == "range"
        )
        assert cap["bind"]["min"] == model.cap.floor
        assert cap["bind"]["max"] == model.cap.ceiling
        assert cap["value"] == model.cap.default
        filters = [
            t.get("filter") for t in spec["layer"][0].get("transform", [])
        ]
        assert any("cyclecap" in str(f) for f in filters)

    def test_no_cap_param_when_the_model_has_none(self):
        model = build_cycle_time_model([_item(1, date(2026, 1, 1), 5.0)], view=None)
        assert model.cap is None
        assert "params" not in to_vega(model)


class TestToVegaInteraction:
    def test_zoom_is_bound_to_the_x_axis_only(self):
        scatter = to_vega(_model())["layer"][0]
        zoom = next(p for p in scatter["params"] if p.get("bind") == "scales")
        assert zoom["select"]["encodings"] == ["x"]

    def test_jitter_offsets_dots_forward_within_their_day(self):
        transform = to_vega(_model())["layer"][0]["transform"]
        calc = next(t for t in transform if "calculate" in t)
        # Forward-only jitter — random() * one day, no negative term.
        assert "random()" in calc["calculate"]
        assert "86400000" in calc["calculate"]
        assert "-" not in calc["calculate"]

    def test_tooltip_reads_the_preformatted_display_date(self):
        tooltip = to_vega(_model())["layer"][0]["encoding"]["tooltip"]
        completed = next(t for t in tooltip if t.get("title") == "Completed")
        # Nominal + the Python-preformatted field — Vega's temporal
        # formatter would shift the UTC date to browser-local.
        assert completed["field"] == "completed_at_display"
        assert completed["type"] == "nominal"
