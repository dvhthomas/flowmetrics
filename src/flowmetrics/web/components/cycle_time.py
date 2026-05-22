"""Cycle-time scatterplot component.

Produces the data + Vega-Lite spec the cycle_time_tile.html.jinja
partial renders. Used by both the dashboard tile and the
/metrics/cycle-time detail page.

`render(...)` is intentionally pure: takes a DuckDB connection +
contract name; returns a JSON-serialisable `CycleTimeData` payload.
The template knows how to lay it out at each `mode`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

import duckdb

from ...utc_dates import attach_utc, to_utc_display_date, to_utc_iso_date
from ...windows import Window

# Chart colors are NOT defined in Python — they live as CSS tokens
# on `:root` (see `_base.html.jinja`) and are substituted into the
# spec at embed time by `window.applyTheme`. Python emits
# `__theme:<token>__` placeholders; the browser resolves them from
# the current CSS values. One theme change in CSS flows everywhere
# without touching Python.
#
# Per-percentile assignment — neutrals + ONE accent.
#   P50 — light gray  (soft reference, "typical")  → --border
#   P85 — primary plum (Vacanti's commitment line — the headline
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


@dataclass(frozen=True)
class CycleTimePoint:
    item_id: str
    title: str
    url: str | None
    # `completed_at` is the ISO calendar date string (YYYY-MM-DD)
    # used by the chart's x-positioning transform. Stays a date so
    # all dots for the same day share an x.
    completed_at: str
    # `completed_at_display` is the SAME date pre-formatted in
    # Python as "%b %d, %Y" — passed to Vega-Lite as a nominal
    # tooltip field. Vega's temporal formatter would otherwise
    # render this value in BROWSER-LOCAL time, so a UTC May 04
    # would show as "May 03" to a PT viewer — and "May 04" to a
    # UTC viewer — for the same data. Pre-formatting in Python
    # makes the tooltip TZ-invariant.
    completed_at_display: str
    cycle_time_days: float


@dataclass(frozen=True)
class CycleTimeData:
    """Payload for the cycle_time_tile partial.

    `points` is the scatter data; `p50` / `p85` / `p95` are the
    empirical percentiles drawn as reference lines (P50 = median,
    P85 = Vacanti's external commitment threshold, P95 = high-stakes
    commitment); `headline` is the at-a-glance summary the tile
    renders above the chart.
    """

    item_count: int
    p50: float
    p85: float
    p95: float
    points: tuple[CycleTimePoint, ...] = ()
    headline: str = ""

    def vega_spec_json(self) -> str:
        """Return the Vega-Lite spec as a JSON string ready to embed
        in a `vegaEmbed(...)` call.

        Three layers stacked:
          1. The scatter points (one mark per completed item).
          2. P50 reference line (dashed, secondary colour).
          3. P85 reference line (solid, accent colour).

        Zoom + reset bound to `bind:scales` with a transparent view
        rect so wheel/drag events fire over empty plot area (the
        zoom-regression lesson from earlier in this project).
        """
        return json.dumps(_build_vega_spec(self), separators=(",", ":"))


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    view: Window | None = None,
) -> CycleTimeData:
    """Read the contract's latest work_items partition and produce the
    typed payload.

    `view` clamps the scatter to items completed inside this
    inclusive range — and the P50/P85/P95 threshold lines are the
    empirical percentiles of THOSE same items. The lines
    summarise the dots on screen, nothing else. When `view` is
    None the full materialised history is used.
    """
    where_clauses = ["contract_id = ?", "completed_at IS NOT NULL"]
    params: list = [contract_name]
    if view is not None:
        where_clauses.append("CAST(completed_at AS DATE) BETWEEN ? AND ?")
        params.extend([view.from_, view.to])
    where_sql = " AND ".join(where_clauses)
    rows = con.execute(
        f"""
        SELECT item_id, title, url, completed_at, cycle_time_days
        FROM work_items
        WHERE {where_sql}
        ORDER BY completed_at
        """,
        params,
    ).fetchall()

    points = tuple(
        CycleTimePoint(
            item_id=str(item_id),
            title=str(title) if title is not None else "",
            url=str(url) if url is not None else None,
            # Both strings come from `flowmetrics.utc_dates` — the
            # only sanctioned UTC-anchored date-formatter. Naive
            # datetimes raise; aware datetimes are UTC-truncated;
            # the tooltip never sees browser-local time.
            completed_at=to_utc_iso_date(attach_utc(completed_at)) if completed_at else "",
            completed_at_display=(
                to_utc_display_date(attach_utc(completed_at)) if completed_at else ""
            ),
            cycle_time_days=float(cycle_time_days) if cycle_time_days is not None else 0.0,
        )
        for (item_id, title, url, completed_at, cycle_time_days) in rows
    )

    if not points:
        # Distinguish "nothing in this window" from "nothing
        # materialised at all". A view window outside the
        # warehouse's data range is a filter artefact — the data
        # exists, just not here. An empty warehouse is a
        # materialise gap. Conflating them ("no completed items")
        # sends the operator looking for the wrong fix.
        cov = con.execute(
            "SELECT count(*), "
            "       min(CAST(completed_at AS DATE)), "
            "       max(CAST(completed_at AS DATE)) "
            "FROM work_items "
            "WHERE contract_id = ? AND completed_at IS NOT NULL",
            [contract_name],
        ).fetchone()
        total_completed = int(cov[0]) if cov and cov[0] else 0
        if total_completed == 0:
            headline = (
                "No data materialised yet — open the Data Source "
                "page to fetch completions from the source system."
            )
        else:
            cov_from = to_utc_display_date(
                attach_utc(datetime.combine(cov[1], time.min))
            )
            cov_to = to_utc_display_date(
                attach_utc(datetime.combine(cov[2], time.min))
            )
            headline = (
                "No completed items in this window. The warehouse "
                f"covers {cov_from} – {cov_to} "
                f"({total_completed} completed items) — widen the "
                "view window to see them."
            )
        return CycleTimeData(
            item_count=0,
            p50=0.0,
            p85=0.0,
            p95=0.0,
            points=(),
            headline=headline,
        )

    # Empirical percentiles via DuckDB (single statistical pass).
    # The sample is the SAME items the scatter shows (the view
    # window) — the threshold lines summarise the dots on screen.
    pct_where = ["contract_id = ?", "cycle_time_days IS NOT NULL"]
    pct_params: list = [contract_name]
    if view is not None:
        pct_where.append("CAST(completed_at AS DATE) BETWEEN ? AND ?")
        pct_params.extend([view.from_, view.to])
    p50_row, p85_row, p95_row = con.execute(
        f"""
        SELECT
            percentile_cont(0.50) WITHIN GROUP (ORDER BY cycle_time_days) AS p50,
            percentile_cont(0.85) WITHIN GROUP (ORDER BY cycle_time_days) AS p85,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY cycle_time_days) AS p95
        FROM work_items
        WHERE {" AND ".join(pct_where)}
        """,
        pct_params,
    ).fetchone()
    p50 = float(p50_row) if p50_row is not None else 0.0
    p85 = float(p85_row) if p85_row is not None else 0.0
    p95 = float(p95_row) if p95_row is not None else 0.0
    headline = (
        f"{len(points)} items completed · "
        f"P50 {p50:.1f}d · P85 {p85:.1f}d · P95 {p95:.1f}d"
    )

    return CycleTimeData(
        item_count=len(points),
        p50=p50,
        p85=p85,
        p95=p95,
        points=points,
        headline=headline,
    )


# ---------------------------------------------------------------------------
# Vega-Lite spec construction
# ---------------------------------------------------------------------------


def _build_vega_spec(data: CycleTimeData) -> dict[str, Any]:
    """Build a Vega-Lite layered spec.

    Top-level encoding declares the x/y scales — the `bind:scales`
    zoom needs to find them here, not per-layer. Reference-line
    layers override only `y` (constant for each line); they inherit
    the x scale from the top level. The scatter layer uses the
    top-level encoding fully.

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
        for p in data.points
    ]
    p50_label = f"P50 ({data.p50:.1f}d)"
    p85_label = f"P85 ({data.p85:.1f}d)"
    p95_label = f"P95 ({data.p95:.1f}d)"
    reference_rows = [
        {"y": data.p50, "label": p50_label, "pct": "P50"},
        {"y": data.p85, "label": p85_label, "pct": "P85"},
        {"y": data.p95, "label": p95_label, "pct": "P95"},
    ]

    # Pad the x-scale domain by one day on each side. Without padding,
    # dots at the first / last data date sit at x=0 and x=plot_width
    # and render half-clipped at the chart's left and right edges.
    # The right-side padding is especially important under jitter: a
    # last-day dot can have a jittered x in (last_date, last_date+1),
    # and without the +1-day padding that jitter band runs off the
    # plot. Both sides padded for visual symmetry.
    point_dates = sorted({p.completed_at for p in data.points})
    if point_dates:
        first_date = date.fromisoformat(point_dates[0])
        last_date = date.fromisoformat(point_dates[-1])
        domain_start = (first_date - timedelta(days=1)).isoformat()
        domain_end = (last_date + timedelta(days=1)).isoformat()
        span_days = (last_date - first_date).days
    else:
        domain_start = domain_end = None
        span_days = 0

    # Tick/gridline interval scales with the window span. A fixed
    # daily interval keeps short windows clean (and stops Vega
    # auto-picking a sub-day granularity that repeats the same
    # "%b %d" label), but across many months it hatches the plot
    # into an unreadable grey wash — one gridline per day. Step up
    # to week/month so the gridline count stays ~10-30.
    if span_days <= 30:
        x_tick_count: dict = {"interval": "day", "step": 1}
    elif span_days <= 210:
        x_tick_count = {"interval": "week", "step": 1}
    elif span_days <= 1095:
        x_tick_count = {"interval": "month", "step": 1}
    else:
        x_tick_count = {"interval": "month", "step": 3}

    # Y-axis cap slider: a range control that EXCLUDES outliers
    # above the cap so the bulk stays readable. It runs from ~P95
    # up to the max observed cycle time. It works by FILTERING the
    # dots (not by pinning the y domain) — so the y-axis auto-
    # scales to whatever is shown and the chart always fills the
    # plot. The percentile VALUES are untouched: they're computed
    # server-side from the full data, and the rule layer is not
    # filtered.
    cycle_vals = sorted(p.cycle_time_days for p in data.points)
    cap_param: dict | None = None
    cap_filter: dict | None = None
    if len(cycle_vals) >= 2:
        cap_floor = math.ceil(data.p95)
        cap_ceiling = math.ceil(cycle_vals[-1])
        if cap_floor < cap_ceiling:
            cap_param = {
                "name": "cyclecap",
                # Default = max → the chart opens showing ALL data;
                # the operator drags down to exclude outliers.
                "value": cap_ceiling,
                "bind": {
                    "input": "range",
                    "min": cap_floor,
                    "max": cap_ceiling,
                    "step": 1,
                    "name": "Max cycle time shown (days)  ",
                },
            }
            cap_filter = {
                "filter": "datum.cycle_time_days <= cyclecap"
            }

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
        # and never inside the previous date's column. The earlier
        # "looks like May DD+1 but tooltip says May DD" perception
        # was a separate TZ-formatting bug; the tooltip now
        # pre-formats in Python (UTC) so the date the user sees on
        # hover matches the column the dot is in.
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
        # All x/y encoding lives on the scatter layer (not top-level).
        # Top-level encoding bleeds into the rule/text reference
        # layers via inheritance — those layers have their own data
        # (no `completed_at` field), which triggers "Infinite extent"
        # warnings AND prevents the labels from rendering. The
        # bind:scales zoom selection still drives both rule and text
        # layers because Vega-Lite shares scales across layers by
        # default (same name → same scale).
        "encoding": {
            "x": {
                # The chart positions dots by the jittered field
                # (produced by the calculate transform above); the
                # tooltip still reads the raw `completed_at` so the
                # date the user sees on hover is the honest date.
                "field": "completed_at_jittered",
                "type": "temporal",
                "scale": (
                    {
                        "type": "utc",
                        "domain": [domain_start, domain_end],
                    }
                    if domain_start
                    else {"type": "utc"}
                ),
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
                    # Span-adaptive tick interval (see x_tick_count
                    # above). Pins ticks to whole-day/week/month
                    # boundaries so Vega never auto-picks a sub-day
                    # granularity (which renders each "%b %d" label
                    # several times) and a multi-month window never
                    # draws a gridline per day.
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
    # One rule layer carrying both percentile lines (data is a 2-row
    # array). Same shape for the text labels. Consolidating reduces
    # layer count from 5 to 3, which avoids a Vega-Lite codegen
    # collision (duplicate signal names) we hit when each percentile
    # had its own layer.
    # P50 / P85 / P95 reference lines. Colour escalates with
    # percentile (grey → red → deep red); P85 is highlighted as the
    # canonical commitment threshold via thicker stroke + solid
    # line; P50 and P95 render dashed.
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
                    # Knox tokens (see top of module).
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
    # of scope for the scale's `domainMax` expr.
    if cap_param is not None:
        spec["params"] = [cap_param]
    return spec


# Re-export for templates that don't want to import from a sub-package
TEMPLATE_NAME = "_partials/cycle_time_tile.html.jinja"
