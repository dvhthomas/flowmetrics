"""Layer 3 — the cycle-time chart view.

`render()` orchestrates query -> model (Layers 1 and 2);
`to_vega()` translates a `CycleTimeModel` into a Vega-Lite spec.
No decisions here — every number on the chart comes from the
model (`flowmetrics.charts.cycle_time`).
"""

from __future__ import annotations

from typing import Any

import duckdb

from ...charts.cycle_time import CycleTimeModel, build_cycle_time_model
from ...warehouse.queries import completed_items
from ...windows import Window
from ._vega import to_vega

# Chart colors are NOT defined in Python — they live as CSS tokens
# on `:root` (see `_base.html.jinja`) and are substituted into the
# spec at embed time by `window.applyTheme`. Python emits
# `__theme:<token>__` placeholders; the browser resolves them from
# the current CSS values. One theme change in CSS flows everywhere
# without touching Python.
#
# Per-percentile assignment — neutrals + ONE accent.
#   P50 — light gray  (soft reference, "typical")  → --border
#   P85 — primary plum (the commitment line — the headline
#         threshold this chart exists to evaluate against)
#   P95 — dark gray   (deep reference)             → --muted
# Plum is reserved for the page CTA (the import button) and the
# single most-meaningful chart accent (P85). Other references
# stay neutral so the eye finds the action and the threshold
# without competing colours.
_PCT_COLOR_P50 = "__theme:border__"
_PCT_COLOR_P85 = "__theme:p-500__"
_PCT_COLOR_P95 = "__theme:muted__"
# Scatter dots — neutral. The data cloud is supporting visual; the
# P85 line is the hero.
_SCATTER_COLOR = "__theme:muted__"


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    view: Window | None = None,
) -> CycleTimeModel:
    """Query completed items and resolve the cycle-time model.

    `view` clamps the scatter — and the P50/P85/P95 percentile
    sample — to its inclusive window; None uses the full
    materialised history.
    """
    return build_cycle_time_model(
        completed_items(con, contract_name), view=view
    )


