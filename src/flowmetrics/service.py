from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .cache import FileCache
from .compute import WindowResult, aggregate, compute_pr_flow
from .github import GitHubClient, fetch_prs_merged_in_window
from .sources import Source
from .sources.jira import JiraSource
from .throughput import daily_throughput

DEFAULT_GAP = timedelta(hours=4)
DEFAULT_MIN_CLUSTER = timedelta(minutes=30)
DEFAULT_CACHE_DIR = Path(".cache/github")
DEFAULT_TRAINING_DAYS = 30  # Vacanti's recommendation in "When Will It Be Done?"
# Default mapping of named workflow statuses to "active". Used by the
# status-duration computation when a source provides explicit statuses
# (Jira). GitHub sources, which infer activity from events, ignore this.
DEFAULT_ACTIVE_STATUSES: frozenset[str] = frozenset({"In Progress", "In Development"})


def this_week_window(today: date | None = None) -> tuple[date, date]:
    """Monday → Sunday window containing `today` (defaults to today)."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def default_history_end() -> date:
    """Yesterday in UTC — the last day with complete throughput data."""
    return datetime.now(UTC).date() - timedelta(days=1)


def default_history_start(end: date | None = None) -> date:
    """29 days before `end` (default: 29 days before yesterday-UTC).

    Yields a 30-day inclusive window — Vacanti's recommended training
    horizon in *When Will It Be Done?*.
    """
    end = end or default_history_end()
    return end - timedelta(days=DEFAULT_TRAINING_DAYS - 1)


# ---------------------------------------------------------------------------
# Source factories
# ---------------------------------------------------------------------------


class _GitHubSourceAdapter:
    """Adapt the existing GitHub fetcher to the Source protocol.

    Keeps `github.py` and its tests/fixtures untouched while exposing a
    uniform interface alongside JiraSource.
    """

    def __init__(self, repo: str, cache: FileCache, read_only: bool = False):
        self._repo = repo
        self._client = GitHubClient(cache, read_only=read_only)

    @property
    def label(self) -> str:
        return self._repo

    def fetch_completed_in_window(self, start: date, stop: date):
        try:
            return fetch_prs_merged_in_window(self._client, self._repo, start, stop)
        finally:
            self._client.close()


def make_github_source(
    repo: str,
    *,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    read_only: bool = False,
) -> Source:
    return _GitHubSourceAdapter(repo, FileCache(cache_dir), read_only=read_only)


def make_jira_source(
    base_url: str,
    project: str,
    *,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    read_only: bool = False,
) -> Source:
    return JiraSource(
        base_url=base_url,
        project=project,
        cache=FileCache(cache_dir),
        read_only=read_only,
    )


# ---------------------------------------------------------------------------
# Source-agnostic service entry points
# ---------------------------------------------------------------------------


def flowmetrics_for_window(
    source: Source,
    start: date,
    stop: date,
    *,
    gap: timedelta = DEFAULT_GAP,
    min_cluster: timedelta = DEFAULT_MIN_CLUSTER,
    active_statuses: frozenset[str] | None = None,
) -> WindowResult:
    """Compute flow efficiency for items completed in `[start, stop]`.

    `active_statuses` is only consulted when items carry
    `status_intervals` (i.e. when the source has named statuses, like Jira).
    """
    if start > stop:
        raise ValueError(f"start ({start}) must be <= stop ({stop})")
    items = source.fetch_completed_in_window(start, stop)
    per_pr = [
        compute_pr_flow(
            item, gap=gap, min_cluster=min_cluster, active_statuses=active_statuses
        )
        for item in items
    ]
    return aggregate(per_pr)


def historical_throughput_samples(
    source: Source,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> tuple[list[int], date, date]:
    """Fetch daily completion counts for `[start_date, end_date]` (inclusive).

    Defaults give the last 30 calendar days ending yesterday-UTC.
    Zero-throughput days are included as real observations.
    """
    end = end_date or default_history_end()
    start = start_date or default_history_start(end)
    if start > end:
        raise ValueError(f"start_date ({start}) must be <= end_date ({end})")
    items = source.fetch_completed_in_window(start, end)
    return daily_throughput(items, start, end), start, end
