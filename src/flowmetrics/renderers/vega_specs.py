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

from ..report import AgingReport, CfdReport, EfficiencyReport, HowManyReport, WhenDoneReport

_VEGA_LITE_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"


def _fe_band(pct: float, is_bot: bool) -> str:
    """FE-band classifier — matches the matplotlib chart's color logic.
    Three risk bands plus a bot bucket. Bot PRs are excluded from the
    'is the team slow' read."""
    if is_bot:
        return "bot"
    if pct < 10:
        return "< 10% (slow)"
    if pct < 50:
        return "10–50%"
    return "≥ 50% (healthy)"


def _forecast_histogram_spec(
    *,
    histogram_rows: list[dict[str, Any]],
    percentile_rule_rows: list[dict[str, Any]],
    x_field: str,
    x_type: str,
    x_title: str,
) -> dict[str, Any]:
    """Shared spec body for the when-done (temporal X) and how-many
    (quantitative X) histograms. Bars colored neutral; percentile
    rule lines yellow→red with P85 solid + heavier, matching the
    Aging convention."""
    bar_layer = {
        "mark": {"type": "bar", "color": "#2b7cff", "opacity": 0.65},
        "data": {"values": histogram_rows},
        "encoding": {
            "x": {
                "field": "outcome",
                "type": x_type,
                "axis": {"title": x_title, "titleFontWeight": "bold"},
            },
            "y": {
                "field": "frequency",
                "type": "quantitative",
                "axis": {"title": "Simulation frequency", "titleFontWeight": "bold"},
            },
            "tooltip": [
                {"field": "outcome", "type": x_type, "title": x_title,
                 **({"format": "%b %d, %Y"} if x_type == "temporal" else {})},
                {"field": "frequency", "title": "Runs"},
            ],
        },
    }

    rule_layer = {
        "mark": {"type": "rule"},
        "data": {"values": percentile_rule_rows},
        "encoding": {
            "x": {"field": "x", "type": x_type},
            "color": {
                "field": "pct",
                "type": "ordinal",
                "sort": ["P50", "P70", "P85", "P95"],
                "scale": {"scheme": "yelloworangered"},
                "legend": {"title": "Confidence", "orient": "right"},
            },
            "strokeDash": {
                "condition": {"test": "datum.pct === 'P85'", "value": [1, 0]},
                "value": [6, 4],
            },
            "size": {
                "condition": {"test": "datum.pct === 'P85'", "value": 4},
                "value": 2,
            },
            "tooltip": [{"field": "label", "title": "Confidence"}],
        },
    }

    return {
        "$schema": _VEGA_LITE_SCHEMA,
        "width": "container",
        "height": 320,
        "background": "transparent",
        "config": {"view": {"fill": None, "stroke": "#e5e5e5", "strokeWidth": 1}},
        "layer": [bar_layer, rule_layer],
    }


def when_done_spec(report: WhenDoneReport) -> dict[str, Any]:
    """When-done forecast histogram: vertical bars over completion
    dates, percentile rules at each confidence threshold. Read
    FORWARD — higher confidence ⇒ later date."""
    histogram_rows = [
        {"outcome": d.isoformat(), "frequency": report.histogram.counts[d]}
        for d in report.histogram.sorted_keys
    ]
    percentile_rule_rows = [
        {
            "pct": f"P{p}",
            "x": report.percentiles[p].isoformat(),
            "label": f"P{p} ({report.percentiles[p].isoformat()})",
        }
        for p in (50, 70, 85, 95)
    ]
    return _forecast_histogram_spec(
        histogram_rows=histogram_rows,
        percentile_rule_rows=percentile_rule_rows,
        x_field="outcome",
        x_type="temporal",
        x_title="Completion date",
    )


def how_many_spec(report: HowManyReport) -> dict[str, Any]:
    """How-many forecast histogram: vertical bars over item counts,
    percentile rules at each confidence threshold. Read BACKWARD —
    higher confidence ⇒ FEWER items."""
    histogram_rows = [
        {"outcome": int(n), "frequency": report.histogram.counts[n]}
        for n in report.histogram.sorted_keys
    ]
    percentile_rule_rows = [
        {"pct": f"P{p}", "x": int(report.percentiles[p]),
         "label": f"P{p} ({report.percentiles[p]} items)"}
        for p in (50, 70, 85, 95)
    ]
    return _forecast_histogram_spec(
        histogram_rows=histogram_rows,
        percentile_rule_rows=percentile_rule_rows,
        x_field="outcome",
        x_type="quantitative",
        x_title="Item count",
    )


