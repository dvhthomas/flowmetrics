from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .cache import FileCache
from .compute import WindowResult, WorkItem, aggregate, compute_pr_flow
from .sources import Source
from .sources.github import (
    GitHubClient,
    fetch_open_prs,
    fetch_open_prs_with_label_snapshot,
    fetch_prs_for_cycle_times,
    fetch_prs_merged_in_window,
)
from .sources.github_labels import WipLabels, is_aging_wip
from .sources.jira import JiraSource
from .throughput import daily_throughput

DEFAULT_GAP = timedelta(hours=4)
DEFAULT_MIN_CLUSTER = timedelta(minutes=30)
DEFAULT_CACHE_DIR = Path(".cache/github")
DEFAULT_TRAINING_DAYS = 30  # the standard rolling-window default
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

    Yields a 30-day inclusive window — the standard rolling training
    horizon.
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

    When ``wip_labels`` is set, ``fetch_in_flight`` switches to the
    label-driven materializer (see docs/SPEC-github-labels.md) and
    filters the result to items whose current status is one of the
    user's WIP labels. Absent ``wip_labels``, the existing review-cycle
    lifecycle is used.
    """

    def __init__(
        self,
        repo: str,
        cache: FileCache,
        read_only: bool = False,
        *,
        wip_labels: WipLabels | None = None,
        include_issues: bool = False,
    ):
        self._repo = repo
        self._client = GitHubClient(cache, read_only=read_only)
        self._wip_labels = wip_labels
        self._include_issues = include_issues

    @property
    def label(self) -> str:
        return self._repo

    def fetch_completed_in_window(self, start: date, stop: date):
        try:
            items = fetch_prs_merged_in_window(self._client, self._repo, start, stop)
            if self._include_issues:
                from .sources.github_issues import fetch_issues_closed_as_workitems
                items = items + fetch_issues_closed_as_workitems(
                    self._repo, start, stop, client=self._client
                )
            return items
        finally:
            self._client.close()

    def fetch_for_percentile_training(self, start: date, stop: date):
        """Lightweight fetch for Aging's percentile lines — no timeline
        events. Saves ~95% of payload on high-volume repos."""
        try:
            items = fetch_prs_for_cycle_times(self._client, self._repo, start, stop)
            if self._include_issues:
                # For percentile training we want cycle times only —
                # the Issue fetcher returns the same WorkItem shape so
                # the consumer doesn't notice it's a mixed population.
                from .sources.github_issues import fetch_issues_closed_as_workitems
                items = items + fetch_issues_closed_as_workitems(
                    self._repo, start, stop, client=self._client
                )
            return items
        finally:
            self._client.close()

    def fetch_in_flight(self, asof: date):
        """Open PRs as in-flight work items.

        Default mode (no ``wip_labels``): a four-state review lifecycle
        derived from `isDraft` + `reviewDecision` (Draft → Awaiting Review
        → Changes Requested → Approved). See docs/DECISIONS.md #9.

        Label mode (``wip_labels`` set): the user's named WIP labels drive
        per-PR ``status_intervals`` via timeline events; items not currently
        in a WIP column are filtered out. See docs/SPEC-github-labels.md.

        When ``include_issues`` is set, also fetches every currently-open
        Issue and folds them in. Each Issue's `status_intervals[-1].status`
        is its current label (or ``"Open"`` if unlabeled) — so the aging
        chart shows them in their respective workflow columns.
        """
        try:
            if self._wip_labels is not None:
                items = fetch_open_prs_with_label_snapshot(
                    self._client,
                    self._repo,
                    asof=asof,
                    wip=self._wip_labels,
                )
                items = [item for item in items if is_aging_wip(item)]
            else:
                items = fetch_open_prs(self._client, self._repo, asof=asof)
            if self._include_issues:
                from .sources.github_issues import fetch_open_issues_as_workitems
                items = items + fetch_open_issues_as_workitems(
                    self._repo, asof, client=self._client
                )
            return items
        finally:
            self._client.close()


def make_github_source(
    repo: str,
    *,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
    read_only: bool = False,
    wip_labels: WipLabels | None = None,
    include_issues: bool = False,
) -> Source:
    return _GitHubSourceAdapter(
        repo,
        FileCache(cache_dir),
        read_only=read_only,
        wip_labels=wip_labels,
        include_issues=include_issues,
    )


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


def fetch_items_active_in_window(
    source: Source, start: date, stop: date,
) -> list[WorkItem]:
    """Items that were active in `[start, stop]` at any point.

    Three categories of items belong on a CFD over a window:

      1. Merged inside the window. Already covered by
         `source.fetch_completed_in_window(start, stop)`.
      2. Still open at the window end. Already covered by
         `source.fetch_in_flight(stop)`.
      3. Carry-over: opened before window, merged inside it.
         Subset of (1).

    The union of (1) and (2) — deduped on `item_id` — covers every
    item that should appear on the CFD without including items that
    were already finished before the window started or are still
    unborn at the window end.

    Without this composition, a CFD restricted to
    `fetch_completed_in_window` shows a suspect 'perfect balance'
    where every arrival is also a departure, because the data set
    is defined to contain only items that both arrived AND departed
    inside the window.
    """
    completed = list(source.fetch_completed_in_window(start, stop))
    in_flight = list(source.fetch_in_flight(stop))
    seen_ids = {item.item_id for item in completed}
    return completed + [item for item in in_flight if item.item_id not in seen_ids]


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
