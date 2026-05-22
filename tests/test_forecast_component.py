"""Layer 3 — tests for the forecast views
(`flowmetrics.web.components.forecast`).

Decisions — daily-throughput derivation, Monte Carlo orchestration,
percentile extraction (forward vs. backward), the headline — are
tested at Layer 2 in `test_charts_forecast.py`, and the underlying
simulation primitives are tested in `test_forecast.py`. This file
covers the view only: `render_*` wires query → model, and
`to_vega()` faithfully translates a model into a Vega-Lite spec.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from flowmetrics.charts.forecast import (
    build_how_many_model,
    build_when_done_model,
)
from flowmetrics.warehouse.queries import CompletedItem
from flowmetrics.web.components.forecast import (
    render_how_many,
    render_when_done,
    to_vega,
)


def _completed(n: int, when: date) -> CompletedItem:
    return CompletedItem(
        item_id=f"#{n}", title=f"t{n}", url=None,
        completed_at=datetime(when.year, when.month, when.day, 12),
        cycle_time_days=1.0,
    )


def _busy_history(days: int = 30) -> list[CompletedItem]:
    return [
        _completed(i, date(2026, 1, 1) + timedelta(days=i))
        for i in range(days)
    ]


def _warehouse() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(
        """CREATE TABLE work_items (
            contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
            title VARCHAR, url VARCHAR,
            created_at TIMESTAMP, completed_at TIMESTAMP,
            cycle_time_days DOUBLE)"""
    )
    rows = [
        ("c", "github", f"#{i}", f"t{i}", None,
         datetime(2026, 1, 1) + timedelta(days=i - 1),
         datetime(2026, 1, 1) + timedelta(days=i), 1.0)
        for i in range(1, 31)
    ]
    con.executemany("INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?)", rows)
    return con


class TestRenderWhenDone:
    def test_render_returns_a_model_from_the_warehouse(self):
        model = render_when_done(
            _warehouse(), "c",
            items=10, start_date=date(2026, 2, 1), runs=500, seed=0,
        )
        assert not model.is_empty
        assert model.daily_throughput_n_days > 0

    def test_empty_warehouse_yields_empty_model(self):
        empty = duckdb.connect(":memory:")
        empty.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE)"""
        )
        model = render_when_done(
            empty, "c",
            items=10, start_date=date(2026, 2, 1), runs=100,
        )
        assert model.is_empty


class TestRenderHowMany:
    def test_render_returns_a_model_from_the_warehouse(self):
        model = render_how_many(
            _warehouse(), "c",
            start_date=date(2026, 2, 1), end_date=date(2026, 2, 10),
            runs=500, seed=0,
        )
        assert not model.is_empty


class TestToVegaWhenDone:
    def _spec(self):
        model = build_when_done_model(
            _busy_history(30), backlog=10,
            start_date=date(2026, 2, 1), runs=500, seed=0,
        )
        return model, to_vega(model)

    def test_spec_layers_bar_rule_text(self):
        _, spec = self._spec()
        marks = [
            lyr["mark"]["type"] if isinstance(lyr["mark"], dict) else lyr["mark"]
            for lyr in spec["layer"]
        ]
        assert marks == ["bar", "rule", "text"]

    def test_histogram_reaches_the_bar_layer(self):
        model, spec = self._spec()
        bars = spec["layer"][0]
        assert len(bars["data"]["values"]) == len(model.histogram)

    def test_percentile_anchors_reach_the_rule_layer(self):
        model, spec = self._spec()
        rule_rows = spec["layer"][1]["data"]["values"]
        anchors = {r["label"]: r["anchor"] for r in rule_rows}
        assert anchors == {
            "P50": model.p50_iso, "P85": model.p85_iso, "P95": model.p95_iso,
        }


class TestToVegaHowMany:
    def _spec(self):
        model = build_how_many_model(
            _busy_history(30),
            start_date=date(2026, 2, 1), end_date=date(2026, 2, 10),
            runs=500, seed=0,
        )
        return model, to_vega(model)

    def test_spec_has_quantitative_x_axis(self):
        _, spec = self._spec()
        bars = spec["layer"][0]
        assert bars["encoding"]["x"]["type"] == "quantitative"

    def test_percentile_anchors_reach_the_rule_layer(self):
        model, spec = self._spec()
        rule_rows = spec["layer"][1]["data"]["values"]
        anchors = {r["label"]: r["anchor"] for r in rule_rows}
        assert anchors == {"P50": model.p50, "P85": model.p85, "P95": model.p95}

    def test_percentile_rows_use_high_confidence_floor_framing(self):
        model, _ = self._spec()
        assert all("≥" in r["value_display"] for r in model.percentile_rows)
