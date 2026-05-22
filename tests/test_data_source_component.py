"""Layer 3 — tests for the Data Source view
(`flowmetrics.web.components.data_source`).

Decisions — 180-day cap, zero-fill, log-thirds level bucketing,
calendar layout, headline — are tested at Layer 2 in
`test_charts_data_source.py`. This file covers the view only:
`render()` wires query → model, and `to_vega()` faithfully
translates a model into the calendar-heatmap spec.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import duckdb

from flowmetrics.charts.data_source import build_data_source_model
from flowmetrics.web.components.data_source import render, to_vega


def _warehouse(items: list[tuple[datetime, datetime | None]]) -> duckdb.DuckDBPyConnection:
    """`items` is a list of (created_at, completed_at)."""
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE work_items ("
        "contract_id VARCHAR, created_at TIMESTAMP, completed_at TIMESTAMP)"
    )
    if items:
        con.executemany(
            "INSERT INTO work_items VALUES ('c', ?, ?)",
            [(c, d) for c, d in items],
        )
    return con


class TestRender:
    def test_render_counts_every_work_item_including_in_flight(self):
        # Two items: one completed, one in-flight. Both count.
        con = _warehouse([
            (datetime(2026, 1, 1, 10), datetime(2026, 1, 2)),
            (datetime(2026, 1, 1, 14), None),
        ])
        model = render(con, "c")
        assert model.total_records == 2

    def test_empty_warehouse_yields_empty_model(self):
        assert render(_warehouse([]), "c").is_empty


class TestToVega:
    def _model_with_days(self):
        per_day = [(date(2026, 1, 1), 1), (date(2026, 1, 5), 3)]
        return build_data_source_model(per_day)

    def test_spec_uses_a_rect_mark(self):
        spec = to_vega(self._model_with_days())
        assert spec["mark"]["type"] == "rect"

    def test_every_model_day_reaches_the_spec(self):
        model = self._model_with_days()
        spec = to_vega(model)
        assert len(spec["data"]["values"]) == len(model.days)

    def test_color_scale_domain_is_the_four_levels(self):
        spec = to_vega(self._model_with_days())
        assert spec["encoding"]["color"]["scale"]["domain"] == [
            "None", "Low", "Medium", "High",
        ]

    def test_chart_title_names_creation_date(self):
        spec = to_vega(self._model_with_days())
        assert spec["title"]["text"] == "Work Items by Creation Date"
        assert spec["encoding"]["x"]["axis"]["title"] == "Created Date"

    def test_subtitle_notes_the_180_day_cap(self):
        spec = to_vega(self._model_with_days())
        assert "180 days" in spec["title"]["subtitle"]

    def test_tooltip_carries_the_day_and_record_count(self):
        spec = to_vega(self._model_with_days())
        tooltip_fields = {t["field"] for t in spec["encoding"]["tooltip"]}
        assert tooltip_fields == {"label", "records"}
