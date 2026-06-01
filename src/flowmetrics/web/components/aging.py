"""Layer 3 — the aging-WIP chart view.

`render()` orchestrates query -> model (Layers 1 and 2);
`to_vega()` translates an `AgingModel` into a Vega-Lite spec. No
decisions here — every number on the chart comes from the model
(`flowmetrics.charts.aging`).
"""

from __future__ import annotations

import random
from datetime import date
from typing import Any

import duckdb

from ...charts.aging import AgingModel, build_aging_model
from ...contract import WorkflowStates
from ...warehouse.queries import (
    completed_items,
    count_open_items,
    in_flight_snapshot,
)
from ...windows import Window
from ._vega import to_vega

# Chart colors are CSS-theme-driven; see _base.html.jinja's
# `flowmetricsTheme` for resolved values. The percentile rules
# keep the neutrals + P85 accent shared with the cycle-time
# chart; the dots themselves are coloured per workflow state
# (a categorical scheme) so each column is easy to read.
_PCT_COLOR_P50 = "__theme:border__"
_PCT_COLOR_P85 = "__theme:p-500__"
_PCT_COLOR_P95 = "__theme:muted__"


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    asof: date,
    states: WorkflowStates | None = None,
    reference: Window | None = None,
    ptile_min: int = 0,
    ptile_max: int = 100,
) -> AgingModel:
    """Query the in-flight snapshot + completed items and resolve
    the aging-WIP model.

    `asof` is the in-flight snapshot date — the caller pins it to
    the latest materialise (the warehouse holds one snapshot, so
    aging can only be faithfully computed at that date).
    `states.wip` restricts the chart to WIP states; `reference`
    windows the percentile sample.

    `ptile_min` / `ptile_max` (0..100) narrow the scatter to
    in-flight items whose AGE rank falls inside that band — the
    page-level Percentile Filter slider. The reference lines
    stay computed from the FULL completed-items sample so the
    bands don't shift while the user drags.
    """
    model = build_aging_model(
        in_flight_snapshot(con, contract_name, asof),
        completed_items(con, contract_name),
        asof=asof,
        open_item_count=count_open_items(con, contract_name),
        reference=reference,
        wip_states=frozenset(states.wip) if states is not None else None,
    )
    if ptile_min <= 0 and ptile_max >= 100:
        return model
    from dataclasses import replace

    from ...charts.ptile_filter import filter_by_rank
    kept = filter_by_rank(
        list(model.items),
        key=lambda it: it.age_days,
        ptile_min=ptile_min,
        ptile_max=ptile_max,
    )
    if len(kept) == len(model.items):
        return model
    # Splice "showing N of total" into the headline's count phrase
    # so the count reflects the filtered scatter without rebuilding
    # the rest (percentile thresholds + reference window stay).
    full_count = len(model.items)
    visible = len(kept)
    headline = model.headline.replace(
        f"{full_count} in-flight item{'' if full_count == 1 else 's'}",
        f"{visible} of {full_count} in-flight items shown",
        1,
    )
    return replace(
        model,
        items=tuple(kept),
        count=visible,
        headline=headline,
    )