def aging_distribution_spec(report: AgingReport) -> dict[str, Any]:
    """Horizontal histogram of in-flight items per percentile band.

    Replaces the earlier stacked-100% bar: when one band dominates
    (e.g. 94% above P95), the smaller bands collapse into illegible
    slivers. A simple horizontal bar chart keeps each band's count
    proportional to the count, not to the share, so a 5-item band is
    visible next to a 365-item band.

    Single sequential color scheme (YlOrRd) preserves the percentile
    severity gradient while keeping the chart visually unified.
    """
    from ..aging import compute_aging_distribution

    dist = compute_aging_distribution(report.items, report.cycle_time_percentiles)
    band_order = [b["label"] for b in dist]
    values = [
        {
            "band": b["label"],
            "count": b["count"],
            "share": b["share"],
        }
        for b in dist
    ]

    bar_layer = {
        "mark": {"type": "bar"},
        "data": {"values": values},
        "encoding": {
            "y": {
                "field": "band",
                "type": "ordinal",
                "sort": band_order,
                "axis": {"title": "Percentile band", "titleFontWeight": "bold"},
            },
            "x": {
                "field": "count",
                "type": "quantitative",
                "axis": {"title": "In-flight items", "titleFontWeight": "bold"},
            },
            "color": {
                "field": "band",
                "type": "ordinal",
                "sort": band_order,
                "scale": {"scheme": "yelloworangered"},
                "legend": None,
            },
            "tooltip": [
                {"field": "band", "title": "Band"},
                {"field": "count", "title": "Items"},
                {"field": "share", "title": "Share", "format": ".0%"},
            ],
        },
    }

    return {
        "$schema": _VEGA_LITE_SCHEMA,
        "width": "container",
        "height": {"step": 32},
        "background": "transparent",
        "config": {"view": {"fill": None, "stroke": "#e5e5e5", "strokeWidth": 1}},
        "layer": [bar_layer],
    }


def cfd_spec(report: CfdReport) -> dict[str, Any]:
    """Stacked-area Cumulative Flow Diagram.

    Each band's vertical thickness at a sample date is the number of
    items currently in that workflow step (Vacanti's property #3 —
    `wip_in_state` = line[step_i] - line[step_{i+1}], or just
    line[terminal] for the bottom band). Stacked together, the bands
    sum to the top line — the cumulative-arrivals count
    (property #1).

    Stacks the terminal workflow step at the visual bottom, the first
    step at the top, matching Vacanti's reference figure. Renders the
    GitHub-PR degenerate two-state case (Open / Merged) without
    complaint.
    """
    workflow = list(report.input.workflow)

    # Build per-step band-width rows. Smaller `stack_order` stacks at
    # the visual bottom in Vega-Lite, so the terminal step gets 0.
    # This is the same band-width view exposed in the JSON envelope as
    # `chart_data.bands`; we compute it inline rather than re-routing
    # so the spec function stays a pure transform of the report.
    rows: list[dict[str, Any]] = []
    for pt in report.points:
        for i, state in enumerate(workflow):
            line = pt.counts_by_state.get(state, 0)
            if i + 1 < len(workflow):
                next_line = pt.counts_by_state.get(workflow[i + 1], 0)
                wip = line - next_line
            else:
                wip = line  # bottom band — no "later" state
            rows.append({
                "sampled_on": pt.sampled_on.isoformat(),
                "state": state,
                "wip_in_state": wip,
                "entered_at_or_later": line,
                # Reverse natural workflow index so the terminal state
                # sinks to the bottom of the stack.
                "stack_order": len(workflow) - 1 - i,
            })

    area_layer = {
        "mark": {"type": "area", "interpolate": "step-after", "opacity": 0.85},
        "data": {"values": rows},
        "encoding": {
            "x": {
                "field": "sampled_on",
                "type": "temporal",
                "axis": {"title": "Date", "titleFontWeight": "bold",
                         "format": "%b %d", "labelAngle": 0},
            },
            "y": {
                "field": "wip_in_state",
                "type": "quantitative",
                "aggregate": "sum",
                "axis": {"title": "Cumulative items",
                         "titleFontWeight": "bold"},
                "stack": "zero",
            },
            "color": {
                "field": "state",
                "type": "nominal",
                "sort": workflow,
                # Qualitative ColorBrewer-style palette: distinct hues
                # for each workflow stage so adjacent bands read as
                # separate stages, not as shades of the same stage.
                # `tableau10` is high-contrast and color-blind-safe
                # for ~5-7 categories.
                "scale": {"scheme": "tableau10"},
                "legend": {"title": "Workflow state", "orient": "right"},
            },
            "order": {"field": "stack_order", "type": "ordinal"},
        },
    }

    # Hover layer — vertical rule on the date nearest the cursor that
    # surfaces each workflow step's WIP-in-state (band width) and a
    # total WIP (items in flight) summary. A pivot widens the long-
    # format `wip_in_state` so each state becomes its own column in
    # the tooltip.
    #
    # WIP-in-flight = top line - bottom line = (sum of band widths) -
    # bottom band. Express as a sum of all non-terminal band widths.
    wip_calc = (
        " + ".join(f"datum['{s}']" for s in workflow[:-1])
        if len(workflow) >= 2 else "0"
    )
    hover_layer = {
        "data": {"values": rows},
        "transform": [
            {"pivot": "state", "value": "wip_in_state", "groupby": ["sampled_on"]},
            {"calculate": wip_calc, "as": "wip"},
        ],
        "mark": {"type": "rule", "color": "#222", "strokeWidth": 2},
        "encoding": {
            "x": {"field": "sampled_on", "type": "temporal"},
            "opacity": {
                "condition": {"param": "cfd_hover", "value": 0.6, "empty": False},
                "value": 0,
            },
            "tooltip": [
                {"field": "sampled_on", "type": "temporal", "title": "Date",
                 "format": "%b %d, %Y"},
                # Each state's band width — items currently in that step.
                *[{"field": s, "type": "quantitative", "title": s}
                  for s in workflow],
                {"field": "wip", "type": "quantitative",
                 "title": "WIP (in flight)"},
            ],
        },
        "params": [
            {
                "name": "cfd_hover",
                "select": {
                    "type": "point",
                    "fields": ["sampled_on"],
                    "nearest": True,
                    "on": "mouseover",
                    "clear": "mouseout",
                },
            },
        ],
    }

    # WIP trend is now communicated by the chart's shape (a widening
    # gap = growing WIP) and the hover tooltip — no in-chart text
    # annotation. The visual signal speaks for itself; if a reader
    # wants exact start vs end numbers they hover the two ends.
    layers: list[dict[str, Any]] = [area_layer, hover_layer]

    return {
        "$schema": _VEGA_LITE_SCHEMA,
        "width": "container",
        "height": 360,
        "background": "transparent",
        "config": {"view": {"fill": None, "stroke": "#e5e5e5", "strokeWidth": 1}},
        "layer": layers,
    }


