"""Label-driven workflow materialization for GitHub PRs.

The caller names which labels constitute WIP via ``--wip-labels``. Every
other label is ignored. Per-PR ``LabeledEvent``/``UnlabeledEvent`` and
lifecycle (``MergedEvent``/``ClosedEvent``/``ReopenedEvent``) timestamps
are walked into a sequence of mutually-exclusive ``StatusInterval``s
that the rest of the pipeline already understands.

See ``docs/SPEC-github-labels.md`` for the determination rule and the
signal-quality contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .compute import StatusInterval, WorkItem

# State machine for the PR's lifecycle, distinct from labels.
_OPEN = "OPEN"
_CLOSED = "CLOSED"
_MERGED = "MERGED"

# Implicit columns. The user does not name these; they are produced by
# the materializer when no WIP label is applied, when a PR is merged
# cleanly, and when a PR is closed-not-merged. See spec §8b.
PRE_WIP_STATUS = "Pre-WIP"
DEPARTED_STATUS = "Departed"
ABANDONED_STATUS = "Abandoned"

_NON_WIP_STATUSES = frozenset({PRE_WIP_STATUS, DEPARTED_STATUS, ABANDONED_STATUS})


@dataclass(frozen=True)
class WipLabels:
    """Ordered, deduped list of label names the caller has declared as WIP.

    Order encodes "most progress wins" — the rightmost member of
    ``ordered`` wins when a PR concurrently holds more than one WIP
    label.
    """

    ordered: tuple[str, ...]

    @classmethod
    def parse(cls, raw: str) -> WipLabels:
        """Parse a comma-separated label list. Rejects empty / dupes.

        Entries are lowercased: GitHub enforces case-insensitive label
        uniqueness per repo (you can't create both `bug` and `Bug`),
        so a user typing `In-Progress` must match a real `in-progress`
        label. The dupe check runs after normalization, so `a,A` is
        also rejected.
        """
        parts = [p.strip().lower() for p in raw.split(",")]
        if any(not p for p in parts):
            raise ValueError(
                "--wip-labels has an empty entry; use 'a,b,c' with no trailing comma."
            )
        if len(set(parts)) != len(parts):
            raise ValueError(
                f"--wip-labels has duplicate entries (case-insensitive): {raw!r}"
            )
        if not parts:
            raise ValueError("--wip-labels must name at least one label.")
        return cls(ordered=tuple(parts))

    def contains(self, label: str) -> bool:
        return label in self.ordered

    def index_of(self, label: str) -> int | None:
        try:
            return self.ordered.index(label)
        except ValueError:
            return None


@dataclass(frozen=True)
class LabelEvent:
    """One LabeledEvent or UnlabeledEvent from a PR's GraphQL timeline."""

    at: datetime
    label: str
    kind: Literal["added", "removed"]


@dataclass(frozen=True)
class LifecycleEvent:
    """One MergedEvent / ClosedEvent / ReopenedEvent from a PR's timeline."""

    at: datetime
    kind: Literal["merged", "closed", "reopened"]


def _rightmost(active: set[str], wip: WipLabels) -> str | None:
    """Return the rightmost-in-``wip.ordered`` label currently in ``active``.

    "Most progress wins" — when a PR concurrently holds more than one
    WIP label, the one further along the user's ordered list is the
    resolved column.
    """
    best_idx = -1
    best_label: str | None = None
    for label in active:
        idx = wip.index_of(label)
        if idx is not None and idx > best_idx:
            best_idx = idx
            best_label = label
    return best_label


def _resolve_status(active: set[str], lifecycle: str, wip: WipLabels) -> str:
    """Map (active WIP labels, lifecycle state) → interval status.

    The five-row table from spec §8b. CLOSED-not-merged is always
    ABANDONED regardless of labels (the v3-spec fix). MERGED with a
    WIP label still applied stays in the WIP column — the merged-but-
    not-shipped signal.
    """
    if lifecycle == _CLOSED:
        return ABANDONED_STATUS
    rightmost = _rightmost(active, wip)
    if lifecycle == _MERGED:
        return rightmost if rightmost is not None else DEPARTED_STATUS
    # _OPEN
    return rightmost if rightmost is not None else PRE_WIP_STATUS


def materialize_status_intervals(
    *,
    created_at: datetime,
    asof: datetime,
    label_events: Sequence[LabelEvent],
    lifecycle_events: Sequence[LifecycleEvent],
    wip: WipLabels,
) -> list[StatusInterval]:
    """Walk a merged timeline of label + lifecycle events into a sequence
    of mutually-exclusive ``StatusInterval``s.

    See ``docs/SPEC-github-labels.md`` §8b for the resolution table.
    """
    # Drop label events whose label isn't in --wip-labels — they're
    # someone else's organizing principle.
    relevant_labels = [ev for ev in label_events if wip.contains(ev.label)]

    # Merge the two streams into one chronologically-ordered list. A
    # tagged union keeps the type narrow enough for mypy while letting
    # the loop branch on .kind.
    merged: list[LabelEvent | LifecycleEvent] = [*relevant_labels, *lifecycle_events]
    merged.sort(key=lambda ev: ev.at)

    intervals: list[StatusInterval] = []
    active: set[str] = set()
    lifecycle = _OPEN
    current_status = _resolve_status(active, lifecycle, wip)
    interval_start = created_at

    for ev in merged:
        if isinstance(ev, LabelEvent):
            if ev.kind == "added":
                active.add(ev.label)
            else:
                active.discard(ev.label)
        else:  # LifecycleEvent
            if ev.kind == "merged":
                lifecycle = _MERGED
            elif ev.kind == "closed":
                lifecycle = _CLOSED
            elif ev.kind == "reopened":
                lifecycle = _OPEN

        new_status = _resolve_status(active, lifecycle, wip)
        if new_status != current_status:
            # Same-instant transitions collapse: emit only when the
            # prior interval has non-zero duration, but always advance
            # current_status so the next emission reflects the latest.
            if ev.at > interval_start:
                intervals.append(StatusInterval(interval_start, ev.at, current_status))
                interval_start = ev.at
            current_status = new_status

    # Terminal status (Departed / Abandoned / merged-but-not-shipped WIP
    # column) extends to asof so CFD time-bucketing sees the post-terminal
    # band. For Aging this is moot — closed/merged PRs aren't returned by
    # the open-PR query in the first place.
    if asof > interval_start:
        intervals.append(StatusInterval(interval_start, asof, current_status))

    return intervals


def is_aging_wip(item: WorkItem) -> bool:
    """True iff the item's current interval is in one of the user's WIP
    columns — i.e. not Pre-WIP, Departed, or Abandoned. Used by `flow
    aging` in label mode to drop rows that aren't currently WIP."""
    if not item.status_intervals:
        return False
    return item.status_intervals[-1].status not in _NON_WIP_STATUSES
