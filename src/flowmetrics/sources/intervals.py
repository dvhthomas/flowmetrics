"""Bridge: WorkItem.status_intervals → canonical StageTransition[].

The bridge lets Phase 1 stand up a canonical stream WITHOUT
rewriting source adapters. Existing sources already populate
`WorkItem.status_intervals`; the bridge maps each interval to a
`StageTransition`, picking the best-effort signal for that row.

The signal mapping is intentionally coarse:

  Jira:
    first interval         → SIGNAL_JIRA_ISSUE_CREATED
    terminal (if resolved) → SIGNAL_JIRA_RESOLVED
    everything else        → SIGNAL_JIRA_STATUS_CHANGED

  GitHub:
    first interval         → SIGNAL_GITHUB_PR_CREATED
    terminal (if merged)   → SIGNAL_GITHUB_PR_MERGED
    everything else        → SIGNAL_GITHUB_LABEL_ADDED

Finer-grained PR-lifecycle signals (ready-for-review, review
approved, etc.) come later — once a source adapter understands
how to emit them at fetch time, the bridge step is dropped for
that source.

The bridge is deleted once every metric reads the canonical
stream natively (Phase 4.5).
"""

from __future__ import annotations

from .. import signals
from ..canonical import StageTransition
from ..compute import WorkItem


def jira_workitem_to_transitions(item: WorkItem) -> list[StageTransition]:
    intervals = item.status_intervals
    if not intervals:
        return []
    rows: list[StageTransition] = []
    last = len(intervals) - 1
    for i, iv in enumerate(intervals):
        if i == 0:
            signal = signals.SIGNAL_JIRA_ISSUE_CREATED
        elif i == last and item.completed_at is not None:
            signal = signals.SIGNAL_JIRA_RESOLVED
        else:
            signal = signals.SIGNAL_JIRA_STATUS_CHANGED
        rows.append(
            StageTransition(
                item_id=item.item_id,
                entered_at=iv.start,
                stage=iv.status,
                signal=signal,
            )
        )
    return rows


def github_workitem_to_transitions(item: WorkItem) -> list[StageTransition]:
    intervals = item.status_intervals
    if not intervals:
        return []
    rows: list[StageTransition] = []
    last = len(intervals) - 1
    for i, iv in enumerate(intervals):
        if i == 0:
            signal = signals.SIGNAL_GITHUB_PR_CREATED
        elif i == last and item.completed_at is not None:
            signal = signals.SIGNAL_GITHUB_PR_MERGED
        else:
            signal = signals.SIGNAL_GITHUB_LABEL_ADDED
        rows.append(
            StageTransition(
                item_id=item.item_id,
                entered_at=iv.start,
                stage=iv.status,
                signal=signal,
            )
        )
    return rows


def workitem_to_transitions(item: WorkItem) -> list[StageTransition]:
    """Source-agnostic entry-point. Dispatches on the `item_id`
    prefix that every source mints (`github:…`, `jira:…`).
    """
    prefix = item.item_id.split(":", 1)[0]
    if prefix == "github":
        return github_workitem_to_transitions(item)
    if prefix == "jira":
        return jira_workitem_to_transitions(item)
    raise ValueError(f"unknown source prefix in item_id={item.item_id!r}")
