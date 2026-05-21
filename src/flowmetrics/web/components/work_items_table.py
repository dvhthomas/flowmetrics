"""Work-items table component — column-config-driven.

Renders a sortable, filterable, paginated table of work items for
the current contract. Routes pass a column list (or accept the
default); the template iterates without metric-specific branching.
Server-side sort + filter + pagination via HTMX.

Column kinds the template understands (`Column.kind`):

  "id-link"        — link to the item's lifecycle page
  "title-link"     — title text with optional external `url`
  "date"           — display string of a date field
  "in-flight-date" — like "date" but renders "— in flight —"
                     when empty
  "int"            — integer-valued numeric, "—" for None

Pagination defaults to 25 items per page. The `count` field on the
payload is the TOTAL matching rows across all pages; `len(rows)`
is items on the current page.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import duckdb

from ...utc_dates import attach_utc, to_utc_display_date, to_utc_iso_date
from ...windows import Window

SortKey = Literal[
    "item_id",
    "title",
    "created_at",
    "completed_at",
    "cycle_time_days",
    # Virtual sort key for the Age column. Age isn't a column on
    # disk — it's `asof - created_at`. Sort-by-age MAPS to
    # ORDER BY created_at with direction inverted (older =
    # higher age). The mapping happens inside render().
    "age_days",
]
SortDir = Literal["asc", "desc"]

DEFAULT_PAGE_SIZE = 25


@dataclass(frozen=True)
class Column:
    """One displayable column on the work-items table.

    `key`     — WorkItemRow attribute name to render in cells.
    `label`   — header text.
    `sort_key`— SQL column to ORDER BY when this header is clicked.
                None means the column is not sortable.
    `align`   — "left" | "right".
    `kind`    — rendering hint, dispatched in the template macro.
                See module docstring for the valid set.
    """

    key: str
    label: str
    sort_key: str | None = None
    align: str = "left"
    kind: str = "text"


@dataclass(frozen=True)
class WorkItemRow:
    item_id: str
    title: str
    url: str | None
    source: str  # 'github' or 'jira'
    created_at: str            # YYYY-MM-DD (UTC) — start date
    created_at_display: str    # "May 04, 2026"
    completed_at: str          # YYYY-MM-DD (UTC); "" for in-flight
    completed_at_display: str  # "May 04, 2026"; "" for in-flight
    cycle_time_days: float | None
    age_days: int | None


@dataclass(frozen=True)
class WorkItemsTableData:
    rows: tuple[WorkItemRow, ...]
    count: int  # TOTAL matching rows (across all pages)
    q: str
    completed_on: str
    completed_on_display: str
    # Echoed for the template — pagination / sort / search links
    # must propagate `in_flight_at` so clicking "Next" or a sort
    # header on the aging page doesn't silently drop the in-flight
    # filter and fall back to the completed-only scope.
    in_flight_at: str
    sort: SortKey
    direction: SortDir
    columns: tuple[Column, ...]
    # Pagination state. `page` is 1-indexed. `total_pages` is
    # ceil(count / page_size); never less than 1 so the partial
    # can render "Page 1 of 1" even on empty datasets without
    # divide-by-zero.
    page: int
    page_size: int
    total_pages: int


# Whitelist for sort keys so we can safely interpolate into SQL
# without parameter binding (DuckDB doesn't support parameterised
# ORDER BY column names). Keep the list closed.
_SORT_COLUMN_SQL: dict[str, str] = {
    "item_id": "item_id",
    "title": "title",
    "created_at": "created_at",
    "completed_at": "completed_at",
    "cycle_time_days": "cycle_time_days",
    # Virtual: maps to created_at; direction is inverted in
    # render() so "Age desc" reads as "oldest first".
    "age_days": "created_at",
}


# Default column set — the completed-items view used on the
# dashboard's old position, cycle-time detail, and throughput
# detail. Routes can override.
DEFAULT_COLUMNS: tuple[Column, ...] = (
    Column(key="item_id", label="#", kind="id-link"),
    Column(key="title", label="Title", sort_key="title", kind="title-link"),
    Column(
        key="created_at_display",
        label="Started",
        sort_key="created_at",
        kind="date",
    ),
    Column(
        key="completed_at_display",
        label="Completed",
        sort_key="completed_at",
        kind="in-flight-date",
    ),
    Column(
        key="cycle_time_days",
        label="Cycle (d)",
        sort_key="cycle_time_days",
        align="right",
        kind="int",
    ),
)


# In-flight scope (aging detail page). Drops Completed + Cycle —
# both always empty for open items — and adds Age.
IN_FLIGHT_COLUMNS: tuple[Column, ...] = (
    Column(key="item_id", label="#", kind="id-link"),
    Column(key="title", label="Title", sort_key="title", kind="title-link"),
    Column(
        key="created_at_display",
        label="Started",
        sort_key="created_at",
        kind="date",
    ),
    # Sortable: clicking Age toggles ORDER BY created_at with
    # inverted direction so "Age ↓" = oldest first.
    Column(
        key="age_days",
        label="Age (d)",
        sort_key="age_days",
        align="right",
        kind="int",
    ),
)


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    q: str | None = None,
    completed_on: str | None = None,
    in_flight_at: str | None = None,
    sort: SortKey = "completed_at",
    direction: SortDir = "desc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    columns: tuple[Column, ...] | None = None,
    wip_states: tuple[str, ...] | None = None,
    view: Window | None = None,
) -> WorkItemsTableData:
    """Read a page of rows for the contract.

    `q` does a case-insensitive substring filter on `title`.
    `completed_on` is a UTC ISO date — only items completed on
    that exact date are returned (used by the throughput chart's
    bar-click handler).
    `in_flight_at` is a UTC ISO date — only items in-flight at
    that date are returned (used by the aging detail page).
    All filters compose (AND).
    `sort`/`direction` come from the SortKey whitelist; invalid
    values fall back to defaults.
    `page` is 1-indexed; `page_size` clamps to 1..200 to keep a
    single request bounded. `count` on the result is the total
    matching across all pages.
    `columns` overrides the rendered column set. Defaults to
    IN_FLIGHT_COLUMNS when `in_flight_at` is set, else
    DEFAULT_COLUMNS.
    """
    if sort not in _SORT_COLUMN_SQL:
        sort = "completed_at"
    if direction not in ("asc", "desc"):
        direction = "desc"
    if page < 1:
        page = 1
    # Bounded page size — prevents an accidentally-huge request
    # from hanging the server. 200 is generous for "I want to see
    # a lot at once" while still capping the cost.
    page_size = max(1, min(int(page_size), 200))

    if columns is None:
        columns = IN_FLIGHT_COLUMNS if in_flight_at else DEFAULT_COLUMNS

    sql_column = _SORT_COLUMN_SQL[sort]
    # Age is `asof - created_at`, so "Age DESC" (oldest first)
    # maps to ORDER BY created_at ASC. Invert the SQL direction
    # for the virtual age_days sort key while keeping the
    # user-facing direction unchanged.
    effective_direction = direction
    if sort == "age_days":
        effective_direction = "desc" if direction == "asc" else "asc"
    order = "ASC" if effective_direction == "asc" else "DESC"
    # In-flight rows have completed_at NULL — relax the
    # "completed_at IS NOT NULL" guard when an in_flight_at
    # filter is active.
    completed_required = "AND completed_at IS NOT NULL" if not in_flight_at else ""
    in_flight_filter = (
        "AND CAST(created_at AS DATE) <= CAST(? AS DATE) "
        "AND (completed_at IS NULL "
        "     OR CAST(completed_at AS DATE) > CAST(? AS DATE))"
        if in_flight_at
        else ""
    )

    # When the table is scoped to in-flight items AND the
    # contract declares WIP states, mirror the aging-chart
    # filter: only items whose CURRENT state (latest transition
    # at or before asof) is in `wip_states`. Without this, the
    # table over-collects — Cassandra's chart shows 322 WIP
    # items but the table would page through all 3000+ items
    # still in any open state (incl. backlog like Triage Needed).
    wip_join = ""
    wip_clause = ""
    wip_params: list = []
    if in_flight_at and wip_states:
        placeholders = ",".join("?" for _ in wip_states)
        # Subquery: for each item, latest transition stage
        # at or before asof. DuckDB's QUALIFY filters the
        # window output without an explicit CTE.
        wip_join = (
            " JOIN ("
            "   SELECT item_id, source, stage AS current_state "
            "   FROM transitions "
            "   WHERE contract_id = ? "
            "     AND CAST(entered_at AS DATE) <= CAST(? AS DATE) "
            "   QUALIFY ROW_NUMBER() OVER ("
            "     PARTITION BY item_id, source "
            "     ORDER BY entered_at DESC"
            "   ) = 1"
            " ) cs USING (item_id, source)"
        )
        wip_params = [contract_name, in_flight_at]
        wip_clause = f" AND cs.current_state IN ({placeholders}) "

    # View-window filter: clamp completed-item rows to the
    # chart's date range so the table never shows rows the
    # chart's view window excludes. Only applies in the
    # completed-items mode — in-flight rows have no completed_at
    # to bound, and the aging page bounds them via in_flight_at.
    view_clause = ""
    view_params: list = []
    if view is not None and not in_flight_at:
        view_clause = (
            " AND CAST(completed_at AS DATE) BETWEEN "
            "CAST(? AS DATE) AND CAST(? AS DATE) "
        )
        view_params = [view.from_, view.to]

    # Build the WHERE clause + params once, used by both the
    # total-count query and the data query.
    where_clause = (
        " WHERE contract_id = ? "
        "  AND created_at IS NOT NULL "
        f"  {completed_required} "
        "  AND (? = '' OR lower(title) LIKE ?) "
        "  AND (? = '' OR CAST(completed_at AS DATE) = CAST(? AS DATE)) "
        f"  {in_flight_filter} "
        f"  {wip_clause}"
        f"  {view_clause}"
    )
    pattern = f"%{(q or '').lower()}%"
    completed_on_arg = completed_on or ""
    where_params: list = [*wip_params, contract_name,
                           q or "", pattern,
                           completed_on_arg, completed_on_arg]
    if in_flight_at:
        where_params.extend([in_flight_at, in_flight_at])
    if in_flight_at and wip_states:
        where_params.extend(list(wip_states))
    where_params.extend(view_params)

    # Total matching rows — feeds the pager.
    total_count = con.execute(
        "SELECT count(*) FROM work_items" + wip_join + where_clause,
        where_params,
    ).fetchone()[0]

    # Stable secondary order by item_id so equal-key rows don't
    # flicker between requests.
    data_sql = (
        "SELECT source, item_id, title, url, "
        "       created_at, completed_at, cycle_time_days "
        "FROM work_items"
        + wip_join
        + where_clause
        + f"ORDER BY {sql_column} {order}, item_id ASC "
        + "LIMIT ? OFFSET ?"
    )
    offset = (page - 1) * page_size
    rows = con.execute(
        data_sql, where_params + [page_size, offset]
    ).fetchall()

    # Pre-parse the asof date once so we can compute age per row
    # without re-parsing the same ISO string N times.
    from datetime import date as _date

    asof_date = _date.fromisoformat(in_flight_at) if in_flight_at else None

    def _age(created_at) -> int | None:
        """Vacanti's Age = CD - SD + 1 (same +1 inclusive rule as
        cycle time; same-day items are 1d, never 0d). Computed
        here at query/view time because `asof` is a runtime
        parameter — materialise can't precompute it."""
        if asof_date is None or created_at is None:
            return None
        aware = attach_utc(created_at)
        return (asof_date - aware.date()).days + 1

    table_rows = tuple(
        WorkItemRow(
            item_id=str(item_id),
            title=str(title) if title is not None else "",
            url=str(url) if url is not None else None,
            source=str(source),
            created_at=to_utc_iso_date(attach_utc(created_at)) if created_at else "",
            created_at_display=(
                to_utc_display_date(attach_utc(created_at)) if created_at else ""
            ),
            completed_at=to_utc_iso_date(attach_utc(completed_at)) if completed_at else "",
            completed_at_display=(
                to_utc_display_date(attach_utc(completed_at)) if completed_at else ""
            ),
            cycle_time_days=(
                float(cycle_time_days) if cycle_time_days is not None else None
            ),
            age_days=_age(created_at),
        )
        for (source, item_id, title, url, created_at, completed_at, cycle_time_days) in rows
    )

    # Pre-format the date filter's display string so the template
    # doesn't have to.
    if completed_on_arg:
        from datetime import UTC, date, datetime

        try:
            iso_date = date.fromisoformat(completed_on_arg)
            anchor = datetime(
                iso_date.year, iso_date.month, iso_date.day, tzinfo=UTC
            )
            completed_on_display = to_utc_display_date(anchor)
        except ValueError:
            completed_on_display = completed_on_arg
    else:
        completed_on_display = ""

    total_pages = max(1, math.ceil(total_count / page_size))
    # Clamp `page` to the valid range — a stale URL pointing at
    # page 7 of a now-3-page result-set should silently land on
    # page 3 rather than 404.
    if page > total_pages:
        page = total_pages

    return WorkItemsTableData(
        rows=table_rows,
        count=int(total_count),
        q=q or "",
        completed_on=completed_on_arg,
        completed_on_display=completed_on_display,
        in_flight_at=in_flight_at or "",
        sort=sort,
        direction=direction,
        columns=columns,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )
