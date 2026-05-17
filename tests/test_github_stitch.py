"""Behavioural spec for the GitHub Issue+PR stitcher.

Stitching is the canonical-model expression of one user fact: a
GitHub Issue's "Done" stage is reached when the PR that closes it
merges — not when the Issue is manually closed. The stitcher
takes:

  - An Issue's existing transition stream (from labels, comments,
    `closed` event)
  - The closing PR's merge transition (with timestamp)

and produces a new Issue transition stream where the terminal
"Done" comes from the PR-merge, carrying
SIGNAL_GITHUB_PR_CLOSES_ISSUE.

The stitcher is pure data — no I/O, no GitHub-API calls. Live
Issue-fetcher integration with the cache is a follow-up: this
module is the algorithmic core that lets the canonical layer be
tested with synthetic fixtures and then plugged into a live
fetcher once that arrives.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from flowmetrics import signals
from flowmetrics.canonical import StageTransition
from flowmetrics.github_stitch import (
    ClosingPR,
    stitch_issue_with_closing_pr,
)


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


ISSUE_TRANSITIONS = [
    StageTransition(
        "github:acme/widget:issue:101",
        _ts(2026, 4, 1, 9, 0, 0),
        "Open",
        signals.SIGNAL_GITHUB_ISSUE_CREATED,
    ),
    StageTransition(
        "github:acme/widget:issue:101",
        _ts(2026, 4, 2, 11, 20, 0),
        "Triaged",
        signals.SIGNAL_GITHUB_LABEL_ADDED,
    ),
    StageTransition(
        "github:acme/widget:issue:101",
        _ts(2026, 4, 10, 16, 45, 0),
        "In Progress",
        signals.SIGNAL_GITHUB_LABEL_ADDED,
    ),
]


class TestStitchCloses:
    def test_appends_a_done_transition_from_pr_merge(self):
        closer = ClosingPR(
            pr_item_id="github:acme/widget:pr:215",
            merged_at=_ts(2026, 4, 19, 14, 32, 18),
        )
        stitched = stitch_issue_with_closing_pr(
            issue_id="github:acme/widget:issue:101",
            issue_transitions=ISSUE_TRANSITIONS,
            closing_pr=closer,
            done_stage="Done",
        )
        last = stitched[-1]
        assert last.stage == "Done"
        assert last.entered_at == _ts(2026, 4, 19, 14, 32, 18)
        assert last.signal == signals.SIGNAL_GITHUB_PR_CLOSES_ISSUE
        assert last.item_id == "github:acme/widget:issue:101"

    def test_existing_transitions_preserved_in_order(self):
        closer = ClosingPR(
            "github:acme/widget:pr:215",
            _ts(2026, 4, 19, 14, 32, 18),
        )
        stitched = stitch_issue_with_closing_pr(
            issue_id="github:acme/widget:issue:101",
            issue_transitions=ISSUE_TRANSITIONS,
            closing_pr=closer,
            done_stage="Done",
        )
        assert [t.stage for t in stitched[:-1]] == [t.stage for t in ISSUE_TRANSITIONS]

    def test_no_closing_pr_returns_existing_transitions_unchanged(self):
        stitched = stitch_issue_with_closing_pr(
            issue_id="github:acme/widget:issue:101",
            issue_transitions=ISSUE_TRANSITIONS,
            closing_pr=None,
            done_stage="Done",
        )
        assert stitched == ISSUE_TRANSITIONS

    def test_drops_a_self_close_event_after_merge(self):
        """If the Issue also has its own `closed` transition after the
        PR merge, we keep the PR-merge as authoritative. The Issue's
        own closed-event is dropped — it's typically the side effect
        of the PR merge anyway."""
        with_self_close = [
            *ISSUE_TRANSITIONS,
            StageTransition(
                "github:acme/widget:issue:101",
                _ts(2026, 4, 19, 14, 32, 19),  # 1 sec after merge
                "Done",
                signals.SIGNAL_GITHUB_ISSUE_CLOSED,
            ),
        ]
        closer = ClosingPR(
            "github:acme/widget:pr:215",
            _ts(2026, 4, 19, 14, 32, 18),
        )
        stitched = stitch_issue_with_closing_pr(
            issue_id="github:acme/widget:issue:101",
            issue_transitions=with_self_close,
            closing_pr=closer,
            done_stage="Done",
        )
        # Exactly one Done — the PR-merge one.
        done_rows = [t for t in stitched if t.stage == "Done"]
        assert len(done_rows) == 1
        assert done_rows[0].signal == signals.SIGNAL_GITHUB_PR_CLOSES_ISSUE


class TestStitchValidation:
    def test_rejects_pr_merge_earlier_than_issue_creation(self):
        """A PR cannot close an Issue that didn't exist yet."""
        closer = ClosingPR(
            "github:acme/widget:pr:215",
            _ts(2026, 3, 1, 0, 0, 0),  # before Issue creation
        )
        with pytest.raises(ValueError, match="merged before"):
            stitch_issue_with_closing_pr(
                issue_id="github:acme/widget:issue:101",
                issue_transitions=ISSUE_TRANSITIONS,
                closing_pr=closer,
                done_stage="Done",
            )

    def test_empty_issue_transitions_with_no_closer_is_empty(self):
        assert (
            stitch_issue_with_closing_pr(
                issue_id="github:acme/widget:issue:101",
                issue_transitions=[],
                closing_pr=None,
                done_stage="Done",
            )
            == []
        )
