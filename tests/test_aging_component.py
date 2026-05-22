"""Layer 3 — tests for the aging-WIP view
(`flowmetrics.web.components.aging`).

The chart DECISIONS — age, the WIP filter, percentile thresholds,
the empty-state classification, the cap bounds, column order and
WIP-count badges — are tested at Layer 2 in
`test_charts_aging.py`, with no warehouse and no Vega. This file
covers the view only:

  - `render()` wires query (Layer 1) → model (Layer 2);
  - `to_vega()` faithfully translates a model into a Vega-Lite
    spec — the model's numbers reach the spec.
"""

from __future__ import annotations

from datetime import date, datetime

import duckdb

from flowmetrics.charts.aging import build_aging_model
from flowmetrics.contract import WorkflowStates
from flowmetrics.warehouse.queries import CompletedItem, InFlightItem
from flowmetrics.web.components.aging import render, to_vega

ASOF = date(2026, 6, 1)


def _inflight(n: int, created: date, state: str = "Review") -> InFlightItem:
    return InFlightItem(
        item_id=f"#{n}",
        title=f"item {n}",
        url=None,
        created_at=datetime(created.year, created.month, created.day, 12),
        current_state=state,
    )


def _completed(n: int, completed: date, cycle: float) -> CompletedItem:
    return CompletedItem(
        item_id=f"c{n}",
        title=f"c{n}",
        url=None,
        completed_at=datetime(completed.year, completed.month, completed.day, 12),
        cycle_time_days=cycle,
    )


def _model():
    """A non-empty aging model with a cap control resolved — two
    states and one ancient outlier so the cap has range to crop."""
    in_flight = [_inflight(i, date(2026, 5, 1), "Review") for i in range(1, 16)]
    in_flight += [_inflight(i, date(2026, 5, 1), "Draft") for i in range(16, 21)]
    in_flight.append(_inflight(99, date(2024, 1, 1), "Review"))
    completed = [_completed(i, date(2026, 5, 1), float(i * 5)) for i in range(1, 11)]
    return build_aging_model(
        in_flight, completed, asof=ASOF, open_item_count=5,
    )


def _warehouse() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(
        """CREATE TABLE work_items (
            contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
            title VARCHAR, url VARCHAR,
            created_at TIMESTAMP, completed_at TIMESTAMP,
            cycle_time_days DOUBLE)"""
    )
    con.execute(
        """CREATE TABLE transitions (
            contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
            entered_at TIMESTAMP, stage VARCHAR, signal VARCHAR)"""
    )
    con.executemany(
        "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?)",
        [
            ("c", "github", "#1", "one", None,
             datetime(2026, 5, 1), None, None),
            ("c", "github", "#2", "two", None,
             datetime(2026, 5, 2), None, None),
            ("c", "github", "#3", "three", None,
             datetime(2026, 5, 1), datetime(2026, 5, 10), 9.0),
        ],
    )
    con.executemany(
        "INSERT INTO transitions VALUES (?,?,?,?,?,?)",
        [
            ("c", "github", "#1", datetime(2026, 5, 2), "Review", "ready"),
            ("c", "github", "#2", datetime(2026, 5, 3), "Draft", "open"),
        ],
    )
    return con


def _layer_marks(spec: dict) -> list[str]:
    return [
        lyr["mark"]["type"] if isinstance(lyr["mark"], dict) else lyr["mark"]
        for lyr in spec["layer"]
    ]


class TestRenderWiresQueryToModel:
    def test_render_returns_a_model_from_the_warehouse(self):
        model = render(_warehouse(), "c", asof=ASOF)
        assert model.count == 2  # #1 + #2 open; #3 completed

    def test_render_applies_the_wip_filter(self):
        states = WorkflowStates(wip=("Review",), done=("Merged",))
        model = render(_warehouse(), "c", asof=ASOF, states=states)
        assert [i.current_state for i in model.items] == ["Review"]

    def test_unknown_contract_yields_an_empty_model(self):
        assert render(_warehouse(), "absent", asof=ASOF).is_empty


class TestToVegaStructure:
    def test_spec_has_dot_badge_and_rule_layers(self):
        assert _layer_marks(to_vega(_model())) == ["point", "text", "rule"]

    def test_dots_reach_the_dot_layer(self):
        model = _model()
        dots = to_vega(model)["layer"][0]["data"]["values"]
        assert len(dots) == model.count

    def test_dots_are_coloured_by_workflow_state(self):
        dot_layer = to_vega(_model())["layer"][0]
        assert dot_layer["encoding"]["color"]["field"] == "current_state"

    def test_wip_badges_reach_the_badge_layer(self):
        model = _model()
        badges = to_vega(model)["layer"][1]["data"]["values"]
        labels = {b["current_state"]: b["label"] for b in badges}
        assert labels == {
            state: f"WIP {count}" for state, count in model.wip_badges
        }

    def test_percentile_values_reach_the_rule_layer(self):
        model = _model()
        rules = to_vega(model)["layer"][2]["data"]["values"]
        ys = {r["label"]: r["age_days"] for r in rules}
        pct = model.percentiles
        assert ys == {"P50": pct.p50, "P85": pct.p85, "P95": pct.p95}

    def test_rule_layer_dropped_without_a_percentile_sample(self):
        # No completed items → 0/0/0 percentiles → no threshold
        # rules (three rules stacked on y=0 read as a real line).
        model = build_aging_model(
            [_inflight(1, date(2026, 5, 1)), _inflight(2, date(2026, 5, 1))],
            [], asof=ASOF, open_item_count=5,
        )
        assert "rule" not in _layer_marks(to_vega(model))


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
        assert any("agecap" in str(f) for f in filters)

    def test_no_cap_param_when_the_model_has_none(self):
        model = build_aging_model(
            [_inflight(1, ASOF)], [], asof=ASOF, open_item_count=5,
        )
        assert model.cap is None
        assert "params" not in to_vega(model)


class TestToVegaInteraction:
    def test_zoom_is_bound_to_the_x_axis_only(self):
        dot_layer = to_vega(_model())["layer"][0]
        zoom = next(p for p in dot_layer["params"] if p.get("bind") == "scales")
        assert zoom["select"]["encodings"] == ["x"]
