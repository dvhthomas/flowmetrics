"""Aging Work In Progress component, per Vacanti.

In-flight items only (started ≤ asof but not yet completed by asof),
plotted by current workflow state (x-axis nominal) and elapsed age in
days (y-axis). Percentile lines from completed-item cycle times serve
as commitment thresholds — an item aging past P85 is likely to miss
the forecast.

The component takes an `asof` UTC date parameter (default = today) so
historical aging views work. The fixture for this codebase has all
items completed within a bounded window, so the default render is
empty against it — pass `asof=2026-05-06` for a non-empty demo.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime

import duckdb

from ...utc_dates import attach_utc, to_utc_display_date


# Chart colors are CSS-theme-driven; see _base.html.jinja's
# `flowmetricsTheme` for resolved values. Same percentile palette
# as the cycle-time chart so the thresholds read as "the same
# thresholds, different metric".
_PCT_COLOR_P50 = "__theme:p-200__"
_PCT_COLOR_P85 = "__theme:p-500__"
_PCT_COLOR_P95 = "__theme:p-700__"
_POINT_COLOR = "__theme:muted__"


@dataclass(frozen=True)
class AgingItem:
    """One in-flight item at the asof date."""

    item_id: str
    title: str
    url: str | None
    current_state: str
    age_days: int


@dataclass(frozen=True)
class AgingData:
    """Payload for the aging tile partial."""

    items: tuple[AgingItem, ...]
    count: int
    asof_iso: str
    asof_display: str
    headline: str
    p50: float
    p85: float
    p95: float

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
            {"label": "P50", "age_days": self.p50, "color": _PCT_COLOR_P50},
            {"label": "P85", "age_days": self.p85, "color": _PCT_COLOR_P85},
            {"label": "P95", "age_days": self.p95, "color": _PCT_COLOR_P95},
        ]

        # Forward jitter — dots for state S live between the S tick
        # and the next-state tick. Same column convention as the
        # cycle-time scatter.
        rng = random.Random(0)
        for v in item_values:
            v["_jitter"] = rng.random() * 0.7

        spec: dict = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "background": "transparent",
            "padding": 12,
            "width": "container",
            "layer": [
                # Percentile threshold lines. Labels are NOT drawn
                # on the chart (anchoring text at the right edge of
                # a layered nominal-x chart turned out brittle —
                # `x: {datum: {expr: "width"}}` collapses the
                # x-scale in some Vega-Lite versions). The
                # percentile values are named explicitly in the
                # metric-summary headline above the tile.
                {
                    "data": {"values": pct_values},
                    "mark": {"type": "rule", "size": 1.5, "strokeDash": [4, 3]},
                    "encoding": {
                        "y": {"field": "age_days", "type": "quantitative"},
                        "color": {
                            "field": "color",
                            "type": "nominal",
                            "scale": None,
                            "legend": None,
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
                # In-flight item dots.
                {
                    "data": {"values": item_values},
                    "mark": {
                        "type": "point",
                        "filled": True,
                        "size": 90,
                        "color": _POINT_COLOR,
                        "opacity": 0.85,
                    },
                    "encoding": {
                        "x": {
                            "field": "current_state",
                            "type": "nominal",
                            "axis": {"title": "Current state", "labelAngle": 0},
                            "sort": None,
                        },
                        "xOffset": {
                            "field": "_jitter",
                            "type": "quantitative",
                            "scale": {"range": [0, 24]},
                        },
                        "y": {
                            "field": "age_days",
                            "type": "quantitative",
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
        return json.dumps(spec)


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    asof: date | None = None,
) -> AgingData:
    """Compute the aging-WIP payload for `contract_name` at `asof`.

    `asof` defaults to today (UTC date). Items are in-flight if:
      - created_at.date() ≤ asof
      - completed_at is null, OR completed_at.date() > asof

    Current state is the latest transition with entered_at ≤ asof.
    Items with no transitions yet at asof are tagged `"Unknown"`.
    """
    if asof is None:
        asof = datetime.now(UTC).date()
    asof_anchor = datetime(asof.year, asof.month, asof.day, tzinfo=UTC)

    rows = con.execute(
        "SELECT item_id, title, url, created_at "
        "FROM work_items "
        "WHERE contract_id = ? "
        "  AND created_at IS NOT NULL "
        "  AND CAST(created_at AS DATE) <= CAST(? AS DATE) "
        "  AND (completed_at IS NULL "
        "       OR CAST(completed_at AS DATE) > CAST(? AS DATE)) "
        "ORDER BY created_at ASC",
        [contract_name, asof, asof],
    ).fetchall()

    items: list[AgingItem] = []
    for item_id, title, url, created_at in rows:
        created_aware = attach_utc(created_at)
        age = (asof - created_aware.date()).days
        # Latest transition at or before asof — that's the current
        # state from asof's point of view.
        state_row = con.execute(
            "SELECT stage FROM transitions "
            "WHERE contract_id = ? AND item_id = ? "
            "  AND CAST(entered_at AS DATE) <= CAST(? AS DATE) "
            "ORDER BY entered_at DESC LIMIT 1",
            [contract_name, str(item_id), asof],
        ).fetchone()
        current_state = state_row[0] if state_row else "Unknown"
        items.append(
            AgingItem(
                item_id=str(item_id),
                title=str(title) if title is not None else "",
                url=str(url) if url is not None else None,
                current_state=str(current_state),
                age_days=int(age),
            )
        )

    # Percentile thresholds from completed cycle times — same source
    # the cycle-time chart's reference lines come from. The aging
    # check is "this in-flight item is now older than the typical
    # commitment threshold". `cycle_time_days IS NOT NULL` filters
    # to completions; in-flight items by definition have null.
    pct_row = con.execute(
        "SELECT percentile_cont(0.50) WITHIN GROUP (ORDER BY cycle_time_days), "
        "       percentile_cont(0.85) WITHIN GROUP (ORDER BY cycle_time_days), "
        "       percentile_cont(0.95) WITHIN GROUP (ORDER BY cycle_time_days) "
        "FROM work_items "
        "WHERE contract_id = ? AND cycle_time_days IS NOT NULL",
        [contract_name],
    ).fetchone()
    p50 = float(pct_row[0] or 0.0)
    p85 = float(pct_row[1] or 0.0)
    p95 = float(pct_row[2] or 0.0)

    asof_display = to_utc_display_date(asof_anchor)
    headline = (
        f"{len(items)} in-flight item{'' if len(items) == 1 else 's'} "
        f"as of {asof_display} (UTC) · "
        f"P50 {p50:.1f}d · P85 {p85:.1f}d · P95 {p95:.1f}d"
    )

    return AgingData(
        items=tuple(items),
        count=len(items),
        asof_iso=asof.isoformat(),
        asof_display=asof_display,
        headline=headline,
        p50=p50,
        p85=p85,
        p95=p95,
    )
