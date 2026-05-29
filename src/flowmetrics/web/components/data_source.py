"""Layer 3 — the Data Source coverage view.

`render()` orchestrates query → model; `to_vega()` translates a
`DataSourceModel` into a Vega-Lite calendar heat-map. Decisions
live in `flowmetrics.charts.data_source`.
"""

from __future__ import annotations

from typing import Any

import duckdb

from ...charts.data_source import (
    DataSourceModel,
    build_data_source_model,
)
from ...warehouse.queries import creations_by_day
from ._vega import to_vega

# Target plot width (px) the coverage heat-map's week columns spread to
# fill — the cell step is derived from this and the week count.
_FILL_WIDTH_PX = 1100


def render(
    con: duckdb.DuckDBPyConnection, contract_name: str
) -> DataSourceModel:
    """Read per-day work-item-creation counts and resolve the
    coverage model."""
    return build_data_source_model(creations_by_day(con, contract_name))


@to_vega.register
def _data_source_to_vega(model: DataSourceModel) -> dict[str, Any]:
    """Translate a `DataSourceModel` into a GitHub-style calendar
    heat-map: one rect per day, laid out week (x) by weekday (y),
    shaded by the model's pre-bucketed coverage level.
    """
    values = [
        {
            "day": d.day_iso,
            "label": d.day_display,
            "records": d.records,
            "week": d.week_iso,
            "weekday": d.weekday,
            "level": d.level,
        }
        for d in model.days
    ]
    if model.days:
        subtitle = (
            f"{model.days[0].day_display} – "
            f"{model.days[-1].day_display}"
            " · most recent 180 days max"
        )
    else:
        subtitle = "Most recent 180 days max"

    # Responsive square cells (the contribution-grid look). The step is
    # sized to consume the available width: ~`_FILL_WIDTH_PX` spread over
    # the week columns, clamped so a single day doesn't stretch into a
    # giant block (cap) and a long range doesn't shrink to specks (floor).
    weeks = len(model.week_starts) or 1
    step = max(16, min(40, round(_FILL_WIDTH_PX / weeks)))

    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "background": "transparent",
        "width": {"step": step},
        "height": {"step": step},
        "title": {
            "text": "Work Items by Creation Date",
            "subtitle": subtitle,
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
            # A thin gap the colour of the page surface separates
            # cells — the contribution-grid look.
            "stroke": "__theme:surface__",
            "strokeWidth": 3,
        },
        "encoding": {
            "x": {
                "field": "week",
                "type": "ordinal",
                "sort": list(model.week_starts),
                "axis": {
                    "title": "Created Date",
                    "labelAngle": 0,
                    "values": list(model.month_ticks),
                    "labelExpr": (
                        "utcFormat(datetime(datum.value), '%b %Y')"
                    ),
                    # Flush the boundary month labels to the plot edges so
                    # the leftmost ("Nov 2025") doesn't overflow into the
                    # weekday (Mon/Wed/Fri) labels, and keep them from
                    # colliding with each other.
                    "labelFlush": True,
                    "labelSeparation": 6,
                    "labelOverlap": False,
                    "domain": False,
                    "ticks": False,
                },
            },
            "y": {
                "field": "weekday",
                "type": "ordinal",
                "sort": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                # Pin the domain so the grid always has all seven rows —
                # a single weekday's data still lands in its own row of a
                # 7-row calendar instead of becoming the only (full-height)
                # row.
                "scale": {
                    "domain": [
                        "Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun",
                    ],
                },
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
                    # ramp. Low is p-200 (not the near-white p-100)
                    # so a 1-2-item day reads as clearly coloured.
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
            "view": {"fill": None, "stroke": None},
            "axis": {
                "labelColor": "__theme:muted__",
                "titleColor": "__theme:muted__",
            },
        },
    }
