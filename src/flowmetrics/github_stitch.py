"""GitHub Issue+PR stitcher — Phase 2 algorithmic core.

A GitHub Issue's "Done" stage is reached when the PR that closes
it MERGES, not when the Issue's own `closed` event fires. (Often
those are the same instant, but the merge is the causal action;
the close is the side-effect, and a manually-closed Issue with no
PR is a different category of completion.)

This module is the pure-data core of stitching. Given:
  - the Issue's existing transition stream,
  - a `ClosingPR` descriptor (id + merge timestamp),

it returns a new transition stream with the closing PR's merge
appended as the terminal "Done" transition, carrying
SIGNAL_GITHUB_PR_CLOSES_ISSUE.

No I/O, no API calls. The live Issue/PR fetcher (which discovers
the linking via GraphQL `closingIssuesReferences` on a PR) is a
follow-up: this module is what that fetcher feeds.

Discovery happens upstream:
  PR.closingIssuesReferences → for each Issue ID, build a
  ClosingPR(pr_item_id, pr.mergedAt) and call the stitcher.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from . import signals
from .canonical import StageTransition


@dataclass(frozen=True)
class ClosingPR:
    """Descriptor of the PR that closed an Issue.

    `pr_item_id` is the canonical work-item id of the PR
    (e.g. `github:acme/widget:pr:215`) so callers can reach back
    into the PR's own stream if they want to attribute the close
    to a person, branch, etc.
    """

    pr_item_id: str
    merged_at: datetime


def stitch_issue_with_closing_pr(
    *,
    issue_id: str,
    issue_transitions: list[StageTransition],
    closing_pr: ClosingPR | None,
    done_stage: str,
) -> list[StageTransition]:
    """Append the closing PR's merge as the Issue's terminal Done
    transition. Drop any self-close Done transitions the Issue
    emitted on its own — they're side-effects of the merge.
    """
    if closing_pr is None:
        return list(issue_transitions)

    if issue_transitions:
        first = min(issue_transitions, key=lambda t: t.entered_at)
        if closing_pr.merged_at < first.entered_at:
            raise ValueError(
                f"closing PR merged before issue creation: "
                f"merged_at={closing_pr.merged_at.isoformat()} "
                f"first_transition={first.entered_at.isoformat()}"
            )

    kept = [t for t in issue_transitions if t.stage != done_stage]
    stitched_done = StageTransition(
        item_id=issue_id,
        entered_at=closing_pr.merged_at,
        stage=done_stage,
        signal=signals.SIGNAL_GITHUB_PR_CLOSES_ISSUE,
    )
    return [*kept, stitched_done]
