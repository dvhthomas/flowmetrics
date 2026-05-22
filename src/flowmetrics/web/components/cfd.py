"""Cumulative Flow Diagram component, per Vacanti.

For each calendar date in the window and each workflow stage,
plot the cumulative count of items that have ENTERED that stage
by that date. Stack bands in workflow order (first stage at top
of the stack, terminal stage at bottom).

Mathematical contract — the invariants that make a CFD readable:

  1. Cumulative arrivals only go UP over time.
     `count(stage, date) ≥ count(stage, date - 1)` for every stage.

  2. Earlier stages dominate later stages.
     `count(stage_N, date) ≥ count(stage_N+1, date)` for every
     date and every adjacent pair in workflow order — items must
     reach stage_N before reaching stage_N+1. The difference
     between adjacent cumulatives IS the WIP in the earlier stage
     at that date.

  3. The terminal stage's cumulative at the final date equals
     the count of completed items in `work_items`. Cross-table
     agreement guard.

The component reads `transitions` (each row = a stage entry
event). It collapses duplicate events per (item, stage) to a
single FIRST-ENTRY timestamp per item per stage — even if the
source emits multiple "entered Awaiting Review" events (e.g.
because the item went back-and-forth), the CFD counts the item
as having entered Awaiting Review starting from its FIRST entry.
That's the conservative reading; flow-cycling between stages is
a separate analysis.

Stage workflow order is inferred from data via pairwise
precedence: for each PAIR of stages (A, B), count items where A
enters before B. The stage that precedes the most others is
"first"; the stage preceded by the most others is "terminal".
Robust against items skipping an early stage (e.g. PRs opened
straight into Awaiting Review, never Draft) — naive "median
entered_at" or "median rank" tie at 1 in that case and tiebreak
alphabetically, which gives a wrong-looking order. A contract
YAML override is a future hook.

Reference: Vacanti, *Actionable Agile Metrics for Predictability*,
10th Anniversary Edition, ch. 3.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import duckdb

from ...contract import WorkflowStates
from ...utc_dates import to_utc_display_date
from ...windows import Window


# Color tokens — neutral grays with one accent. The terminal
# stage (departures) gets the brand plum since departures are
# the metric the page exists to surface. Other stages cycle
# through neutral shades.
_BAND_COLORS = [
    "__theme:border__",   # lightest — first stage (incoming queue)
    "__theme:muted__",    # mid — WIP stages
    "__theme:p-500__",    # accent — terminal (departures)
]


@dataclass(frozen=True)
class CfdDailyPoint:
    """Cumulative arrivals at each stage as of `date_iso`."""

    date_iso: str         # YYYY-MM-DD (UTC)
    date_display: str     # "May 04, 2026"
    counts: dict[str, int]  # stage_name → cumulative count entered by this date


@dataclass(frozen=True)
class CfdData:
    """Payload for the CFD tile partial."""

    daily: tuple[CfdDailyPoint, ...]
    stages: tuple[str, ...]   # in workflow order (first → terminal)
    headline: str
    first_date_iso: str | None
    last_date_iso: str | None

    def vega_spec_json(self) -> str:
        """Vega-Lite stacked area with per-stage cumulative counts.

        The stack order matches `stages` so bands read
        top-to-bottom in workflow progression: incoming queue at
        the top, terminal departures at the bottom. The fixture's
        GitHub model gives Draft → Awaiting Review → Merged.
        """
        # Long-form values: one row per (date, stage). Vega-Lite
        # stacks these by the `stage` color encoding's sort order.
        # We feed the per-stage WIP (count_stage - count_next_stage,
        # 0 for the terminal stage's items already there) so the
        # area heights ADD up to the cumulative-arrivals figure
        # without each band needing to know about other bands.
        # That's Vacanti's CFD math made explicit.
        values: list[dict] = []
        for d in self.daily:
            for i, stage in enumerate(self.stages):
                cur = d.counts[stage]
                # For non-terminal stages, the band height is
                # cur - count_of_next_stage. That's the WIP in
                # THIS stage at this date.
                if i < len(self.stages) - 1:
                    band_height = cur - d.counts[self.stages[i + 1]]
                else:
                    # Terminal stage: full cumulative (the items
                    # that have departed). Stacks at the bottom.
                    band_height = cur
                values.append({
                    "date_iso": d.date_iso,
                    "date_display": d.date_display,
                    "stage": stage,
                    "cumulative": cur,
                    "wip": max(0, band_height),
                    # Stable per-stage sort key so Vega stacks in
                    # workflow order regardless of dict iteration.
                    "stage_order": i,
                })

        # Color strategy: muted/pastel categorical scheme so each
        # band is visually distinguishable without screaming for
        # attention. The handcrafted "lightest at top, accent at
        # bottom" gradient couldn't distinguish 3+ middle WIP
        # stages from each other (they all collapsed to mid-gray).
        # Vega's `set3` is a soft 12-color qualitative palette
        # designed for stacked-area charts of this kind.

        # Y-axis floor slider: when a Period window starts partway
        # into the data, the bottom (departed) band carries an
        # inert base of items that completed BEFORE the window —
        # nothing about them moves inside it. This range control
        # raises the y-domain's floor so the operator can crop
        # that carry-in and zoom into the active bands.
        #
        # The max crop is the carry-in at the FIRST visible date,
        # not the final departed count: the terminal band grows
        # across the window, so cropping past its left-edge value
        # would eat into the curve that actually rises inside the
        # window. Default 0 = full chart, no crop.
        base_carry_in = (
            self.daily[0].counts[self.stages[-1]]
            if self.daily and self.stages else 0
        )
        floor_param: dict | None = None
        if base_carry_in > 1:
            floor_param = {
                "name": "cfdfloor",
                "value": 0,
                "bind": {
                    "input": "range",
                    "min": 0,
                    "max": base_carry_in,
                    "step": max(1, base_carry_in // 100),
                    "name": "Crop base — hide first N items  ",
                },
            }

        spec: dict = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "background": "transparent",
            "padding": 12,
            "width": "container",
            "data": {"values": values},
            # `clip` keeps the bands inside the plot rectangle so
            # raising the y-floor slider crops them cleanly instead
            # of spilling the cumulative areas below the axis.
            "mark": {"type": "area", "opacity": 0.95, "clip": True},
            "encoding": {
                "x": {
                    "field": "date_iso",
                    "type": "nominal",
                    # No outer padding — the cumulative area fills
                    # the plot edge-to-edge. The point-scale
                    # default (0.5) leaves an empty strip before
                    # the first date and after the last.
                    "scale": {"paddingOuter": 0},
                    "axis": {
                        "title": "Date (UTC)",
                        "labelAngle": 0,
                        # Thin labels to ~10 evenly-spaced ticks so
                        # wider windows stay legible. Nominal-axis
                        # `labelOverlap` doesn't reliably thin in
                        # Vega-Lite, so we pre-pick the dates that
                        # SHOULD show a label and let labelExpr
                        # render blank for the rest. utcFormat
                        # (NOT timeFormat) keeps it TZ-safe.
                        # Ceiling-division step so 11-19 dates
                        # actually get thinned (floor division
                        # kept step=1 in that range).
                        "values": [
                            d.date_iso
                            for d in self.daily[
                                :: max(1, (len(self.daily) + 9) // 10)
                            ]
                        ],
                        "labelExpr": (
                            "utcFormat(datetime(datum.value), '%b %d')"
                        ),
                    },
                    "sort": [d.date_iso for d in self.daily],
                },
                "y": {
                    "field": "wip",
                    "type": "quantitative",
                    "aggregate": "sum",
                    "stack": "zero",
                    "scale": (
                        {"domainMin": {"expr": "cfdfloor"}}
                        if floor_param else {}
                    ),
                    "axis": {"title": "Items"},
                },
                "color": {
                    "field": "stage",
                    "type": "nominal",
                    "scale": {
                        "domain": list(self.stages),
                        "scheme": "set3",
                    },
                    "legend": {
                        "title": None,
                        "orient": "top-right",
                    },
                },
                # Explicit stack order — Vega-Lite uses `order` to
                # decide which series sits on top. Terminal stage
                # at the bottom (low stage_order index? no, high
                # — `order` ascending means the lowest stack_order
                # paints at the bottom of the stack). We invert so
                # workflow-first → top of stack.
                "order": {
                    "field": "stage_order",
                    "type": "quantitative",
                    "sort": "descending",
                },
                "tooltip": [
                    {"field": "date_display", "type": "nominal", "title": "Date"},
                    {"field": "stage", "type": "nominal", "title": "Stage"},
                    {
                        "field": "wip",
                        "type": "quantitative",
                        "title": "WIP in stage",
                    },
                    {
                        "field": "cumulative",
                        "type": "quantitative",
                        "title": "Cumulative arrivals",
                    },
                ],
            },
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
        if floor_param is not None:
            spec["params"] = [floor_param]
        return json.dumps(spec)


def _empty(headline: str = "No transitions yet.") -> CfdData:
    return CfdData(
        daily=(),
        stages=(),
        headline=headline,
        first_date_iso=None,
        last_date_iso=None,
    )


def _infer_stage_order(
    con: duckdb.DuckDBPyConnection, contract_name: str
) -> tuple[str, ...]:
    """Pairwise-precedence ordering across all observed states.
    Each stage's net "precedes" count (precedes − preceded-by).
    Higher = earlier in workflow. Alphabetical tiebreak for
    pairs that never co-occur. Used when no contract states
    are declared."""
    pairwise_rows = con.execute(
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
    all_rows = con.execute(
        "SELECT DISTINCT stage FROM transitions WHERE contract_id = ?",
        [contract_name],
    ).fetchall()
    if not all_rows:
        return ()
    all_stages = sorted(str(s) for (s,) in all_rows)
    precedes: dict[str, int] = {s: 0 for s in all_stages}
    for earlier, later, cnt in pairwise_rows:
        precedes[str(earlier)] = precedes.get(str(earlier), 0) + int(cnt)
        precedes[str(later)] = precedes.get(str(later), 0) - int(cnt)
    return tuple(sorted(all_stages, key=lambda s: (-precedes[s], s)))


def _load_first_entries(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    only_stages: tuple[str, ...] | None,
) -> list[tuple[str, str, date]]:
    """Per-item first-entry per stage. When `only_stages` is set,
    raw transitions for other states are filtered out at the SQL
    layer (backlog exclusion). Ping-pong transitions collapse to
    the FIRST entry per (item, stage)."""
    if only_stages is not None:
        if not only_stages:
            return []
        placeholders = ",".join("?" for _ in only_stages)
        return con.execute(
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
    return con.execute(
        """
        SELECT item_id, stage,
               CAST(min(entered_at) AS DATE) AS entered_date
        FROM transitions
        WHERE contract_id = ?
        GROUP BY item_id, stage
        """,
        [contract_name],
    ).fetchall()


def _compute_reached_dates(
    first_entries: list[tuple[str, str, date]],
    stages: tuple[str, ...],
) -> dict[str, list[date]]:
    """For each (item, stage), the date at which the item
    reached this stage OR any later stage. Without this
    expansion, items that skip an early stage cause band
    crossings on the CFD (count(later) > count(earlier)) —
    mathematically nonsense.

    Per-item: a single backward sweep through the stage order
    from terminal to first. The running min propagates earlier
    so that later-stage entries set a "reached" date for every
    earlier stage too.
    """
    per_item: dict[str, dict[str, date]] = {}
    for item_id, stage, entered_date in first_entries:
        per_item.setdefault(str(item_id), {})[str(stage)] = entered_date
    reached: dict[str, list[date]] = {s: [] for s in stages}
    for entries in per_item.values():
        running: date | None = None
        for stage in reversed(stages):
            own = entries.get(stage)
            if own is not None and (running is None or own < running):
                running = own
            if running is not None:
                reached[stage].append(running)
    return reached


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    states: WorkflowStates | None = None,
    view: Window | None = None,
) -> CfdData:
    """Compute the CFD payload for `contract_name`.

    Reads `transitions` for the contract, collapses to
    first-entry-per-item-per-stage, and builds the cumulative
    arrivals per (date, stage).

    State classification (`states: WorkflowStates`):
      The CFD bands are `states.cfd_bands()` — wip + done in
      kanban order (top of stack = incoming WIP; bottom =
      departures). Each raw state is its OWN band. Raw
      transitions for states outside that list are treated as
      backlog and excluded from CFD math entirely (Vacanti —
      backlog is not WIP). When `states` is None, the CFD falls
      back to pairwise-precedence inference over all observed raw
      states.

    `view` — a VISUAL window. The cumulative math always covers
    the FULL transition history (the count at any date includes
    every prior arrival), so the carry-in at the window's left
    edge is the true running total. `view` only clamps which
    dates the x-axis shows; pair it with the y-axis floor slider
    to crop the inert carry-in base. When `view` is None the
    chart spans the full observed data span.
    """
    # Resolve stages. Explicit YAML wins; otherwise infer.
    if states is not None:
        stages: tuple[str, ...] = states.cfd_bands()
    else:
        stages = _infer_stage_order(con, contract_name)
        if not stages:
            return _empty()

    first_entries = _load_first_entries(
        con, contract_name,
        only_stages=stages if states is not None else None,
    )
    if not first_entries:
        return _empty()

    reached_by_stage = _compute_reached_dates(first_entries, stages)

    # X-axis span. `view` is a visual window — it clamps which
    # dates show, NOT the cumulative math. It is intersected with
    # the observed data span so the chart never paints empty
    # columns before the first arrival or after the last. Without
    # a view, the full observed history (first stage-entry → last).
    all_dates = [d for _, _, d in first_entries]
    data_min, data_max = min(all_dates), max(all_dates)
    if view is not None:
        first_date = max(view.from_, data_min)
        last_date = min(view.to, data_max)
    else:
        first_date, last_date = data_min, data_max
    if first_date > last_date:
        return _empty("No transitions in the selected period.")

    # Cumulative count per (date, stage). Sort each stage's
    # reached-date list once, then bisect for each date in the
    # window. O(stages × days × log(items)).
    from bisect import bisect_right
    for s in reached_by_stage:
        reached_by_stage[s].sort()

    daily: list[CfdDailyPoint] = []
    cur = first_date
    while cur <= last_date:
        counts: dict[str, int] = {}
        for stage in stages:
            counts[stage] = bisect_right(reached_by_stage[stage], cur)
        anchored = datetime(cur.year, cur.month, cur.day, tzinfo=UTC)
        daily.append(
            CfdDailyPoint(
                date_iso=cur.isoformat(),
                date_display=to_utc_display_date(anchored),
                counts=counts,
            )
        )
        cur += timedelta(days=1)

    last_point = daily[-1]
    terminal_stage = stages[-1]
    departures = last_point.counts[terminal_stage]
    # Distinct items the workflow has touched. count[stages[0]]
    # would undercount items that skipped the first stage (e.g.
    # PR opened straight into Awaiting Review).
    total_distinct = len({item_id for item_id, _, _ in first_entries})
    in_flight = total_distinct - departures
    headline = (
        f"{total_distinct} item"
        f"{'' if total_distinct == 1 else 's'} touched · "
        f"{departures} departed · {in_flight} in the system · "
        f"{len(daily)} day{'' if len(daily) == 1 else 's'} "
        f"({to_utc_display_date(datetime(first_date.year, first_date.month, first_date.day, tzinfo=UTC))} – "
        f"{to_utc_display_date(datetime(last_date.year, last_date.month, last_date.day, tzinfo=UTC))})"
    )

    return CfdData(
        daily=tuple(daily),
        stages=stages,
        headline=headline,
        first_date_iso=first_date.isoformat(),
        last_date_iso=last_date.isoformat(),
    )
