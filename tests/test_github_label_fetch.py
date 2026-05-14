"""Cache-fixture integration test for fetch_open_prs_with_labels.

Exercises the full GraphQL-parse → LabelEvent → materializer →
WorkItem path against a recorded-shape payload, with the network
blocked. The fixture's timeline mirrors a real ``dvhthomas/kno`` issue
history (since kno's PRs do not yet carry workflow labels, but its
issues demonstrate the exact same timeline event shape).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx

from flowmetrics.cache import FileCache
from flowmetrics.github import (
    OPEN_PR_LABEL_QUERY,
    OPEN_PR_LABEL_SNAPSHOT_QUERY,
    GitHubClient,
    fetch_open_prs_with_label_snapshot,
    fetch_open_prs_with_labels,
)
from flowmetrics.github_labels import (
    PRE_WIP_STATUS,
    WipLabels,
)


def _no_network_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected network call to {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _variables(repo: str) -> dict:
    return {
        "q": f"repo:{repo} is:pr is:open archived:false",
        "first": 100,
        "after": None,
    }


# A two-PR payload modeled on the timeline shape we observed in
# `dvhthomas/kno` issue #1 (shaping → in-progress → in-review → done)
# and one minimal PR with no workflow labels.
_PAYLOAD = {
    "data": {
        "search": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "issueCount": 2,
            "nodes": [
                {
                    "number": 101,
                    "title": "feat: rich workflow",
                    "createdAt": "2026-05-01T09:00:00Z",
                    "mergedAt": None,
                    "closedAt": None,
                    "author": {"__typename": "User", "login": "alice"},
                    "labels": {
                        "nodes": [{"name": "in-review"}, {"name": "area:web"}]
                    },
                    "timelineItems": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            # Classifier labels — ignored by the materializer.
                            {
                                "__typename": "LabeledEvent",
                                "createdAt": "2026-05-01T09:00:10Z",
                                "label": {"name": "area:web"},
                            },
                            # Workflow progression.
                            {
                                "__typename": "LabeledEvent",
                                "createdAt": "2026-05-01T10:00:00Z",
                                "label": {"name": "shaping"},
                            },
                            {
                                "__typename": "UnlabeledEvent",
                                "createdAt": "2026-05-02T10:00:00Z",
                                "label": {"name": "shaping"},
                            },
                            {
                                "__typename": "LabeledEvent",
                                "createdAt": "2026-05-02T10:00:00Z",
                                "label": {"name": "in-progress"},
                            },
                            {
                                "__typename": "UnlabeledEvent",
                                "createdAt": "2026-05-04T10:00:00Z",
                                "label": {"name": "in-progress"},
                            },
                            {
                                "__typename": "LabeledEvent",
                                "createdAt": "2026-05-04T10:00:00Z",
                                "label": {"name": "in-review"},
                            },
                        ],
                    },
                },
                {
                    "number": 102,
                    "title": "chore: bump dep",
                    "createdAt": "2026-05-03T12:00:00Z",
                    "mergedAt": None,
                    "closedAt": None,
                    "author": {"__typename": "Bot", "login": "renovate[bot]"},
                    "labels": {"nodes": [{"name": "dependencies"}]},
                    "timelineItems": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "__typename": "LabeledEvent",
                                "createdAt": "2026-05-03T12:00:05Z",
                                "label": {"name": "dependencies"},
                            },
                        ],
                    },
                },
            ],
        }
    }
}


_MIXED_CASE_PAYLOAD = {
    "data": {
        "search": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "issueCount": 1,
            "nodes": [
                {
                    "number": 201,
                    "title": "PR with mixed-case label events",
                    "createdAt": "2026-05-01T09:00:00Z",
                    "mergedAt": None,
                    "closedAt": None,
                    "author": {"__typename": "User", "login": "alice"},
                    "labels": {"nodes": [{"name": "In-Progress"}]},
                    "timelineItems": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            # GitHub preserves the original casing on
                            # historical events; the fetcher must
                            # normalize to lowercase so matching against
                            # user input works.
                            {
                                "__typename": "LabeledEvent",
                                "createdAt": "2026-05-01T10:00:00Z",
                                "label": {"name": "In-Progress"},
                            },
                        ],
                    },
                },
            ],
        }
    }
}


def test_fetcher_lowercases_incoming_label_names(tmp_path):
    """A repo whose labels happen to use mixed case (e.g. `In-Progress`)
    must still match `--wip-labels "in-progress"`. The fetcher
    normalizes case on the way in."""
    cache = FileCache(tmp_path)
    cache.put(
        FileCache.make_key(OPEN_PR_LABEL_QUERY, _variables("acme/proj")),
        _MIXED_CASE_PAYLOAD,
    )
    client = GitHubClient(cache, read_only=True, http_client=_no_network_client())
    wip = WipLabels.parse("in-progress")

    items = fetch_open_prs_with_labels(
        client, "acme/proj", asof=date(2026, 5, 10), wip=wip
    )

    assert len(items) == 1
    # Despite the GraphQL payload using "In-Progress", the materialized
    # interval status is the normalized lowercase form.
    assert items[0].status_intervals[-1].status == "in-progress"


# ---------------------------------------------------------------------------
# Snapshot fetcher (the Aging path) — no timeline events, just current labels.
# ---------------------------------------------------------------------------


_SNAPSHOT_PAYLOAD = {
    "data": {
        "search": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "issueCount": 3,
            "nodes": [
                # Currently in `in-review` (the rightmost WIP label held).
                {
                    "number": 301,
                    "title": "PR with one WIP label",
                    "createdAt": "2026-05-01T09:00:00Z",
                    "author": {"__typename": "User", "login": "alice"},
                    "labels": {"nodes": [{"name": "in-review"}, {"name": "area:web"}]},
                },
                # Holds both shaping AND in-progress — rightmost wins → in-progress.
                {
                    "number": 302,
                    "title": "PR with two WIP labels concurrent",
                    "createdAt": "2026-05-03T09:00:00Z",
                    "author": {"__typename": "User", "login": "bob"},
                    "labels": {
                        "nodes": [{"name": "shaping"}, {"name": "in-progress"}]
                    },
                },
                # Bot PR with no workflow label — sits in Pre-WIP.
                {
                    "number": 303,
                    "title": "Bot PR",
                    "createdAt": "2026-05-04T09:00:00Z",
                    "author": {"__typename": "Bot", "login": "renovate[bot]"},
                    "labels": {"nodes": [{"name": "dependencies"}]},
                },
            ],
        }
    }
}


def _snapshot_variables(repo: str) -> dict:
    return {
        "q": f"repo:{repo} is:pr is:open archived:false",
        "first": 100,
        "after": None,
    }


def test_snapshot_fetcher_builds_workitems_with_one_synthetic_interval(tmp_path):
    """The Aging-only fetch path: no timeline, just current labels.
    Each WorkItem carries a single StatusInterval whose status is the
    rightmost-in-WIP of the current labels, or Pre-WIP if none."""
    cache = FileCache(tmp_path)
    cache.put(
        FileCache.make_key(OPEN_PR_LABEL_SNAPSHOT_QUERY, _snapshot_variables("acme/proj")),
        _SNAPSHOT_PAYLOAD,
    )
    client = GitHubClient(cache, read_only=True, http_client=_no_network_client())
    wip = WipLabels.parse("shaping,in-progress,in-review")

    items = fetch_open_prs_with_label_snapshot(
        client, "acme/proj", asof=date(2026, 5, 10), wip=wip
    )

    assert len(items) == 3
    one_label, two_labels, bot = items

    # One synthetic interval each, all ending at asof.
    asof_dt = datetime(2026, 5, 10, tzinfo=UTC)
    for item in items:
        assert len(item.status_intervals) == 1
        iv = item.status_intervals[0]
        assert iv.start == item.created_at
        assert iv.end == asof_dt

    assert one_label.item_id == "#301"
    assert one_label.status_intervals[0].status == "in-review"

    # Concurrent shaping + in-progress → rightmost-in-WIP wins.
    assert two_labels.item_id == "#302"
    assert two_labels.status_intervals[0].status == "in-progress"

    # No WIP labels → Pre-WIP (will be filtered by is_aging_wip downstream).
    assert bot.item_id == "#303"
    assert bot.is_bot is True
    assert bot.status_intervals[0].status == PRE_WIP_STATUS


def test_snapshot_fetcher_lowercases_incoming_label_names(tmp_path):
    """A repo whose labels use mixed case (`In-Progress`) must still
    match `--wip-labels "in-progress"`."""
    cache = FileCache(tmp_path)
    cache.put(
        FileCache.make_key(OPEN_PR_LABEL_SNAPSHOT_QUERY, _snapshot_variables("acme/proj")),
        {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "issueCount": 1,
                    "nodes": [
                        {
                            "number": 401,
                            "title": "Mixed-case label",
                            "createdAt": "2026-05-01T09:00:00Z",
                            "author": {"__typename": "User", "login": "alice"},
                            "labels": {"nodes": [{"name": "In-Progress"}]},
                        }
                    ],
                }
            }
        },
    )
    client = GitHubClient(cache, read_only=True, http_client=_no_network_client())
    wip = WipLabels.parse("in-progress")

    items = fetch_open_prs_with_label_snapshot(
        client, "acme/proj", asof=date(2026, 5, 10), wip=wip
    )
    assert items[0].status_intervals[0].status == "in-progress"


# ---------------------------------------------------------------------------
# Timeline-driven fetcher (scaffolding for CFD-future).
# ---------------------------------------------------------------------------


def test_fetcher_materializes_intervals_from_recorded_payload(tmp_path):
    cache = FileCache(tmp_path)
    cache.put(
        FileCache.make_key(OPEN_PR_LABEL_QUERY, _variables("dvhthomas/kno")),
        _PAYLOAD,
    )
    client = GitHubClient(cache, read_only=True, http_client=_no_network_client())
    wip = WipLabels.parse("shaping,in-progress,in-review")

    items = fetch_open_prs_with_labels(
        client, "dvhthomas/kno", asof=date(2026, 5, 10), wip=wip
    )

    assert len(items) == 2

    rich, bot = items
    assert rich.item_id == "#101"
    assert rich.is_bot is False
    assert rich.merged_at is None
    # Pre-WIP → shaping → in-progress → in-review (current), extending to asof.
    statuses = [iv.status for iv in rich.status_intervals]
    assert statuses == [PRE_WIP_STATUS, "shaping", "in-progress", "in-review"]
    assert rich.status_intervals[-1].end == datetime(2026, 5, 10, tzinfo=UTC)

    assert bot.item_id == "#102"
    assert bot.is_bot is True
    # Bot PR with no workflow labels — sits in Pre-WIP for its full life.
    assert len(bot.status_intervals) == 1
    assert bot.status_intervals[0].status == PRE_WIP_STATUS
