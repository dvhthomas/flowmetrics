"""Vega-Lite spec generators for the interactive HTML charts.

A spec is a JSON-serializable Python dict that Vega-Embed renders in
the browser. Keeping the spec generators here (alongside the PNG-
producing matplotlib code in html_renderer.py) means the two chart
paths share data sources but have independent rendering machinery.

See docs/SPEC-github-labels.md and the vendored static/README.md for
why we embed Vega-Lite for offline use.
"""

from __future__ import annotations

from typing import Any

from ..report import AgingReport

_VEGA_LITE_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"


def aging_spec(report: AgingReport) -> dict[str, Any]:
    """Build the Vega-Lite spec for an Aging chart.

    Layered spec:
    - One `circle` layer plotting each in-flight item by
      (current_state, age_days), with tooltip + href channels.
    - One `rule` layer per non-zero percentile (50/70/85/95) drawing
      a horizontal threshold across the chart.

    The X-axis sort honours the user-supplied workflow ordering;
    Vega-Lite's default alphabetical sort would scramble it.
    """
    values = [
        {
            "item_id": it.item_id,
            "title": it.title,
            "current_state": it.current_state,
            "age_days": it.age_days,
            "pr_url": it.pr_url,
        }
        for it in report.items
    ]

    # Per-state count + share labels — restored from the matplotlib
    # version. Sit at the top of each workflow column so each pile is
    # named (e.g. "172 · 30%"). Total = items rendered on the chart.
    total = len(report.items) or 1
    state_counts: dict[str, int] = {state: 0 for state in report.input.workflow}
    for it in report.items:
        if it.current_state in state_counts:
            state_counts[it.current_state] += 1
    per_state_count_rows = [
        {
            "current_state": state,
            "count": count,
            "label": f"{count} · {round(100 * count / total)}%",
        }
        for state, count in state_counts.items()
    ]

    circle_layer: dict[str, Any] = {
        "mark": {"type": "circle", "size": 60, "opacity": 0.5},
        "data": {"values": values},
        # Per-item random offset for the xOffset channel. Without jitter,
        # all items in a column would stack vertically at the band centre;
        # with it, they fan out within the band so density is legible.
        "transform": [{"calculate": "random()", "as": "jitter"}],
        # Interval selection bound to scales gives drag-to-pan and
        # scroll-to-zoom on the Y axis. Restricted to `y` because the X
        # axis is nominal (workflow states); zooming categories has no
        # meaning. Lives on this layer (not top-level) because top-level
        # params on a layered spec produce per-layer copies that clash
        # on signal names — "Duplicate signal name: zoom_tuple" at
        # runtime. Scales remain shared across layers by Vega-Lite
        # default, so the zoom affects the percentile rules too.
        "params": [
            {
                "name": "zoom",
                "select": {"type": "interval", "encodings": ["y"]},
                "bind": "scales",
            }
        ],
        "encoding": {
            "x": {
                "field": "current_state",
                "type": "nominal",
                "axis": {"title": "WIP Stage", "labelAngle": 0,
                         "titleFontWeight": "bold", "titlePadding": 8},
                "sort": list(report.input.workflow),
                # Force every workflow state to appear on the axis, even
                # if the data has zero items in it. Otherwise `sort`
                # alone restricts the domain to states that have data,
                # which makes an empty column look like a missing column.
                "scale": {"domain": list(report.input.workflow)},
            },
            "y": {
                "field": "age_days",
                "type": "quantitative",
                "axis": {"title": "Age (days)"},
            },
            "xOffset": {
                "field": "jitter",
                "type": "quantitative",
            },
            "tooltip": [
                {"field": "item_id", "title": "ID"},
                {"field": "title", "title": "Title"},
                {"field": "current_state", "title": "State"},
                {"field": "age_days", "title": "Age (d)"},
            ],
            "href": {"field": "pr_url", "type": "nominal"},
        },
    }

    # Consolidate all four threshold values into one dataset so the
    # rule layer can carry a single color/text encoding across them.
    # Yellow→red sequential palette: P50 reads as "still ok", P95 reads
    # as "danger". Matches the headline's "past P85/P95" risk framing.
    #
    # Thresholds above ~1.5x the highest in-flight age are dropped so
    # the chart fits the actual data. Otherwise a P95 that's far above
    # the in-flight range (common in long-tail backlogs) stretches the
    # Y axis 5x and wastes most of the canvas.
    max_age = max((it.age_days for it in report.items), default=0)
    y_cap = max_age * 1.5 if max_age > 0 else float("inf")
    percentile_rows = [
        {"pct": f"P{p}", "y": v, "label": f"P{p} ({v:.1f}d)"}
        for p, v in sorted(report.cycle_time_percentiles.items())
        if 0 < v <= y_cap
    ]

    # Faint alternating background shade per workflow column — every
    # other column tinted so the eye can locate stages. Even-indexed
    # columns (0, 2, 4...) are shaded; leftmost is gently emphasized.
    workflow_list = list(report.input.workflow)
    shaded_states = workflow_list[::2]
    shade_layer = {
        "mark": {"type": "rect", "color": "#1a1a1a", "opacity": 0.04},
        "data": {"values": [{"state": s} for s in shaded_states]},
        "encoding": {
            "x": {
                "field": "state",
                "type": "nominal",
                "sort": workflow_list,
                "scale": {"domain": workflow_list},
            },
        },
    }

    rule_layers: list[dict[str, Any]] = []
    if percentile_rows:
        rule_layers.append(
            {
                # P85 is the forecast threshold in Vacanti's framing —
                # solid + heavier so it stands out. Other percentiles
                # stay dashed and lighter. Conditional encodings on
                # `strokeDash` and `size` keep them in one layer (one
                # color legend, one tooltip rule).
                "mark": {"type": "rule"},
                "data": {"values": percentile_rows},
                "encoding": {
                    "y": {"field": "y", "type": "quantitative"},
                    "color": {
                        "field": "pct",
                        "type": "ordinal",
                        "sort": ["P50", "P70", "P85", "P95"],
                        "scale": {"scheme": "yelloworangered"},
                        "legend": {
                            "title": "Cycle-time percentile",
                            "orient": "right",
                        },
                    },
                    "strokeDash": {
                        "condition": {
                            "test": "datum.pct === 'P85'",
                            "value": [1, 0],  # solid
                        },
                        "value": [6, 4],
                    },
                    "size": {
                        "condition": {
                            "test": "datum.pct === 'P85'",
                            "value": 4,
                        },
                        "value": 2,
                    },
                    "tooltip": [{"field": "label", "title": "Threshold"}],
                },
            }
        )
        # Direct text labels alongside the rules so the reader doesn't
        # have to consult the legend to know which line is which.
        rule_layers.append(
            {
                "mark": {
                    "type": "text",
                    "align": "left",
                    "baseline": "bottom",
                    "dx": 4,
                    "dy": -3,
                    "fontSize": 11,
                    "fontWeight": "bold",
                },
                "data": {"values": percentile_rows},
                "encoding": {
                    "y": {"field": "y", "type": "quantitative"},
                    "text": {"field": "label"},
                    "color": {
                        "field": "pct",
                        "type": "ordinal",
                        "sort": ["P50", "P70", "P85", "P95"],
                        "scale": {"scheme": "yelloworangered"},
                        "legend": None,  # legend already lives on the rule layer
                    },
                },
            }
        )

    # Per-state header layer — count + share above each column. Uses
    # `value` for absolute y positioning at the top of the chart canvas
    # (independent of any data point). Axis NOT suppressed here: when
    # layers share a scale, an `axis: None` on any layer can kill the
    # shared axis. Let this layer inherit the circle layer's axis.
    header_layer = {
        "mark": {
            "type": "text",
            "baseline": "bottom",
            "fontWeight": "bold",
            "fontSize": 12,
            "dy": -6,
        },
        "data": {"values": per_state_count_rows},
        "encoding": {
            "x": {
                "field": "current_state",
                "type": "nominal",
                "sort": list(report.input.workflow),
                "scale": {"domain": list(report.input.workflow)},
            },
            "y": {"value": 0},
            "text": {"field": "label"},
        },
    }

    return {
        "$schema": _VEGA_LITE_SCHEMA,
        "width": "container",
        "height": 360,
        "padding": {"top": 24, "bottom": 8, "left": 8, "right": 8},
        # Explicit background + view config so the chart sits on plain
        # white regardless of Vega's default theme. Without this the
        # color scheme on the rules can bleed a subtle tint through
        # the view background — clean Tufte chart needs neither.
        "background": "transparent",
        "config": {
            "view": {"fill": None, "stroke": "#e5e5e5", "strokeWidth": 1},
        },
        # Layer order: column shade → percentile rules → circles →
        # per-state header labels. Shade paints first (behind
        # everything); rules paint behind dots; dots on top.
        "layer": [shade_layer, *rule_layers, circle_layer, header_layer],
    }
