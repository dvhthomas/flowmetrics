"""flowmetrics CLI.

For agent use, pass `--format json`. JSON output includes:
- A versioned `schema` field.
- The raw result, plus `chart_data` so an agent can reason about charts
  it can't see.
- An `interpretation` block (headline, key insight, next actions, caveats).
- A `logs` field that captures stderr + warnings (so nothing is lost to
  stderr when stdout is consumed as JSON).
- A `docs` block pointing at the explainer docs.

Errors in JSON mode are a `flowmetrics.error.v1` envelope on stdout.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from random import Random
from typing import Any

import click

from .aging import compute_aging, cycle_time_percentiles
from .cfd import build_cfd
from .compute import WindowResult, compute_pr_flow  # compute_pr_flow used in aging path
from .forecast import (
    ResultsHistogram,
    backward_percentile,
    build_histogram,
    forward_percentile,
    monte_carlo_how_many,
    monte_carlo_when_done,
)
from .interpretation import (
    interpret_aging,
    interpret_cfd,
    interpret_efficiency,
    interpret_how_many,
    interpret_scatterplot,
    interpret_when_done,
)
from .logcapture import LogCapture
from .renderers import html_renderer, json_renderer, text_renderer
from .report import (
    AgingInput,
    AgingReport,
    CfdInput,
    CfdReport,
    EfficiencyInput,
    EfficiencyReport,
    HowManyInput,
    HowManyReport,
    ScatterplotInput,
    ScatterplotPoint,
    ScatterplotReport,
    SimulationSummary,
    WhenDoneInput,
    WhenDoneReport,
    build_training_summary,
)
from .service import (
    DEFAULT_ACTIVE_STATUSES,
    DEFAULT_CACHE_DIR,
    DEFAULT_GAP,
    DEFAULT_MIN_CLUSTER,
    DEFAULT_TRAINING_DAYS,
    flowmetrics_for_window,
    historical_throughput_samples,
    make_github_source,
    make_jira_source,
    this_week_window,
)
from .sources import Source
from .sources.github_labels import WipLabels


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


_FORMAT_OPTION = click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json", "html"]),
    default="text",
    show_default=True,
    help="Output format. text=humans (default), json=agents, html=archival report file.",
)
_OUTPUT_OPTION = click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="File to write to. Defaults to stdout (text/json) or reports/... (html).",
)
_VERBOSE_OPTION = click.option(
    "--verbose",
    "-v",
    is_flag=True,
    default=False,
    help="With --format text, print the full report (tables, interpretation, "
    "detail, vocabulary). Default text output is a one-line headline.",
)

_SOURCE_OPTIONS = [
    click.option(
        "--repo", default=None,
        help="GitHub repo as owner/name (e.g. astral-sh/uv). "
        "Mutually exclusive with --jira-url/--jira-project.",
    ),
    click.option(
        "--jira-url", default=None,
        help="Jira base URL (e.g. https://issues.apache.org/jira). "
        "Used together with --jira-project.",
    ),
    click.option(
        "--jira-project", default=None,
        help="Jira project key (e.g. BIGTOP). Used together with --jira-url.",
    ),
]


def _apply_source_options(f):
    for decorator in reversed(_SOURCE_OPTIONS):
        f = decorator(f)
    return f


def _build_source(
    *,
    repo: str | None,
    jira_url: str | None,
    jira_project: str | None,
    cache_dir: Path,
    offline: bool,
    wip_labels: WipLabels | None = None,
) -> Source:
    """Pick the source backend from whichever flag set the user provided.

    `--repo` ⇒ GitHub. `--jira-url` + `--jira-project` ⇒ Jira. Mutually
    exclusive; exactly one set must be present.

    `wip_labels` is GitHub-only — Jira workflows come from issue
    changelogs, so passing it with a Jira source is an error.
    """
    have_github = bool(repo)
    have_jira = bool(jira_url or jira_project)
    if have_github and have_jira:
        raise click.UsageError(
            "Pass either --repo (GitHub) or --jira-url + --jira-project (Jira), not both."
        )
    if wip_labels is not None and not have_github:
        raise click.UsageError(
            "--wip-labels is GitHub-only. Jira workflows come from issue changelogs; "
            "use --workflow instead."
        )
    if have_github:
        return make_github_source(
            repo, cache_dir=cache_dir, read_only=offline, wip_labels=wip_labels
        )
    if have_jira:
        if not (jira_url and jira_project):
            raise click.UsageError(
                "Jira needs both --jira-url AND --jira-project."
            )
        return make_jira_source(
            jira_url, jira_project, cache_dir=cache_dir, read_only=offline
        )
    raise click.UsageError(
        "No source specified. Pass --repo OWNER/NAME (GitHub) "
        "or --jira-url URL + --jira-project KEY (Jira)."
    )


def _dispatch(
    fmt: str,
    output: Path | None,
    build_report: Callable[[], Any],
    *,
    verbose: bool = False,
) -> None:
    if fmt == "json":
        # cap.lines is populated by __exit__ — must be read AFTER the
        # `with` block, not inside it. Both the success and error renders
        # happen post-exit so they see the captured stderr/warnings.
        report: Any = None
        error: Exception | None = None
        with LogCapture() as cap:
            try:
                report = build_report()
            except Exception as exc:
                error = exc
        if error is not None:
            _emit(
                output,
                json_renderer.render_error(
                    error_type=type(error).__name__,
                    message=str(error),
                    hint=_hint_for(error),
                    logs=cap.lines,
                ),
            )
            sys.exit(1)
        _emit(output, json_renderer.render(report, logs=cap.lines))
    elif fmt == "text":
        report = build_report()
        _emit(output, text_renderer.render(report, verbose=verbose))
    elif fmt == "html":
        report = build_report()
        out = output or html_renderer.default_output_path(report)
        html_renderer.render_to_file(report, out)
        click.echo(f"Wrote {out}")
    else:  # pragma: no cover
        raise click.UsageError(f"unknown format: {fmt}")


def _emit(output: Path | None, content: str) -> None:
    if output is None:
        click.echo(content, nl=False)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    click.echo(f"Wrote {output}", err=True)


def _hint_for(exc: Exception) -> str | None:
    name = type(exc).__name__
    if name == "CacheMiss":
        return (
            "No cache entry. Re-run with --online (default), or populate the cache by "
            "running once with network access."
        )
    if name == "RuntimeError" and "token" in str(exc).lower():
        return "Run `gh auth login` or set GITHUB_TOKEN."
    return None


@click.group()
def cli() -> None:
    """Flow metrics and Monte Carlo forecasting from GitHub PR data.

    For agent use, pass `--format json` to any command. JSON output
    includes schema URI, raw data, chart data, interpretation, and
    captured stderr — nothing is silently dropped.
    """


@cli.command(short_help="Flow efficiency (active vs. wait time)")
@_apply_source_options
@click.option(
    "--start",
    type=str,
    default=None,
    help="Window start (YYYY-MM-DD). Default: Monday of current week.",
)
@click.option(
    "--stop",
    type=str,
    default=None,
    help="Window stop (YYYY-MM-DD). Default: Sunday of current week.",
)
@click.option(
    "--cache-dir", type=click.Path(path_type=Path), default=DEFAULT_CACHE_DIR, show_default=True
)
@click.option(
    "--offline/--online",
    default=False,
    help="Offline reads cache only; online hits the source API on cache miss.",
)
@click.option(
    "--gap-hours", type=float, default=DEFAULT_GAP.total_seconds() / 3600, show_default=True
)
@click.option(
    "--min-cluster-minutes",
    type=float,
    default=DEFAULT_MIN_CLUSTER.total_seconds() / 60,
    show_default=True,
)
@click.option(
    "--active-statuses",
    type=str,
    default=",".join(sorted(DEFAULT_ACTIVE_STATUSES)),
    show_default=True,
    help="Jira only: comma-separated workflow statuses counted as active. "
    "Ignored for GitHub (which infers activity from event timestamps).",
)
@_FORMAT_OPTION
@_OUTPUT_OPTION
@_VERBOSE_OPTION
def efficiency(
    repo: str | None,
    jira_url: str | None,
    jira_project: str | None,
    start: str | None,
    stop: str | None,
    cache_dir: Path,
    offline: bool,
    gap_hours: float,
    min_cluster_minutes: float,
    active_statuses: str,
    fmt: str,
    output: Path | None,
    verbose: bool,
) -> None:
    """Flow efficiency for a date window (defaults to this week).

    For agent use, pass --format json (schema: flowmetrics.efficiency.v1).
    """
    if start is None and stop is None:
        start_d, stop_d = this_week_window()
    elif start is not None and stop is not None:
        start_d, stop_d = _parse_date(start), _parse_date(stop)
    else:
        raise click.UsageError("Provide both --start and --stop, or neither.")

    src = _build_source(
        repo=repo,
        jira_url=jira_url, jira_project=jira_project,
        cache_dir=cache_dir, offline=offline,
    )

    active_set = frozenset(
        s.strip() for s in active_statuses.split(",") if s.strip()
    )

    def build() -> EfficiencyReport:
        result: WindowResult = flowmetrics_for_window(
            src, start_d, stop_d,
            gap=timedelta(hours=gap_hours),
            min_cluster=timedelta(minutes=min_cluster_minutes),
            active_statuses=active_set,
        )
        input_ = EfficiencyInput(
            repo=src.label,
            start=start_d,
            stop=stop_d,
            active_statuses=tuple(sorted(active_set)),
            gap_hours=gap_hours,
            min_cluster_minutes=min_cluster_minutes,
            offline=offline,
            jira_url=jira_url,
        )
        return EfficiencyReport(
            input=input_,
            result=result,
            interpretation=interpret_efficiency(input_, result),
        )

    _dispatch(fmt, output, build, verbose=verbose)


@cli.command(short_help="Cumulative Flow Diagram per Vacanti")
@_apply_source_options
@click.option("--start", type=str, required=True, help="Window start (YYYY-MM-DD).")
@click.option("--stop", type=str, required=True, help="Window stop (YYYY-MM-DD).")
@click.option(
    "--workflow",
    type=str,
    required=True,
    help=(
        "Comma-separated workflow states, earliest → latest. "
        "Example: 'Open,In Progress,Done'."
    ),
)
@click.option(
    "--interval-days",
    type=int,
    default=1,
    show_default=True,
    help="Sample interval in days.",
)
@click.option(
    "--cache-dir", type=click.Path(path_type=Path), default=DEFAULT_CACHE_DIR, show_default=True
)
@click.option("--offline/--online", default=False)
@_FORMAT_OPTION
@_OUTPUT_OPTION
@_VERBOSE_OPTION
def cfd(
    repo: str | None,
    jira_url: str | None,
    jira_project: str | None,
    start: str,
    stop: str,
    workflow: str,
    interval_days: int,
    cache_dir: Path,
    offline: bool,
    fmt: str,
    output: Path | None,
    verbose: bool,
) -> None:
    """Cumulative Flow Diagram per Vacanti — stacked workflow states over time.

    The CFD uses items completed inside the window and the timestamped
    status_intervals each item carries. Sources that don't expose
    workflow history (GitHub PRs) produce a degenerate two-line CFD
    (arrivals / departures only). Jira issues with full changelog yield
    the full multi-band view.
    """
    start_d = _parse_date(start)
    stop_d = _parse_date(stop)
    workflow_tuple = tuple(s.strip() for s in workflow.split(",") if s.strip())
    if not workflow_tuple:
        raise click.UsageError("--workflow needs at least one state")

    src = _build_source(
        repo=repo,
        jira_url=jira_url,
        jira_project=jira_project,
        cache_dir=cache_dir,
        offline=offline,
    )

    def build() -> CfdReport:
        # Active-in-window = items merged in the window UNION items
        # still open at window end. Restricting to
        # fetch_completed_in_window dropped pre-window WIP and
        # currently-open items, which made the CFD show a suspect
        # perfect balance (every arrival also a departure).
        from .service import fetch_items_active_in_window
        items = fetch_items_active_in_window(src, start_d, stop_d)
        points = build_cfd(
            items,
            workflow=workflow_tuple,
            start=start_d,
            stop=stop_d,
            interval=timedelta(days=interval_days),
        )
        input_ = CfdInput(
            repo=src.label,
            start=start_d,
            stop=stop_d,
            workflow=workflow_tuple,
            interval_days=interval_days,
            offline=offline,
            jira_url=jira_url,
        )
        return CfdReport(
            input=input_,
            points=points,
            interpretation=interpret_cfd(input_, points),
        )

    _dispatch(fmt, output, build, verbose=verbose)


@cli.command(short_help="Cycle Time Scatterplot per Vacanti")
@_apply_source_options
@click.option(
    "--start",
    type=str,
    default=None,
    help=(
        "Window start (YYYY-MM-DD). Default: 29 days before --stop, "
        "giving the recommended 30-day window."
    ),
)
@click.option(
    "--stop",
    type=str,
    default=None,
    help="Window stop (YYYY-MM-DD). Default: yesterday-UTC.",
)
@click.option(
    "--cache-dir", type=click.Path(path_type=Path), default=DEFAULT_CACHE_DIR, show_default=True
)
@click.option("--offline/--online", default=False)
@click.option(
    "--include-issues/--no-include-issues",
    default=False,
    help=(
        "GitHub-only. When set, also fetch closed Issues and plot them "
        "alongside PRs. For Issues closed by a merged PR, the cycle "
        "time uses the PR's mergedAt (the stitched 'done' instant), "
        "not the Issue's own closedAt. Surfaces longer-tailed work "
        "items that PRs alone miss."
    ),
)
@_FORMAT_OPTION
@_OUTPUT_OPTION
@_VERBOSE_OPTION
def scatterplot(
    repo: str | None,
    jira_url: str | None,
    jira_project: str | None,
    start: str | None,
    stop: str | None,
    cache_dir: Path,
    offline: bool,
    include_issues: bool,
    fmt: str,
    output: Path | None,
    verbose: bool,
) -> None:
    """Cycle Time Scatterplot — empirical cycle-time distribution.

    For each item completed in the window, plot a dot at (completion
    date, cycle time in days). Horizontal percentile lines (P50, P70,
    P85, P95) mark probability-of-completion thresholds. P85 is the
    conventional external-commitment line: a new item has an 85%
    chance of finishing in P85 days or less.

    For a sprint/standup-friendly view, set --start / --stop to a
    7-day window. The default is the 30-day window ending yesterday-
    UTC.
    """
    from .service import default_history_end, default_history_start
    stop_d = _parse_date(stop) if stop else default_history_end()
    start_d = _parse_date(start) if start else default_history_start(stop_d)
    if start_d > stop_d:
        raise click.UsageError(
            f"--start ({start_d}) must be on or before --stop ({stop_d})"
        )

    src = _build_source(
        repo=repo,
        jira_url=jira_url,
        jira_project=jira_project,
        cache_dir=cache_dir,
        offline=offline,
    )

    if include_issues and not repo:
        raise click.UsageError(
            "--include-issues is GitHub-only (Issues are a GitHub concept)."
        )

    def build() -> ScatterplotReport:
        from .percentiles import chart_percentiles
        items = src.fetch_for_percentile_training(start_d, stop_d)
        points: list[ScatterplotPoint] = []
        cycle_days: list[float] = []
        for it in items:
            if it.completed_at is None:
                continue
            cycle = (it.completed_at - it.created_at).total_seconds() / 86400
            # `it.url` is set by the source — GitHub PR URL or Jira
            # browse URL. Renderer consumes it directly; no
            # pattern-matching of item_id here any more.
            points.append(ScatterplotPoint(
                item_id=it.item_id,
                title=it.title,
                completed_at=it.completed_at.date(),
                cycle_time_days=cycle,
                url=it.url,
            ))
            cycle_days.append(cycle)

        if include_issues:
            # Layer in Issues closed in window. Cycle time uses the
            # stitched PR-merge timestamp when an Issue was closed by
            # a PR (the causal "done" instant), per github_issues parser.
            from .cache import FileCache
            from .sources.github import GitHubClient, resolve_token
            from .sources.github_issues import fetch_issues_closed_in_window
            client = GitHubClient(
                cache=FileCache(cache_dir),
                read_only=offline,
                token=resolve_token() if not offline else None,
            )
            try:
                entries = fetch_issues_closed_in_window(repo, start_d, stop_d, client=client)
            finally:
                client.close()
            for stream_item, _txs in entries:
                if stream_item.completed_at is None:
                    continue
                cycle = (
                    (stream_item.completed_at - stream_item.created_at).total_seconds()
                    / 86400
                )
                points.append(ScatterplotPoint(
                    item_id=stream_item.item_id,
                    title=stream_item.title,
                    completed_at=stream_item.completed_at.date(),
                    cycle_time_days=cycle,
                    url=stream_item.url,
                ))
                cycle_days.append(cycle)

        percentiles = chart_percentiles(cycle_days) if cycle_days else {
            50: 0.0, 70: 0.0, 85: 0.0, 95: 0.0
        }
        input_ = ScatterplotInput(
            repo=src.label,
            start=start_d,
            stop=stop_d,
            offline=offline,
            jira_url=jira_url,
        )
        return ScatterplotReport(
            input=input_,
            points=points,
            cycle_time_percentiles=percentiles,
            interpretation=interpret_scatterplot(input_, points, percentiles),
        )

    _dispatch(fmt, output, build, verbose=verbose)




@cli.command(short_help="Aging Work In Progress (Vacanti WWIBD)")
@_apply_source_options
@click.option(
    "--asof",
    type=str,
    default=None,
    help="As-of date (YYYY-MM-DD). Defaults to today (UTC).",
)
@click.option(
    "--workflow",
    type=str,
    default=None,
    help=(
        "Comma-separated workflow states, earliest → latest. "
        "Example: 'Open,In Progress,Patch Available,Resolved'. "
        "Required unless --wip-labels is supplied."
    ),
)
@click.option(
    "--wip-labels",
    "wip_labels_raw",
    type=str,
    default=None,
    help=(
        "GitHub-only: comma-separated PR labels that count as WIP, "
        "ordered with most progress on the right. When set, PR aging is "
        "driven by the timestamps of LabeledEvent/UnlabeledEvent on PRs "
        "instead of the default isDraft/reviewDecision review cycle. "
        "Example: 'shaping,in-progress,in-review'."
    ),
)
@click.option(
    "--max-age-days",
    type=int,
    default=None,
    help=(
        "Opt-in: exclude in-flight items older than this many days from "
        "the chart and from past-P85/P95 counts. Default (unset) shows "
        "every in-flight item per Vacanti — the right choice when you "
        "want full visibility on stalled work. Use this when long-tail "
        "stalled items dwarf actionable WIP. Example: --max-age-days=180."
    ),
)
@click.option(
    "--history-start",
    type=str,
    default=None,
    help=(
        "First day of the completed-items window used for cycle-time "
        f"percentile lines. Defaults to {DEFAULT_TRAINING_DAYS - 1} days "
        "before --history-end."
    ),
)
@click.option(
    "--history-end",
    type=str,
    default=None,
    help="Last day of the completed-items percentile window. Defaults to yesterday-UTC.",
)
@click.option(
    "--cache-dir", type=click.Path(path_type=Path), default=DEFAULT_CACHE_DIR, show_default=True
)
@click.option("--offline/--online", default=False)
@_FORMAT_OPTION
@_OUTPUT_OPTION
@_VERBOSE_OPTION
def aging(
    repo: str | None,
    jira_url: str | None,
    jira_project: str | None,
    asof: str | None,
    workflow: str | None,
    wip_labels_raw: str | None,
    max_age_days: int | None,
    history_start: str | None,
    history_end: str | None,
    cache_dir: Path,
    offline: bool,
    fmt: str,
    output: Path | None,
    verbose: bool,
) -> None:
    """Aging Work In Progress chart per Vacanti.

    Each in-flight item is plotted in the column of its current workflow
    state at a height equal to its age in days. Percentile lines come
    from the cycle times of recently completed items (the "Scatterplot"
    distribution) and serve as risk checkpoints.

    GitHub has two modes. Default (review-cycle): pass --workflow with
    the four phases Draft → Awaiting Review → Changes Requested →
    Approved; state comes from isDraft + reviewDecision. Label-driven:
    pass --wip-labels with your PR labels in order, most progress on
    the right; state is materialized from timeline LabeledEvent /
    UnlabeledEvent timestamps. See docs/SPEC-github-labels.md.

    Jira always uses --workflow against changelog statuses.
    """
    asof_d = _parse_date(asof) if asof else date.today()

    try:
        wip = WipLabels.parse(wip_labels_raw) if wip_labels_raw else None
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc

    # In label mode the workflow tuple comes from --wip-labels; users
    # don't need to repeat themselves. --workflow is still honored if
    # explicitly passed (e.g. to add a "Triage" column heading), but
    # defaults to the WIP labels list.
    if workflow:
        workflow_tuple = tuple(s.strip() for s in workflow.split(",") if s.strip())
    elif wip is not None:
        workflow_tuple = wip.ordered
    else:
        raise click.UsageError(
            "Pass --workflow (review-cycle / Jira) or --wip-labels (GitHub label mode)."
        )
    if not workflow_tuple:
        raise click.UsageError("--workflow needs at least one state")

    src = _build_source(
        repo=repo,
        jira_url=jira_url,
        jira_project=jira_project,
        cache_dir=cache_dir,
        offline=offline,
        wip_labels=wip,
    )

    def build() -> AgingReport:
        in_flight = src.fetch_in_flight(asof_d)

        # Percentile window: completed items, defaulting to Vacanti's 30 days.
        hist_end = _parse_date(history_end) if history_end else (asof_d - timedelta(days=1))
        hist_start = (
            _parse_date(history_start) if history_start
            else hist_end - timedelta(days=DEFAULT_TRAINING_DAYS - 1)
        )
        # Lightweight fetch — Aging only needs cycle_time, not activity
        # events. See Source.fetch_for_percentile_training docstring.
        completed_items = src.fetch_for_percentile_training(hist_start, hist_end)
        completed_flows = [
            compute_pr_flow(item, gap=DEFAULT_GAP, min_cluster=DEFAULT_MIN_CLUSTER)
            for item in completed_items
            if item.completed_at is not None
        ]
        pct = cycle_time_percentiles(completed_flows)

        # Per-item drill-down URLs come from `WorkItem.url` set by
        # the source at fetch time — no per-source URL stitching here.
        aging_items = compute_aging(
            in_flight,
            asof=asof_d,
            max_age_days=max_age_days,
        )
        excluded = len(in_flight) - len(aging_items)
        in_flight_total = len(in_flight)
        input_ = AgingInput(
            repo=src.label,
            asof=asof_d,
            workflow=workflow_tuple,
            history_start=hist_start,
            history_end=hist_end,
            offline=offline,
            from_wip_labels=wip is not None,
            max_age_days=max_age_days,
            jira_url=jira_url,
        )
        return AgingReport(
            input=input_,
            items=aging_items,
            cycle_time_percentiles=pct,
            completed_count=len(completed_flows),
            interpretation=interpret_aging(
                input_,
                aging_items,
                pct,
                len(completed_flows),
                excluded_above_max_age=excluded,
            ),
            in_flight_total=in_flight_total,
            excluded_above_max_age=excluded,
        )

    _dispatch(fmt, output, build, verbose=verbose)


@cli.group(short_help="Monte Carlo forecasting (when-done / how-many)")
def forecast() -> None:
    """Monte Carlo forecasting (Vacanti's "When Will It Be Done?")."""


_HISTORY_OPTIONS = [
    click.option(
        "--history-start",
        type=str,
        default=None,
        help=(
            "First day of the training window (YYYY-MM-DD, UTC). "
            f"Defaults to {DEFAULT_TRAINING_DAYS - 1} days before --history-end, "
            f"giving Vacanti's recommended {DEFAULT_TRAINING_DAYS}-day window."
        ),
    ),
    click.option(
        "--history-end",
        type=str,
        default=None,
        help=(
            "Last day of the training window (YYYY-MM-DD, UTC). "
            "Defaults to yesterday-UTC, since today's merges are still "
            "incomplete and would bias the simulator low."
        ),
    ),
    click.option(
        "--cache-dir", type=click.Path(path_type=Path), default=DEFAULT_CACHE_DIR, show_default=True
    ),
    click.option("--offline/--online", default=False),
    click.option(
        "--runs",
        type=int,
        default=10_000,
        show_default=True,
        help="Monte Carlo iterations. Vacanti: ~1k gives the shape, ~10k stabilises.",
    ),
    click.option("--seed", type=int, default=None, help="RNG seed for reproducible output."),
]


def _apply_history_options(f: Callable[..., Any]) -> Callable[..., Any]:
    for decorator in reversed(_HISTORY_OPTIONS):
        f = decorator(f)
    return f


def _resolve_history(
    src: Source,
    history_start: str | None,
    history_end: str | None,
) -> tuple[list[int], date, date]:
    start_date = _parse_date(history_start) if history_start else None
    end_date = _parse_date(history_end) if history_end else None
    return historical_throughput_samples(
        src, start_date=start_date, end_date=end_date
    )


@forecast.command("when-done")
@_apply_source_options
@click.option(
    "--items",
    "items",
    type=int,
    required=True,
    help=(
        "Number of items to complete. We use 'items' rather than 'backlog' "
        "because Vacanti flags 'backlog' as overloaded (Scrum uses it for "
        "the prioritized list)."
    ),
)
@click.option("--start-date", type=str, default=None)
@_apply_history_options
@_FORMAT_OPTION
@_OUTPUT_OPTION
@_VERBOSE_OPTION
def forecast_when_done(

    repo: str | None,
    jira_url: str | None,
    jira_project: str | None,
    items: int,
    start_date: str | None,
    history_start: str | None,
    history_end: str | None,
    cache_dir: Path,
    offline: bool,
    runs: int,
    seed: int | None,
    fmt: str,
    output: Path | None,
    verbose: bool,
) -> None:
    """When will N items be done?

    Date-axis forecast with forward percentiles. For agent use, pass
    --format json (schema: flowmetrics.forecast.when_done.v1).
    """

    src = _build_source(
        repo=repo,
        jira_url=jira_url, jira_project=jira_project,
        cache_dir=cache_dir, offline=offline,
    )

    def build() -> WhenDoneReport:
        samples, train_start, train_end = _resolve_history(src, history_start, history_end)
        if sum(samples) == 0:
            raise RuntimeError(
                f"No completed items in training window {train_start}→{train_end}; cannot forecast."
            )
        start = _parse_date(start_date) if start_date else date.today()
        rng = Random(seed) if seed is not None else Random()
        results = monte_carlo_when_done(samples, items, start, runs=runs, rng=rng)
        hist: ResultsHistogram[date] = build_histogram(results)
        percentiles = {p: forward_percentile(hist, p) for p in (50, 70, 85, 95)}

        input_ = WhenDoneInput(
            repo=src.label,
            items=items,
            start_date=start,
            history_start=train_start,
            history_end=train_end,
            offline=offline,
            jira_url=jira_url,
        )
        training = build_training_summary(samples, train_start, train_end)
        return WhenDoneReport(
            input=input_,
            training=training,
            simulation=SimulationSummary(runs=runs, seed=seed),
            histogram=hist,
            percentiles=percentiles,
            interpretation=interpret_when_done(input_, training, hist, percentiles),
        )

    _dispatch(fmt, output, build, verbose=verbose)


@forecast.command("how-many")
@_apply_source_options
@click.option("--target-date", type=str, required=True)
@click.option("--start-date", type=str, default=None)
@_apply_history_options
@_FORMAT_OPTION
@_OUTPUT_OPTION
@_VERBOSE_OPTION
def forecast_how_many(

    repo: str | None,
    jira_url: str | None,
    jira_project: str | None,
    target_date: str,
    start_date: str | None,
    history_start: str | None,
    history_end: str | None,
    cache_dir: Path,
    offline: bool,
    runs: int,
    seed: int | None,
    fmt: str,
    output: Path | None,
    verbose: bool,
) -> None:
    """How many items by a given date?

    Items-axis forecast with backward percentiles. For agent use, pass
    --format json (schema: flowmetrics.forecast.how_many.v1).
    """

    src = _build_source(
        repo=repo,
        jira_url=jira_url, jira_project=jira_project,
        cache_dir=cache_dir, offline=offline,
    )

    def build() -> HowManyReport:
        samples, train_start, train_end = _resolve_history(src, history_start, history_end)
        if sum(samples) == 0:
            raise RuntimeError(
                f"No completed items in training window {train_start}→{train_end}; cannot forecast."
            )
        start = _parse_date(start_date) if start_date else date.today()
        end = _parse_date(target_date)
        rng = Random(seed) if seed is not None else Random()
        results = monte_carlo_how_many(samples, start_date=start, end_date=end, runs=runs, rng=rng)
        hist: ResultsHistogram[int] = build_histogram(results)
        percentiles = {p: backward_percentile(hist, p) for p in (50, 70, 85, 95)}

        input_ = HowManyInput(
            repo=src.label,
            start_date=start,
            target_date=end,
            history_start=train_start,
            history_end=train_end,
            offline=offline,
            jira_url=jira_url,
        )
        training = build_training_summary(samples, train_start, train_end)
        return HowManyReport(
            input=input_,
            training=training,
            simulation=SimulationSummary(runs=runs, seed=seed),
            histogram=hist,
            percentiles=percentiles,
            interpretation=interpret_how_many(input_, training, hist, percentiles),
        )

    _dispatch(fmt, output, build, verbose=verbose)
