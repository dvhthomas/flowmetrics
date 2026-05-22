"""Aging Work In Progress component, per Vacanti.

In-flight items only (started ≤ asof but not yet completed by asof),
plotted by current workflow state (x-axis nominal) and elapsed age in
days (y-axis). Percentile lines from completed-item cycle times serve
as commitment thresholds — an item aging past P85 is likely to miss
the forecast.

Aging WIP is a "right now" metric — current open work, aged. The
caller pins the required `asof` UTC date to the in-flight snapshot
date (the latest materialise), NOT a scrollable view anchor: the
warehouse holds one in-flight snapshot, so aging can only be
faithfully computed at that date.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime

import duckdb

from ...contract import WorkflowStates
from ...utc_dates import attach_utc, to_utc_display_date
from ...windows import Window


# Chart colors are CSS-theme-driven; see _base.html.jinja's
# `flowmetricsTheme` for resolved values. The percentile rules
# keep the neutrals + P85 accent shared with the cycle-time
# chart; the dots themselves are coloured per workflow state
# (a categorical scheme) so each column is easy to read.
_PCT_COLOR_P50 = "__theme:border__"
_PCT_COLOR_P85 = "__theme:p-500__"
_PCT_COLOR_P95 = "__theme:muted__"


@dataclass(frozen=True)
class AgingItem:
    """One in-flight item at the asof date."""

    item_id: str
    title: str
    url: str | None
    current_state: str
    age_days: int


@dataclass(frozen=True)
class PercentileProvenance:
    """The percentile thresholds drawn on the Aging chart, plus
    the provenance the operator needs to read them honestly.

    The chart can lie if the percentiles come from a 7-day
    completed sample but the visible in-flight items have been
    aging for 30+ days. Surfacing the source counts + window +
    smell flag lets the UI flag the disparity instead of
    silently treating the P-lines as gospel.
    """

    p50: float
    p85: float
    p95: float
    # Number of completed items the thresholds were computed from.
    source_count: int
    # Date range of those completions (for the "P-lines from
    # May 4 – May 10" provenance line in the UI).
    source_window_earliest_iso: str | None
    source_window_latest_iso: str | None
    source_window_display: str
    # Smell signal: when in-flight ages dwarf the historical
    # window, the UI surfaces a callout. Empty string when not
    # triggered.
    smell: bool
    smell_text: str


@dataclass(frozen=True)
class WarehouseCoverage:
    """What date range the warehouse covers and when it was last
    refreshed. Used by the empty-state UI to name the actual
    gap when the chart can't render ('data is from May 4 to
    May 10; you asked about May 19')."""

    # Earliest / latest completion dates we have on hand.
    earliest_iso: str | None
    latest_iso: str | None
    earliest_display: str | None
    latest_display: str | None
    # Last materialise timestamp. Distinct from `latest_iso`:
    # the warehouse may have been materialised recently even
    # though no items completed recently.
    last_materialised_iso: str | None