@to_vega.register
def _cycle_time_to_vega(model: CycleTimeModel) -> dict[str, Any]:
    """Translate a `CycleTimeModel` into a Vega-Lite layered spec.

    Three layers: the scatter points, then the P50/P85/P95
    reference rule + its text label. Mechanical translation — the
    cap bounds, tick interval and x-domain are all read off the
    model.

    `view.fill: "transparent"` is load-bearing for the zoom
    interaction: without it, the view's background rect doesn't
    capture pointer events over empty plot area, so wheel/drag
    zoom is dead unless the cursor is exactly on a data point.
    """
    points_data = [
        {
            "item_id": p.item_id,
            "title": p.title,
            "url": p.url,
            "completed_at": p.completed_at,
            "completed_at_display": p.completed_at_display,
            "cycle_time_days": p.cycle_time_days,
        }
        for p in model.points
    ]
    pct = model.percentiles
    reference_rows = [
        {"y": pct.p50, "label": f"P50 ({pct.p50:.1f}d)", "pct": "P50"},
        {"y": pct.p85, "label": f"P85 ({pct.p85:.1f}d)", "pct": "P85"},
        {"y": pct.p95, "label": f"P95 ({pct.p95:.1f}d)", "pct": "P95"},
    ]

    # X scale + tick interval come straight from the model.
    x_scale: dict[str, Any] = (
        {"type": "utc", "domain": list(model.x_domain)}
        if model.x_domain is not None
        else {"type": "utc"}
    )
    x_tick_count = {"interval": model.ticks.interval, "step": model.ticks.step}

    # Y-axis cap slider — present only when the model resolved one
    # (≥ 2 items and P95 below the slowest). It FILTERS the dots so
    # the y-axis auto-scales to what's shown.
    cap_param: dict | None = None
    cap_filter: dict | None = None
    if model.cap is not None:
        cap_param = {
            "name": "cyclecap",
            # Default = max → the chart opens showing ALL data;
            # the operator drags down to exclude outliers.
            "value": model.cap.default,
            "bind": {
                "input": "range",
                "min": model.cap.floor,
                "max": model.cap.ceiling,
                "step": 1,
                "name": "Max cycle time shown (days)  ",
            },
        }
        cap_filter = {"filter": "datum.cycle_time_days <= cyclecap"}

    scatter_layer = {
        "mark": {
            "type": "point",
            "filled": True,
            "opacity": 0.7,
            "size": 80,
            "color": _SCATTER_COLOR,
            # Signal clickability — the fragment script navigates
            # to the item's lifecycle page on click.
            "cursor": "pointer",
        },
        # Data lives on this layer (not top-level) so the rule/text
        # reference layers — each with their own 2-row dataset — don't
        # have the top-level x-encoding (`field: completed_at`) bleed
        # into them via inheritance. Without per-layer data isolation,
        # the text layer silently fails to render its P50/P85 labels
        # because it tries to look up a nonexistent `completed_at`
        # field in the percentile-row data.
        "data": {"values": points_data},
        # Intra-day jitter: spread dots within their date column so
        # same-day items don't stack on a single x line. Forward
        # offset only — `random() * 86400000` in [0, 86_400_000)
        # ms. Column convention (operator's mental model): a "May
        # 04" dot lives in [May 04 tick, May 05 tick); jittering
        # forward keeps it strictly to the right of its tick label
        # and never inside the previous date's column.
        # Cap filter (when present) drops dots above the slider
        # value BEFORE the jitter calculate; the y-axis then
        # auto-scales to what remains.
        "transform": [
            *([cap_filter] if cap_filter else []),
            {
                "calculate": (
                    "time(datum.completed_at) + random() * 86400000"
                ),
                "as": "completed_at_jittered",
            },
        ],
        # Wheel/drag zoom on the date (x) axis only — the y axis is
        # owned by the cap slider. The interval selection must live
        # on this layer (a top-level selection on a layered spec
        # produces duplicate per-layer signals); the cap is a plain
        # value param and goes top-level.
        "params": [
            {
                "name": "cycle_zoom",
                "select": {"type": "interval", "encodings": ["x"]},
                "bind": "scales",
            },
        ],
        "encoding": {
            "x": {
                # The chart positions dots by the jittered field
                # (produced by the calculate transform above); the
                # tooltip still reads the raw `completed_at` so the
                # date the user sees on hover is the honest date.
                "field": "completed_at_jittered",
                "type": "temporal",
                "scale": x_scale,
                "axis": {
                    # "(UTC)" annotation is load-bearing for honesty:
                    # the data and the tooltip both speak in UTC,
                    # and a viewer in PT shouldn't assume "May 04"
                    # means May 04 *local*. See
                    # `flowmetrics.utc_dates` for the rationale.
                    "title": "Completion date (UTC)",
                    "titleFontWeight": "bold",
                    "format": "%b %d",
                    "labelAngle": 0,
                    # Span-adaptive tick interval (resolved by the
                    # chart model). Pins ticks to whole-day/week/
                    # month boundaries so Vega never auto-picks a
                    # sub-day granularity (which repeats the "%b %d"
                    # label) and a multi-month window never draws a
                    # gridline per day.
                    "tickCount": x_tick_count,
                    "labelOverlap": "parity",
                },
            },
            "y": {
                "field": "cycle_time_days",
                "type": "quantitative",
                # Auto domain — it re-fits whatever survives the cap
                # filter, so the visible dots always fill the plot.
                "scale": {"zero": True},
                "axis": {
                    "title": "Cycle time (days)",
                    "titleFontWeight": "bold",
                },
            },
            "tooltip": [
                {"field": "title", "title": "Title"},
                {
                    # Read the Python-pre-formatted display string,
                    # NOT the raw date as `type: temporal`. Vega's
                    # temporal formatter renders in browser-local
                    # time, which shifts a UTC May 04 to "May 03"
                    # for a PT viewer. Nominal means Vega prints
                    # the literal string we passed.
                    "field": "completed_at_display",
                    "type": "nominal",
                    "title": "Completed",
                },
                {
                    "field": "cycle_time_days",
                    "title": "Cycle (days)",
                    "format": ".1f",
                },
            ],
        },
    }
    # One rule layer carrying all three percentile lines (data is a
    # 3-row array). Same shape for the text labels. Consolidating
    # reduces layer count, which avoids a Vega-Lite codegen
    # collision (duplicate signal names) hit when each percentile
    # had its own layer. Colour escalates with percentile; P85 is
    # the canonical commitment threshold — solid + thicker stroke,
    # P50/P95 dashed.
    rule_layer = {
        "data": {"values": reference_rows},
        "mark": {"type": "rule"},
        "encoding": {
            "y": {"field": "y", "type": "quantitative"},
            "color": {
                "field": "pct",
                "type": "ordinal",
                "scale": {
                    "domain": ["P50", "P85", "P95"],
                    "range": [_PCT_COLOR_P50, _PCT_COLOR_P85, _PCT_COLOR_P95],
                },
                "legend": None,
            },
            "strokeDash": {
                "condition": {"test": "datum.pct === 'P85'", "value": [1, 0]},
                "value": [6, 4],
            },
            "size": {
                "condition": {"test": "datum.pct === 'P85'", "value": 2.5},
                "value": 1.5,
            },
        },
    }
    text_layer = {
        "data": {"values": reference_rows},
        "mark": {
            "type": "text",
            "align": "left",
            "baseline": "bottom",
            "dx": 6,
            "dy": -3,
            "fontSize": 11,
            "fontWeight": "bold",
        },
        "encoding": {
            "y": {"field": "y", "type": "quantitative"},
            "text": {"field": "label"},
            "color": {
                "field": "pct",
                "type": "ordinal",
                "scale": {
                    "domain": ["P50", "P85", "P95"],
                    "range": [_PCT_COLOR_P50, _PCT_COLOR_P85, _PCT_COLOR_P95],
                },
                "legend": None,
            },
        },
    }

    spec: dict[str, Any] = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 360,
        "padding": {"top": 10, "right": 20, "bottom": 30, "left": 50},
        "view": {"fill": "transparent", "stroke": None},
        # No top-level data and no top-level encoding. Each layer
        # owns both, and Vega-Lite shares scales across layers by
        # default (same field name → same scale). The scatter
        # layer defines the x and y scales; rule/text layers
        # reference the same y scale by name via their own y
        # encoding.
        "layer": [scatter_layer, rule_layer, text_layer],
        "config": {
            "font": (
                '-apple-system, BlinkMacSystemFont, "Segoe UI", '
                "Roboto, sans-serif"
            ),
        },
    }
    # The cap value param is top-level: the y scale is shared
    # across layers, so a param scoped to one layer would be out
    # of scope for the filter's `cyclecap` signal.
    if cap_param is not None:
        spec["params"] = [cap_param]
    return spec
