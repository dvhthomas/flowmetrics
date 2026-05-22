"""Layer 3 — the CFD chart view.

`render()` orchestrates query -> stage inference -> model;
`to_vega()` translates a `CfdModel` into a Vega-Lite stacked-area
spec. No decisions here — every number comes from the model
(`flowmetrics.charts.cfd`).
"""

from __future__ import annotations

from typing import Any

import duckdb

from ...charts.cfd import CfdModel, build_cfd_model, infer_stage_order
from ...contract import WorkflowStates
from ...warehouse.queries import (
    first_stage_entries,
    observed_stages,
    pairwise_stage_precedence,
)
from ...windows import Window
from ._vega import to_vega


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
    # arrivals — Vacanti's CFD math made explicit.
    values: list[dict] = []
    for d in model.daily:
        for i, stage in enumerate(model.stages):
            cur = d.counts[stage]
            if i < len(model.stages) - 1:
                band_height = cur - d.counts[model.stages[i + 1]]
            else:
                band_height = cur
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

    # Thin axis labels to ~10 evenly-spaced ticks so wider windows
    # stay legible. Nominal-axis `labelOverlap` doesn't thin
    # reliably; pre-pick the dates that SHOULD show a label.
    # Ceiling-division step so 11–19 dates actually get thinned.
    step = max(1, (len(model.daily) + 9) // 10)
    axis_label_values = [d.date_iso for d in model.daily[::step]]

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

    spec: dict = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "background": "transparent",
        "padding": 12,
        "width": "container",
        "data": {"values": values},
        # `clip` keeps the bands inside the plot rectangle so
        # raising the y-floor slider crops them cleanly instead
        # of spilling the cumulative areas below the axis.
        "mark": {"type": "area", "opacity": 0.95, "clip": True},
        "encoding": {
            "x": {
                "field": "date_iso",
                "type": "nominal",
                # No outer padding — the cumulative area fills the
                # plot edge-to-edge.
                "scale": {"paddingOuter": 0},
                "axis": {
                    "title": "Date (UTC)",
                    "labelAngle": 0,
                    "values": axis_label_values,
                    "labelExpr": (
                        "utcFormat(datetime(datum.value), '%b %d')"
                    ),
                },
                "sort": [d.date_iso for d in model.daily],
            },
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
                    "scheme": "set3",
                },
                "legend": {"title": None, "orient": "top-right"},
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
            "tooltip": [
                {"field": "date_display", "type": "nominal", "title": "Date"},
                {"field": "stage", "type": "nominal", "title": "Stage"},
                {
                    "field": "wip",
                    "type": "quantitative",
                    "title": "WIP in stage",
                },
                {
                    "field": "cumulative",
                    "type": "quantitative",
                    "title": "Cumulative arrivals",
                },
            ],
        },
        "config": {
            "view": {"stroke": None},
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
    if floor_param is not None:
        spec["params"] = [floor_param]
    return spec
