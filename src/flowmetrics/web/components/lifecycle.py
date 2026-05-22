"""Per-item lifecycle component.

Answers "what happened to *this* item, in time order?". Reads from
the `transitions` Parquet (stage entry events written by
`flow materialise`) plus the item's `work_items` row (for title +
URL), and returns a typed payload the Jinja partial renders as a
Vega-Lite timeline.

The render function is the contract; the partial is the view. See
tests/test_lifecycle_component.py for the pinned shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import duckdb

from ...utc_dates import attach_utc, to_utc_display_date


class ItemNotFound(Exception):
    """The (contract, source, item_id) tuple did not match any row in
    the work_items table. The route layer maps this to a 404."""


@dataclass(frozen=True)
class LifecycleEvent:
    """One transition: an item entered `stage` at `entered_at`.

    `dwell_days` is the time spent in the *prior* stage — None for
    the first event (nothing before it to measure)."""

    stage: str
    signal: str
    entered_at_iso: str       # "2026-05-10T17:19:39Z" — UTC, jsonable
    entered_at_display: str   # "May 10, 2026 17:19 UTC" — humans
    dwell_days: float | None  # days since previous event; None on first


@dataclass(frozen=True)
class LifecycleStage:
    """A period the item spent in `stage`: from `entered_at` until
    the next event (which is when the item left this stage).

    Derived by pairing consecutive events. n events → n-1 stages;
    the last event is captured separately as `terminal_stage` on the
    LifecycleData payload."""

    stage: str
    entered_at_iso: str
    entered_at_display: str
    exited_at_iso: str
    exited_at_display: str
    duration_seconds: float
    duration_display: str  # "37m 39s" — pre-formatted for the chart
    # Y-axis label for the gantt: "Draft · 11m 8s". Combines the
    # stage name and its duration so the gantt's y-axis carries the
    # duration too — replaces an earlier in-bar text overlay that
    # was overflowing narrow bars and getting clipped near the left
    # chart edge.
    y_label: str


SourceLiteral = Literal["github", "jira"]


@dataclass(frozen=True)
class LifecycleData:
    item_id: str
    source: str
    title: str
    url: str | None
    events: tuple[LifecycleEvent, ...]
    stages: tuple[LifecycleStage, ...]
    # The terminal-state name (e.g. "Merged", "Done") — the stage the
    # item finally entered. None if there are 0 events (impossible
    # in practice; defensive).
    terminal_stage: str | None
    # JSON literals for Vega-Lite inline data — pre-stringified so
    # the template can drop them straight into `data: {values: …}`
    # blocks without further escaping.
    events_json: str
    stages_json: str
    # Whether a gantt-style chart is worth rendering for this item.
    # Trivial lifecycles (1 stage = 2 events: just "created then
    # finished") get a compact summary card instead. The chart only
    # adds insight when there are at least 2 stages to compare.
    is_chartable: bool
    # Canonical cycle time, copied from the work_items row so the
    # lifecycle page reports the SAME number the work-items table
    # does (Vacanti's elapsed-plus-one-day).
    cycle_time_days: float


def _display_datetime(d) -> str:
    """Human-readable UTC datetime — date via the shared utility,
    time and 'UTC' tag appended for hour-level resolution. The date
    is what `to_utc_display_date` would print; we add HH:MM here
    rather than in the utility because most callers only want dates."""
    aware = attach_utc(d)
    date_part = to_utc_display_date(aware)
    return f"{date_part} {aware.strftime('%H:%M')} UTC"


def _iso_z(d) -> str:
    """ISO-8601 with a trailing Z. The Python default `isoformat()`
    emits '+00:00'; Vega-Lite is happier with the literal 'Z' for
    time scales, and it's the standard JSON shape."""
    aware = attach_utc(d)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration_display(seconds: float) -> str:
    """Human-readable duration. Granularity follows the magnitude:

        < 60s        → "Ns"
        < 60m        → "Mm Ss"
        < 24h        → "Hh Mm"
        ≥ 24h        → "Dd Hh"
    """
    secs = int(round(seconds))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    days = secs // 86400
    hours = (secs % 86400) // 3600
    return f"{days}d {hours}h"


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    source: str,
    item_id: str,
) -> LifecycleData:
    """Read the item's identity + every transition, return a payload
    the timeline partial can render.

    Raises ItemNotFound if the (contract, source, item_id) tuple
    doesn't match any work_items row."""
    header_row = con.execute(
        "SELECT title, url, created_at, completed_at, cycle_time_days "
        "FROM work_items "
        "WHERE contract_id = ? AND source = ? AND item_id = ? "
        "LIMIT 1",
        [contract_name, source, item_id],
    ).fetchone()
    if header_row is None:
        raise ItemNotFound(
            f"no work item found for contract={contract_name!r} "
            f"source={source!r} item_id={item_id!r}"
        )
    title, url, header_created_at, header_completed_at, cycle_time_days = (
        header_row
    )

    rows = con.execute(
        "SELECT entered_at, stage, signal FROM transitions "
        "WHERE contract_id = ? AND source = ? AND item_id = ? "
        "ORDER BY entered_at ASC",
        [contract_name, source, item_id],
    ).fetchall()

    # Drop transitions that landed AFTER completion. Post-merge
    # label edits (e.g. someone re-tagging a PR days later) end
    # up in the transitions table — they don't change cycle time
    # (which is computed from work_items.created_at/completed_at)
    # but they pollute the lifecycle view with a fake "Merged ·
    # 2d 8h" dwell that's actually post-cycle activity. Truncate
    # the events to the cycle window. For in-flight items
    # (completed_at IS NULL), keep every event — the open
    # lifecycle has no truncation point.
    if header_completed_at is not None:
        completed_aware = attach_utc(header_completed_at)
        # Compare at second-precision: the completion event's
        # transition often has microsecond-level offset from the
        # work_item's completed_at (Jira returns one timestamp at
        # ms resolution, the API records both fields separately).
        # Without truncating to seconds we'd drop the very event
        # that marked completion — leaving the lifecycle with 0
        # stages and crashing the summary template.
        completed_seconds = completed_aware.replace(microsecond=0)
        rows = [
            (entered_at, stage, signal)
            for entered_at, stage, signal in rows
            if attach_utc(entered_at).replace(microsecond=0)
            <= completed_seconds
        ]

    events: list[LifecycleEvent] = []
    prev_dt = None
    for entered_at, stage, signal in rows:
        aware = attach_utc(entered_at)
        dwell: float | None = None
        if prev_dt is not None:
            dwell = (aware - prev_dt).total_seconds() / 86400.0
            # Floor at 0 for numerical noise; never go negative.
            if dwell < 0:
                dwell = 0.0
        events.append(
            LifecycleEvent(
                stage=str(stage),
                signal=str(signal),
                entered_at_iso=_iso_z(aware),
                entered_at_display=_display_datetime(aware),
                dwell_days=dwell,
            )
        )
        prev_dt = aware

    # Pair consecutive events into stages. n events → n-1 stages.
    # The final event names the terminal state (Merged, Done) — it
    # doesn't anchor a stage of its own because there's no "next
    # event" to give it an exit time.
    stages: list[LifecycleStage] = []
    for i in range(len(events) - 1):
        cur, nxt = events[i], events[i + 1]
        # Re-parse the ISO strings to compute duration so the source
        # of truth is the already-formatted iso (avoids any drift
        # between the iso strings and the seconds value).
        dur = (
            datetime.fromisoformat(nxt.entered_at_iso.replace("Z", "+00:00"))
            - datetime.fromisoformat(cur.entered_at_iso.replace("Z", "+00:00"))
        ).total_seconds()
        if dur < 0:
            dur = 0.0
        dur_display = _duration_display(dur)
        stages.append(
            LifecycleStage(
                stage=cur.stage,
                entered_at_iso=cur.entered_at_iso,
                entered_at_display=cur.entered_at_display,
                exited_at_iso=nxt.entered_at_iso,
                exited_at_display=nxt.entered_at_display,
                duration_seconds=dur,
                duration_display=dur_display,
                y_label=f"{cur.stage} · {dur_display}",
            )
        )

    # Pre-stringified JSON for Vega-Lite inline data — both for the
    # event-level scatter (events_json) and the stage-level gantt
    # (stages_json). The template's `|safe` filter is the only
    # escape boundary.
    events_json = json.dumps(
        [
            {
                "stage": e.stage,
                "signal": e.signal,
                "entered_at_iso": e.entered_at_iso,
                "entered_at_display": e.entered_at_display,
                "dwell_days": e.dwell_days,
            }
            for e in events
        ]
    )
    stages_json = json.dumps(
        [
            {
                "stage": s.stage,
                "entered_at_iso": s.entered_at_iso,
                "entered_at_display": s.entered_at_display,
                "exited_at_iso": s.exited_at_iso,
                "exited_at_display": s.exited_at_display,
                "duration_seconds": s.duration_seconds,
                "duration_display": s.duration_display,
                "y_label": s.y_label,
            }
            for s in stages
        ]
    )

    return LifecycleData(
        item_id=item_id,
        source=source,
        title=str(title) if title is not None else "",
        url=str(url) if url is not None else None,
        events=tuple(events),
        stages=tuple(stages),
        terminal_stage=events[-1].stage if events else None,
        events_json=events_json,
        stages_json=stages_json,
        # Chart is informative only when there are ≥2 stages to
        # compare. A single-stage lifecycle (e.g. PR created directly
        # into review, then merged) gets a compact summary card on
        # the view side instead — the chart would just show one bar.
        is_chartable=len(stages) >= 2,
        cycle_time_days=(
            float(cycle_time_days) if cycle_time_days is not None else 0.0
        ),
    )
