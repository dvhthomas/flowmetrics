"""Layer 3 — the forecast chart views.

Two Monte Carlo charts. `render_when_done` / `render_how_many`
orchestrate query → model; `to_vega` translates either model into
a Vega-Lite histogram spec with P50/P85/P95 threshold rules.
Decisions live in `flowmetrics.charts.forecast`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import duckdb

from ...charts.forecast import (
    _PCT_COLOR_P50,
    _PCT_COLOR_P85,
    _PCT_COLOR_P95,
    DEFAULT_RUNS,
    HowManyModel,
    WhenDoneModel,
    build_how_many_model,
    build_when_done_model,
)
from ...warehouse.queries import completed_items
from ...windows import Window
from ._vega import to_vega

# Histogram bar colour — neutral so the percentile rules pop.
_BAR_COLOR = "__theme:border__"


def render_when_done(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    items: int,
    start_date: date,
    runs: int = DEFAULT_RUNS,
    seed: int = 0,
    reference: Window | None = None,
) -> WhenDoneModel:
    """Query completed items and resolve a "when done" model."""
    return build_when_done_model(
        completed_items(con, contract_name),
        backlog=items,
        start_date=start_date,
        runs=runs,
        seed=seed,
        reference=reference,
    )


def render_how_many(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    start_date: date,
    end_date: date,
    runs: int = DEFAULT_RUNS,
    seed: int = 0,
    reference: Window | None = None,
) -> HowManyModel:
    """Query completed items and resolve a "how many" model."""
    return build_how_many_model(
        completed_items(con, contract_name),
        start_date=start_date,
        end_date=end_date,
        runs=runs,
        seed=seed,
        reference=reference,
    )


@to_vega.register
def _when_done_to_vega(model: WhenDoneModel) -> dict[str, Any]:
    # Pre-thin x-axis labels (nominal axes don't auto-thin in
    # Vega-Lite). Target ~10 visible labels; ceiling division so
    # the step grows past 1 for 11–19 bars.
    date_isos = [b["date_iso"] for b in model.histogram]
    target = 10
    if len(date_isos) > target:
        step = (len(date_isos) + target - 1) // target
        axis_values: list | None = date_isos[::step]
    else:
        axis_values = None
    return _histogram_spec(
        values=[
            {"date_iso": b["date_iso"], "count": b["count"]}
            for b in model.histogram
        ],
        x_field="date_iso",
        x_type="nominal",
        x_title="Completion date (UTC)",
        x_format_expr="utcFormat(datetime(datum.value), '%b %d')",
        x_axis_values=axis_values,
        pcts=[
            {"label": "P50", "anchor": model.p50_iso, "color": _PCT_COLOR_P50},
            {"label": "P85", "anchor": model.p85_iso, "color": _PCT_COLOR_P85},
            {"label": "P95", "anchor": model.p95_iso, "color": _PCT_COLOR_P95},
        ],
        pct_field="anchor",
        pct_field_type="nominal",
    )


@to_vega.register
def _how_many_to_vega(model: HowManyModel) -> dict[str, Any]:
    return _histogram_spec(
        values=[
            {"count": b["count"], "runs": b["runs"]}
            for b in model.histogram
        ],
        x_field="count",
        x_type="quantitative",
        x_title="Items completed in window",
        x_format_expr=None,
        pcts=[
            {"label": "P50", "anchor": model.p50, "color": _PCT_COLOR_P50},
            {"label": "P85", "anchor": model.p85, "color": _PCT_COLOR_P85},
            {"label": "P95", "anchor": model.p95, "color": _PCT_COLOR_P95},
        ],
        pct_field="anchor",
        pct_field_type="quantitative",
        y_field="runs",
    )


def _histogram_spec(
    *,
    values: list[dict],
    x_field: str,
    x_type: str,
    x_title: str,
    x_format_expr: str | None,
    pcts: list[dict],
    pct_field: str,
    pct_field_type: str,
    y_field: str = "count",
    x_axis_values: list | None = None,
) -> dict[str, Any]:
    """Shared Vega-Lite spec: histogram bars + P50/P85/P95 rules.
    Both forecast charts use this."""
    x_axis: dict = {
        "title": x_title,
        "labelAngle": 0,
        "grid": False,
    }
    if x_format_expr:
        x_axis["labelExpr"] = x_format_expr
    if x_axis_values is not None:
        x_axis["values"] = x_axis_values

    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "background": "transparent",
        "padding": 12,
        "width": "container",
        "layer": [
            {
                "data": {"values": values},
                "mark": {
                    "type": "bar", "color": _BAR_COLOR, "cornerRadius": 1,
                },
                "encoding": {
                    "x": {"field": x_field, "type": x_type, "axis": x_axis},
                    "y": {
                        "field": y_field,
                        "type": "quantitative",
                        "axis": {"title": "Simulations", "format": "d"},
                    },
                    "tooltip": [
                        {"field": x_field, "type": x_type, "title": x_title},
                        {
                            "field": y_field,
                            "type": "quantitative",
                            "title": "Simulations",
                        },
                    ],
                },
            },
            {
                "data": {"values": pcts},
                "mark": {"type": "rule", "size": 2.5},
                "encoding": {
                    "x": {
                        "field": pct_field,
                        "type": pct_field_type,
                        # Anchor at band CENTER for nominal/ordinal
                        # axes. `band` is a no-op for quantitative
                        # scales, so safe to set unconditionally.
                        "band": 0.5,
                    },
                    "color": {
                        "field": "label",
                        "type": "nominal",
                        "scale": {
                            "domain": ["P50", "P85", "P95"],
                            "range": [
                                _PCT_COLOR_P50,
                                _PCT_COLOR_P85,
                                _PCT_COLOR_P95,
                            ],
                        },
                        "legend": None,
                    },
                    # Distinct dash patterns per percentile so even
                    # when two rules coincide (a common case with
                    # small backlogs) each line is identifiable.
                    "strokeDash": {
                        "field": "label",
                        "type": "nominal",
                        "scale": {
                            "domain": ["P50", "P85", "P95"],
                            "range": [[2, 3], [6, 4], [12, 5]],
                        },
                        "legend": None,
                    },
                    # Pixel-level horizontal offset per percentile —
                    # when two rules share the same x value they'd
                    # otherwise pixel-overlap.
                    "xOffset": {
                        "field": "label",
                        "type": "nominal",
                        "scale": {
                            "domain": ["P50", "P85", "P95"],
                            "range": [-4, 0, 4],
                        },
                        "legend": None,
                    },
                    "tooltip": [
                        {
                            "field": "label", "type": "nominal",
                            "title": "Percentile",
                        },
                        {
                            "field": pct_field,
                            "type": pct_field_type,
                            "title": "At",
                        },
                    ],
                },
            },
            # Inline labels on each rule so the chart names what
            # each line is without relying on a legend.
            {
                "data": {"values": pcts},
                "mark": {
                    "type": "text",
                    "baseline": "bottom",
                    "dy": -4,
                    "fontSize": 11,
                    "fontWeight": 600,
                },
                "encoding": {
                    "x": {
                        "field": pct_field, "type": pct_field_type,
                        "band": 0.5,
                    },
                    "y": {"value": 0},
                    "text": {"field": "label", "type": "nominal"},
                    "color": {
                        "field": "label",
                        "type": "nominal",
                        "scale": {
                            "domain": ["P50", "P85", "P95"],
                            "range": [
                                _PCT_COLOR_P50,
                                _PCT_COLOR_P85,
                                _PCT_COLOR_P95,
                            ],
                        },
                        "legend": None,
                    },
                    "xOffset": {
                        "field": "label",
                        "type": "nominal",
                        "scale": {
                            "domain": ["P50", "P85", "P95"],
                            "range": [-12, 0, 12],
                        },
                        "legend": None,
                    },
                },
            },
        ],
        "config": {
            "view": {"fill": None, "stroke": None},
            "axis": {
                "labelFont": "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif",
                "titleFont": "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif",
                "labelColor": "__theme:fg__",
                "titleColor": "__theme:muted__",
            },
        },
    }
