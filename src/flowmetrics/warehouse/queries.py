"""Layer 1 — raw warehouse queries.

Each function takes a DuckDB connection and returns a list of
frozen row dataclasses. No windowing, no decisions: this layer
only fetches. `flowmetrics.charts` (Layer 2) windows and decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import duckdb


@dataclass(frozen=True)
class CompletedItem:
    """One completed work item, straight from `work_items`.

    `completed_at` is non-null by construction (the query filters
    on it). `cycle_time_days` can still be null — a data-quality
    gap the model layer decides how to treat.
    """

    item_id: str
    title: str | None
    url: str | None
    completed_at: datetime
    cycle_time_days: float | None


def completed_items(
    con: duckdb.DuckDBPyConnection, contract_name: str
) -> list[CompletedItem]:
    """Every completed item for `contract_name`, oldest completion
    first. In-flight items (no `completed_at`) are excluded."""
    rows = con.execute(
        """
        SELECT item_id, title, url, completed_at, cycle_time_days
        FROM work_items
        WHERE contract_id = ? AND completed_at IS NOT NULL
        ORDER BY completed_at
        """,
        [contract_name],
    ).fetchall()
    return [
        CompletedItem(
            item_id=str(item_id),
            title=str(title) if title is not None else None,
            url=str(url) if url is not None else None,
            completed_at=completed_at,
            cycle_time_days=(
                float(cycle_time_days)
                if cycle_time_days is not None
                else None
            ),
        )
        for (item_id, title, url, completed_at, cycle_time_days) in rows
    ]
