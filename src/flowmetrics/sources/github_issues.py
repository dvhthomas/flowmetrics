"""GitHub Issue → canonical-stream parser.

Pure functions over GraphQL responses; no network I/O (that lives
on the caller). Each Issue node returns:
  - one `StreamItem` carrying canonical id + url + lifecycle dates
  - a chronologically-sorted list of `StageTransition` rows
    reflecting the Issue's label timeline and final close, with
    the terminal transition stitched from the closing PR's
    `mergedAt` when present (carrying SIGNAL_GITHUB_PR_CLOSES_ISSUE).

Stage names follow the GitHub-Issue label convention used by
each repo. Without a label, an Issue lives in the synthetic
"Open" stage. When labels are present, the most recent active
label name is the stage.

Driven by a real cached fixture (see
tests/fixtures/canonical/github_issues_calcmark.json).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from .. import signals
from ..canonical import StageTransition, WorkflowDef
from ..stream import Stream, StreamItem

ISSUE_SEARCH_QUERY = """
query($q: String!, $first: Int!, $after: String) {
  search(query: $q, type: ISSUE, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    issueCount
    nodes {
      ... on Issue {
        number
        title
        createdAt
        closedAt
        url
        author { login }
        labels(first: 20) { nodes { name } }
        timelineItems(first: 100,
                      itemTypes: [LABELED_EVENT, UNLABELED_EVENT,
                                  CLOSED_EVENT, REOPENED_EVENT]) {
          nodes {
            __typename
            ... on LabeledEvent { createdAt label { name } }
            ... on UnlabeledEvent { createdAt label { name } }
            ... on ClosedEvent { createdAt }
            ... on ReopenedEvent { createdAt }
          }
        }
        closedByPullRequestsReferences(first: 5, includeClosedPrs: true) {
          nodes { number mergedAt url state }
        }
      }
    }
  }
  rateLimit { remaining cost }
}
""".strip()


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def _issue_item_id(repo: str, number: int) -> str:
    return f"github:{repo}:issue:{number}"


# Stage names used for the canonical stream when nothing more
# specific is available. The aging consumer will treat these as
# WIP per the workflow def the user supplies.
_OPEN_STAGE = "Open"
_DONE_STAGE = "Done"


def parse_issue_node(
    node: dict[str, Any], *, repo: str,
) -> tuple[StreamItem, list[StageTransition]]:
    number = node["number"]
    item_id = _issue_item_id(repo, number)
    created_at = _parse_dt(node["createdAt"])
    closed_at = _parse_dt(node["closedAt"]) if node.get("closedAt") else None

    # When a PR closed the issue, the STITCHED completion time is the
    # PR's mergedAt — that's the causal "done" instant. The Issue's
    # own closedAt fires a few seconds later as a side-effect (often
    # the same instant). Cycle-time consumers (scatterplot, forecast)
    # care about the merge, not the close-event.
    closing_prs_for_completion = (
        (node.get("closedByPullRequestsReferences") or {}).get("nodes") or []
    )
    merged_closer_for_completion = next(
        (pr for pr in closing_prs_for_completion if pr.get("mergedAt")),
        None,
    )
    completed_at: datetime | None
    if merged_closer_for_completion is not None:
        completed_at = _parse_dt(merged_closer_for_completion["mergedAt"])
    else:
        completed_at = closed_at

    item = StreamItem(
        item_id=item_id,
        title=node.get("title") or "",
        url=node.get("url"),
        created_at=created_at,
        completed_at=completed_at,
    )

    transitions: list[StageTransition] = []

    # First transition: issue creation
    transitions.append(
        StageTransition(
            item_id=item_id,
            entered_at=created_at,
            stage=_OPEN_STAGE,
            signal=signals.SIGNAL_GITHUB_ISSUE_CREATED,
        )
    )

    # Mid transitions: label add/remove events become stage transitions
    # (stage name = label name).
    for ti in (node.get("timelineItems") or {}).get("nodes") or []:
        typ = ti.get("__typename")
        if typ == "LabeledEvent" and ti.get("createdAt") and ti.get("label"):
            transitions.append(
                StageTransition(
                    item_id=item_id,
                    entered_at=_parse_dt(ti["createdAt"]),
                    stage=ti["label"]["name"],
                    signal=signals.SIGNAL_GITHUB_LABEL_ADDED,
                )
            )
        elif typ == "UnlabeledEvent" and ti.get("createdAt") and ti.get("label"):
            # When a label is removed, we don't know what stage the issue
            # is "in" now. Surface as a transition into the open stage
            # so the audit trail records the removal.
            transitions.append(
                StageTransition(
                    item_id=item_id,
                    entered_at=_parse_dt(ti["createdAt"]),
                    stage=_OPEN_STAGE,
                    signal=signals.SIGNAL_GITHUB_LABEL_REMOVED,
                )
            )

    # Terminal transition: closing event. If a PR closed the issue, use
    # the PR's mergedAt + the cross-source SIGNAL_GITHUB_PR_CLOSES_ISSUE.
    # Otherwise use the issue's own closedAt + SIGNAL_GITHUB_ISSUE_CLOSED.
    if closed_at is not None:
        closing_prs = (
            (node.get("closedByPullRequestsReferences") or {}).get("nodes") or []
        )
        merged_closer = next(
            (pr for pr in closing_prs if pr.get("mergedAt")),
            None,
        )
        if merged_closer is not None:
            transitions.append(
                StageTransition(
                    item_id=item_id,
                    entered_at=_parse_dt(merged_closer["mergedAt"]),
                    stage=_DONE_STAGE,
                    signal=signals.SIGNAL_GITHUB_PR_CLOSES_ISSUE,
                )
            )
        else:
            transitions.append(
                StageTransition(
                    item_id=item_id,
                    entered_at=closed_at,
                    stage=_DONE_STAGE,
                    signal=signals.SIGNAL_GITHUB_ISSUE_CLOSED,
                )
            )

    transitions.sort(key=lambda t: t.entered_at)
    return item, transitions


def parse_search_result(
    response: dict[str, Any], *, repo: str,
) -> list[tuple[StreamItem, list[StageTransition]]]:
    """Parse a full GraphQL search response into per-issue stream
    entries. Handles the `data.search.nodes` shape; ignores the
    rate-limit envelope.
    """
    nodes = (
        response.get("data", {}).get("search", {}).get("nodes") or []
    )
    return [parse_issue_node(n, repo=repo) for n in nodes if n.get("number")]


def fetch_issues_closed_in_window(
    repo: str, start: date, stop: date, *, client,
) -> list[tuple[StreamItem, list[StageTransition]]]:
    """Live (cached) fetch of every issue closed in [start, stop].

    Paginates GraphQL search until exhausted. Each page hits the
    same cache as the PR fetcher (one POST per uncached page).
    Returns parsed (StreamItem, [StageTransition]) pairs.
    """
    entries: list[tuple[StreamItem, list[StageTransition]]] = []
    cursor: str | None = None
    while True:
        variables = {
            "q": f"repo:{repo} is:issue closed:{start.isoformat()}..{stop.isoformat()}",
            "first": 50,
            "after": cursor,
        }
        payload = client.graphql(ISSUE_SEARCH_QUERY, variables)
        entries.extend(parse_search_result(payload, repo=repo))
        page = payload.get("data", {}).get("search", {}).get("pageInfo", {})
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not cursor:
            break
    return entries


def build_stream_for_aging(
    repo: str, start: date, stop: date, *, client, workflow: WorkflowDef,
) -> Stream:
    """Build a canonical Stream for the Aging report from a
    GitHub repo's Issues + their closing PRs.

    Only Issues that closed in [start, stop] are included.
    Each Issue's terminal Done transition is either:
      - mergedAt of the closing PR (SIGNAL_GITHUB_PR_CLOSES_ISSUE), or
      - the Issue's own closedAt (SIGNAL_GITHUB_ISSUE_CLOSED).

    The workflow def is supplied by the caller — typically
    derived from the repo's label vocabulary.
    """
    entries = fetch_issues_closed_in_window(repo, start, stop, client=client)
    items: list[StreamItem] = []
    transitions: list[StageTransition] = []
    for item, txs in entries:
        items.append(item)
        transitions.extend(txs)
    return Stream(items=items, transitions=transitions, workflow=workflow)
