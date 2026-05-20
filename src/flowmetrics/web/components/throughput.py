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

from ...utc_dates import to_utc_display_date, to_utc_iso_date
from ...windows import Window


@dataclass(frozen=True)
class DailyThroughput:
    """One row of the daily series: a calendar date + the number of
    items that finished on that exact UTC date."""

    date_iso: str          # YYYY-MM-DD (UTC)
    date_display: str      # "May 04, 2026" — pre-formatted for tooltips
    count: int
    # True for Saturdays and Sundays (UTC). The chart paints a faint
    # background band over weekend columns so weekend-vs-weekday is
    # visually obvious without reading the dates. Hardcoded to
    # Sat/Sun for v1 — a configurable weekend-days set is a future
    # hook (some teams' weekend isn't Sat/Sun).
    is_weekend: bool


@dataclass(frozen=True)
class ThroughputData:
    """Payload for the throughput tile partial."""

    daily: tuple[DailyThroughput, ...]
    headline: str
    # Window endpoints — exposed so the template can name the range
    # without re-deriving from `daily`.
    first_date_iso: str | None
    last_date_iso: str | None

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
                "is_weekend": d.is_weekend,
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

        # Shared x-axis encoding — referenced by both the weekend
        # shade layer and the bar layer so they line up exactly.
        x_encoding = {
            "field": "date_iso",
            "type": "nominal",
            "axis": {
                "title": "Completion date (UTC)",
                "labelAngle": 0,
                # `utcFormat` (not `timeFormat`) renders the
                # date ignoring browser TZ — same TZ-safety
                # contract the tooltip nominal-pre-format
                # idiom enforces, applied to the axis label.
                # Audited by `tests/test_chart_tooltip_safety.py`.
                "labelExpr": (
                    "utcFormat(datetime(datum.value), '%b %d')"
                ),
            },
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
                # under the bars (first layer). Filtered to
                # `is_weekend == true`; spans the full y-range via
                # `y` and `y2` left unbound (Vega-Lite stretches a
                # rect over the full chart height).
                {
                    "transform": [
                        {"filter": "datum.is_weekend"},
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
                # The throughput bars themselves.
                {
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
) -> ThroughputData:
    """Compute the daily throughput series for `contract_name`.

    `view`: clamps the x-axis to completions inside this
    inclusive date range. When None the window is data-derived
    (first completion → last completion).
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

    if not rows:
        return ThroughputData(
            daily=(),
            headline="No completed items in this window.",
            first_date_iso=None,
            last_date_iso=None,
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
        daily.append(
            DailyThroughput(
                date_iso=to_utc_iso_date(anchored),
                date_display=to_utc_display_date(anchored),
                count=by_date.get(cur, 0),
                # Python: weekday() returns 0=Mon … 6=Sun. Treat
                # Sat (5) and Sun (6) as weekend. Configurable
                # per-team is a future hook.
                is_weekend=cur.weekday() >= 5,
            )
        )
        cur += timedelta(days=1)

    total = sum(d.count for d in daily)
    span_days = len(daily)
    avg = total / span_days if span_days else 0.0
    headline = (
        f"{total} items over {span_days} day"
        f"{'' if span_days == 1 else 's'} · "
        f"{avg:.1f}/day"
    )

    return ThroughputData(
        daily=tuple(daily),
        headline=headline,
        first_date_iso=daily[0].date_iso,
        last_date_iso=daily[-1].date_iso,
    )
