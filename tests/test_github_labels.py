"""Unit tests for the label-driven materializer in github_labels.py.

Each case maps to one row of the resolution table in
``docs/SPEC-github-labels.md`` §8b. The table is the contract; these
tests pin it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from flowmetrics.github_labels import (
    ABANDONED_STATUS,
    DEPARTED_STATUS,
    PRE_WIP_STATUS,
    LabelEvent,
    LifecycleEvent,
    WipLabels,
    is_aging_wip,
    materialize_status_intervals,
)


def dt(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


WIP = WipLabels.parse("shaping,in-progress,in-review")


# ---------------------------------------------------------------------------
# WipLabels parsing
# ---------------------------------------------------------------------------


def test_wip_labels_parse_basic() -> None:
    w = WipLabels.parse("a,b,c")
    assert w.ordered == ("a", "b", "c")


def test_wip_labels_parse_strips_whitespace() -> None:
    w = WipLabels.parse(" a , b ,c ")
    assert w.ordered == ("a", "b", "c")


def test_wip_labels_parse_rejects_empty_entry() -> None:
    with pytest.raises(ValueError, match="empty entry"):
        WipLabels.parse("a,,b")


def test_wip_labels_parse_rejects_trailing_comma() -> None:
    with pytest.raises(ValueError, match="empty entry"):
        WipLabels.parse("a,b,")


def test_wip_labels_parse_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        WipLabels.parse("a,b,a")


# ---------------------------------------------------------------------------
# Case-insensitive matching — GitHub enforces case-insensitive label
# uniqueness per repo (you can't create both `bug` and `Bug`). A user
# typing `--wip-labels "In-Progress"` against an actual `in-progress`
# label must match, or every event is silently dropped.
# ---------------------------------------------------------------------------


def test_wip_labels_parse_lowercases_entries() -> None:
    """User input is normalized so subsequent comparisons are
    case-insensitive by construction."""
    w = WipLabels.parse("Shaping,In-Progress,IN-REVIEW")
    assert w.ordered == ("shaping", "in-progress", "in-review")


def test_wip_labels_parse_rejects_case_insensitive_dupes() -> None:
    """`in-progress` and `IN-PROGRESS` normalize to the same label —
    the existing dupe check must catch it after normalization."""
    with pytest.raises(ValueError, match="duplicate"):
        WipLabels.parse("in-progress,IN-PROGRESS")


def test_materializer_matches_mixed_case_user_input_to_lowercase_events() -> None:
    """User types mixed case in --wip-labels. The fetcher lowercases
    incoming events. Matching succeeds end-to-end."""
    wip = WipLabels.parse("In-Progress")  # normalized to ("in-progress",)
    t0 = dt(2026, 5, 1, 9, 0)
    t1 = dt(2026, 5, 1, 10, 0)

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=dt(2026, 5, 10, 0, 0),
        label_events=[
            # Simulating what the fetcher produces — lowercased on the way in.
            LabelEvent(at=t1, label="in-progress", kind="added"),
        ],
        lifecycle_events=[],
        wip=wip,
    )
    assert intervals[-1].status == "in-progress"


# ---------------------------------------------------------------------------
# Materializer — 16 cases from spec §9
# ---------------------------------------------------------------------------


def test_open_pr_no_events_is_pre_wip() -> None:
    intervals = materialize_status_intervals(
        created_at=dt(2026, 5, 1, 9, 0),
        asof=dt(2026, 5, 10, 0, 0),
        label_events=[],
        lifecycle_events=[],
        wip=WIP,
    )
    assert len(intervals) == 1
    iv = intervals[0]
    assert iv.start == dt(2026, 5, 1, 9, 0)
    assert iv.end == dt(2026, 5, 10, 0, 0)
    assert iv.status == PRE_WIP_STATUS


def test_open_pr_only_non_wip_labels_is_pre_wip() -> None:
    intervals = materialize_status_intervals(
        created_at=dt(2026, 5, 1, 9, 0),
        asof=dt(2026, 5, 10, 0, 0),
        label_events=[
            LabelEvent(at=dt(2026, 5, 1, 10, 0), label="bug", kind="added"),
            LabelEvent(at=dt(2026, 5, 1, 10, 1), label="area:web", kind="added"),
        ],
        lifecycle_events=[],
        wip=WIP,
    )
    assert len(intervals) == 1
    assert intervals[0].status == PRE_WIP_STATUS


def test_linear_progression_merged_cleanly() -> None:
    """Shaping → in-progress → in-review → labels removed → Merged.

    Six intervals: Pre-WIP, shaping, in-progress, in-review, Pre-WIP
    (between label removal and merge), Departed (terminal tail).
    """
    t0 = dt(2026, 5, 1, 9, 0)  # created
    t1 = dt(2026, 5, 1, 10, 0)  # +shaping
    t2 = dt(2026, 5, 2, 10, 0)  # -shaping, +in-progress
    t3 = dt(2026, 5, 4, 10, 0)  # -in-progress, +in-review
    t4 = dt(2026, 5, 5, 10, 0)  # -in-review
    t5 = dt(2026, 5, 5, 11, 0)  # merged
    asof = dt(2026, 5, 10, 0, 0)

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=asof,
        label_events=[
            LabelEvent(at=t1, label="shaping", kind="added"),
            LabelEvent(at=t2, label="shaping", kind="removed"),
            LabelEvent(at=t2, label="in-progress", kind="added"),
            LabelEvent(at=t3, label="in-progress", kind="removed"),
            LabelEvent(at=t3, label="in-review", kind="added"),
            LabelEvent(at=t4, label="in-review", kind="removed"),
        ],
        lifecycle_events=[LifecycleEvent(at=t5, kind="merged")],
        wip=WIP,
    )

    assert [(iv.start, iv.end, iv.status) for iv in intervals] == [
        (t0, t1, PRE_WIP_STATUS),
        (t1, t2, "shaping"),
        (t2, t3, "in-progress"),
        (t3, t4, "in-review"),
        (t4, t5, PRE_WIP_STATUS),
        (t5, asof, DEPARTED_STATUS),
    ]


def test_merged_but_not_shipped_stays_in_wip_column() -> None:
    """WIP label still applied at MergedEvent → final interval status
    remains the WIP column. The interval extends to asof so CFD time-
    bucketing sees the post-merge band correctly (the merged-but-not-
    shipped signal persists)."""
    t0 = dt(2026, 5, 1, 9, 0)
    t1 = dt(2026, 5, 1, 10, 0)  # +in-review
    t2 = dt(2026, 5, 5, 11, 0)  # merged WITHOUT removing in-review
    asof = dt(2026, 5, 10, 0, 0)

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=asof,
        label_events=[LabelEvent(at=t1, label="in-review", kind="added")],
        lifecycle_events=[LifecycleEvent(at=t2, kind="merged")],
        wip=WIP,
    )

    # in-review never changes (lifecycle MERGED + active in-review →
    # still in-review per the resolution table). The last interval
    # spans from when in-review was added to asof.
    assert intervals[-1].status == "in-review"
    assert intervals[-1].end == asof


def test_merged_after_all_wip_removed_is_departed() -> None:
    t0 = dt(2026, 5, 1, 9, 0)
    t1 = dt(2026, 5, 1, 10, 0)
    t2 = dt(2026, 5, 4, 10, 0)
    t3 = dt(2026, 5, 5, 11, 0)
    asof = dt(2026, 5, 10, 0, 0)

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=asof,
        label_events=[
            LabelEvent(at=t1, label="in-progress", kind="added"),
            LabelEvent(at=t2, label="in-progress", kind="removed"),
        ],
        lifecycle_events=[LifecycleEvent(at=t3, kind="merged")],
        wip=WIP,
    )

    # Departed tail extends to asof.
    assert intervals[-1].status == DEPARTED_STATUS
    assert intervals[-1].end == asof
    # The merge-event transition happens at t3.
    assert intervals[-1].start == t3


def test_closed_not_merged_with_wip_label_is_abandoned() -> None:
    """The v3-spec bug fix: a closed-not-merged PR with a stale WIP
    label must land in ABANDONED at the close timestamp, not stay in
    the WIP column forever."""
    t0 = dt(2026, 5, 1, 9, 0)
    t1 = dt(2026, 5, 1, 10, 0)
    t2 = dt(2026, 5, 3, 11, 0)  # closed; in-progress label still applied
    asof = dt(2026, 5, 10, 0, 0)

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=asof,
        label_events=[LabelEvent(at=t1, label="in-progress", kind="added")],
        lifecycle_events=[LifecycleEvent(at=t2, kind="closed")],
        wip=WIP,
    )

    # ABANDONED begins at close, extends to asof.
    assert intervals[-1].status == ABANDONED_STATUS
    assert intervals[-1].start == t2
    assert intervals[-1].end == asof


def test_closed_not_merged_without_wip_label_is_abandoned() -> None:
    t0 = dt(2026, 5, 1, 9, 0)
    t2 = dt(2026, 5, 3, 11, 0)

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=dt(2026, 5, 10, 0, 0),
        label_events=[],
        lifecycle_events=[LifecycleEvent(at=t2, kind="closed")],
        wip=WIP,
    )

    assert intervals[-1].status == ABANDONED_STATUS


def test_reopen_cycle_returns_to_label_resolution() -> None:
    """Created → +shaping → closed → reopened → +in-progress → merged.

    The closed gap must appear as ABANDONED; after reopen, label
    resolution takes over again.
    """
    t0 = dt(2026, 5, 1, 9, 0)
    t1 = dt(2026, 5, 1, 10, 0)  # +shaping
    t2 = dt(2026, 5, 2, 10, 0)  # closed
    t3 = dt(2026, 5, 3, 10, 0)  # reopened
    t4 = dt(2026, 5, 3, 11, 0)  # +in-progress, -shaping
    t5 = dt(2026, 5, 5, 11, 0)  # merged

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=dt(2026, 5, 10, 0, 0),
        label_events=[
            LabelEvent(at=t1, label="shaping", kind="added"),
            LabelEvent(at=t4, label="shaping", kind="removed"),
            LabelEvent(at=t4, label="in-progress", kind="added"),
        ],
        lifecycle_events=[
            LifecycleEvent(at=t2, kind="closed"),
            LifecycleEvent(at=t3, kind="reopened"),
            LifecycleEvent(at=t5, kind="merged"),
        ],
        wip=WIP,
    )

    statuses = [iv.status for iv in intervals]
    assert PRE_WIP_STATUS in statuses
    assert "shaping" in statuses
    assert ABANDONED_STATUS in statuses
    assert "in-progress" in statuses
    # Merged with in-progress still applied → final stays in in-progress.
    # The interval started when in-progress was added (t4), not at merge
    # (t5) — the merge didn't transition the status, just the lifecycle.
    assert intervals[-1].status == "in-progress"
    assert intervals[-1].start == t4
    assert intervals[-1].end == dt(2026, 5, 10, 0, 0)


def test_two_reopen_cycles_idempotent() -> None:
    """Multiple close→reopen pairs each get their own ABANDONED interval."""
    t0 = dt(2026, 5, 1, 9, 0)
    intervals = materialize_status_intervals(
        created_at=t0,
        asof=dt(2026, 5, 10, 0, 0),
        label_events=[
            LabelEvent(at=dt(2026, 5, 1, 10, 0), label="in-progress", kind="added"),
        ],
        lifecycle_events=[
            LifecycleEvent(at=dt(2026, 5, 2, 10, 0), kind="closed"),
            LifecycleEvent(at=dt(2026, 5, 3, 10, 0), kind="reopened"),
            LifecycleEvent(at=dt(2026, 5, 4, 10, 0), kind="closed"),
            LifecycleEvent(at=dt(2026, 5, 5, 10, 0), kind="reopened"),
        ],
        wip=WIP,
    )
    abandoned_count = sum(1 for iv in intervals if iv.status == ABANDONED_STATUS)
    assert abandoned_count == 2


def test_backward_move_emits_backward_interval() -> None:
    """in-progress → shaping. Backward intervals are valid."""
    t0 = dt(2026, 5, 1, 9, 0)
    t1 = dt(2026, 5, 1, 10, 0)
    t2 = dt(2026, 5, 2, 10, 0)

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=dt(2026, 5, 10, 0, 0),
        label_events=[
            LabelEvent(at=t1, label="in-progress", kind="added"),
            LabelEvent(at=t2, label="in-progress", kind="removed"),
            LabelEvent(at=t2, label="shaping", kind="added"),
        ],
        lifecycle_events=[],
        wip=WIP,
    )

    statuses = [iv.status for iv in intervals]
    assert statuses == [PRE_WIP_STATUS, "in-progress", "shaping"]


def test_concurrent_labels_rightmost_wins() -> None:
    """Both shaping and in-progress applied at same instant → in-progress."""
    t0 = dt(2026, 5, 1, 9, 0)
    t1 = dt(2026, 5, 1, 10, 0)

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=dt(2026, 5, 10, 0, 0),
        label_events=[
            LabelEvent(at=t1, label="shaping", kind="added"),
            LabelEvent(at=t1, label="in-progress", kind="added"),
        ],
        lifecycle_events=[],
        wip=WIP,
    )
    # Final column should be in-progress, not shaping.
    assert intervals[-1].status == "in-progress"


def test_same_instant_add_then_remove_no_zero_length() -> None:
    """Add label_a then immediately remove it at the same timestamp →
    no zero-length interval is emitted."""
    t0 = dt(2026, 5, 1, 9, 0)
    t1 = dt(2026, 5, 1, 10, 0)

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=dt(2026, 5, 10, 0, 0),
        label_events=[
            LabelEvent(at=t1, label="in-progress", kind="added"),
            LabelEvent(at=t1, label="in-progress", kind="removed"),
        ],
        lifecycle_events=[],
        wip=WIP,
    )
    for iv in intervals:
        assert iv.start < iv.end, f"zero-length interval: {iv}"


def test_open_pr_with_current_wip_label_ends_at_asof() -> None:
    t0 = dt(2026, 5, 1, 9, 0)
    t1 = dt(2026, 5, 1, 10, 0)
    asof = dt(2026, 5, 10, 0, 0)

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=asof,
        label_events=[LabelEvent(at=t1, label="in-review", kind="added")],
        lifecycle_events=[],
        wip=WIP,
    )
    assert intervals[-1].status == "in-review"
    assert intervals[-1].end == asof


def test_open_pr_with_last_wip_removed_is_pre_wip_not_departed() -> None:
    """An OPEN PR whose last WIP label was just removed is back in
    Pre-WIP — not Departed (only MERGED can be Departed) and not
    Abandoned (only CLOSED can be Abandoned)."""
    t0 = dt(2026, 5, 1, 9, 0)
    t1 = dt(2026, 5, 1, 10, 0)
    t2 = dt(2026, 5, 2, 10, 0)
    asof = dt(2026, 5, 10, 0, 0)

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=asof,
        label_events=[
            LabelEvent(at=t1, label="in-progress", kind="added"),
            LabelEvent(at=t2, label="in-progress", kind="removed"),
        ],
        lifecycle_events=[],
        wip=WIP,
    )
    assert intervals[-1].status == PRE_WIP_STATUS


def test_single_label_wip_set() -> None:
    """`--wip-labels "a"` still works — two-column behavior."""
    single = WipLabels.parse("in-progress")
    t0 = dt(2026, 5, 1, 9, 0)
    t1 = dt(2026, 5, 1, 10, 0)

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=dt(2026, 5, 10, 0, 0),
        label_events=[LabelEvent(at=t1, label="in-progress", kind="added")],
        lifecycle_events=[],
        wip=single,
    )
    assert intervals[0].status == PRE_WIP_STATUS
    assert intervals[-1].status == "in-progress"


def test_only_historical_label_name_filtered_silently() -> None:
    """Caller passes only the new name; events of the renamed-old label
    are silently filtered. (The discoverability path — warning the
    user — is the fetcher's responsibility, not the materializer's.)"""
    t0 = dt(2026, 5, 1, 9, 0)
    t1 = dt(2026, 5, 1, 10, 0)

    intervals = materialize_status_intervals(
        created_at=t0,
        asof=dt(2026, 5, 10, 0, 0),
        label_events=[
            # "wip" is the new name; "in-progress" the old, not in --wip-labels
            LabelEvent(at=t1, label="wip", kind="added"),
        ],
        lifecycle_events=[],
        wip=WIP,  # contains in-progress, not wip
    )
    assert intervals == [
        intervals[0]
    ]  # single Pre-WIP interval — the unknown label was filtered
    assert intervals[0].status == PRE_WIP_STATUS


# ---------------------------------------------------------------------------
# is_aging_wip filter
# ---------------------------------------------------------------------------


def test_is_aging_wip_true_for_wip_status() -> None:
    from flowmetrics.compute import StatusInterval, WorkItem

    item = WorkItem(
        item_id="#1",
        title="t",
        created_at=dt(2026, 5, 1, 9, 0),
        merged_at=None,
        status_intervals=[
            StatusInterval(dt(2026, 5, 1, 9, 0), dt(2026, 5, 10, 0, 0), "in-progress")
        ],
    )
    assert is_aging_wip(item) is True


@pytest.mark.parametrize(
    "status", [PRE_WIP_STATUS, DEPARTED_STATUS, ABANDONED_STATUS]
)
def test_is_aging_wip_false_for_non_wip_statuses(status: str) -> None:
    from flowmetrics.compute import StatusInterval, WorkItem

    item = WorkItem(
        item_id="#1",
        title="t",
        created_at=dt(2026, 5, 1, 9, 0),
        merged_at=None,
        status_intervals=[
            StatusInterval(dt(2026, 5, 1, 9, 0), dt(2026, 5, 10, 0, 0), status)
        ],
    )
    assert is_aging_wip(item) is False


def test_is_aging_wip_false_for_empty_intervals() -> None:
    from flowmetrics.compute import WorkItem

    item = WorkItem(
        item_id="#1",
        title="t",
        created_at=dt(2026, 5, 1, 9, 0),
        merged_at=None,
    )
    assert is_aging_wip(item) is False
