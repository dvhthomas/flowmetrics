from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .cache import FileCache
from .compute import WindowResult, aggregate, compute_pr_flow
from .github import GitHubClient, fetch_prs_merged_in_window
from .throughput import daily_throughput

DEFAULT_GAP = timedelta(hours=4)
DEFAULT_MIN_CLUSTER = timedelta(minutes=30)
DEFAULT_CACHE_DIR = Path(".cache/github")
DEFAULT_TRAINING_DAYS = 30  # Vacanti's recommendation in "When Will It Be Done?"


def this_week_window(today: date | None = None) -> tuple[date, date]:
    """Monday → Sunday window containing `today` (defaults to today)."""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def default_history_end() -> date:
    """Yesterday in UTC — the last day with complete throughput data.

    GitHub stores `mergedAt` in UTC. Today's bucket is always partial
    (work that would have merged later today hasn't happened yet), so
    including it biases the simulator low. Using yesterday-in-UTC means
    every day in the training window represents a complete observation.
    """
    return datetime.now(UTC).date() - timedelta(days=1)


def default_history_start(end: date | None = None) -> date:
    """29 days before `end` (default: 29 days before yesterday-UTC).

    Yields a 30-day inclusive window — Vacanti's recommended training
    horizon in *When Will It Be Done?*.
    """
    end = end or default_history_end()
    return end - timedelta(days=DEFAULT_TRAINING_DAYS - 1)


def flowmetrics_for_window(
    repo: str,
    start: date,
    stop: date,
    *,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    read_only: bool = False,
    gap: timedelta = DEFAULT_GAP,
    min_cluster: timedelta = DEFAULT_MIN_CLUSTER,
) -> WindowResult:
    """Compute flow efficiency for PRs merged in `[start, stop]` of `repo`."""
    if start > stop:
        raise ValueError(f"start ({start}) must be <= stop ({stop})")

    cache = FileCache(cache_dir)
    client = GitHubClient(cache, read_only=read_only)
    try:
        prs = fetch_prs_merged_in_window(client, repo, start, stop)
    finally:
        client.close()

    per_pr = [compute_pr_flow(pr, gap=gap, min_cluster=min_cluster) for pr in prs]
    return aggregate(per_pr)


def historical_throughput_samples(
    repo: str,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    read_only: bool = False,
) -> tuple[list[int], date, date]:
    """Fetch daily merge counts for `[start_date, end_date]` (inclusive).

    Returns (samples, training_start, training_end). Defaults give the
    last 30 calendar days ending yesterday-UTC — Vacanti's recommended
    training horizon. Zero-merge days are included as real observations.

    The previous `training_days` parameter was redundant — `start` +
    `end` carry the same information with less ambiguity.
    """
    end = end_date or default_history_end()
    start = start_date or default_history_start(end)
    if start > end:
        raise ValueError(f"start_date ({start}) must be <= end_date ({end})")

    cache = FileCache(cache_dir)
    client = GitHubClient(cache, read_only=read_only)
    try:
        prs = fetch_prs_merged_in_window(client, repo, start, end)
    finally:
        client.close()

    return daily_throughput(prs, start, end), start, end
