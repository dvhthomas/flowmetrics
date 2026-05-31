"""Layer 3 — tests for the CFD view
(`flowmetrics.web.components.cfd`).

The chart DECISIONS — stage inference, the Vacanti cumulative
invariants, the visual-window clamping, the y-floor crop bounds,
the headline — are tested at Layer 2 in `test_charts_cfd.py`,
with no DuckDB and no Vega. This file covers the view only:

  - `render()` wires queries (Layer 1) → model (Layer 2);
  - `to_vega()` faithfully translates a `CfdModel` into a
    Vega-Lite spec — the model's numbers reach the spec.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from flowmetrics.charts.cfd import build_cfd_model
from flowmetrics.contract import WorkflowStates
from flowmetrics.warehouse.queries import StageEntry
from flowmetrics.web.components.cfd import render, to_vega
from flowmetrics.windows import Window


def _entry(item_id: str, stage: str, d: date) -> StageEntry:
    return StageEntry(item_id=item_id, stage=stage, entered_date=d)


def _model_with_crop():
    """A non-empty CFD model whose visual window starts partway in,
    so a y-floor crop control is resolved."""
    entries: list[StageEntry] = []
    for i in range(1, 6):
        entries.append(_entry(f"#{i}", "A", date(2026, 1, i)))
        entries.append(_entry(f"#{i}", "B", date(2026, 1, i)))
    return build_cfd_model(
        entries, ("A", "B"),
        view=Window(from_=date(2026, 1, 3), to=date(2026, 1, 5)),
    )


def _warehouse() -> duckdb.DuckDBPyConnection:
    """An in-memory transitions table — enough for render() to do
    pairwise stage inference and fetch first entries."""
    con = duckdb.connect(":memory:")
    con.execute(
        """CREATE TABLE transitions (
            contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
            entered_at TIMESTAMP, stage VARCHAR, signal VARCHAR)"""
    )
    rows = []
    # Three items, all flowing Draft → Awaiting Review → Merged.
    for i, item in enumerate(("#1", "#2", "#3"), start=1):
        rows += [
            ("c", "github", item,
             datetime(2026, 1, i), "Draft", "open"),
            ("c", "github", item,
             datetime(2026, 1, i + 1), "Awaiting Review", "ready"),
            ("c", "github", item,
             datetime(2026, 1, i + 2), "Merged", "merge"),
        ]
    con.executemany(
        "INSERT INTO transitions VALUES (?,?,?,?,?,?)", rows,
    )
    return con


class TestRenderWiresQueryToModel:
    def test_render_returns_a_model_from_the_warehouse(self):
        model = render(_warehouse(), "c")
        assert not model.is_empty
        # Inferred order: Draft → Awaiting Review → Merged.
        assert model.stages == ("Draft", "Awaiting Review", "Merged")

    def test_render_passes_the_view_window_through(self):
        model = render(
            _warehouse(), "c",
            view=Window(from_=date(2026, 1, 3), to=date(2026, 1, 5)),
        )
        assert model.first_date_iso == "2026-01-03"
        assert model.last_date_iso == "2026-01-05"

    def test_explicit_states_pin_the_stage_order(self):
        states = WorkflowStates(
            wip=("Awaiting Review",), done=("Merged",),
        )
        model = render(_warehouse(), "c", states=states)
        assert model.stages == ("Awaiting Review", "Merged")

    def test_unknown_contract_yields_an_empty_model(self):
        assert render(_warehouse(), "absent").is_empty


class TestToVegaStructure:
    def test_spec_uses_a_clipped_stacked_area(self):
        spec = to_vega(_model_with_crop())
        # The bands now live in the first layer of a layered spec
        # (the Vacanti boundary lines stack on top).
        mark = spec["layer"][0]["mark"]
        assert mark["type"] == "area"
        assert mark["clip"] is True

    def test_stage_color_domain_matches_the_model(self):
        model = _model_with_crop()
        spec = to_vega(model)
        area = spec["layer"][0]
        assert area["encoding"]["color"]["scale"]["domain"] == list(model.stages)

    def test_x_scale_fills_edge_to_edge(self):
        # The cumulative area fills the plot edge-to-edge: a `point`
        # scale anchors first / last data points at the plot extremes
        # (a `band` scale would leave half-bandwidth empty strips),
        # and `padding: 0` strips any remaining slack.
        spec = to_vega(_model_with_crop())
        x_scale = spec["encoding"]["x"]["scale"]
        assert x_scale["type"] == "point"
        assert x_scale["padding"] == 0

    def test_axis_labels_are_thinned_to_about_ten(self):
        # 31-day window → labels thinned (no one-label-per-day wash).
        entries = [
            _entry(f"#{i}", "A", date(2026, 1, 1) + timedelta(days=i))
            for i in range(31)
        ]
        spec = to_vega(build_cfd_model(entries, ("A",)))
        labels = spec["encoding"]["x"]["axis"]["values"]
        assert 0 < len(labels) <= 11

    def test_daily_data_reaches_the_spec_data(self):
        model = _model_with_crop()
        spec = to_vega(model)
        # One row per (date, stage).
        expected = len(model.daily) * len(model.stages)
        assert len(spec["data"]["values"]) == expected


class TestToVegaCrop:
    def test_crop_becomes_a_floor_param_when_present(self):
        model = _model_with_crop()
        assert model.crop is not None
        spec = to_vega(model)
        floor = next(
            p for p in spec["params"]
            if isinstance(p.get("bind"), dict)
            and p["bind"].get("input") == "range"
        )
        assert floor["bind"]["min"] == model.crop.floor
        assert floor["bind"]["max"] == model.crop.ceiling
        assert floor["value"] == model.crop.default
        # The y scale's domainMin follows the floor param. The y
        # encoding now lives on the area layer; the shared y-scale
        # (resolve: shared) propagates the floor to the line layers.
        area_y_scale = spec["layer"][0]["encoding"]["y"]["scale"]
        assert area_y_scale["domainMin"] == {"expr": floor["name"]}

    def test_no_floor_param_when_model_has_no_crop(self):
        entries = [_entry("#1", "A", date(2026, 1, 1))]
        model = build_cfd_model(entries, ("A",))
        assert model.crop is None
        assert "params" not in to_vega(model)


class TestBoundaryLines:
    """The spec is layered: a stacked area for the bands, plus two
    cumulative lines bracketing the WIP zone — the top boundary is
    cumulative arrivals (slope = arrival rate), the bottom of WIP
    is cumulative departures (slope = throughput). Standard CFD
    reads."""

    def test_spec_layers_are_area_then_arrival_then_departure(self):
        spec = to_vega(_model_with_crop())
        layers = spec["layer"]
        assert len(layers) == 3
        marks = [layer["mark"]["type"] for layer in layers]
        assert marks == ["area", "line", "line"]

    def test_arrival_line_uses_cumulative_at_workflow_first_stage(self):
        # Top of stack = cumulative arrivals to the first stage = the
        # row whose stage_order is 0.
        spec = to_vega(_model_with_crop())
        arrival = spec["layer"][1]
        assert arrival["transform"] == [
            {"filter": "datum.stage_order === 0"},
        ]
        assert arrival["encoding"]["y"]["field"] == "cumulative"

    def test_departure_line_uses_cumulative_at_the_terminal_stage(self):
        # Top of Done = cumulative departures = the row whose
        # stage_order is the last (terminal) stage.
        model = _model_with_crop()
        terminal_order = len(model.stages) - 1
        spec = to_vega(model)
        departure = spec["layer"][2]
        assert departure["transform"] == [
            {"filter": f"datum.stage_order === {terminal_order}"},
        ]
        assert departure["encoding"]["y"]["field"] == "cumulative"

    def test_boundary_lines_each_use_a_themed_colour(self):
        # Theme tokens so the boundary lines pick up the brand
        # palette via applyTheme, like every other chart colour.
        spec = to_vega(_model_with_crop())
        for line in spec["layer"][1:]:
            color = line["mark"]["color"]
            assert color.startswith("__theme:"), color

    def test_arrival_and_departure_lines_are_distinct_colours(self):
        # The reader needs to be able to tell the arrival slope
        # apart from the throughput slope without a legend.
        spec = to_vega(_model_with_crop())
        arrival_color = spec["layer"][1]["mark"]["color"]
        departure_color = spec["layer"][2]["mark"]["color"]
        assert arrival_color != departure_color

    def test_boundary_lines_use_dedicated_bright_tokens_not_brand_shades(self):
        # The lines need to pop against the pastel WIP bands AND
        # the dark "above" zone. Generic `p-700`/`t-700` blend with
        # the band hues they sit on top of, so they reach for
        # purpose-built tokens that are tuned for contrast and
        # don't move when the brand palette is retuned.
        spec = to_vega(_model_with_crop())
        assert (spec["layer"][1]["mark"]["color"]
                == "__theme:cfd-line-arrival__")
        assert (spec["layer"][2]["mark"]["color"]
                == "__theme:cfd-line-throughput__")

    def test_layered_spec_shares_the_y_scale(self):
        # The area's y = sum(wip) (stack: zero) and the line layers'
        # y = cumulative carry different field names. Without an
        # explicit `resolve.scale.y = "shared"`, Vega-Lite would
        # build a separate y-scale per layer and the hover overlay's
        # `view.scale("y")` would no longer match the visible chart.
        spec = to_vega(_model_with_crop())
        assert spec.get("resolve", {}).get("scale", {}).get("y") == "shared"

    def test_single_stage_model_omits_boundary_lines(self):
        # Degenerate workflow: terminal only, no WIP zone exists.
        # Drawing arrival and departure boundaries would just
        # overplot one line, so the spec falls back to area-only.
        single = build_cfd_model(
            [_entry("#1", "A", date(2026, 1, 1))], ("A",),
        )
        spec = to_vega(single)
        assert len(spec["layer"]) == 1
        assert spec["layer"][0]["mark"]["type"] == "area"
