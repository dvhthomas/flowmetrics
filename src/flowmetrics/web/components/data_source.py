"""Data Source coverage component.

A GitHub-style calendar heat-map of work-item creation dates —
the coverage view on the Data Source page. Each cell is one day,
shaded by how many work items were created that day; a day with
no creations is a distinct grey cell. The operator sees, at a
glance, which stretches of time have items and which are blank.

Coverage counts EVERY work item (`count(*)`), keyed by
`created_at` — not just completions. A workflow can be almost
entirely in-flight (Cassandra: 3,214 items, 17 completed); a
completions-only chart would wrongly read as "no data".

A blank cell means no work item was *created* on that day — it
does NOT by itself prove a materialise gap (it could be a
genuinely quiet day). The heat-map shows where creations cluster
and where they don't; the operator decides what to backfill.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import duckdb

from ...utc_dates import to_utc_display_date

# The chart shows at most this many daily cells — the most recent
# stretch of the data (~26 week columns in the calendar grid).
_MAX_DAYS = 180


def _coverage_level(records: int, max_records: int) -> str:
    """Bucket a day's work-item-creation count into a heat-map
    level. `"None"` = nothing created that day; otherwise Low /
    Medium / High by thirds of a LOG scale — log so one spike day
    doesn't flatten every other day into the bottom bucket."""
    if records <= 0:
        return "None"
    if max_records <= 1:
        return "Low"
    t = math.log(records) / math.log(max_records)  # 0 … 1
    if t <= 1 / 3:
        return "Low"
    if t <= 2 / 3:
        return "Medium"
    return "High"


@dataclass(frozen=True)
class DayBucket:
    """One day on the coverage chart."""

    day_iso: str  # "2025-04-05"
    day_display: str  # "Apr 05, 2025"
    records: int


