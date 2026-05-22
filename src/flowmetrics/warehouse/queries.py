"""Layer 1 — raw warehouse queries.

Each function takes a DuckDB connection and returns a list of
frozen row dataclasses. No windowing, no decisions: this layer
only fetches. `flowmetrics.charts` (Layer 2) windows and decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

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


@dataclass(frozen=True)
class InFlightItem:
    """One in-flight work item at a snapshot date, with its current
    workflow state resolved — the latest transition at or before
    the snapshot, or `"Unknown"` if it has never transitioned."""

    item_id: str
    title: str | None
    url: str | None
    created_at: datetime
    current_state: str


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


def in_flight_snapshot(
    con: duckdb.DuckDBPyConnection, contract_name: str, asof: date
) -> list[InFlightItem]:
    """Items in flight at `asof` — created on or before it and not
    yet completed by it — each tagged with its current state (the
    latest transition at or before `asof`). One window-function
    query, not N+1: per-item state resolution is a single pass.
    """
    rows = con.execute(
        """
        WITH latest_state AS (
            SELECT item_id, stage,
                   ROW_NUMBER() OVER (
                       PARTITION BY item_id ORDER BY entered_at DESC
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
    return [
        InFlightItem(
            item_id=str(item_id),
            title=str(title) if title is not None else None,
            url=str(url) if url is not None else None,
            created_at=created_at,
            current_state=str(current_state),
        )
        for (item_id, title, url, created_at, current_state) in rows
    ]


def count_open_items(
    con: duckdb.DuckDBPyConnection, contract_name: str
) -> int:
    """How many work items have no completion recorded — i.e.
    whether the warehouse has ever captured open work at all
    (distinguishes a never-captured snapshot from a genuinely
    empty one)."""
    return int(
        con.execute(
            "SELECT count(*) FROM work_items "
            "WHERE contract_id = ? AND completed_at IS NULL",
            [contract_name],
        ).fetchone()[0]
    )
