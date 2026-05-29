"""Layer 3 — the throughput chart view.

`render()` orchestrates query -> model (Layers 1 and 2);
`to_vega()` translates a `ThroughputModel` into a Vega-Lite spec.
No decisions here — every number comes from the model
(`flowmetrics.charts.throughput`).
"""

from __future__ import annotations

from typing import Any

import duckdb

from ...charts.throughput import ThroughputModel, build_throughput_model
from ...warehouse.queries import completed_items
from ...windows import Window
from ._vega import to_vega


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    view: Window | None = None,
) -> ThroughputModel:
    """Query completed items and resolve the throughput model.
    `view` clamps the x-axis (and the rate's divisor) to its
    inclusive window; None uses the full materialised history."""
    return build_throughput_model(
        completed_items(con, contract_name), view=view,
    )


@to_vega.register
def _throughput_to_vega(model: ThroughputModel) -> dict[str, Any]:
    """Translate a `ThroughputModel` into a Vega-Lite layered spec.

    Four layers stacked under the bars + on top:
      1. A faint rect over weekend columns.
      2. A heavier rect over `missing` columns (gaps).
      3. The bars themselves — covered days only.
      4. A "—" em-dash text mark on uncovered days so they read
         as gaps without hovering.
    """
    # Neutral gray. The dashboard's coloured accent budget is spent
    # on the import-button CTA and the cycle-time P85 commitment
    # line; throughput bars stay monochrome.
    bar_color = "__theme:muted__"

    values = [
        {
            "date_iso": d.date_iso,
            "date_display": d.date_display,
            "count": d.count,
            "day_type": d.day_type,
            "data_coverage": d.data_coverage,
        }
        for d in model.daily
    ]

    # Pin the x-axis ordering to the ascending date sequence from
    # the data. Without an explicit `sort` array on every x
    # encoding, a layered chart's domain is built in layer-scan
    # order — the weekend-shade layer is scanned first and its
    # filtered Sat/Sun subset ends up at the LEFT of the axis.
    date_order = [d.date_iso for d in model.daily]

    # Pre-thin axis labels for long windows. Nominal `labelOverlap`
    # is a no-op in Vega-Lite, so pick ~10 evenly-spaced ticks.
    axis_config: dict = {
        "title": "Completion date (UTC)",
        "labelAngle": 0,
        # `utcFormat` (not `timeFormat`) renders the date ignoring
        # browser TZ — same TZ-safety contract the tooltip
        # nominal-pre-format idiom enforces.
        "labelExpr": "utcFormat(datetime(datum.value), '%b %d')",
    }
    if len(date_order) > 10:
        step = (len(date_order) + 9) // 10  # ceil(n/10)
        axis_config["values"] = date_order[::step]

    x_encoding = {
        "field": "date_iso",
        "type": "nominal",
        "axis": axis_config,
        "sort": date_order,
    }

    # Reference-band layers — only drawn when the model has them
    # (i.e. at least one warehouse-covered day). Both modes (include
    # weekends / weekdays only) are emitted as separate rows in a
    # tiny inline dataset; a Vega `param` toggle picks which mode is
    # visible via a transform filter — no re-render or refetch.
    band_layers: list[dict[str, Any]] = []
    band_params: list[dict[str, Any]] = []
    if model.reference is not None:
        def _band_row(mode: str, label: str, value: float) -> dict[str, Any]:
            # Inline label carries the value — "P50: 3.0" — so the
            # reader doesn't have to mentally project from a
            # gridline to the y-axis.
            return {
                "mode": mode, "label": label, "value": value,
                "text": f"{label}: {value:.1f}",
            }

        band_values: list[dict[str, Any]] = [
            _band_row("include_weekends", "P50",
                      model.reference.include_weekends.p50),
            _band_row("include_weekends", "P85",
                      model.reference.include_weekends.p85),
        ]
        if model.reference.weekdays_only is not None:
            band_values.extend([
                _band_row("weekdays_only", "P50",
                          model.reference.weekdays_only.p50),
                _band_row("weekdays_only", "P85",
                          model.reference.weekdays_only.p85),
            ])

        band_params.append({
            "name": "weekendsMode",
            "value": "include_weekends",
            "bind": {
                "input": "select",
                "options": ["include_weekends", "weekdays_only"],
                "labels": ["Include weekends", "Weekdays only"],
                "name": "Reference band: ",
            },
        })
        band_layers.extend([
            # P50/P85 horizontal rule marks. The filter keeps just
            # the two rows matching the current toggle.
            {
                "data": {"values": band_values},
                "transform": [{"filter": "datum.mode === weekendsMode"}],
                "mark": {"type": "rule", "strokeDash": [4, 3]},
                "encoding": {
                    "y": {"field": "value", "type": "quantitative"},
                    "color": {
                        "field": "label", "type": "nominal",
                        "scale": {
                            "domain": ["P50", "P85"],
                            "range": ["__theme:border__", "__theme:p-500__"],
                        },
                        "legend": None,
                    },
                    "tooltip": [
                        {"field": "label", "type": "nominal",
                         "title": "Percentile"},
                        {"field": "value", "type": "quantitative",
                         "title": "Items/day", "format": ".1f"},
                    ],
                },
            },
            # Per-rule label ("P50", "P85") pinned at the right
            # edge of the chart. Same filter as the rule layer.
            {
                "data": {"values": band_values},
                "transform": [{"filter": "datum.mode === weekendsMode"}],
                "mark": {
                    "type": "text",
                    "align": "right",
                    "baseline": "bottom",
                    "dx": -4, "dy": -2,
                    "fontSize": 10,
                    "fontWeight": 600,
                },
                "encoding": {
                    "x": {"value": {"expr": "width"}},
                    "y": {"field": "value", "type": "quantitative"},
                    "text": {"field": "text", "type": "nominal"},
                    "color": {
                        "field": "label", "type": "nominal",
                        "scale": {
                            "domain": ["P50", "P85"],
                            "range": ["__theme:muted__", "__theme:p-500__"],
                        },
                        "legend": None,
                    },
                },
            },
        ])

    spec: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "background": "transparent",
        "padding": 12,
        "data": {"values": values},
        "width": "container",
        "layer": [
            # Faint background rect on weekend columns. Drawn under
            # the bars (first layer). Spans full y-range.
            {
                "transform": [
                    {"filter": "datum.day_type === 'weekend'"},
                ],
                "mark": {
                    "type": "rect",
                    "color": "__theme:muted__",
                    "opacity": 0.08,
                },
                "encoding": {"x": x_encoding},
            },
            # Heavier rect on `missing` (uncovered) columns so a
            # no-data day is visually distinct from a real zero.
            {
                "transform": [
                    {"filter": "datum.data_coverage !== 'warehouse'"},
                ],
                "mark": {
                    "type": "rect",
                    "color": "__theme:border__",
                    "opacity": 0.55,
                },
                "encoding": {
                    "x": x_encoding,
                    "tooltip": [
                        {
                            "field": "date_display",
                            "type": "nominal",
                            "title": "Completed",
                        },
                        {
                            "field": "data_coverage",
                            "type": "nominal",
                            "title": "Data",
                        },
                    ],
                },
            },
            # Throughput bars — covered days only. A height-0 bar
            # on a covered day is a true zero (single-pixel sliver),
            # distinguishable from the empty `missing` column above.
            {
                "transform": [
                    {"filter": "datum.data_coverage === 'warehouse'"},
                ],
                "mark": {
                    "type": "bar",
                    "color": bar_color,
                    "cornerRadius": 2,
                },
                "encoding": {
                    "x": x_encoding,
                    "y": {
                        "field": "count",
                        "type": "quantitative",
                        "axis": {
                            "title": "Items completed",
                            "tickMinStep": 1,
                            "format": "d",
                        },
                    },
                    "tooltip": [
                        {
                            "field": "date_display",
                            "type": "nominal",
                            "title": "Completed",
                        },
                        {
                            "field": "count",
                            "type": "quantitative",
                            "title": "Items",
                        },
                    ],
                },
            },
            # "no data" marker for uncovered days — a small em-dash
            # at the baseline; tells true zero from gap without
            # hovering.
            {
                "transform": [
                    {"filter": "datum.data_coverage !== 'warehouse'"},
                ],
                "mark": {
                    "type": "text",
                    "text": "—",
                    "baseline": "bottom",
                    "dy": -2,
                    "color": "__theme:muted__",
                    "fontSize": 11,
                },
                "encoding": {"x": x_encoding, "y": {"value": 0}},
            },
            *band_layers,
        ],
        "config": {
            "view": {"fill": None, "stroke": None},
            "axis": {
                "labelFont": (
                    "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif"
                ),
                "titleFont": (
                    "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif"
                ),
                "labelColor": "__theme:fg__",
                "titleColor": "__theme:muted__",
            },
        },
    }
    if band_params:
        spec["params"] = band_params
    return spec
