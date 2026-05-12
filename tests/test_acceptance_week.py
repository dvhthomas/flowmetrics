"""Acceptance test: 'what's flow efficiency for this week: start-stop' via GitHub.

This test exercises the full pipeline (HTTP client → cache → fetch →
compute → aggregate) but against a pre-seeded cache so it runs offline
and makes zero network calls. The httpx transport raises if anything
tries to reach the network.
"""

from __future__ import annotations

from datetime import date

import pytest

from flowmetrics import flowmetrics_for_window, make_github_source, this_week_window
from flowmetrics.cache import CacheMiss, FileCache
from flowmetrics.github import PR_SEARCH_QUERY

REPO = "pallets/flask"
START = date(2026, 5, 4)
STOP = date(2026, 5, 10)


def _seed_cache(cache_dir, response: dict) -> None:
    cache = FileCache(cache_dir)
    variables = {
        "q": f"repo:{REPO} is:pr is:merged merged:{START.isoformat()}..{STOP.isoformat()}",
        "first": 100,
        "after": None,
    }
    cache.put(FileCache.make_key(PR_SEARCH_QUERY, variables), response)


def _pr_node(
    number: int,
    created: str,
    merged: str | None,
    activity_events: list[dict],
) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "createdAt": created,
        "mergedAt": merged,
        "timelineItems": {
            "pageInfo": {"hasNextPage": False},
            "nodes": activity_events,
        },
    }


class TestThisWeekDefaults:
    def test_this_week_window_is_monday_to_sunday(self):
        # 2026-05-12 is a Tuesday; Monday is 2026-05-11.
        start, stop = this_week_window(today=date(2026, 5, 12))
        assert start == date(2026, 5, 11)
        assert stop == date(2026, 5, 17)


class TestFlowEfficiencyForWindow:
    def test_returns_a_result_for_a_week_using_cache(self, tmp_path):
        # Three PRs, mixed efficiency, all merged inside the window.
        response = {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "issueCount": 3,
                    "nodes": [
                        # Quick PR: opened 9am, merged 11am same day, two commits
                        _pr_node(
                            number=101,
                            created="2026-05-05T09:00:00Z",
                            merged="2026-05-05T11:00:00Z",
                            activity_events=[
                                {
                                    "__typename": "PullRequestCommit",
                                    "commit": {"committedDate": "2026-05-05T09:30:00Z"},
                                },
                                {
                                    "__typename": "PullRequestReview",
                                    "submittedAt": "2026-05-05T10:30:00Z",
                                },
                                {
                                    "__typename": "MergedEvent",
                                    "createdAt": "2026-05-05T11:00:00Z",
                                },
                            ],
                        ),
                        # Sluggish PR: opened Mon 9am, merged Fri 9am (96h),
                        # one commit Tuesday morning then nothing
                        _pr_node(
                            number=102,
                            created="2026-05-04T09:00:00Z",
                            merged="2026-05-08T09:00:00Z",
                            activity_events=[
                                {
                                    "__typename": "PullRequestCommit",
                                    "commit": {"committedDate": "2026-05-05T10:00:00Z"},
                                },
                                {
                                    "__typename": "MergedEvent",
                                    "createdAt": "2026-05-08T09:00:00Z",
                                },
                            ],
                        ),
                        # Medium PR: opened Tue 9am, merged Wed 5pm,
                        # busy activity for the first morning then quiet
                        _pr_node(
                            number=103,
                            created="2026-05-05T09:00:00Z",
                            merged="2026-05-06T17:00:00Z",
                            activity_events=[
                                {
                                    "__typename": "PullRequestCommit",
                                    "commit": {"committedDate": "2026-05-05T09:30:00Z"},
                                },
                                {
                                    "__typename": "IssueComment",
                                    "createdAt": "2026-05-05T11:00:00Z",
                                },
                                {
                                    "__typename": "PullRequestReview",
                                    "submittedAt": "2026-05-05T11:30:00Z",
                                },
                                {
                                    "__typename": "MergedEvent",
                                    "createdAt": "2026-05-06T17:00:00Z",
                                },
                            ],
                        ),
                    ],
                },
                "rateLimit": {
                    "remaining": 4999,
                    "limit": 5000,
                    "resetAt": "2026-05-05T18:00:00Z",
                    "cost": 1,
                },
            }
        }
        _seed_cache(tmp_path, response)

        result = flowmetrics_for_window(make_github_source(REPO, cache_dir=tmp_path, read_only=True), START, STOP)

        assert result.pr_count == 3
        # Vacanti's framing: portfolio efficiency is sum(active)/sum(cycle),
        # heavily weighted by the long-running PR (102) since it has the most
        # cycle time. We expect a single-digit-percent result.
        assert 0.0 < result.portfolio_efficiency < 0.20
        # The fast PR (#101) should be at or near 1.0
        fast = next(p for p in result.per_pr if p.item_id == "#101")
        assert fast.efficiency == pytest.approx(1.0)
        # The slow PR (#102) should be well under 5%
        slow = next(p for p in result.per_pr if p.item_id == "#102")
        assert slow.efficiency < 0.05

    def test_offline_with_no_cache_fails_loudly(self, tmp_path):
        # No cache primed, read_only=True → CacheMiss surfaces.
        with pytest.raises(CacheMiss):
            flowmetrics_for_window(make_github_source(REPO, cache_dir=tmp_path, read_only=True), START, STOP)

    def test_invalid_window_is_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            flowmetrics_for_window(
                make_github_source(REPO, cache_dir=tmp_path, read_only=True),
                date(2026, 5, 10),
                date(2026, 5, 4),
            )

    def test_zero_prs_in_window_returns_empty_result(self, tmp_path):
        response = {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "issueCount": 0,
                    "nodes": [],
                },
                "rateLimit": {
                    "remaining": 4999,
                    "limit": 5000,
                    "resetAt": "2026-05-05T18:00:00Z",
                    "cost": 1,
                },
            }
        }
        _seed_cache(tmp_path, response)

        result = flowmetrics_for_window(make_github_source(REPO, cache_dir=tmp_path, read_only=True), START, STOP)
        assert result.pr_count == 0
        assert result.portfolio_efficiency == 0.0
