"""Jira source — reads completed issues from any Jira Server / Cloud instance.

Public test target: Apache Software Foundation Jira at
``https://issues.apache.org/jira`` (anonymous REST access works for most
projects). Other Jira deployments need credentials; not yet wired.

Active vs wait time on Jira is fundamentally different from GitHub:
issues have explicit named statuses with transitions in the changelog,
so a more accurate model would map ``In Progress`` → active, ``In
Review`` → wait, etc. For now we reuse GitHub's event-clustering model
by treating status-transition timestamps as activity events; this gives
the same output shape with less accuracy. The status-mapping refinement
is captured as a future task.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx
from dateutil.parser import isoparse

from ..cache import CacheMiss, FileCache
from ..compute import WorkItem

# Pseudo-query string used as part of the cache key. The actual REST API
# uses query parameters, but for cache-key stability we hash this constant
# together with the variables (same pattern as the GitHub GraphQL client).
JIRA_SEARCH_QUERY = "jira:search:v2:expand=changelog"

_RETRY_STATUSES = frozenset({500, 502, 503, 504})


class JiraError(Exception):
    """Raised when Jira returns an error envelope."""


def _parse_dt(s: str) -> datetime:
    dt = isoparse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


@dataclass
class JiraSource:
    """Fetches resolved Jira issues in a date window.

    Caching, retries, and read-only mode mirror the GitHub source so the
    rest of the pipeline doesn't know which one it's running against.
    """

    base_url: str
    project: str
    cache: FileCache
    read_only: bool = False
    http_client: httpx.Client | None = None
    timeout: float = 30.0
    user_agent: str = "flowmetrics/0.1 (+https://github.com/dvhthomas/flowmetrics)"
    max_retries: int = 3
    retry_initial_seconds: float = 1.0
    _owns_client: bool = field(default=False, init=False, repr=False)

    @property
    def label(self) -> str:
        return f"jira:{self.project}"

    def _client(self) -> httpx.Client:
        if self.http_client is not None:
            return self.http_client
        self.http_client = httpx.Client(timeout=self.timeout)
        self._owns_client = True
        return self.http_client

    def close(self) -> None:
        if self._owns_client and self.http_client is not None:
            self.http_client.close()
            self.http_client = None
            self._owns_client = False

    def _search(self, jql: str, start_at: int, max_results: int) -> dict[str, Any]:
        variables = {
            "base_url": self.base_url,
            "jql": jql,
            "startAt": start_at,
            "maxResults": max_results,
        }
        key = FileCache.make_key(JIRA_SEARCH_QUERY, variables)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if self.read_only:
            raise CacheMiss(
                f"No cache entry for Jira search key={key} (read_only=True). "
                "Record a fixture by running once with read_only=False."
            )

        url = f"{self.base_url.rstrip('/')}/rest/api/2/search"
        params = {
            "jql": jql,
            "startAt": str(start_at),
            "maxResults": str(max_results),
            "fields": "summary,created,resolutiondate,status,reporter",
            "expand": "changelog",
        }
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }

        response = self._get_with_retries(url, headers, params)
        response.raise_for_status()
        payload = response.json()
        if payload.get("errorMessages"):
            raise JiraError(payload["errorMessages"])
        self.cache.put(key, payload)
        return payload

    def _get_with_retries(
        self, url: str, headers: dict[str, str], params: dict[str, str]
    ) -> httpx.Response:
        client = self._client()
        delay = self.retry_initial_seconds
        last: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            response = client.get(url, headers=headers, params=params)
            last = response
            if response.status_code not in _RETRY_STATUSES:
                return response
            if attempt == self.max_retries:
                break
            print(
                f"flowmetrics: Jira returned {response.status_code}, retrying in "
                f"{delay:.1f}s (attempt {attempt + 1}/{self.max_retries})",
                file=sys.stderr,
            )
            if delay > 0:
                time.sleep(delay)
            delay *= 2
        assert last is not None
        return last

    def fetch_completed_in_window(self, start: date, stop: date) -> list[WorkItem]:
        jql = (
            f'project = "{self.project}" '
            f'AND resolutiondate >= "{start.isoformat()}" '
            f'AND resolutiondate <= "{stop.isoformat()}" '
            "AND statusCategory = Done "
            "ORDER BY resolutiondate ASC"
        )
        items: list[WorkItem] = []
        start_at = 0
        max_results = 100
        while True:
            payload = self._search(jql, start_at, max_results)
            for issue in payload.get("issues", []):
                item = _issue_to_work_item(issue)
                if item is not None:
                    items.append(item)
            total = payload.get("total", 0)
            start_at += max_results
            if start_at >= total:
                break
        return items


def _issue_to_work_item(issue: dict[str, Any]) -> WorkItem | None:
    fields = issue.get("fields") or {}
    if not fields.get("resolutiondate"):
        return None

    key = issue.get("key", "")
    title = fields.get("summary", "")
    created = _parse_dt(fields["created"])
    resolved = _parse_dt(fields["resolutiondate"])

    activity: list[datetime] = []
    for history in (issue.get("changelog") or {}).get("histories", []):
        if not history.get("created"):
            continue
        ts = _parse_dt(history["created"])
        # Only count status transitions as activity (consistent with how
        # GitHub treats timeline events: each transition = "something happened").
        items = history.get("items") or []
        if any(it.get("field") == "status" for it in items):
            activity.append(ts)

    reporter = fields.get("reporter") or {}
    author_login = reporter.get("name") or reporter.get("accountId")

    return WorkItem(
        item_id=key,
        title=title,
        created_at=created,
        merged_at=resolved,
        activity=activity,
        is_bot=False,  # Jira doesn't have a canonical bot marker; could be inferred later
        author_login=author_login,
    )


# Re-export for testing convenience
_ = json  # pragma: no cover — silence unused import warning
