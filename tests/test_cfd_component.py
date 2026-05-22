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
        mark = spec["mark"]
        assert mark["type"] == "area"
        assert mark["clip"] is True

    def test_stage_color_domain_matches_the_model(self):
        model = _model_with_crop()
        spec = to_vega(model)
        assert spec["encoding"]["color"]["scale"]["domain"] == list(model.stages)

    def test_x_scale_has_no_outer_padding(self):
        # Cumulative area fills the plot edge-to-edge.
        spec = to_vega(_model_with_crop())
        assert spec["encoding"]["x"]["scale"]["paddingOuter"] == 0

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
        # The y scale's domainMin follows the floor param.
        assert spec["encoding"]["y"]["scale"]["domainMin"] == {
            "expr": floor["name"],
        }

    def test_no_floor_param_when_model_has_no_crop(self):
        entries = [_entry("#1", "A", date(2026, 1, 1))]
        model = build_cfd_model(entries, ("A",))
        assert model.crop is None
        assert "params" not in to_vega(model)