@to_vega.register
def _aging_to_vega(model: AgingModel) -> dict[str, Any]:
    """Translate an `AgingModel` into a Vega-Lite layered spec —
    point marks per in-flight item, a top-pinned "WIP N" header
    row, and the P50/P85/P95 threshold rules. Mechanical: the cap
    bounds, column order and badge counts are read off the model.

    Y-axis quantitative (age_days). X-axis nominal (current_state),
    with forward jitter so dots in a column don't collapse onto a
    single line.
    """
    item_values = [
        {
            "item_id": i.item_id,
            "title": i.title,
            "url": i.url,
            "current_state": i.current_state,
            "age_days": i.age_days,
        }
        for i in model.items
    ]
    # Canonical Vega-Lite jitter (`point_offset_random`): a
    # deterministic quantitative xOffset field. Combined with a
    # band-scale x, Vega auto-fits the offset to the band's actual
    # width at render time — no pixel range baked in.
    rng = random.Random(0)
    for v in item_values:
        v["_jitter"] = rng.random()

    ordered_states = list(model.ordered_states)

    pct = model.percentiles
    pct_values = [
        {"label": "P50", "age_days": pct.p50, "color": _PCT_COLOR_P50},
        {"label": "P85", "age_days": pct.p85, "color": _PCT_COLOR_P85},
        {"label": "P95", "age_days": pct.p95, "color": _PCT_COLOR_P95},
    ]

    # Per-state "WIP N" header labels — one per column, pinned at
    # a constant height above the dots (not floating at each
    # column's tallest dot).
    badge_values = [
        {"current_state": s, "label": f"WIP {n}"}
        for s, n in model.wip_badges
    ]

    # Y-axis cap slider — present only when the model resolved one.
    # FILTERS the dots so the y-axis auto-scales to what's shown;
    # the percentile rules are a separate layer, unaffected.
    cap_param: dict | None = None
    cap_filter: dict | None = None
    if model.cap is not None:
        cap_param = {
            "name": "agecap",
            "value": model.cap.default,
            "bind": {
                "input": "range",
                "min": model.cap.floor,
                "max": model.cap.ceiling,
                "step": 1,
                "name": "Cap (d) ",
            },
        }
        cap_filter = {"filter": "datum.age_days <= agecap"}

    spec: dict = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "background": "transparent",
        "padding": 12,
        "width": "container",
        # The dot layer colours by `current_state`; the rule layer
        # colours P50/P85/P95. Vega-Lite shares colour scales
        # across layers by default — which would merge these two
        # unrelated scales. Resolve colour independently.
        "resolve": {"scale": {"color": "independent"}},
        "layer": [
            # In-flight item dots — painted FIRST so the threshold
            # rules above sit on top against the dot cloud.
            {
                # Wheel/drag zoom bound to scales. `params` lives
                # on this (data-bearing) layer — a top-level
                # selection on a layered spec makes duplicate
                # per-layer signals. Zoom is x-only; the y axis is
                # the cap slider's.
                "params": [
                    {
                        "name": "aging_zoom",
                        "select": {
                            "type": "interval",
                            "encodings": ["x"],
                        },
                        "bind": "scales",
                    },
                ],
                "data": {"values": item_values},
                # Cap filter (when present) drops dots older than
                # the slider value; the y-axis auto-scales to what
                # remains.
                "transform": [cap_filter] if cap_filter else [],
                "mark": {
                    "type": "point",
                    "filled": True,
                    "size": 90,
                    "opacity": 0.85,
                    # Signal clickability (the fragment script
                    # navigates to the item's lifecycle page).
                    "cursor": "pointer",
                },
                "encoding": {
                    "x": {
                        "field": "current_state",
                        "type": "nominal",
                        # Explicit `band` scale — the nominal+xOffset
                        # default is `point`, which sticks each label
                        # at its tick with no room for jitter.
                        "scale": {
                            "type": "band",
                            "paddingInner": 0.1,
                            "paddingOuter": 0.1,
                        },
                        "axis": {"title": "Current state", "labelAngle": 0},
                        "sort": ordered_states,
                    },
                    "xOffset": {
                        # Canonical `point_offset_random` — a
                        # quantitative offset with no explicit
                        # scale.range; Vega fits it to the band.
                        "field": "_jitter",
                        "type": "quantitative",
                    },
                    # Colour each dot by its workflow state so a
                    # viewer can tell its column even mid-zoom. The
                    # x-axis already labels columns, so no legend.
                    "color": {
                        "field": "current_state",
                        "type": "nominal",
                        "scale": {"scheme": "tableau10"},
                        "legend": None,
                    },
                    "y": {
                        "field": "age_days",
                        "type": "quantitative",
                        # Floored at 0 (age can't go negative);
                        # domainMax auto-fits whatever survives the
                        # cap filter, with headroom for the
                        # top-pinned "WIP N" header.
                        "scale": {"domainMin": 0},
                        "axis": {"title": "Age (days)"},
                    },
                    "tooltip": [
                        {"field": "item_id", "type": "nominal", "title": "#"},
                        {"field": "title", "type": "nominal", "title": "Title"},
                        {
                            "field": "current_state",
                            "type": "nominal",
                            "title": "State",
                        },
                        {
                            "field": "age_days",
                            "type": "quantitative",
                            "title": "Age (d)",
                        },
                    ],
                },
            },
            # Per-state "WIP N" header — pinned to the TOP of the
            # chart (`y: {value: 0}` is a fixed pixel position) in
            # the headroom band above the dots.
            {
                "data": {"values": badge_values},
                "mark": {
                    "type": "text",
                    "baseline": "top",
                    "dy": 3,
                    "fontSize": 11,
                    "fontWeight": 600,
                    "color": "__theme:muted__",
                },
                "encoding": {
                    "x": {
                        "field": "current_state",
                        "type": "nominal",
                        "sort": ordered_states,
                    },
                    "y": {"value": 0},
                    "text": {"field": "label", "type": "nominal"},
                },
            },
            # Percentile threshold rules — painted AFTER the dots +
            # badges so they sit on top. The colour-with-legend
            # encoding labels P50/P85/P95 without anchoring text
            # marks at the chart's right edge.
            {
                "data": {"values": pct_values},
                "mark": {
                    "type": "rule",
                    "size": 2.5,
                    "strokeDash": [5, 3],
                },
                "encoding": {
                    "y": {"field": "age_days", "type": "quantitative"},
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
                        "legend": {
                            "title": None,
                            "orient": "top-right",
                            "symbolType": "stroke",
                            "symbolStrokeWidth": 2.5,
                        },
                    },
                    "tooltip": [
                        {
                            "field": "label",
                            "type": "nominal",
                            "title": "Threshold",
                        },
                        {
                            "field": "age_days",
                            "type": "quantitative",
                            "title": "Days",
                            "format": ".1f",
                        },
                    ],
                },
            },
        ],
        "config": {
            "view": {"fill": "__theme:bg__", "stroke": None},
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
    # The cap value param is top-level: the y scale is shared
    # across layers, so a layer-scoped param would be out of scope
    # for the filter's `agecap` signal.
    if cap_param is not None:
        spec["params"] = [cap_param]

    # No completions in the reference window → percentiles are
    # 0/0/0. Drop the rule layer entirely; three dashed rules
    # stacked on y=0 read as a real threshold.
    if pct.source_count == 0:
        spec["layer"] = [
            lyr
            for lyr in spec["layer"]
            if not (
                isinstance(lyr.get("mark"), dict)
                and lyr["mark"].get("type") == "rule"
            )
        ]
    return spec
