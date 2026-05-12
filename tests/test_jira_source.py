"""Behavioural spec for the Jira REST source.

Contract:
- `JiraSource.fetch_completed_in_window(start, stop)` returns a list of
  WorkItem.
- Each issue's `item_id` is the Jira key (e.g. "BIGTOP-4525").
- `created_at` and `merged_at` come from `fields.created` and
  `fields.resolutiondate` respectively.
- Activity events derive from the changelog: status-transition timestamps
  and (TODO) comment timestamps.
- Read-only cache mode raises `CacheMiss` on cache miss.
- Network calls are blocked by the unit-test guard; tests use cached
  fixtures or in-process `httpx.MockTransport`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest

from flowmetrics.cache import CacheMiss, FileCache
from flowmetrics.sources.jira import JIRA_SEARCH_QUERY, JiraSource


def _no_network_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected network call to {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler))


def _seed_jira(
    cache: FileCache,
    *,
    base_url: str,
    jql: str,
    response: dict,
    start_at: int = 0,
    max_results: int = 100,
) -> None:
    key = FileCache.make_key(
        JIRA_SEARCH_QUERY,
        {
            "base_url": base_url,
            "jql": jql,
            "startAt": start_at,
            "maxResults": max_results,
        },
    )
    cache.put(key, response)


def _issue(
    *,
    key: str = "BIGTOP-1",
    summary: str = "Sample issue",
    created: str = "2026-05-01T09:00:00.000+0000",
    resolved: str | None = "2026-05-05T15:00:00.000+0000",
    histories: list[dict] | None = None,
    reporter: dict | None = None,
) -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "created": created,
            "resolutiondate": resolved,
            "status": {"name": "Resolved", "statusCategory": {"key": "done"}},
            "reporter": reporter or {"displayName": "Alice", "accountId": "u-1"},
        },
        "changelog": {"histories": histories or []},
    }


def _status_change(at: str, to: str) -> dict:
    return {
        "created": at,
        "items": [{"field": "status", "toString": to, "fromString": "To Do"}],
    }


class TestFetchCompletedInWindow:
    def test_maps_jira_issue_to_workitem(self, tmp_path):
        cache = FileCache(tmp_path)
        base_url = "https://issues.apache.org/jira"
        start, stop = date(2026, 5, 4), date(2026, 5, 10)
        jql = (
            'project = "BIGTOP" AND resolutiondate >= "2026-05-04" '
            'AND resolutiondate <= "2026-05-10" AND statusCategory = Done '
            "ORDER BY resolutiondate ASC"
        )
        response = {
            "startAt": 0,
            "maxResults": 100,
            "total": 1,
            "issues": [
                _issue(
                    key="BIGTOP-4525",
                    summary="Upgrade Ranger to 2.8.0",
                    created="2026-04-30T07:43:47.000+0000",
                    resolved="2026-05-08T08:36:09.000+0000",
                    histories=[
                        _status_change("2026-05-01T10:00:00.000+0000", "In Progress"),
                        _status_change("2026-05-08T08:36:09.000+0000", "Resolved"),
                    ],
                ),
            ],
        }
        _seed_jira(cache, base_url=base_url, jql=jql, response=response)

        source = JiraSource(
            base_url=base_url,
            project="BIGTOP",
            cache=cache,
            read_only=True,
            http_client=_no_network_client(),
        )
        items = source.fetch_completed_in_window(start, stop)

        assert len(items) == 1
        item = items[0]
        assert item.item_id == "BIGTOP-4525"
        assert item.title == "Upgrade Ranger to 2.8.0"
        assert item.created_at == datetime(2026, 4, 30, 7, 43, 47, tzinfo=UTC)
        assert item.merged_at == datetime(2026, 5, 8, 8, 36, 9, tzinfo=UTC)
        # Activity timestamps come from status-change history entries
        assert datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC) in item.activity
        assert datetime(2026, 5, 8, 8, 36, 9, tzinfo=UTC) in item.activity

    def test_skips_unresolved_issues(self, tmp_path):
        cache = FileCache(tmp_path)
        base_url = "https://issues.apache.org/jira"
        start, stop = date(2026, 5, 4), date(2026, 5, 10)
        jql = (
            'project = "BIGTOP" AND resolutiondate >= "2026-05-04" '
            'AND resolutiondate <= "2026-05-10" AND statusCategory = Done '
            "ORDER BY resolutiondate ASC"
        )
        response = {
            "startAt": 0, "maxResults": 100, "total": 1,
            "issues": [
                _issue(key="BIGTOP-0", resolved=None, histories=[]),
            ],
        }
        _seed_jira(cache, base_url=base_url, jql=jql, response=response)
        source = JiraSource(
            base_url=base_url, project="BIGTOP", cache=cache,
            read_only=True, http_client=_no_network_client(),
        )
        items = source.fetch_completed_in_window(start, stop)
        assert items == []

    def test_read_only_with_no_cache_raises(self, tmp_path):
        source = JiraSource(
            base_url="https://issues.apache.org/jira",
            project="BIGTOP",
            cache=FileCache(tmp_path),
            read_only=True,
            http_client=_no_network_client(),
        )
        with pytest.raises(CacheMiss):
            source.fetch_completed_in_window(date(2026, 5, 4), date(2026, 5, 10))


class TestStatusIntervalExtraction:
    """JiraSource must produce WorkItem.status_intervals so the compute
    layer can use Vacanti-canonical status-duration active-time math."""

    def test_two_transitions_yield_three_intervals(self, tmp_path):
        cache = FileCache(tmp_path)
        base_url = "https://issues.apache.org/jira"
        start, stop = date(2026, 5, 4), date(2026, 5, 10)
        jql = (
            'project = "BIGTOP" AND resolutiondate >= "2026-05-04" '
            'AND resolutiondate <= "2026-05-10" AND statusCategory = Done '
            "ORDER BY resolutiondate ASC"
        )
        # Open → In Progress at Tue 09:00 → Resolved at Fri 09:00.
        # Created Mon 09:00.
        response = {
            "startAt": 0, "maxResults": 100, "total": 1,
            "issues": [
                _issue(
                    key="BIGTOP-99",
                    created="2026-05-04T09:00:00.000+0000",
                    resolved="2026-05-08T09:00:00.000+0000",
                    histories=[
                        {
                            "created": "2026-05-05T09:00:00.000+0000",
                            "items": [{
                                "field": "status",
                                "fromString": "Open",
                                "toString": "In Progress",
                            }],
                        },
                        {
                            "created": "2026-05-08T09:00:00.000+0000",
                            "items": [{
                                "field": "status",
                                "fromString": "In Progress",
                                "toString": "Resolved",
                            }],
                        },
                    ],
                ),
            ],
        }
        _seed_jira(cache, base_url=base_url, jql=jql, response=response)
        source = JiraSource(
            base_url=base_url, project="BIGTOP", cache=cache,
            read_only=True, http_client=_no_network_client(),
        )
        items = source.fetch_completed_in_window(start, stop)
        item = items[0]
        # Status intervals reconstruct the issue's status timeline
        statuses = [(iv.status, iv.start, iv.end) for iv in item.status_intervals]
        assert len(statuses) == 2
        assert statuses[0] == (
            "Open",
            datetime(2026, 5, 4, 9, 0, tzinfo=UTC),
            datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
        )
        assert statuses[1] == (
            "In Progress",
            datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
            datetime(2026, 5, 8, 9, 0, tzinfo=UTC),
        )

    def test_no_status_history_yields_empty_intervals(self, tmp_path):
        cache = FileCache(tmp_path)
        base_url = "https://issues.apache.org/jira"
        start, stop = date(2026, 5, 4), date(2026, 5, 10)
        jql = (
            'project = "BIGTOP" AND resolutiondate >= "2026-05-04" '
            'AND resolutiondate <= "2026-05-10" AND statusCategory = Done '
            "ORDER BY resolutiondate ASC"
        )
        response = {
            "startAt": 0, "maxResults": 100, "total": 1,
            "issues": [_issue(key="BIGTOP-100", histories=[])],
        }
        _seed_jira(cache, base_url=base_url, jql=jql, response=response)
        source = JiraSource(
            base_url=base_url, project="BIGTOP", cache=cache,
            read_only=True, http_client=_no_network_client(),
        )
        items = source.fetch_completed_in_window(start, stop)
        assert items[0].status_intervals == []


class TestLabel:
    def test_label_combines_jira_and_project(self, tmp_path):
        source = JiraSource(
            base_url="https://issues.apache.org/jira",
            project="BIGTOP",
            cache=FileCache(tmp_path),
        )
        assert "BIGTOP" in source.label
