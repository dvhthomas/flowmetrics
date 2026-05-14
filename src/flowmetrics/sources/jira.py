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
from ..compute import StatusInterval, WorkItem

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
        return self._paginated_fetch(jql, in_flight_asof=None)

    def fetch_for_percentile_training(
        self, start: date, stop: date
    ) -> list[WorkItem]:
        # Jira's changelog query carries no inline timeline payload that
        # could be over-fetched, so the lightweight variant is just the
        # same query. This stub exists so the Source protocol stays uniform.
        return self.fetch_completed_in_window(start, stop)

    def fetch_in_flight(self, asof: date) -> list[WorkItem]:
        """Unresolved issues for this project, with their current status
        appended as a final synthetic interval ending at `asof`."""
        jql = (
            f'project = "{self.project}" '
            "AND resolution = Unresolved "
            "ORDER BY created ASC"
        )
        return self._paginated_fetch(jql, in_flight_asof=asof)

    def _paginated_fetch(self, jql: str, *, in_flight_asof: date | None) -> list[WorkItem]:
        items: list[WorkItem] = []
        start_at = 0
        max_results = 100
        while True:
            payload = self._search(jql, start_at, max_results)
            for issue in payload.get("issues", []):
                item = _issue_to_work_item(issue, in_flight_asof=in_flight_asof)
                if item is not None:
                    items.append(item)
            total = payload.get("total", 0)
            start_at += max_results
            if start_at >= total:
                break
        return items


def _issue_to_work_item(
    issue: dict[str, Any],
    *,
    in_flight_asof: date | None = None,
) -> WorkItem | None:
    """Convert one Jira issue dict to a WorkItem.

    `in_flight_asof=None` ⇒ completed-item path: requires `resolutiondate`
    and emits intervals up to (but excluding) the resolution status.

    `in_flight_asof=<date>` ⇒ in-flight path: skips items that DO have a
    resolutiondate, and appends a final synthetic interval ending at
    `in_flight_asof` with the current status (so Aging can read it).
    """
    fields = issue.get("fields") or {}
    has_resolution = bool(fields.get("resolutiondate"))
    if in_flight_asof is None and not has_resolution:
        return None
    if in_flight_asof is not None and has_resolution:
        return None

    key = issue.get("key", "")
    title = fields.get("summary", "")
    created = _parse_dt(fields["created"])
    resolved = _parse_dt(fields["resolutiondate"]) if has_resolution else None

    # Walk the changelog forward; each status-field item closes the
    # previous status's interval.
    status_changes: list[tuple[datetime, str, str]] = []
    activity: list[datetime] = []
    for history in (issue.get("changelog") or {}).get("histories", []):
        if not history.get("created"):
            continue
        ts = _parse_dt(history["created"])
        for it in history.get("items") or []:
            if it.get("field") == "status":
                status_changes.append(
                    (ts, it.get("fromString") or "Unknown", it.get("toString") or "Unknown")
                )
                activity.append(ts)
                break
    status_changes.sort(key=lambda x: x[0])

    intervals: list[StatusInterval] = []
    current_status: str | None = None
    if status_changes:
        prev_ts = created
        prev_status = status_changes[0][1]  # fromString of first transition
        for ts, _from, to_status in status_changes:
            intervals.append(StatusInterval(prev_ts, ts, prev_status))
            prev_ts = ts
            prev_status = to_status
        current_status = status_changes[-1][2]  # toString of last transition

    if in_flight_asof is not None:
        # In-flight items: append a final interval representing the
        # currently-occupied status, ending at `asof`. If there were no
        # transitions in the changelog, fall back to the `status.name`
        # field on the issue (issues that never moved).
        if current_status is None:
            status_obj = fields.get("status") or {}
            current_status = status_obj.get("name") or "Unknown"
        last_start = intervals[-1].end if intervals else created
        asof_dt = datetime.combine(in_flight_asof, datetime.min.time()).replace(tzinfo=UTC)
        if asof_dt > last_start:
            intervals.append(StatusInterval(last_start, asof_dt, current_status))

    reporter = fields.get("reporter") or {}
    author_login = reporter.get("name") or reporter.get("accountId")

    return WorkItem(
        item_id=key,
        title=title,
        created_at=created,
        merged_at=resolved,
        activity=activity,
        is_bot=False,
        author_login=author_login,
        status_intervals=intervals,
    )


# Re-export for testing convenience
_ = json  # pragma: no cover — silence unused import warning
