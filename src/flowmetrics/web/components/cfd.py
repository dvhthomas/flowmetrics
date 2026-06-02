"""Layer 3 — the CFD chart view.

`render()` orchestrates query -> stage inference -> model;
`to_vega()` translates a `CfdModel` into a Vega-Lite stacked-area
spec. No decisions here — every number comes from the model
(`flowmetrics.charts.cfd`).
"""

from __future__ import annotations

import json
from typing import Any

import duckdb

from ...charts.cfd import (
    CfdModel,
    build_cfd_model,
    daily_flow_metrics,
    infer_stage_order,
)
from ...warehouse.queries import (
    first_stage_entries,
    observed_stages,
    pairwise_stage_precedence,
)
from ...windows import Window
from ...workflow import WorkflowStates
from ._vega import to_vega

# Categorical palette derived from the brand hue: nine pastels evenly
# spaced around the wheel at a shared saturation / lightness — distinct
# but thematically linked (brand green first). Defined as CSS theme
# tokens (`--cfd-1`…`--cfd-9`) so they resolve from the theme at embed
# time, like every other chart colour. Replaces Vega's off-brand "set3".
_CFD_BAND_TOKENS = [f"__theme:cfd-{i}__" for i in range(1, 10)]


def _palette_for_stages(stage_count: int) -> list[str]:
    """One theme token per stage, in workflow order. The terminal
    stage (last) always uses the muted `cfd-terminal` token so the
    active WIP bands above it visually dominate the read."""
    if stage_count <= 0:
        return []
    if stage_count == 1:
        return ["__theme:cfd-terminal__"]
    return [*_CFD_BAND_TOKENS[: stage_count - 1], "__theme:cfd-terminal__"]


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    states: WorkflowStates | None = None,
    view: Window | None = None,
) -> CfdModel:
    """Resolve stages (YAML if provided, otherwise infer by
    pairwise precedence), fetch first-stage entries, and build the
    model. `view` clamps the chart's x-axis to its intersection
    with the observed data span; the cumulative math stays
    full-history."""
    if states is not None:
        stages = states.cfd_bands()
    else:
        stages = infer_stage_order(
            pairwise_stage_precedence(con, contract_name),
            observed_stages(con, contract_name),
        )
    entries = first_stage_entries(
        con, contract_name,
        only_stages=stages if states is not None else None,
    )
    return build_cfd_model(entries, stages, view=view)


def cfd_daily_metrics_json(model: CfdModel) -> str:
    """Per-day flow metrics keyed by date_iso, as compact JSON — the
    Jinja global the CFD chart fragment embeds so the hover overlay can
    populate the per-day panel without another round-trip."""
    obj = {
        m.date_iso: {
            "date_display": m.date_display,
            "stages": m.wip_by_stage,
            "total_wip": m.total_wip,
            "arrivals": m.arrivals,
            "departures": m.departures,
            "throughput": round(m.throughput, 2),
            "avg_cycle_time": (
                round(m.avg_cycle_time, 1)
                if m.avg_cycle_time is not None else None
            ),
        }
        for m in daily_flow_metrics(model)
    }
    return json.dumps(obj, separators=(",", ":"))