def efficiency_spec(report: EfficiencyReport) -> dict[str, Any]:
    """Per-PR FE chart, brought to Aging parity.

    Vacanti's framing: long-running PRs dominate the portfolio FE.
    Sorting by cycle time descending puts the system-bottleneck PRs at
    the top. Color encodes FE band. The portfolio FE is a vertical
    rule — the system-level reference number the bars are compared
    against.

    Click-through where a PR URL exists (GitHub source). Tooltip
    shows item_id, title, cycle hours, and FE %.
    """
    repo = report.input.repo
    is_github = "/" in repo and not repo.startswith("jira:")

    def _url(item_id: str) -> str | None:
        if not is_github or not item_id.startswith("#"):
            return None
        return f"https://github.com/{repo}/pull/{item_id.lstrip('#')}"

    per_pr = report.result.per_pr
    values = [
        {
            "item_id": p.item_id,
            "title": p.title,
            "cycle_hours": round(p.cycle_time.total_seconds() / 3600, 2),
            "active_hours": round(p.active_time.total_seconds() / 3600, 2),
            "efficiency_pct": round(p.efficiency * 100, 1),
            "is_bot": p.is_bot,
            "band": _fe_band(p.efficiency * 100, p.is_bot),
            "pr_url": _url(p.item_id),
        }
        for p in per_pr
    ]

    bar_layer: dict[str, Any] = {
        "mark": {"type": "bar", "height": {"band": 0.8}},
        "data": {"values": values},
        "encoding": {
            "y": {
                "field": "item_id",
                "type": "nominal",
                "axis": {"title": None, "labelLimit": 80, "labelFontSize": 10},
                # Slowest at top — biggest cycle_hours value first.
                "sort": {"field": "cycle_hours", "order": "descending"},
            },
            "x": {
                "field": "efficiency_pct",
                "type": "quantitative",
                "axis": {"title": "Flow efficiency (%)", "titleFontWeight": "bold"},
                "scale": {"domain": [0, 100]},
            },
            "color": {
                "field": "band",
                "type": "nominal",
                "scale": {
                    "domain": ["< 10% (slow)", "10–50%", "≥ 50% (healthy)", "bot"],
                    "range": ["#cc3333", "#d4a72c", "#2ca02c", "#bbbbbb"],
                },
                "legend": {"title": "FE band", "orient": "right"},
            },
            "tooltip": [
                {"field": "item_id", "title": "ID"},
                {"field": "title", "title": "Title"},
                {"field": "efficiency_pct", "title": "FE %"},
                {"field": "cycle_hours", "title": "Cycle (h)"},
                {"field": "active_hours", "title": "Active (h)"},
            ],
            "href": {"field": "pr_url", "type": "nominal"},
        },
    }

    # Portfolio FE — Vacanti's system-level reference. Drawn as a
    # vertical rule across the whole chart so any PR can be compared
    # against the system number.
    portfolio_pct = round(report.result.portfolio_efficiency * 100, 1)
    rule_layer = {
        "mark": {"type": "rule", "color": "#2b7cff", "size": 2,
                 "strokeDash": [6, 4]},
        "data": {"values": [
            {"portfolio_pct": portfolio_pct, "label": f"Portfolio FE: {portfolio_pct}%"}
        ]},
        "encoding": {
            "x": {"field": "portfolio_pct", "type": "quantitative"},
            "tooltip": [{"field": "label"}],
        },
    }

    # Dynamic chart height — ~22px per row gives a readable bar; capped
    # via container height for very large datasets.
    height = max(120, min(20 * len(values), 1600))

    return {
        "$schema": _VEGA_LITE_SCHEMA,
        "width": "container",
        "height": height,
        "background": "transparent",
        "config": {"view": {"fill": None, "stroke": "#e5e5e5", "strokeWidth": 1}},
        "layer": [bar_layer, rule_layer],
    }


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