@dataclass(frozen=True)
class AgingData:
    """Payload for the aging tile partial."""

    items: tuple[AgingItem, ...]
    count: int
    asof_iso: str
    asof_display: str
    headline: str
    percentiles: PercentileProvenance
    coverage: WarehouseCoverage
    # Empty-state classification for the view layer. None when
    # `items` is non-empty. Otherwise one of:
    #
    #   "asof_after_coverage"   — warehouse has data up to
    #       `coverage.latest_*` but not through `asof`. Action:
    #       backfill the gap.
    #   "asof_before_coverage"  — symmetric: asof predates the
    #       earliest data on hand. Action: backfill backwards.
    #   "no_work_in_flight"     — warehouse covers asof, no items
    #       were in flight. The real answer; no fetch would help.
    empty_state: str | None

    def vega_spec_json(self) -> str:
        """Vega-Lite layered spec: point marks per in-flight item +
        rule lines for percentile thresholds.

        Y-axis quantitative (age_days). X-axis nominal (current_state).
        Forward jitter on x so dots within the same column don't
        collapse on a single line."""
        item_values = [
            {
                "item_id": i.item_id,
                "title": i.title,
                "url": i.url,
                "current_state": i.current_state,
                "age_days": i.age_days,
            }
            for i in self.items
        ]

        # Percentile reference rows: drawn as horizontal rules
        # spanning the full x-range with right-aligned labels.
        pct_values = [
            {"label": "P50", "age_days": self.percentiles.p50, "color": _PCT_COLOR_P50},
            {"label": "P85", "age_days": self.percentiles.p85, "color": _PCT_COLOR_P85},
            {"label": "P95", "age_days": self.percentiles.p95, "color": _PCT_COLOR_P95},
        ]

        # Canonical Vega-Lite jitter pattern (`point_offset_random`):
        # a quantitative xOffset field with values drawn from
        # `random()` ∈ [0, 1). Combined with a band-scale x, Vega
        # auto-fits the offset to the BAND'S actual width at
        # render time — no pixel range baked in, so the dot
        # cloud fills whatever width each band ends up with.
        rng = random.Random(0)
        for v in item_values:
            v["_jitter"] = rng.random()

        # X-axis state columns in display order (first-appearance
        # order in the data). Pinned explicitly as `sort` on every
        # x encoding so the dots and the count headers agree on
        # the column layout.
        ordered_states: list[str] = []
        for v in item_values:
            if v["current_state"] not in ordered_states:
                ordered_states.append(v["current_state"])

        # Per-state WIP count, pre-aggregated here (not via a Vega
        # `aggregate: count`) so each label can read "WIP N" and
        # sit as a fixed header at the TOP of the chart — above
        # its column, at a constant height — rather than floating
        # above the tallest dot at a per-column height.
        state_counts: dict[str, int] = {}
        for v in item_values:
            s = v["current_state"]
            state_counts[s] = state_counts.get(s, 0) + 1
        badge_values = [
            {"current_state": s, "label": f"WIP {state_counts[s]}"}
            for s in ordered_states
        ]

        max_age = max((i.age_days for i in self.items), default=1.0)

        # Y-axis cap slider: a range control that EXCLUDES in-flight
        # items older than the cap so a few ancient items don't
        # squash the readable bulk. Runs from the P95 commitment
        # line up to the oldest item; default = max (opens showing
        # all). It FILTERS the dots (not the y domain) so the axis
        # auto-scales to what's shown and the plot always fills.
        # The percentile threshold lines are unaffected — separate
        # layer, separate data.
        ages = sorted(i.age_days for i in self.items)
        cap_param: dict | None = None
        cap_filter: dict | None = None
        if len(ages) >= 2:
            # Floor the cap at the P95 reference line — the dashed
            # commitment threshold. Cropping down to it focuses the
            # view on items at or below the high-stakes threshold;
            # the ancient outliers above it are exactly what the
            # slider hides. Fall back to the ages' own P95 when
            # there is no percentile line (no completed sample to
            # draw it from).
            if self.percentiles.p95 > 0:
                cap_floor = math.ceil(self.percentiles.p95)
            else:
                idx = min(len(ages) - 1, math.ceil(0.95 * len(ages)) - 1)
                cap_floor = math.ceil(ages[idx])
            cap_ceiling = math.ceil(max_age)
            if cap_floor < cap_ceiling:
                cap_param = {
                    "name": "agecap",
                    "value": cap_ceiling,
                    "bind": {
                        "input": "range",
                        "min": cap_floor,
                        "max": cap_ceiling,
                        "step": 1,
                        "name": "Max age shown (days)  ",
                    },
                }
                cap_filter = {"filter": "datum.age_days <= agecap"}

        spec: dict = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "background": "transparent",
            "padding": 12,
            "width": "container",
            # The dot layer colours by `current_state`; the rule
            # layer colours P50/P85/P95. Vega-Lite shares colour
            # scales across layers by default — which would merge
            # these two unrelated scales. Resolve colour
            # independently so each layer keeps its own.
            "resolve": {"scale": {"color": "independent"}},
            "layer": [
                # In-flight item dots — painted FIRST so the
                # threshold rules above sit on top and stay
                # visible against the dot cloud.
                {
                    # Zoom + pan via mouse wheel / click-drag,
                    # bound to scales. Per the layered-chart
                    # cycle-time precedent, `params` lives on
                    # this (the data-bearing) layer rather than
                    # at top level — top-level params on a
                    # layered spec produce per-layer copies of
                    # the selection and Vega complains about
                    # duplicate signal names.
                    # Zoom is bound to the x axis only — the y axis
                    # is driven by the cap slider, so binding y to
                    # the zoom too would fight the cap's domainMax.
                    # The interval selection stays on this layer (a
                    # top-level selection on a layered spec makes
                    # duplicate per-layer signals); the cap value
                    # param goes top-level — see below.
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
                    # Cap filter (when present) drops dots older
                    # than the slider value; the y-axis then auto-
                    # scales to what remains.
                    "transform": [cap_filter] if cap_filter else [],
                    "mark": {
                        "type": "point",
                        "filled": True,
                        "size": 90,
                        "opacity": 0.85,
                        # Signal clickability (the fragment script
                        # navigates to the item's lifecycle page on
                        # click).
                        "cursor": "pointer",
                    },
                    "encoding": {
                        "x": {
                            "field": "current_state",
                            "type": "nominal",
                            # Explicit `band` scale (Vega-Lite's
                            # default for nominal+xOffset is `point`,
                            # which sticks each label AT its tick and
                            # gives no horizontal room for jitter).
                            # Band gives each category a real width;
                            # the axis label sits at the band center.
                            "scale": {
                                "type": "band",
                                "paddingInner": 0.1,
                                "paddingOuter": 0.1,
                            },
                            "axis": {"title": "Current state", "labelAngle": 0},
                            "sort": ordered_states,
                        },
                        "xOffset": {
                            # Canonical `point_offset_random` —
                            # quantitative offset with NO explicit
                            # scale.range. Vega auto-fits the offset
                            # to the band's actual width at render
                            # time, so the dot cloud adapts to any
                            # viewport without pixel constants
                            # baked into the spec.
                            "field": "_jitter",
                            "type": "quantitative",
                        },
                        # Colour each dot by its workflow state, so
                        # a viewer can tell which category a point
                        # belongs to even mid-zoom. The x-axis
                        # already labels each column, so the colour
                        # needs no legend — it just reinforces the
                        # grouping. P50/P85/P95 rules stay readable:
                        # they are horizontal dashed lines, a wholly
                        # different shape from the dot cloud.
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
                            # domainMax auto-fits whatever survives
                            # the cap filter, with Vega's `nice`
                            # rounding giving headroom for the
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
                # Per-state "WIP N" header — pinned to the TOP of
                # the chart (`y: {value: 0}` is a fixed pixel
                # position, not a data value) and sitting in the
                # clear headroom band above the dots. A stable
                # header row, one per column, not a label floating
                # at the height of each column's tallest dot.
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
                # Percentile threshold rules — painted AFTER the
                # dots + count badge so they sit on top and stay
                # visible against the dot cloud (Vega-Lite paints
                # layers in order).
                # The color-with-legend encoding labels P50/P85/P95
                # without us having to anchor text marks at the
                # chart's right edge (which was unreliable across
                # Vega versions on a layered nominal-x chart).
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
                            {"field": "label", "type": "nominal", "title": "Threshold"},
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
        # The cap value param is top-level: the y scale is shared
        # across layers, so a param scoped to one layer would be
        # out of scope for the scale's `domainMax` expr.
        if cap_param is not None:
            spec["params"] = [cap_param]

        # No completions in the reference window → percentiles
        # are 0/0/0. Drop the rule layer entirely; three dashed
        # rules stacked on y=0 read as a real threshold.
        if self.percentiles.source_count == 0:
            spec["layer"] = [
                lyr
                for lyr in spec["layer"]
                if not (
                    isinstance(lyr.get("mark"), dict)
                    and lyr["mark"].get("type") == "rule"
                )
            ]
        return json.dumps(spec)


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    asof: date,
    contract_start: date | None = None,
    contract_stop: date | None = None,
    states: WorkflowStates | None = None,
    reference: Window | None = None,
) -> AgingData:
    """Compute the aging-WIP payload for `contract_name` at `asof`.

    Aging WIP is a "right now" metric — current open work, aged.
    The caller pins `asof` to the in-flight snapshot date (the
    latest materialise), NOT a scrollable view anchor: the
    warehouse holds one in-flight snapshot, so aging can only be
    faithfully computed at that date. Items are in-flight if:
      - created_at.date() ≤ asof
      - completed_at is null, OR completed_at.date() > asof

    Current state is the latest transition with entered_at ≤ asof.
    Items with no transitions yet at asof are tagged `"Unknown"`.

    WIP filter (`states`):
      When provided, only items currently in `states.wip`
      appear on the chart. Backlog AND done both fall out
      (done items are also excluded by the completion filter,
      so this is belt-and-braces for the edge case of a state
      classified as `done` for items still showing as in-flight).
      Surviving items keep their RAW state name so operators
      see where work is stuck (Changes Suggested, Awaiting
      Feedback) rather than aggregated bucket names.

    `contract_start` / `contract_stop` come from the contract YAML
    (set by the caller — the component doesn't touch the
    filesystem). Used only to classify the empty state: an asof
    outside the contract window is "not missing data, just outside
    scope," not the same as a stale warehouse.
    """
    asof_anchor = datetime(asof.year, asof.month, asof.day, tzinfo=UTC)
    asof_display = to_utc_display_date(asof_anchor)

    # Warehouse coverage — the completion dates the warehouse
    # actually holds. Drives both the coverage gate just below
    # and the empty-state messages further down.
    coverage_row = con.execute(
        "SELECT min(CAST(completed_at AS DATE)), "
        "       max(CAST(completed_at AS DATE)), "
        "       max(materialised_at) "
        "FROM work_items WHERE contract_id = ?",
        [contract_name],
    ).fetchone()
    earliest_data_date = coverage_row[0] if coverage_row else None
    latest_data_date = coverage_row[1] if coverage_row else None
    last_mat_dt = coverage_row[2] if coverage_row else None
    if last_mat_dt is not None:
        last_mat_aware = (
            last_mat_dt.replace(tzinfo=UTC)
            if last_mat_dt.tzinfo is None
            else last_mat_dt
        )
        warehouse_last_materialised_iso = (
            last_mat_aware.astimezone(UTC).date().isoformat()
        )
    else:
        warehouse_last_materialised_iso = None

    def _both(d: date | None) -> tuple[str | None, str | None]:
        """A date → (ISO, human-display) pair, or (None, None)."""
        if d is None:
            return None, None
        anc = datetime(d.year, d.month, d.day, tzinfo=UTC)
        return d.isoformat(), to_utc_display_date(anc)

    cov_earliest_iso, cov_earliest_display = _both(earliest_data_date)
    cov_latest_iso, cov_latest_display = _both(latest_data_date)
    coverage = WarehouseCoverage(
        earliest_iso=cov_earliest_iso,
        latest_iso=cov_latest_iso,
        earliest_display=cov_earliest_display,
        latest_display=cov_latest_display,
        last_materialised_iso=warehouse_last_materialised_iso,
    )

    # ONE bulk query for in-flight items + their current_state
    # (latest transition at or before asof). The earlier shape
    # was N+1: fetch items, then per-item query for the state —
    # ~3.5s on Cassandra's 3000+ in-flight items. The window-
    # function CTE collapses it to a single round-trip and drops
    # render time below 50ms.
    rows = con.execute(
        """
        WITH latest_state AS (
            SELECT item_id, stage,
                   ROW_NUMBER() OVER (
                       PARTITION BY item_id
                       ORDER BY entered_at DESC
                   ) AS rn
            FROM transitions
            WHERE contract_id = ?
              AND CAST(entered_at AS DATE) <= CAST(? AS DATE)
        )
        SELECT w.item_id, w.title, w.url, w.created_at,
               COALESCE(ls.stage, 'Unknown') AS current_state
        FROM work_items w
        LEFT JOIN latest_state ls
          ON ls.item_id = w.item_id AND ls.rn = 1
        WHERE w.contract_id = ?
          AND w.created_at IS NOT NULL
          AND CAST(w.created_at AS DATE) <= CAST(? AS DATE)
          AND (w.completed_at IS NULL
               OR CAST(w.completed_at AS DATE) > CAST(? AS DATE))
        ORDER BY w.created_at ASC
        """,
        [contract_name, asof, contract_name, asof, asof],
    ).fetchall()

    items: list[AgingItem] = []
    for item_id, title, url, created_at, current_state in rows:
        created_aware = attach_utc(created_at)
        # Vacanti's Age formula: CD - SD + 1 (same `+1` inclusive
        # rule as cycle time; a same-day item ages as 1d). p. 60,
        # Actionable Agile Metrics 10th Anniversary Edition.
        # Computed at query/view time because asof is a runtime
        # parameter — materialise can't precompute this.
        age = (asof - created_aware.date()).days + 1
        # WIP filter: drop items whose current_state isn't in
        # `states.wip`. Backlog and done both fall out by being
        # absent from that set. Surviving items keep their raw
        # state name on the chart.
        if states is not None and str(current_state) not in states.wip:
            continue
        items.append(
            AgingItem(
                item_id=str(item_id),
                title=str(title) if title is not None else "",
                url=str(url) if url is not None else None,
                current_state=str(current_state),
                age_days=int(age),
            )
        )

    # Percentile thresholds from completed cycle times — same
    # source the cycle-time chart's reference lines come from.
    # When `reference` is supplied, the percentiles draw their
    # sample from completions inside that inclusive date range
    # only — the operator's "ptiles over the last X days"
    # question. Without it, percentiles come from the full
    # completion history.
    pct_where = ["contract_id = ?", "cycle_time_days IS NOT NULL"]
    pct_params: list = [contract_name]
    if reference is not None:
        pct_where.append("CAST(completed_at AS DATE) BETWEEN ? AND ?")
        pct_params.extend([reference.from_, reference.to])
    pct_row = con.execute(
        f"SELECT percentile_cont(0.50) WITHIN GROUP (ORDER BY cycle_time_days), "
        f"       percentile_cont(0.85) WITHIN GROUP (ORDER BY cycle_time_days), "
        f"       percentile_cont(0.95) WITHIN GROUP (ORDER BY cycle_time_days), "
        f"       count(*), "
        f"       min(CAST(completed_at AS DATE)), "
        f"       max(CAST(completed_at AS DATE)) "
        f"FROM work_items "
        f"WHERE {' AND '.join(pct_where)}",
        pct_params,
    ).fetchone()
    p50 = float(pct_row[0] or 0.0)
    p85 = float(pct_row[1] or 0.0)
    p95 = float(pct_row[2] or 0.0)
    pct_source_count = int(pct_row[3] or 0)
    pct_source_earliest = pct_row[4]
    pct_source_latest = pct_row[5]
    if pct_source_earliest is not None and pct_source_latest is not None:
        anc_e = datetime(
            pct_source_earliest.year,
            pct_source_earliest.month,
            pct_source_earliest.day,
            tzinfo=UTC,
        )
        anc_l = datetime(
            pct_source_latest.year,
            pct_source_latest.month,
            pct_source_latest.day,
            tzinfo=UTC,
        )
        pct_source_window_display = (
            f"{to_utc_display_date(anc_e)} – {to_utc_display_date(anc_l)}"
        )
        pct_source_earliest_iso = pct_source_earliest.isoformat()
        pct_source_latest_iso = pct_source_latest.isoformat()
    else:
        pct_source_window_display = "no completed items yet"
        pct_source_earliest_iso = None
        pct_source_latest_iso = None

    count_part = (
        f"{len(items)} in-flight item{'' if len(items) == 1 else 's'} "
        f"as of {asof_display} (UTC)"
    )
    # When the reference window captures no completions the
    # percentiles collapse to 0/0/0 — printing "P50 0.0d" reads
    # as a real threshold. Say so plainly instead.
    if pct_source_count > 0:
        pct_part = (
            f"P50 {p50:.1f}d · P85 {p85:.1f}d · P95 {p95:.1f}d "
            f"from {pct_source_count} completed item"
            f"{'' if pct_source_count == 1 else 's'}"
            f" ({pct_source_window_display})"
        )
    else:
        pct_part = (
            "no percentile thresholds — no completed items in the "
            "reference period"
        )
    headline = f"{count_part} · {pct_part}"

    # Smell ratio: if in-flight ages span far longer than the
    # historical sample window driving the percentiles, the
    # thresholds are statistically shaky. Flag at 3× as a
    # reasonable "consider broadening" trigger (configurable
    # later if teams want different sensitivity).
    #
    # Window for the ratio: prefer the user's CONFIGURED
    # reference window when set (that's what the filter bar
    # promises). Falls back to the observed completion span
    # (min..max of completed_at among matching rows) only when
    # no reference window was supplied — otherwise we'd report
    # "7d" when the user explicitly chose 14, and the message
    # contradicts the dropdown above it.
    SMELL_RATIO_THRESHOLD = 3.0
    smell = False
    smell_text = ""
    # No smell without a sample: a "NNN× wider" callout against
    # an empty reference window is noise, not signal.
    if items and pct_source_count > 0:
        if reference is not None:
            window_days = reference.days_inclusive
        elif (
            pct_source_earliest is not None
            and pct_source_latest is not None
        ):
            window_days = (pct_source_latest - pct_source_earliest).days + 1
        else:
            window_days = 0
        max_age = max(i.age_days for i in items)
        if window_days > 0 and max_age / window_days >= SMELL_RATIO_THRESHOLD:
            ratio = max_age / window_days
            smell = True
            smell_text = (
                f"In-flight ages reach {max_age}d but percentiles are "
                f"drawn from a {window_days}d window — that's {ratio:.1f}× "
                f"wider. Consider broadening the historical sample for "
                f"more representative thresholds."
            )

    # Classify empty state. Non-empty → None. Else, action-first:
    # tell the operator what data the warehouse has and what range
    # they'd need to import to answer their question.
    #   "asof_after_coverage"        asof > latest completion on
    #                                hand. Import the gap forward.
    #   "asof_before_coverage"       asof < earliest completion
    #                                (symmetric).
    #   "in_flight_never_captured"   warehouse covers asof in the
    #                                COMPLETED dimension but has
    #                                never recorded an in-flight
    #                                row. The aging answer is
    #                                artificially empty; importing
    #                                will fetch current open work.
    #   "no_work_in_flight"          warehouse has captured in-flight
    #                                rows at some point but none
    #                                are open at asof. The real
    #                                answer; no fetch helps.
    have_any_in_flight_row = con.execute(
        "SELECT count(*) FROM work_items "
        "WHERE contract_id = ? AND completed_at IS NULL",
        [contract_name],
    ).fetchone()[0]
    if items:
        empty_state: str | None = None
    elif latest_data_date is not None and asof > latest_data_date:
        empty_state = "asof_after_coverage"
    elif earliest_data_date is not None and asof < earliest_data_date:
        empty_state = "asof_before_coverage"
    elif have_any_in_flight_row == 0:
        empty_state = "in_flight_never_captured"
    else:
        empty_state = "no_work_in_flight"

    return AgingData(
        items=tuple(items),
        count=len(items),
        asof_iso=asof.isoformat(),
        asof_display=asof_display,
        headline=headline,
        empty_state=empty_state,
        percentiles=PercentileProvenance(
            p50=p50, p85=p85, p95=p95,
            source_count=pct_source_count,
            source_window_earliest_iso=pct_source_earliest_iso,
            source_window_latest_iso=pct_source_latest_iso,
            source_window_display=pct_source_window_display,
            smell=smell,
            smell_text=smell_text,
        ),
        coverage=coverage,
    )
