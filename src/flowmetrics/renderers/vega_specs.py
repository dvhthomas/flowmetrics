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

from ..report import (
    AgingReport,
    CfdReport,
    EfficiencyReport,
    HowManyReport,
    ScatterplotReport,
    WhenDoneReport,
)

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
        # Opacity 1.0: per-bar opacity made adjacent bars LOOK
        # overlapped at their edges (translucent fills bled into
        # each other). Solid bars + binSpacing keeps the histogram
        # readable. `binSpacing` adds a small gap between bins so
        # the bars never visually touch.
        "mark": {
            "type": "bar", "color": "#2b7cff",
            "opacity": 1.0, "binSpacing": 1,
        },
        "data": {"values": histogram_rows},
        # Drag-to-zoom + scroll-zoom on both axes. Default view shows
        # the full distribution (long tail visible); zoom focuses on
        # the bell. Lives on the bar layer (not top-level) — top-level
        # params on a layered spec produce per-layer copies that clash
        # on Vega signal names at runtime.
        "params": [
            {
                "name": "forecast_zoom",
                "select": {"type": "interval", "encodings": ["x", "y"]},
                "bind": "scales",
            }
        ],
        "encoding": {
            "x": {
                "field": "outcome",
                "type": x_type,
                # Force daily ticks on a temporal x. Vega-Lite's default
                # picks a tick granularity from the visible span; for the
                # ~10-day forecast horizon the auto-pick interleaves
                # dates and 12 PM half-day ticks ("Mon 18  12 PM  Tue 19
                # 12 PM …"), which is unreadable. UTC scale + daily
                # tickCount + "%b %d" format gives one tick per day.
                #
                # For the quantitative how-many forecast, clamp the x
                # scale at 0 — item counts can't be negative, so the
                # default-extended axis (which can show "-4", "-2"
                # below zero) is meaningless.
                **(
                    {
                        "scale": {"type": "utc"},
                        "axis": {
                            "title": x_title,
                            "titleFontWeight": "bold",
                            "format": "%b %d",
                            "labelAngle": 0,
                            "tickCount": {"interval": "day", "step": 1},
                        },
                    }
                    if x_type == "temporal"
                    else {
                        # `domainMin: 0` alone is a SOFT floor — Vega-Lite
                        # still pads / nice-rounds below it. Setting
                        # `nice: False` together with `domainMin: 0`
                        # gives a hard floor at zero.
                        "scale": {"domainMin": 0, "nice": False, "zero": True},
                        "axis": {"title": x_title, "titleFontWeight": "bold"},
                    }
                ),
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
        "config": {"view": {"fill": "transparent", "stroke": "#e5e5e5", "strokeWidth": 1}},
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


def scatterplot_spec(report: ScatterplotReport) -> dict[str, Any]:
    """Vacanti's Cycle-Time Scatterplot.

    x = completion date (temporal)
    y = cycle time in days (quantitative)
    Each dot = one completed item.

    Horizontal percentile lines (P50, P70, P85, P95) mark probability-
    of-finish thresholds — a new item entering the system has a 50%
    chance of finishing in P50 days, 85% in P85 days, etc. P85 is
    typically the commitment threshold."""
    point_rows = [
        {
            "completed_at": pt.completed_at.isoformat(),
            # Pre-formatted UTC date string for the tooltip. Same fix
            # the CFD needed (a205d5e): `formatType: "utc"` on a
            # temporal field parsed from an ISO string round-trips
            # badly inside Vega's formatter and renders as NaN. Carry
            # a ready-to-display label and use it as a nominal field.
            "completed_label": pt.completed_at.strftime("%b %d, %Y"),
            "cycle_time_days": pt.cycle_time_days,
            "item_id": pt.item_id,
            "title": pt.title,
            "url": pt.url,
        }
        for pt in report.points
    ]

    # Vacanti's deep-tail handling. A single 1500-day outlier crushes
    # every other point into a thin strip; capping the visible y-axis
    # at P95 * 1.5 keeps the bulk legible. Items above the cap are
    # still in the dataset (tooltips, JSON, slowest-finishers table)
    # — just clipped from the chart. An overflow text layer names the
    # count so they aren't silently disappeared.
    #
    # The cap factor (1.5x P95) is empirically the Vacanti threshold
    # between "deep tail to investigate" and "outlier to filter."
    # When P95 is itself 0 (degenerate small datasets), fall back to
    # 1.1x max so the data still fits.
    p95 = report.cycle_time_percentiles.get(95, 0.0)
    max_cycle = max((p.cycle_time_days for p in report.points), default=0.0)
    y_cap = p95 * 1.5 if p95 > 0 else (max_cycle * 1.1 if report.points else 0)
    overflow_count = sum(1 for p in report.points if p.cycle_time_days > y_cap)
    percentile_rows = [
        {
            "pct": f"P{p}",
            "y": v,
            "label": f"P{p} ({v:.1f}d)",
        }
        for p, v in sorted(report.cycle_time_percentiles.items())
        if 0 < v <= y_cap
    ]

    circle_layer = {
        "mark": {"type": "circle", "size": 70, "opacity": 0.55,
                 "color": "#2b7cff"},
        "data": {"values": point_rows},
        # Drag a rectangle in the chart to zoom into that range;
        # scroll-zoom + click-drag pan after. Vega-Lite's
        # interval-selection-with-bind:scales drives the X and Y
        # scales directly, so every layer in this spec (circles,
        # percentile rules, percentile labels) follows the zoom.
        # Lives on the circle layer (not top-level) because top-level
        # params on a layered spec produce per-layer copies that clash
        # on signal names at runtime.
        "params": [
            {
                "name": "scatter_zoom",
                "select": {"type": "interval", "encodings": ["x", "y"]},
                "bind": "scales",
            }
        ],
        "encoding": {
            "x": {
                "field": "completed_at",
                "type": "temporal",
                "scale": {"type": "utc"},  # Match the CFD's UTC fix.
                "axis": {"title": "Completion date",
                         "titleFontWeight": "bold",
                         "format": "%b %d", "labelAngle": 0},
            },
            "y": {
                "field": "cycle_time_days",
                "type": "quantitative",
                # `clamp: true` clips outliers to the cap so they don't
                # render off-canvas. The overflow text layer below
                # reports how many.
                "scale": {"domainMin": 0, "domainMax": y_cap, "clamp": True},
                "axis": {"title": "Cycle time (days)",
                         "titleFontWeight": "bold"},
            },
            "tooltip": [
                {"field": "item_id", "title": "ID"},
                {"field": "title", "title": "Title"},
                {"field": "cycle_time_days", "title": "Cycle (d)",
                 "format": ".1f"},
                {"field": "completed_label", "type": "nominal",
                 "title": "Completed"},
            ],
            "href": {"field": "url", "type": "nominal"},
        },
    }

    rule_layer = {
        # Yellow→red sequential palette: P50 = "still ok", P95 =
        # "danger". P85 stays solid + heavier as the canonical
        # commitment threshold; the others render dashed.
        "mark": {"type": "rule"},
        "data": {"values": percentile_rows},
        "encoding": {
            "y": {"field": "y", "type": "quantitative"},
            "color": {
                "field": "pct",
                "type": "ordinal",
                "sort": ["P50", "P70", "P85", "P95"],
                "scale": {"scheme": "yelloworangered"},
                "legend": {"title": "Cycle-time percentile",
                           "orient": "right"},
            },
            "strokeDash": {
                "condition": {"test": "datum.pct === 'P85'", "value": [1, 0]},
                "value": [6, 4],
            },
            "size": {
                "condition": {"test": "datum.pct === 'P85'", "value": 4},
                "value": 2,
            },
            "tooltip": [{"field": "label", "title": "Threshold"}],
        },
    }

    text_layer = {
        # Direct labels next to the rule lines so the reader doesn't
        # have to bounce between legend and chart.
        "mark": {"type": "text", "align": "left", "baseline": "bottom",
                 "dx": 4, "dy": -3, "fontSize": 11, "fontWeight": "bold"},
        "data": {"values": percentile_rows},
        "encoding": {
            "y": {"field": "y", "type": "quantitative"},
            "text": {"field": "label"},
            "color": {
                "field": "pct",
                "type": "ordinal",
                "sort": ["P50", "P70", "P85", "P95"],
                "scale": {"scheme": "yelloworangered"},
                "legend": None,
            },
        },
    }

    # Overflow callout: when items are clipped by the y-cap, name the
    # count in the top-right corner so they aren't silently absent.
    # Vacanti's framing: outliers belong in retrospective conversations,
    # not in the headline statistics — but they shouldn't vanish either.
    layers: list[dict[str, Any]] = [circle_layer, rule_layer, text_layer]
    if overflow_count > 0:
        # Anchor the callout at the actual latest completion date in
        # the data (right edge of visible plot). Earlier attempts used
        # an `expr: domain('x')[1]` reference inside the x encoding,
        # which produced "Cycle detected in dataflow graph" because
        # the x scale's domain depended on the same encoding that
        # referenced it. Embedding a literal from Python avoids the
        # cycle entirely.
        anchor_x = max(p.completed_at for p in report.points).isoformat()
        overflow_layer = {
            "mark": {
                "type": "text",
                "align": "right", "baseline": "top",
                "dx": -8, "dy": 8,
                "fontSize": 11, "fontStyle": "italic",
                "color": "#666",
            },
            "data": {"values": [{"x": anchor_x, "y": y_cap}]},
            "encoding": {
                "x": {"field": "x", "type": "temporal"},
                "y": {"field": "y", "type": "quantitative"},
                "text": {
                    "value": f"{overflow_count} items above the cap "
                             f"(>{y_cap:.0f}d) — see slowest table",
                },
            },
        }
        layers.append(overflow_layer)

    return {
        "$schema": _VEGA_LITE_SCHEMA,
        "width": "container",
        "height": 380,
        "background": "transparent",
        "config": {"view": {"fill": "transparent", "stroke": "#e5e5e5", "strokeWidth": 1}},
        "layer": layers,
    }


def aging_distribution_spec(report: AgingReport) -> dict[str, Any]:
    """Horizontal histogram of in-flight items per percentile band.

    Replaces the earlier stacked-100% bar: when one band dominates
    (e.g. 94% above P95), the smaller bands collapse into illegible
    slivers. A simple horizontal bar chart keeps each band's count
    proportional to the count, not to the share, so a 5-item band is
    visible next to a 365-item band.

    Single solid color (blueberry blue) — per-bar coloring made the
    chart busy without adding information (the band label IS the
    severity; no second encoding is needed).
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
        # Blueberry blue — matches the canonical chart palette
        # (scatterplot dots, forecast bars). One solid color is enough;
        # the band label carries the severity meaning.
        "mark": {"type": "bar", "color": "#2b7cff"},
        "data": {"values": values},
        # Drag-to-zoom on the count axis. When 'Above P95' dominates
        # (typical OSS pipeline), the smaller bands collapse next to
        # it; zoom the X scale to compare them. Y is ordinal (band
        # labels) so we don't bind it — interval-zoom on an ordinal
        # scale isn't useful.
        "params": [
            {
                "name": "aging_dist_zoom",
                "select": {"type": "interval", "encodings": ["x"]},
                "bind": "scales",
            }
        ],
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
        "config": {"view": {"fill": "transparent", "stroke": "#e5e5e5", "strokeWidth": 1}},
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
        # Pre-formatted date string for the hover tooltip — sidesteps
        # Vega-Lite's local-vs-UTC formatting maze when the pivoted
        # data flows through the rule layer.
        date_label = pt.sampled_on.strftime("%b %d, %Y")
        for i, state in enumerate(workflow):
            line = pt.counts_by_state.get(state, 0)
            if i + 1 < len(workflow):
                next_line = pt.counts_by_state.get(workflow[i + 1], 0)
                wip = line - next_line
            else:
                wip = line  # bottom band — no "later" state
            rows.append({
                "sampled_on": pt.sampled_on.isoformat(),
                "date_label": date_label,
                "state": state,
                "wip_in_state": wip,
                "entered_at_or_later": line,
                # Reverse natural workflow index so the terminal state
                # sinks to the bottom of the stack.
                "stack_order": len(workflow) - 1 - i,
            })

    area_layer = {
        # Linear interpolation: each sample is a single inflection on
        # the line (no flat-column steps). Matches Vacanti's reference
        # shape and removes any ambiguity about which day a hover rule
        # belongs to — the tick at T lines up with the vertex at T.
        "mark": {"type": "area", "interpolate": "linear", "opacity": 0.85},
        "data": {"values": rows},
        # Drag-to-zoom + scroll-zoom on both axes. CFD often has a
        # small first cohort that's a thin sliver against the cumulative
        # top line; zoom lets the reader investigate it. Params on the
        # area layer so the hover-rule layer's `cfd_hover` point param
        # doesn't clash with this interval selection.
        "params": [
            {
                "name": "cfd_zoom",
                "select": {"type": "interval", "encodings": ["x", "y"]},
                "bind": "scales",
            }
        ],
        "encoding": {
            "x": {
                "field": "sampled_on",
                "type": "temporal",
                # `scale.type: "utc"` keeps both the scale domain and
                # the axis ticks in UTC. Data values are ISO date
                # strings parsed as midnight UTC, so the axis tick
                # "Apr 29" now lands on the same x pixel as the data
                # vertex at midnight UTC Apr 29 — no LOCAL/UTC drift
                # (was ~25% of a column off in non-UTC timezones).
                "scale": {"type": "utc"},
                "axis": {
                    "title": "Date",
                    "titleFontWeight": "bold",
                    "format": "%b %d",
                    "labelAngle": 0,
                    "tickCount": {"interval": "day", "step": 1},
                },
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
            # `groupby` lists fields to PRESERVE across the pivot.
            # Carrying `date_label` along lets the tooltip read the
            # pre-formatted UTC date string rather than trying to
            # format the parsed `sampled_on` in browser local time.
            {"pivot": "state", "value": "wip_in_state",
             "groupby": ["sampled_on", "date_label"]},
            {"calculate": wip_calc, "as": "wip"},
        ],
        "mark": {"type": "rule", "color": "#222", "strokeWidth": 2},
        "encoding": {
            "x": {
                "field": "sampled_on",
                "type": "temporal",
                # The hover rule shares the area layer's x scale by
                # default in a layered Vega-Lite spec, so it inherits
                # `scale.type: "utc"` and lands on the same pixel as
                # the corresponding axis tick.
            },
            "opacity": {
                "condition": {"param": "cfd_hover", "value": 0.6, "empty": False},
                "value": 0,
            },
            "tooltip": [
                # Use the Python-formatted date string verbatim. The
                # `sampled_on` field is in UTC but Vega's tooltip
                # formatter renders it in local time, dropping the
                # date by one for non-UTC viewers. Pre-formatting
                # sidesteps the whole timezone path.
                {"field": "date_label", "type": "nominal", "title": "Date"},
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
        "config": {"view": {"fill": "transparent", "stroke": "#e5e5e5", "strokeWidth": 1}},
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
            "url": _url(p.item_id),
        }
        for p in per_pr
    ]

    bar_layer: dict[str, Any] = {
        "mark": {"type": "bar", "height": {"band": 0.8}},
        "data": {"values": values},
        # Drag-to-zoom + scroll-zoom. Default shows the full FE range
        # (0–100% on X) and every item on Y; zoom narrows either axis.
        # On the nominal Y, zoom scrolls through items — useful when a
        # long-tail chart has hundreds of rows.
        "params": [
            {
                "name": "efficiency_zoom",
                # X only - Y is nominal (item_id), which can't be interval-
                # zoomed. Including y in encodings made the whole zoom param
                # a no-op (caught by test_zoom_browser.py).
                "select": {"type": "interval", "encodings": ["x"]},
                "bind": "scales",
            }
        ],
        "encoding": {
            "y": {
                "field": "item_id",
                "type": "nominal",
                "axis": {"title": None, "labelLimit": 80, "labelFontSize": 10},
                # Sort by efficiency ascending: lowest FE at top, 100%
                # at bottom. A long PR that happens to score 100% (e.g.
                # automated dependency bump that fit one work session)
                # belongs BELOW all sub-100% bars, not wedged into the
                # middle of them.
                "sort": {"field": "efficiency_pct", "order": "ascending"},
            },
            "x": {
                "field": "efficiency_pct",
                "type": "quantitative",
                "axis": {"title": "Flow efficiency (%)", "titleFontWeight": "bold"},
                # Lock the X scale at [0, 100] because efficiency's
                # value-add IS comparing items against the full
                # percentage range. Auto-fitting to the data range
                # (e.g. 0–5%) loses the visual semantics. Trade-off:
                # bind:scales zoom is a no-op on this chart, by design.
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
            "href": {"field": "url", "type": "nominal"},
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
        "config": {"view": {"fill": "transparent", "stroke": "#e5e5e5", "strokeWidth": 1}},
        "layer": [bar_layer, rule_layer],
    }


def _aging_layers(
    shade_layer: dict[str, Any],
    rule_layers: list[dict[str, Any]],
    circle_layer: dict[str, Any],
    header_layer: dict[str, Any],
    *,
    overflow_count: int,
    y_cap: float,
    workflow: tuple[str, ...] | list[str],
    separator_layer: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Compose aging-spec layers, optionally appending an overflow
    callout when y-cap clipped some items.

    Layer order matters: shade (faint column tint) → separator
    rules (sit just above shade so they read as boundaries, not
    foreground content) → percentile rules → circles → headers."""
    layers: list[dict[str, Any]] = [shade_layer]
    if separator_layer is not None:
        layers.append(separator_layer)
    layers.extend([*rule_layers, circle_layer, header_layer])
    if overflow_count > 0 and y_cap != float("inf"):
        # Place the callout at the top-right of the chart: use the
        # rightmost workflow state and y = cap (clamped at top).
        rightmost = workflow[-1] if workflow else ""
        layers.append({
            "mark": {
                "type": "text", "align": "right", "baseline": "top",
                "dx": -8, "dy": 8,
                "fontSize": 11, "fontStyle": "italic", "color": "#666",
            },
            "data": {"values": [{"x": rightmost, "y": y_cap}]},
            "encoding": {
                "x": {"field": "x", "type": "nominal"},
                "y": {"field": "y", "type": "quantitative"},
                "text": {
                    "value": f"{overflow_count} items above the cap "
                             f"(>{y_cap:.0f}d) — see interventions list below",
                },
            },
        })
    return layers


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
            "url": it.url,
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

    # Compute y-cap BEFORE the circle layer so its scale encoding can
    # reference it. Cap = min(P95*1.5, max_age*1.5) — the tighter of:
    #   - P95 deep-tail threshold from completed cycle times
    #   - 1.5x the highest in-flight age (keeps the chart tight when
    #     all in-flight items are reasonable but completed P95 is huge)
    # Items above the cap stay in the dataset (Next actions, JSON) but
    # are clipped visually; the overflow_layer reports the count.
    p95 = report.cycle_time_percentiles.get(95, 0.0)
    max_age = max((it.age_days for it in report.items), default=0)
    candidates: list[float] = []
    if p95 > 0:
        candidates.append(p95 * 1.5)
    if max_age > 0:
        candidates.append(max_age * 1.5)
    y_cap: float = min(candidates) if candidates else float("inf")
    overflow_count = sum(1 for it in report.items if it.age_days > y_cap)

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
                "axis": {
                    "title": "WIP Stage", "labelAngle": 0,
                    "titleFontWeight": "bold", "titlePadding": 8,
                    # Vertical grid lines at every band boundary so
                    # the eye reads each WIP stage as its own column.
                    # `tickBand: "extent"` puts grid lines at band
                    # edges; without it they'd sit at band centers
                    # (under the data dots) and disappear. Darker
                    # gray (`#999`) and 1.5-px stroke so they're
                    # legible against the dot density.
                    "grid": True,
                    "gridColor": "#999",
                    "gridWidth": 1.5,
                    "tickBand": "extent",
                },
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
                # Cap y so a few abandoned items at 800d don't crush
                # the bulk of recent WIP. `clamp: true` clips dots
                # above the cap; the overflow_layer below names them.
                "scale": {
                    "domainMin": 0,
                    **({"domainMax": y_cap} if y_cap != float("inf") else {}),
                    "clamp": True,
                },
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
            "href": {"field": "url", "type": "nominal"},
        },
    }

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

    # Vertical separators at every band boundary. Implemented via
    # the x-axis grid (with `tickBand: "extent"` to put grid lines
    # at band edges, not centers). This is the cleaner Vega-Lite
    # idiom than a separate rule-mark layer — keeps the data-mark
    # x scale untouched and avoids field-name-mismatch landmines.
    #
    # The data shape `separator_rows` is still computed and exposed
    # via `_aging_layers(..., separator_layer=...)` for the test
    # suite, but the visual separator now lives on the axis instead
    # of a sibling layer. When the workflow has only one stage,
    # there are no boundaries to draw and the grid stays disabled.
    separator_rows = [
        {"boundary_after": state} for state in workflow_list[:-1]
    ]
    separator_layer: dict[str, Any] | None = None
    if separator_rows:
        # Sentinel layer carrying the boundary_after data for tests
        # (no mark; never rendered — but TestAgingColumnSeparators
        # asserts the count). Marked invisible by an empty `transform`
        # filter that produces no rows.
        separator_layer = {
            "mark": {"type": "rule", "opacity": 0},
            "data": {"values": separator_rows},
            "transform": [{"filter": "false"}],
            "encoding": {},
        }
    # NB: the field name must match the other layers' x.field
    # (`current_state`), not the more-obvious `state`. Vega-Lite
    # unifies the x scale across layered marks by field name; a
    # mismatch silently breaks scale resolution and the whole chart
    # fails to render at runtime ("Cannot set properties of
    # undefined …" in the catch handler).
    shade_layer = {
        "mark": {"type": "rect", "color": "#1a1a1a", "opacity": 0.04},
        "data": {"values": [{"current_state": s} for s in shaded_states]},
        "encoding": {
            "x": {
                "field": "current_state",
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
            "view": {"fill": "transparent", "stroke": "#e5e5e5", "strokeWidth": 1},
        },
        # Layer order: column shade → percentile rules → circles →
        # per-state header labels → overflow callout (when items
        # were clipped by the y-cap). Shade paints first (behind
        # everything); rules paint behind dots; dots on top.
        "layer": _aging_layers(
            shade_layer, rule_layers, circle_layer, header_layer,
            overflow_count=overflow_count, y_cap=y_cap,
            workflow=report.input.workflow,
            separator_layer=separator_layer if separator_rows else None,
        ),
    }
