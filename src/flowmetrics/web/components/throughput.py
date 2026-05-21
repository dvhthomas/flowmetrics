"""Throughput component, per Vacanti.

Throughput = items completed per day. The series enumerates every
calendar date between the first and last completion (inclusive)
and counts items that finished on each exact date. Zero-completion
days are real observations of capacity and must be included as
zeros — downstream Monte Carlo / forecast work depends on the
empirical "slow day" distribution.

Reference: Vacanti, *Actionable Agile Metrics for Predictability*,
10th Anniversary Edition, pp. 61–63.

The component mirrors `cycle_time`'s shape:
  - `render(con, contract)` reads DuckDB → typed `ThroughputData`.
  - `ThroughputData.vega_spec_json()` builds a Vega-Lite bar spec.
  - All dates are UTC-anchored via `flowmetrics.utc_dates`; tooltip
    date strings are pre-formatted as nominal (not temporal) so
    Vega can't shift them by browser-local timezone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import duckdb

from typing import Literal

from ...utc_dates import to_utc_display_date, to_utc_iso_date
from ...windows import Window


@dataclass(frozen=True)
class DailyThroughput:
    """One row of the daily series: a calendar date + the number of
    items that finished on that exact UTC date."""

    date_iso: str          # YYYY-MM-DD (UTC)
    date_display: str      # "May 04, 2026" — pre-formatted for tooltips
    count: int
    # Calendar-day classification. The chart paints a faint
    # background band over weekend columns so weekend-vs-weekday
    # is visually obvious without reading the dates. `holiday`
    # is a future hook (per-contract calendar of holidays);
    # only `weekday` / `weekend` are emitted today.
    day_type: Literal["weekday", "weekend", "holiday"]
    # Warehouse coverage for this date. A zero count on a
    # `warehouse` day is a TRUE zero (no completions that day,
    # e.g. weekend); a "zero" on a `missing` day is a GAP (no
    # data, backfill-able from the system of record). `stale`
    # is a future hook for "data exists but is older than the
    # cron heartbeat"; only `warehouse` / `missing` emitted
    # today. Defaults `warehouse` so contracts without explicit
    # materialise bounds don't get every day flagged missing.
    data_coverage: Literal["warehouse", "missing", "stale"] = "warehouse"


@dataclass(frozen=True)
class ThroughputData:
    """Payload for the throughput tile partial."""

    daily: tuple[DailyThroughput, ...]
    headline: str
    # Window endpoints — exposed so the template can name the range
    # without re-deriving from `daily`.
    first_date_iso: str | None
    last_date_iso: str | None
    # Warehouse coverage bounds — earliest/latest completion the
    # warehouse has on hand, independent of the current view
    # window. The empty-state template uses these to distinguish
    # "no data in the warehouse at all" from "warehouse has
    # data, just not in this view" — the second is actionable
    # (widen the view, or run `flow materialise --since X`).
    warehouse_earliest_iso: str | None = None
    warehouse_latest_iso: str | None = None
    warehouse_earliest_display: str | None = None
    warehouse_latest_display: str | None = None

    def vega_spec_json(self) -> str:
        """Vega-Lite bar chart: one bar per enumerated date, height
        = count. Dates rendered as nominal pre-formatted strings to
        avoid the TZ-shift bug the cycle-time chart was burned by."""
        # Neutral gray. The dashboard's coloured accent budget is
        # spent on the import-button CTA and the cycle-time P85
        # commitment line; everything else — including throughput
        # bars — stays monochrome so the eye finds the meaning
        # without colour fighting for attention.
        bar_color = "__theme:muted__"

        values = [
            {
                "date_iso": d.date_iso,
                "date_display": d.date_display,
                "count": d.count,
                "day_type": d.day_type,
                "data_coverage": d.data_coverage,
            }
            for d in self.daily
        ]

        # Pin the x-axis ordering to the ascending date sequence
        # from the data. Without this, the layered chart's domain
        # is built in layer-scan order — the weekend-shade layer
        # is scanned first and its filtered subset (Sat/Sun) ends
        # up at the LEFT of the axis, pushing the weekday columns
        # to the right. Explicit sort array on every x encoding
        # forces ascending order regardless of layer-scan order.
        date_order = [d.date_iso for d in self.daily]

        # Pre-thin axis labels for long windows. Vega-Lite's
        # nominal axis doesn't auto-thin (labelOverlap is a
        # no-op for nominal scales), so we pick ~10 evenly-
        # spaced ticks ourselves. Same fix as CFD + forecast.
        axis_config: dict = {
            "title": "Completion date (UTC)",
            "labelAngle": 0,
            # `utcFormat` (not `timeFormat`) renders the date
            # ignoring browser TZ — same TZ-safety contract the
            # tooltip nominal-pre-format idiom enforces, applied
            # to the axis label. Audited by
            # `tests/test_chart_tooltip_safety.py`.
            "labelExpr": (
                "utcFormat(datetime(datum.value), '%b %d')"
            ),
        }
        if len(date_order) > 10:
            step = (len(date_order) + 9) // 10  # ceil(n/10)
            axis_config["values"] = date_order[::step]

        # Shared x-axis encoding — referenced by both the weekend
        # shade layer and the bar layer so they line up exactly.
        x_encoding = {
            "field": "date_iso",
            "type": "nominal",
            "axis": axis_config,
            "sort": date_order,
        }

        spec: dict = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "background": "transparent",
            "padding": 12,
            "data": {"values": values},
            "width": "container",
            "layer": [
                # Faint background rect on weekend columns. Drawn
                # under the bars (first layer). Spans the full
                # y-range via `y`/`y2` left unbound (Vega-Lite
                # stretches a rect over the full chart height).
                {
                    "transform": [
                        {"filter": "datum.day_type === 'weekend'"},
                    ],
                    "mark": {
                        "type": "rect",
                        "color": "__theme:muted__",
                        "opacity": 0.08,
                    },
                    "encoding": {
                        "x": x_encoding,
                    },
                },
                # Slightly heavier rect on `missing` (uncovered)
                # columns so a no-data day is visually distinct
                # from a real zero-completion day. Same shape
                # as the weekend backdrop; different opacity +
                # diagonal hint via stroke.
                {
                    "transform": [
                        {"filter": "datum.data_coverage !== 'warehouse'"},
                    ],
                    "mark": {
                        "type": "rect",
                        "color": "__theme:border__",
                        "opacity": 0.55,
                    },
                    "encoding": {
                        "x": x_encoding,
                        "tooltip": [
                            {
                                "field": "date_display",
                                "type": "nominal",
                                "title": "Completed",
                            },
                            {
                                "field": "data_coverage",
                                "type": "nominal",
                                "title": "Data",
                            },
                        ],
                    },
                },
                # Throughput bars — covered days only. A bar of
                # height 0 on a covered day is a true zero
                # (rendered as a single-pixel sliver at the
                # baseline, distinguishable from the empty
                # column under the `missing` backdrop above).
                {
                    "transform": [
                        {"filter": "datum.data_coverage === 'warehouse'"},
                    ],
                    "mark": {
                        "type": "bar",
                        "color": bar_color,
                        "cornerRadius": 2,
                    },
                    "encoding": {
                        "x": x_encoding,
                        "y": {
                            "field": "count",
                            "type": "quantitative",
                            "axis": {
                                "title": "Items completed",
                                "tickMinStep": 1,
                                "format": "d",
                            },
                        },
                        "tooltip": [
                            {
                                "field": "date_display",
                                "type": "nominal",
                                "title": "Completed",
                            },
                            {
                                "field": "count",
                                "type": "quantitative",
                                "title": "Items",
                            },
                        ],
                    },
                },
                # "no data" marker for uncovered days — a small
                # em-dash anchored at the baseline. Lets the
                # viewer tell apart "true zero" from "gap" even
                # without hovering.
                {
                    "transform": [
                        {"filter": "datum.data_coverage !== 'warehouse'"},
                    ],
                    "mark": {
                        "type": "text",
                        "text": "—",
                        "baseline": "bottom",
                        "dy": -2,
                        "color": "__theme:muted__",
                        "fontSize": 11,
                    },
                    "encoding": {
                        "x": x_encoding,
                        "y": {"value": 0},
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
    view: Window | None = None,
    warehouse_start: date | None = None,
    warehouse_stop: date | None = None,
) -> ThroughputData:
    """Compute the daily throughput series for `contract_name`.

    `view`: clamps the x-axis to completions inside this
    inclusive date range. When None the window is data-derived
    (first completion → last completion).

    `warehouse_start` / `warehouse_stop`: the contract's
    materialise window — the date range `flow materialise` last
    queried. Days inside this range are tagged `is_covered=True`
    on each `DailyThroughput` row, so a zero on a covered day
    is a TRUE zero (no completions that day) while a "zero" on
    an uncovered day is a GAP (no data, backfill-able). The
    spec renders these differently — covered days get bars,
    uncovered get a "—" marker. When either is None all days
    are tagged covered (no gap visualization).
    """
    where = ["contract_id = ?", "created_at IS NOT NULL", "completed_at IS NOT NULL"]
    params: list = [contract_name]
    if view is not None:
        where.append("CAST(completed_at AS DATE) BETWEEN ? AND ?")
        params.extend([view.from_, view.to])
    rows = con.execute(
        f"SELECT CAST(completed_at AS DATE) AS d, count(*) AS n "
        f"FROM work_items "
        f"WHERE {' AND '.join(where)} "
        f"GROUP BY 1 ORDER BY 1 ASC",
        params,
    ).fetchall()

    # Warehouse coverage display fields. Populated in both
    # branches so the template's empty state can name where
    # data DOES exist when the view has zero matching rows.
    wh_earliest_iso = warehouse_start.isoformat() if warehouse_start else None
    wh_latest_iso = warehouse_stop.isoformat() if warehouse_stop else None
    wh_earliest_display = (
        to_utc_display_date(
            datetime.combine(warehouse_start, time.min, tzinfo=UTC)
        )
        if warehouse_start else None
    )
    wh_latest_display = (
        to_utc_display_date(
            datetime.combine(warehouse_stop, time.min, tzinfo=UTC)
        )
        if warehouse_stop else None
    )

    if not rows:
        return ThroughputData(
            daily=(),
            headline="No completed items in this window.",
            first_date_iso=None,
            last_date_iso=None,
            warehouse_earliest_iso=wh_earliest_iso,
            warehouse_latest_iso=wh_latest_iso,
            warehouse_earliest_display=wh_earliest_display,
            warehouse_latest_display=wh_latest_display,
        )

    # Dict keyed by date for O(1) lookup during the enumeration.
    by_date: dict[date, int] = {d: int(n) for d, n in rows}
    # Span the view window when one's been chosen — Vacanti says
    # throughput averages cover the chosen PERIOD, not just the
    # observed-completion span. A 30-day view with 4 completion
    # days should average over 30, not 4. Without a view, fall
    # back to the data-derived range.
    if view is not None:
        first_d = view.from_
        last_d = view.to
    else:
        first_d = min(by_date)
        last_d = max(by_date)

    # Enumerate every calendar date in [first_d, last_d]. Zero-count
    # days fill in for dates with no completions — Vacanti's rule.
    daily: list[DailyThroughput] = []
    cur = first_d
    while cur <= last_d:
        # Anchor the date as a UTC midnight datetime so the shared
        # utility's TZ-strict validation accepts it.
        anchored = datetime.combine(cur, time.min, tzinfo=UTC)
        # `data_coverage`: inside the materialise window =
        # `warehouse`; outside = `missing` (gap; backfill-able).
        # Without both warehouse bounds we can't tell coverage
        # from actual zeros, so default `warehouse` for the
        # whole series.
        coverage: Literal["warehouse", "missing", "stale"]
        if warehouse_start is None or warehouse_stop is None:
            coverage = "warehouse"
        elif warehouse_start <= cur <= warehouse_stop:
            coverage = "warehouse"
        else:
            coverage = "missing"
        # Python: weekday() returns 0=Mon … 6=Sun. Sat/Sun =
        # weekend. Per-contract holiday calendar is a future
        # hook (would override weekend/weekday on those dates).
        day_type: Literal["weekday", "weekend", "holiday"] = (
            "weekend" if cur.weekday() >= 5 else "weekday"
        )
        daily.append(
            DailyThroughput(
                date_iso=to_utc_iso_date(anchored),
                date_display=to_utc_display_date(anchored),
                count=by_date.get(cur, 0),
                day_type=day_type,
                data_coverage=coverage,
            )
        )
        cur += timedelta(days=1)

    total = sum(d.count for d in daily)
    span_days = len(daily)
    # Throughput average divides by COVERED days only — days the
    # warehouse actually has data for. A missing day is "we
    # didn't observe this", NOT "zero items completed", so
    # averaging it in would understate the rate. With a 30-day
    # view over a 7-day materialise window, the divisor is 7.
    covered_days = sum(1 for d in daily if d.data_coverage == "warehouse")
    avg = total / covered_days if covered_days else 0.0
    if covered_days == span_days:
        # No gaps — straightforward.
        headline = (
            f"{total} items over {span_days} day"
            f"{'' if span_days == 1 else 's'} · {avg:.1f}/day"
        )
    else:
        # Gaps present — name BOTH numbers so the rate isn't
        # mistaken for a per-window-day average.
        headline = (
            f"{total} items · {avg:.1f}/day over {covered_days} day"
            f"{'' if covered_days == 1 else 's'} with data "
            f"({span_days}-day window)"
        )

    return ThroughputData(
        daily=tuple(daily),
        headline=headline,
        first_date_iso=daily[0].date_iso,
        last_date_iso=daily[-1].date_iso,
        warehouse_earliest_iso=wh_earliest_iso,
        warehouse_latest_iso=wh_latest_iso,
        warehouse_earliest_display=wh_earliest_display,
        warehouse_latest_display=wh_latest_display,
    )
