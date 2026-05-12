from datetime import UTC, date, datetime

import httpx
import pytest

from flowmetrics.cache import CacheMiss, FileCache
from flowmetrics.github import (
    PR_SEARCH_QUERY,
    GitHubClient,
    extract_activity,
    fetch_prs_merged_in_window,
)


def dt(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _no_network_client() -> httpx.Client:
    """An httpx client whose transport raises on any request — proves the
    code under test never hit the network."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(
            f"Unexpected network call to {request.url}; cache should have served this"
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _pr_search_variables(repo: str, start: date, stop: date) -> dict:
    return {
        "q": f"repo:{repo} is:pr is:merged merged:{start.isoformat()}..{stop.isoformat()}",
        "first": 100,
        "after": None,
    }


def _stub_pr(*, number: int, author: dict | None = None, title: str = "PR") -> dict:
    return {
        "number": number,
        "title": title,
        "createdAt": "2026-05-05T09:00:00Z",
        "mergedAt": "2026-05-05T10:00:00Z",
        "author": author,
        "timelineItems": {"pageInfo": {"hasNextPage": False}, "nodes": []},
    }


def _seed_search(cache: FileCache, repo: str, start: date, stop: date, prs: list[dict]) -> None:
    variables = _pr_search_variables(repo, start, stop)
    response = {
        "data": {
            "search": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "issueCount": len(prs),
                "nodes": prs,
            }
        }
    }
    cache.put(FileCache.make_key(PR_SEARCH_QUERY, variables), response)


class TestBotDetection:
    """Bot PRs are flagged so we can distinguish human-fast from bot-fast."""

    def test_bot_typename_marks_pr_as_bot(self, tmp_path):
        cache = FileCache(tmp_path)
        start, stop = date(2026, 5, 4), date(2026, 5, 10)
        _seed_search(
            cache,
            "x/y",
            start,
            stop,
            [
                _stub_pr(number=1, author={"__typename": "User", "login": "alice"}),
                _stub_pr(number=2, author={"__typename": "Bot", "login": "dependabot[bot]"}),
            ],
        )
        client = GitHubClient(cache, read_only=True, http_client=_no_network_client())
        prs = fetch_prs_merged_in_window(client, "x/y", start, stop)
        by_number = {p.number: p for p in prs}
        assert by_number[1].is_bot is False
        assert by_number[2].is_bot is True

    def test_login_ending_in_bot_marks_pr_as_bot(self, tmp_path):
        cache = FileCache(tmp_path)
        start, stop = date(2026, 5, 4), date(2026, 5, 10)
        _seed_search(
            cache,
            "x/y",
            start,
            stop,
            [
                _stub_pr(number=3, author={"__typename": "User", "login": "github-actions[bot]"}),
            ],
        )
        client = GitHubClient(cache, read_only=True, http_client=_no_network_client())
        prs = fetch_prs_merged_in_window(client, "x/y", start, stop)
        assert prs[0].is_bot is True

    def test_missing_author_does_not_crash(self, tmp_path):
        cache = FileCache(tmp_path)
        start, stop = date(2026, 5, 4), date(2026, 5, 10)
        _seed_search(
            cache,
            "x/y",
            start,
            stop,
            [
                _stub_pr(number=4, author=None),
            ],
        )
        client = GitHubClient(cache, read_only=True, http_client=_no_network_client())
        prs = fetch_prs_merged_in_window(client, "x/y", start, stop)
        assert prs[0].is_bot is False


class TestExtractActivity:
    def test_collects_commit_review_comment_and_merge_events(self):
        node = {
            "timelineItems": {
                "nodes": [
                    {
                        "__typename": "PullRequestCommit",
                        "commit": {"committedDate": "2026-05-05T10:00:00Z"},
                    },
                    {
                        "__typename": "PullRequestReview",
                        "submittedAt": "2026-05-05T14:00:00Z",
                    },
                    {
                        "__typename": "IssueComment",
                        "createdAt": "2026-05-05T15:00:00Z",
                    },
                    {
                        "__typename": "PullRequestReviewThread",
                        "comments": {"nodes": [{"createdAt": "2026-05-05T15:30:00Z"}]},
                    },
                    {
                        "__typename": "ReadyForReviewEvent",
                        "createdAt": "2026-05-05T13:00:00Z",
                    },
                    {
                        "__typename": "MergedEvent",
                        "createdAt": "2026-05-05T16:00:00Z",
                    },
                ]
            }
        }
        events = extract_activity(node)
        assert dt(2026, 5, 5, 10, 0) in events
        assert dt(2026, 5, 5, 14, 0) in events
        assert dt(2026, 5, 5, 15, 0) in events
        assert dt(2026, 5, 5, 15, 30) in events
        assert dt(2026, 5, 5, 13, 0) in events
        assert dt(2026, 5, 5, 16, 0) in events

    def test_ignores_unknown_typenames(self):
        node = {
            "timelineItems": {
                "nodes": [{"__typename": "LabeledEvent", "createdAt": "2026-05-05T10:00:00Z"}]
            }
        }
        assert extract_activity(node) == []

    def test_handles_missing_timeline(self):
        assert extract_activity({}) == []


class TestGitHubClientCache:
    def test_read_only_with_no_cache_raises(self, tmp_path):
        client = GitHubClient(FileCache(tmp_path), read_only=True)
        with pytest.raises(CacheMiss):
            client.graphql("query{viewer{login}}", {})

    def test_cache_hit_returns_payload_without_network_call(self, tmp_path):
        cache = FileCache(tmp_path)
        payload = {"data": {"viewer": {"login": "octocat"}}}
        cache.put(FileCache.make_key("query{viewer{login}}", {}), payload)
        client = GitHubClient(cache, http_client=_no_network_client())
        assert client.graphql("query{viewer{login}}", {}) == payload


class TestFetchPrsMergedInWindow:
    def test_fetch_uses_cache_and_maps_to_pull_request_events(self, tmp_path):
        cache = FileCache(tmp_path)
        repo = "pallets/flask"
        start, stop = date(2026, 5, 4), date(2026, 5, 10)
        variables = {
            "q": f"repo:{repo} is:pr is:merged merged:{start.isoformat()}..{stop.isoformat()}",
            "first": 100,
            "after": None,
        }
        response = {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "issueCount": 1,
                    "nodes": [
                        {
                            "number": 5500,
                            "title": "Fix something",
                            "createdAt": "2026-05-05T09:00:00Z",
                            "mergedAt": "2026-05-05T17:00:00Z",
                            "timelineItems": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [
                                    {
                                        "__typename": "PullRequestCommit",
                                        "commit": {"committedDate": "2026-05-05T10:00:00Z"},
                                    },
                                    {
                                        "__typename": "IssueComment",
                                        "createdAt": "2026-05-05T12:00:00Z",
                                    },
                                    {
                                        "__typename": "MergedEvent",
                                        "createdAt": "2026-05-05T17:00:00Z",
                                    },
                                ],
                            },
                        }
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
        cache.put(FileCache.make_key(PR_SEARCH_QUERY, variables), response)

        client = GitHubClient(cache, read_only=True, http_client=_no_network_client())
        prs = fetch_prs_merged_in_window(client, repo, start, stop)

        assert len(prs) == 1
        pr = prs[0]
        assert pr.number == 5500
        assert pr.title == "Fix something"
        assert pr.created_at == dt(2026, 5, 5, 9, 0)
        assert pr.merged_at == dt(2026, 5, 5, 17, 0)
        assert dt(2026, 5, 5, 10, 0) in pr.activity
        assert dt(2026, 5, 5, 12, 0) in pr.activity
        assert dt(2026, 5, 5, 17, 0) in pr.activity

    def test_skips_unmerged_nodes(self, tmp_path):
        cache = FileCache(tmp_path)
        repo = "pallets/flask"
        start, stop = date(2026, 5, 4), date(2026, 5, 10)
        variables = {
            "q": f"repo:{repo} is:pr is:merged merged:{start.isoformat()}..{stop.isoformat()}",
            "first": 100,
            "after": None,
        }
        response = {
            "data": {
                "search": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "issueCount": 1,
                    "nodes": [
                        {
                            "number": 5501,
                            "title": "Open PR",
                            "createdAt": "2026-05-05T09:00:00Z",
                            "mergedAt": None,
                            "timelineItems": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [],
                            },
                        }
                    ],
                }
            }
        }
        cache.put(FileCache.make_key(PR_SEARCH_QUERY, variables), response)

        client = GitHubClient(cache, read_only=True, http_client=_no_network_client())
        assert fetch_prs_merged_in_window(client, repo, start, stop) == []