@dataclass(frozen=True)
class DataSourceData:
    """Payload for the Data Source coverage view."""

    days: tuple[DayBucket, ...]
    total_records: int
    latest_display: str | None
    headline: str

    def vega_spec_json(self) -> str:
        """A GitHub-style calendar heat-map: one rect cell per day,
        laid out week (x) by weekday (y), shaded by how many work
        items were created that day. A day with no creations is a
        distinct grey 'None' cell — present, never omitted."""
        from datetime import date as _date

        counts = [b.records for b in self.days if b.records > 0]
        max_records = max(counts) if counts else 1

        weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        values: list[dict] = []
        week_starts: list[str] = []
        for b in self.days:
            d = _date.fromisoformat(b.day_iso)
            # Monday-anchored week — the cell's x column.
            week_start = (d - timedelta(days=d.weekday())).isoformat()
            if week_start not in week_starts:
                week_starts.append(week_start)
            values.append({
                "day": b.day_iso,
                "label": b.day_display,
                "records": b.records,
                "week": week_start,
                "weekday": weekday_names[d.weekday()],
                "level": _coverage_level(b.records, max_records),
            })

        # One x-axis label per month — the first week column whose
        # Monday lands in a new calendar month.
        month_ticks: list[str] = []
        seen_months: set[str] = set()
        for ws in week_starts:
            ym = ws[:7]
            if ym not in seen_months:
                seen_months.add(ym)
                month_ticks.append(ws)

        spec = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "background": "transparent",
            "width": "container",
            "height": 140,
            "title": {
                "text": "Work Items by Creation Date",
                # The chart is capped at the most recent 180 days
                # — name the charted span so nobody expects to
                # scroll back to year-old history here.
                "subtitle": (
                    f"{self.days[0].day_display} – "
                    f"{self.days[-1].day_display}"
                    " · most recent 180 days max"
                    if self.days
                    else "Most recent 180 days max"
                ),
                "anchor": "start",
                "color": "__theme:fg__",
                "fontSize": 13,
                "subtitleColor": "__theme:muted__",
                "subtitleFontSize": 11,
            },
            "data": {"values": values},
            "mark": {
                "type": "rect",
                "cornerRadius": 2,
                # A thin gap the colour of the page surface
                # separates the cells — the contribution-grid look.
                "stroke": "__theme:surface__",
                "strokeWidth": 3,
            },
            "encoding": {
                "x": {
                    "field": "week",
                    "type": "ordinal",
                    "sort": week_starts,
                    "axis": {
                        "title": "Created Date",
                        "labelAngle": 0,
                        "values": month_ticks,
                        "labelExpr": (
                            "utcFormat(datetime(datum.value), '%b %Y')"
                        ),
                        "domain": False,
                        "ticks": False,
                    },
                },
                "y": {
                    "field": "weekday",
                    "type": "ordinal",
                    "sort": weekday_names,
                    "axis": {
                        "title": None,
                        "values": ["Mon", "Wed", "Fri"],
                        "domain": False,
                        "ticks": False,
                    },
                },
                "color": {
                    "field": "level",
                    "type": "ordinal",
                    "scale": {
                        "domain": ["None", "Low", "Medium", "High"],
                        # None=grey; Low/Medium/High climb the plum
                        # ramp. Low is p-200 (not the near-white
                        # p-100) so a 1-2-item day reads as clearly
                        # coloured, not mistaken for an empty cell —
                        # otherwise genuine low-activity stretches
                        # (e.g. weekends) look falsely blank.
                        "range": [
                            "__theme:border__",
                            "__theme:p-200__",
                            "__theme:p-400__",
                            "__theme:p-700__",
                        ],
                    },
                    "legend": {
                        "title": None,
                        "orient": "bottom",
                        "direction": "horizontal",
                    },
                },
                "tooltip": [
                    {"field": "label", "type": "nominal", "title": "Day"},
                    {
                        "field": "records",
                        "type": "quantitative",
                        "title": "Work items created",
                    },
                ],
            },
            "config": {
                "view": {"stroke": None},
                "axis": {
                    "labelColor": "__theme:muted__",
                    "titleColor": "__theme:muted__",
                },
            },
        }
        return json.dumps(spec)


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
) -> DataSourceData:
    """Per-day work-item coverage for `contract_name`.

    Counts every work item by `created_at` day. The chart spans
    the most recent `_MAX_DAYS` of the data, with a zero-count
    bucket for every day in between so days with no creations
    still get a heat-map cell. Dates come from the data — the
    component invents no clock.
    """
    rows = con.execute(
        "SELECT CAST(created_at AS DATE) AS d, count(*) "
        "FROM work_items "
        "WHERE contract_id = ? AND created_at IS NOT NULL "
        "GROUP BY 1",
        [contract_name],
    ).fetchall()
    bounds = con.execute(
        "SELECT min(CAST(created_at AS DATE)), "
        "       max(CAST(created_at AS DATE)), count(*) "
        "FROM work_items "
        "WHERE contract_id = ? AND created_at IS NOT NULL",
        [contract_name],
    ).fetchone()
    earliest, latest, total = bounds if bounds else (None, None, 0)
    total = int(total or 0)

    if total == 0 or earliest is None or latest is None:
        return DataSourceData(
            days=(),
            total_records=0,
            latest_display=None,
            headline=(
                "No work items in the warehouse yet — use Backfill "
                "below to fetch data."
            ),
        )

    per_day = {d.isoformat(): int(count) for d, count in rows}

    # Span: the most recent _MAX_DAYS of the data, earliest →
    # latest. Capped so a long-lived workflow doesn't blow the
    # bar count up.
    span_start = latest - timedelta(days=_MAX_DAYS - 1)
    if span_start < earliest:
        span_start = earliest

    days: list[DayBucket] = []
    cur = span_start
    while cur <= latest:
        anc = datetime(cur.year, cur.month, cur.day, tzinfo=UTC)
        days.append(
            DayBucket(
                day_iso=cur.isoformat(),
                day_display=to_utc_display_date(anc),
                records=per_day.get(cur.isoformat(), 0),
            )
        )
        cur += timedelta(days=1)

    earliest_display = to_utc_display_date(
        datetime(earliest.year, earliest.month, earliest.day, tzinfo=UTC)
    )
    latest_display = to_utc_display_date(
        datetime(latest.year, latest.month, latest.day, tzinfo=UTC)
    )
    headline = (
        f"{total} work item{'' if total == 1 else 's'} in the "
        f"warehouse · created {earliest_display} – {latest_display}"
    )
    return DataSourceData(
        days=tuple(days),
        total_records=total,
        latest_display=latest_display,
        headline=headline,
    )
