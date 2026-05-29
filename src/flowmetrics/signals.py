"""Named-event constants — one per recognized source event.

Every `StageTransition` row carries a `signal` value from this
module. Every `WorkItem.provenance` entry is one or more values
from here. The constant IS the audit trail: anyone reading the
canonical event stream can tell exactly which underlying GitHub /
Jira event produced any given row, without pattern-matching item
IDs or string-searching descriptions.

Inspired by gh-velocity's `model.Signal*` constants (Go,
github.com/dvhthomas/gh-velocity), brought into flowmetrics'
Python + Jira-equal-citizen world.

Conventions
-----------

- Each constant is a string, prefixed with its source: `github-*`
  or `jira-*`. The prefix is part of the audit signal.
- Values are kebab-case, lower-case, no underscores. Easy to grep
  in JSON output.
- The Python identifier is `SCREAMING_SNAKE` for readability at
  call sites (`signals.SIGNAL_GITHUB_PR_MERGED`).
- Cross-source / derived signals would carry the prefix
  `flowmetrics-*` (none yet — added when needed).

Adding a new signal: append the constant here, then immediately
add it to the corresponding source's emission code. Never let a
source emit a string literal that isn't backed by a constant
here — the audit-trail discipline depends on it.
"""

from __future__ import annotations

# ----------------------------------------------------------------------
# GitHub source signals
# ----------------------------------------------------------------------

SIGNAL_GITHUB_ISSUE_CREATED = "github-issue-created"
SIGNAL_GITHUB_ISSUE_CLOSED = "github-issue-closed"
SIGNAL_GITHUB_LABEL_ADDED = "github-label-added"
SIGNAL_GITHUB_LABEL_REMOVED = "github-label-removed"
SIGNAL_GITHUB_PR_CREATED = "github-pr-created"
SIGNAL_GITHUB_PR_READY_FOR_REVIEW = "github-pr-ready-for-review"
SIGNAL_GITHUB_PR_REVIEW_CHANGES_REQUESTED = "github-pr-review-changes-requested"
SIGNAL_GITHUB_PR_REVIEW_APPROVED = "github-pr-review-approved"
SIGNAL_GITHUB_PR_MERGED = "github-pr-merged"

# The linking signal: a PR's `closingIssuesReferences` resolves
# to an Issue. The PR's merge event becomes a transition on the
# Issue's stream — that transition carries this signal.
SIGNAL_GITHUB_PR_CLOSES_ISSUE = "github-pr-closes-issue"


# ----------------------------------------------------------------------
# Jira source signals
# ----------------------------------------------------------------------

SIGNAL_JIRA_ISSUE_CREATED = "jira-issue-created"
SIGNAL_JIRA_STATUS_CHANGED = "jira-status-changed"
SIGNAL_JIRA_RESOLVED = "jira-resolved"


# ----------------------------------------------------------------------
# Step-matcher event codes.
#
# A contract step can match on a lifecycle `event`. Users (and
# hand-edited YAML) reference the event by a short, source-scoped code
# rather than the verbose signal string — `pr-ready`, not
# `github-pr-ready-for-review`. These maps are the single source of
# truth: code → signal constant, per source.
# ----------------------------------------------------------------------

GITHUB_EVENT_CODES: dict[str, str] = {
    "pr-opened": SIGNAL_GITHUB_PR_CREATED,
    "pr-ready": SIGNAL_GITHUB_PR_READY_FOR_REVIEW,
    "changes-requested": SIGNAL_GITHUB_PR_REVIEW_CHANGES_REQUESTED,
    "approved": SIGNAL_GITHUB_PR_REVIEW_APPROVED,
    "pr-merged": SIGNAL_GITHUB_PR_MERGED,
    "issue-opened": SIGNAL_GITHUB_ISSUE_CREATED,
    "issue-closed": SIGNAL_GITHUB_ISSUE_CLOSED,
}

JIRA_EVENT_CODES: dict[str, str] = {
    "created": SIGNAL_JIRA_ISSUE_CREATED,
    "status-changed": SIGNAL_JIRA_STATUS_CHANGED,
    "resolved": SIGNAL_JIRA_RESOLVED,
}


def event_codes_for(source: str) -> dict[str, str]:
    """The code→signal map for a source ('github' or 'jira')."""
    return GITHUB_EVENT_CODES if source == "github" else JIRA_EVENT_CODES