@to_vega.register
def _cfd_to_vega(model: CfdModel) -> dict[str, Any]:
    """Translate a `CfdModel` into a Vega-Lite stacked-area spec.

    Long-form `values` rows: one per (date, stage) carrying the
    stage's WIP band height (cur - count_of_next_stage), so the
    stacked-area heights ADD up to the cumulative-arrivals figure
    without each band needing to know about other bands.
    """
    # Long-form values: one row per (date, stage). Each row carries
    # the band height for that stage (its cumulative minus the next
    # stage's cumulative; the terminal band's height is its full
    # cumulative). Stacking these adds back up to the cumulative
    # arrivals — standard CFD math made explicit.
    values: list[dict] = []
    for d in model.daily:
        for i, stage in enumerate(model.stages):
            cur = d.counts[stage]
            band_height = (
                cur - d.counts[model.stages[i + 1]]
                if i < len(model.stages) - 1
                else cur
            )
            values.append({
                "date_iso": d.date_iso,
                "date_display": d.date_display,
                "stage": stage,
                "cumulative": cur,
                "wip": max(0, band_height),
                # Stable per-stage sort key so Vega stacks in
                # workflow order regardless of dict iteration.
                "stage_order": i,
            })

    # Y-floor crop slider — present only when the model resolved
    # one (the window's left edge carries inert items to crop).
    floor_param: dict | None = None
    if model.crop is not None:
        floor_param = {
            "name": "cfdfloor",
            "value": model.crop.default,
            "bind": {
                "input": "range",
                "min": model.crop.floor,
                "max": model.crop.ceiling,
                "step": max(1, model.crop.ceiling // 100),
                "name": "Crop base — hide first N items  ",
            },
        }

    # x encoding is shared by every layer so the bands, the
    # arrival line, and the departure line all sit on the same
    # time axis. A `temporal` scale (continuous) is required for
    # Vega-Lite's scale-binding zoom; the default fills the plot
    # edge-to-edge without per-tick padding.
    x_encoding = {
        "field": "date_iso",
        "type": "temporal",
        "axis": {
            "title": "Date (UTC)",
            "labelAngle": 0,
            "tickCount": 10,
            "format": "%b %d",
            "formatType": "time",
        },
    }

    # Interval selection bound to scales — gives the chart
    # wheel-zoom + drag-pan along the time axis with no extra
    # wiring. Defined INSIDE the area layer (Vega-Lite throws a
    # "Duplicate signal name" if a scale-binding param sits at
    # the top of a layered spec; line layers inherit the shared
    # x scale and zoom in step with the area).
    zoom_param = {
        "name": "cfdzoom",
        "select": {"type": "interval", "encodings": ["x"]},
        "bind": "scales",
    }

    # Area layer — the existing stacked bands.
    area_layer = {
        # `clip` keeps the bands inside the plot rectangle so
        # raising the y-floor slider crops them cleanly instead
        # of spilling the cumulative areas below the axis.
        "mark": {"type": "area", "opacity": 0.95, "clip": True},
        "params": [zoom_param],
        "encoding": {
            "y": {
                "field": "wip",
                "type": "quantitative",
                "aggregate": "sum",
                "stack": "zero",
                "scale": (
                    {"domainMin": {"expr": "cfdfloor"}}
                    if floor_param else {}
                ),
                "axis": {"title": "Items"},
            },
            "color": {
                "field": "stage",
                "type": "nominal",
                "scale": {
                    "domain": list(model.stages),
                    "range": _palette_for_stages(len(model.stages)),
                },
                # Bottom-orient so the legend sits under the x-axis
                # (its own row, no overlap with the bands). The
                # detail page strips the legend entirely client-side
                # because the floating panel lists every stage.
                "legend": {
                    "title": None, "orient": "bottom",
                    "direction": "horizontal",
                },
            },
            # Explicit stack order — Vega-Lite's `order` ascending
            # means the lowest stack_order paints at the bottom of
            # the stack. Sort descending so workflow-first sits at
            # the top of the stack and terminal at the bottom.
            "order": {
                "field": "stage_order",
                "type": "quantitative",
                "sort": "descending",
            },
            # No per-band Vega tooltip — the hover side panel is the
            # readout (full per-day breakdown), so a second popup would
            # just clutter.
        },
    }

    layers: list[dict] = [area_layer]

    # Boundary lines bracketing the WIP zone. Top line (arrivals)
    # at cumulative-to-workflow-first; bottom of WIP (departures)
    # at cumulative-to-terminal. Their slopes are the arrival rate
    # and the throughput; the vertical gap between them at any date
    # is total WIP. Skip for a 1-stage model — both lines would
    # overplot.
    if len(model.stages) >= 2:
        terminal_order = len(model.stages) - 1
        # Arrival rate — slope of the top of the stack.
        layers.append({
            "transform": [{"filter": "datum.stage_order === 0"}],
            "mark": {
                "type": "line",
                "color": "__theme:cfd-line-arrival__",
                "strokeWidth": 2,
                "interpolate": "linear",
                "clip": True,
            },
            "encoding": {
                "y": {"field": "cumulative", "type": "quantitative"},
            },
        })
        # Throughput / departure rate — slope of the top of Done.
        layers.append({
            "transform": [
                {"filter": f"datum.stage_order === {terminal_order}"},
            ],
            "mark": {
                "type": "line",
                "color": "__theme:cfd-line-throughput__",
                "strokeWidth": 2,
                "interpolate": "linear",
                "clip": True,
            },
            "encoding": {
                "y": {"field": "cumulative", "type": "quantitative"},
            },
        })

    spec: dict = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "background": "transparent",
        "padding": 12,
        "width": "container",
        "data": {"values": values},
        "encoding": {"x": x_encoding},
        "layer": layers,
        # Without explicit sharing, Vega-Lite would build a separate
        # y-scale for area (field=wip, stacked sum) vs the lines
        # (field=cumulative) — and the hover overlay's view.scale("y")
        # would no longer match what the reader sees.
        "resolve": {"scale": {"y": "shared"}},
        "config": {
            # The plot rectangle paints dark. Anywhere a stacked
            # band paints over it disappears; anywhere ABOVE the
            # topmost band the dark fill shows through, framing the
            # active WIP region.
            "view": {"fill": "__theme:cfd-above__", "stroke": None},
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
    # The zoom param lives on the area layer (see comment above);
    # only the y-floor crop slider, when present, goes at the
    # top level.
    if floor_param is not None:
        spec["params"] = [floor_param]
    return spec
