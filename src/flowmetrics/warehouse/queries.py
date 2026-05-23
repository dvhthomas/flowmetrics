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


@dataclass(frozen=True)
class StageEntry:
    """An item's first entry into a stage, by calendar date."""

    item_id: str
    stage: str
    entered_date: date


def first_stage_entries(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    only_stages: tuple[str, ...] | None = None,
) -> list[StageEntry]:
    """First-entry date per (item, stage) for `contract_name`.
    Collapses ping-pong transitions to the first visit. When
    `only_stages` is set, transitions for other states are
    filtered out at the SQL layer — that's how backlog states get
    excluded from the CFD without round-tripping through Python.
    """
    if only_stages is not None:
        if not only_stages:
            return []
        placeholders = ",".join("?" for _ in only_stages)
        rows = con.execute(
            f"""
            SELECT item_id, stage,
                   CAST(min(entered_at) AS DATE) AS entered_date
            FROM transitions
            WHERE contract_id = ?
              AND stage IN ({placeholders})
            GROUP BY item_id, stage
            """,
            [contract_name, *only_stages],
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT item_id, stage,
                   CAST(min(entered_at) AS DATE) AS entered_date
            FROM transitions
            WHERE contract_id = ?
            GROUP BY item_id, stage
            """,
            [contract_name],
        ).fetchall()
    return [
        StageEntry(
            item_id=str(item_id),
            stage=str(stage),
            entered_date=entered_date,
        )
        for (item_id, stage, entered_date) in rows
    ]


def observed_stages(
    con: duckdb.DuckDBPyConnection, contract_name: str
) -> list[str]:
    """Every distinct stage that has appeared in `transitions` for
    the contract, sorted alphabetically."""
    rows = con.execute(
        "SELECT DISTINCT stage FROM transitions WHERE contract_id = ?",
        [contract_name],
    ).fetchall()
    return sorted(str(s) for (s,) in rows)


def pairwise_stage_precedence(
    con: duckdb.DuckDBPyConnection, contract_name: str
) -> list[tuple[str, str, int]]:
    """For every ordered pair `(A, B)` of stages, the count of
    items whose first entry into A preceded their first entry
    into B. The CFD's stage-order inference is built on these
    counts when no contract YAML pins the workflow."""
    rows = con.execute(
        """
        WITH item_stages AS (
            SELECT item_id, stage,
                   min(entered_at) AS first_entered
            FROM transitions
            WHERE contract_id = ?
            GROUP BY item_id, stage
        )
        SELECT a.stage AS earlier, b.stage AS later, count(*) AS cnt
        FROM item_stages a
        JOIN item_stages b ON a.item_id = b.item_id
        WHERE a.first_entered < b.first_entered
        GROUP BY 1, 2
        """,
        [contract_name],
    ).fetchall()
    return [(str(a), str(b), int(c)) for (a, b, c) in rows]


def creations_by_day(
    con: duckdb.DuckDBPyConnection, contract_name: str
) -> list[tuple[date, int]]:
    """Per-day count of work items created. Ascending date order;
    days with no creations are NOT included (the model fills them
    in)."""
    rows = con.execute(
        "SELECT CAST(created_at AS DATE), count(*) "
        "FROM work_items "
        "WHERE contract_id = ? AND created_at IS NOT NULL "
        "GROUP BY 1 ORDER BY 1",
        [contract_name],
    ).fetchall()
    return [(d, int(c)) for d, c in rows]


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


def completion_date_range(
    con: duckdb.DuckDBPyConnection, contract_name: str
) -> tuple[date | None, date | None]:
    """`(earliest, latest)` completed_at dates the warehouse holds
    for `contract_name`. `(None, None)` when no completions yet —
    drives both the filter-bar date-input bounds and the empty-
    state UIs that name where data actually exists."""
    row = con.execute(
        "SELECT min(CAST(completed_at AS DATE)), "
        "       max(CAST(completed_at AS DATE)) "
        "FROM work_items "
        "WHERE contract_id = ? AND completed_at IS NOT NULL",
        [contract_name],
    ).fetchone()
    if row and row[1] is not None:
        return row[0], row[1]
    return None, None


def latest_materialised_at(
    con: duckdb.DuckDBPyConnection, contract_name: str
) -> date | None:
    """The latest materialise date for `contract_name` — i.e. the
    asof of the warehouse's most recent in-flight snapshot. None
    when the warehouse holds no rows yet."""
    row = con.execute(
        "SELECT max(materialised_at) FROM work_items "
        "WHERE contract_id = ?",
        [contract_name],
    ).fetchone()
    mat = row[0] if row else None
    return mat.date() if mat is not None else None
