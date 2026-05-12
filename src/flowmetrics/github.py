from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx
from dateutil.parser import isoparse

from .cache import CacheMiss, FileCache
from .compute import WorkItem

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"


class GitHubError(Exception):
    """Raised when the GraphQL response contains an `errors` array."""


def resolve_token() -> str:
    """Get a GitHub token: $GITHUB_TOKEN, else `gh auth token`."""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token.strip()
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "No GitHub token available. Set $GITHUB_TOKEN or run `gh auth login`."
        ) from exc
    return result.stdout.strip()


# Single canonical query — keeping it constant means the cache key is
# stable across runs for the same (repo, window).
PR_SEARCH_QUERY = """
query($q: String!, $first: Int!, $after: String) {
  search(query: $q, type: ISSUE, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    issueCount
    nodes {
      ... on PullRequest {
        number
        title
        createdAt
        mergedAt
        author {
          __typename
          login
        }
        timelineItems(first: 100) {
          pageInfo { hasNextPage }
          nodes {
            __typename
            ... on PullRequestCommit { commit { committedDate } }
            ... on PullRequestReview { submittedAt }
            ... on IssueComment { createdAt }
            ... on PullRequestReviewThread {
              comments(first: 1) { nodes { createdAt } }
            }
            ... on ReadyForReviewEvent { createdAt }
            ... on ConvertToDraftEvent { createdAt }
            ... on ReviewRequestedEvent { createdAt }
            ... on MergedEvent { createdAt }
            ... on ClosedEvent { createdAt }
          }
        }
      }
    }
  }
  rateLimit { remaining limit resetAt cost }
}
""".strip()


_RETRY_STATUSES = frozenset({500, 502, 503, 504})


@dataclass
class GitHubClient:
    """GraphQL client with disk-backed cache and transient-error retries.

    Issues a single POST per uncached (query, variables) pair. The cache
    means reruns over the same window make zero requests. Credentials
    come from $GITHUB_TOKEN, falling back to `gh auth token`.

    On 5xx responses we retry with exponential backoff up to
    `max_retries` times. 4xx errors fail fast.
    """

    cache: FileCache
    read_only: bool = False
    token: str | None = None
    http_client: httpx.Client | None = None
    timeout: float = 30.0
    user_agent: str = "flowmetrics/0.1 (+https://github.com/)"
    max_retries: int = 3
    retry_initial_seconds: float = 1.0
    _owns_client: bool = field(default=False, init=False, repr=False)

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

    def _post_with_retries(self, query: str, variables: dict[str, Any]) -> httpx.Response:
        """POST to GraphQL with exponential-backoff retry on 5xx."""
        client = self._client()
        body = {"query": query, "variables": variables}
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": self.user_agent,
        }

        delay = self.retry_initial_seconds
        last_response: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            response = client.post(GITHUB_GRAPHQL_URL, headers=headers, json=body)
            last_response = response
            if response.status_code not in _RETRY_STATUSES:
                return response  # success OR a fail-fast 4xx — let caller raise
            if attempt == self.max_retries:
                break
            print(
                f"flowmetrics: GitHub returned {response.status_code}, retrying in "
                f"{delay:.1f}s (attempt {attempt + 1}/{self.max_retries})",
                file=sys.stderr,
            )
            if delay > 0:
                time.sleep(delay)
            delay *= 2  # exponential backoff
        assert last_response is not None
        return last_response

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        key = FileCache.make_key(query, variables)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        if self.read_only:
            raise CacheMiss(
                f"No cache entry for key={key} (read_only=True). "
                "Record a fixture by running once with read_only=False."
            )

        if self.token is None:
            self.token = resolve_token()

        response = self._post_with_retries(query, variables)
        response.raise_for_status()
        payload = response.json()
        if "errors" in payload:
            raise GitHubError(payload["errors"])
        self.cache.put(key, payload)
        return payload


def _parse_dt(s: str) -> datetime:
    dt = isoparse(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def extract_activity(pr_node: dict[str, Any]) -> list[datetime]:
    """Pull every activity timestamp out of a PR GraphQL node."""
    timeline = pr_node.get("timelineItems", {}).get("nodes", [])
    events: list[datetime] = []
    for item in timeline:
        type_name = item.get("__typename")
        match type_name:
            case "PullRequestCommit":
                commit = item.get("commit") or {}
                if "committedDate" in commit:
                    events.append(_parse_dt(commit["committedDate"]))
            case "PullRequestReview":
                if item.get("submittedAt"):
                    events.append(_parse_dt(item["submittedAt"]))
            case "PullRequestReviewThread":
                comments = (item.get("comments") or {}).get("nodes", [])
                for c in comments:
                    if "createdAt" in c:
                        events.append(_parse_dt(c["createdAt"]))
            case (
                "IssueComment"
                | "ReadyForReviewEvent"
                | "ConvertToDraftEvent"
                | "ReviewRequestedEvent"
                | "MergedEvent"
                | "ClosedEvent"
            ):
                if item.get("createdAt"):
                    events.append(_parse_dt(item["createdAt"]))
    return events


def _is_bot(author: dict[str, Any] | None) -> bool:
    """A PR's author is a bot if its GraphQL __typename is `Bot` or its
    login ends with the conventional `[bot]` suffix (covers github-actions
    and other apps that appear as `User` with a bot-suffixed login)."""
    if not author:
        return False
    if author.get("__typename") == "Bot":
        return True
    login = author.get("login") or ""
    return login.endswith("[bot]")


def _pr_node_to_events(node: dict[str, Any]) -> WorkItem | None:
    if not node.get("mergedAt"):
        return None
    author = node.get("author")
    return WorkItem(
        item_id=f"#{node['number']}",
        title=node.get("title", ""),
        created_at=_parse_dt(node["createdAt"]),
        merged_at=_parse_dt(node["mergedAt"]),
        activity=extract_activity(node),
        is_bot=_is_bot(author),
        author_login=(author or {}).get("login"),
    )


def fetch_prs_merged_in_window(
    client: GitHubClient,
    repo: str,
    start: date,
    stop: date,
    *,
    page_size: int = 100,
) -> list[WorkItem]:
    """Fetch every PR merged in [start, stop] (inclusive)."""
    q = f"repo:{repo} is:pr is:merged merged:{start.isoformat()}..{stop.isoformat()}"
    prs: list[WorkItem] = []
    after: str | None = None
    while True:
        payload = client.graphql(
            PR_SEARCH_QUERY,
            {"q": q, "first": page_size, "after": after},
        )
        search = payload["data"]["search"]
        for node in search["nodes"]:
            if not node:  # nodes can include non-PR items in odd cases
                continue
            events = _pr_node_to_events(node)
            if events is not None:
                prs.append(events)
        page_info = search["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        after = page_info["endCursor"]
    return prs
