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

import json
import shutil
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from random import Random
from typing import Any

import click

from .forecast import (
    ResultsHistogram,
    backward_percentile,
    build_histogram,
    forward_percentile,
    monte_carlo_how_many,
    monte_carlo_when_done,
)
from .interpretation import (
    interpret_how_many,
    interpret_when_done,
)
from .logcapture import LogCapture
from .renderers import json_renderer, text_renderer
from .report import (
    HowManyInput,
    HowManyReport,
    SimulationSummary,
    WhenDoneInput,
    WhenDoneReport,
    build_training_summary,
)
from .service import (
    DEFAULT_CACHE_DIR,
    DEFAULT_GAP,
    DEFAULT_MIN_CLUSTER,
    DEFAULT_TRAINING_DAYS,
    historical_throughput_samples,
    make_github_source,
    make_jira_source,
)
from .sources import Source
from .sources.github_labels import WipLabels


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


_FORMAT_OPTION = click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format. text=humans (default), json=agents.",
)
_OUTPUT_OPTION = click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="File to write to. Defaults to stdout.",
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


_WORKFLOW_OPTIONS = [
    click.option(
        "--workflow-name", "workflow_name", type=str, default=None,
        help=(
            "Look up the workflow in `<workflows-dir>/workflows.db` "
            "(DB-first, YAML fallback in the same dir). The workflow's "
            "source + stages drive the query."
        ),
    ),
    click.option(
        "--workflow-yaml", "workflow_yaml", type=click.Path(path_type=Path),
        default=None,
        help=(
            "Path to a workflow YAML file. Use this for ad-hoc "
            "queries against a workflow that isn't in the store."
        ),
    ),
    click.option(
        "--workflows-dir", "workflows_dir",
        type=click.Path(path_type=Path),
        default=Path("./contracts"), show_default=True,
        help="Where --workflow-name looks up the store (DB + un-migrated YAMLs).",
    ),
]


def _apply_workflow_options(f):
    """Decorator: add --workflow-name / --workflow-yaml / --workflows-dir
    to a subcommand. Used by every `flow metric ...` and
    `flow forecast ...` — they all share the same 'point at a
    workflow definition' shape."""
    for decorator in reversed(_WORKFLOW_OPTIONS):
        f = decorator(f)
    return f


def _resolve_workflow(
    *,
    workflow_name: str | None,
    workflow_yaml: Path | None,
    workflows_dir: Path,
):
    """Resolve the workflow definition via either
    `--workflow-name NAME` (store) or `--workflow-yaml PATH` (file).
    Exactly one MUST be set. Returns a `Workflow` Pydantic model.
    """
    if workflow_name and workflow_yaml:
        raise click.UsageError(
            "--workflow-name and --workflow-yaml are mutually exclusive."
        )
    if not workflow_name and not workflow_yaml:
        raise click.UsageError(
            "Pass --workflow-name NAME (stored workflow) or "
            "--workflow-yaml PATH (YAML file). Run `flow workflows "
            "list` to see what's in the store."
        )

    if workflow_name:
        from .workflows_db import WorkflowStore
        wf = WorkflowStore(workflows_dir).get(workflow_name)
        if wf is None:
            raise click.UsageError(
                f"workflow {workflow_name!r} not found under "
                f"{workflows_dir} (no DB row and no matching YAML). "
                "Run `flow workflows list` to see what's configured."
            )
        return wf

    from .workflow import WorkflowError, parse_workflow_text
    path = Path(workflow_yaml)
    if not path.exists():
        raise click.UsageError(f"YAML file {path} does not exist.")
    try:
        return parse_workflow_text(path.read_text(encoding="utf-8"), path.stem)
    except WorkflowError as exc:
        raise click.UsageError(f"failed to parse {path}: {exc}") from exc


def _build_source_from_workflow(
    wf, *, cache_dir: Path, offline: bool,
    include_issues: bool = False, wip_labels=None,
):
    """Build a Source from a `Workflow` model. Centralised so each
    subcommand body stays small."""
    return _build_source(
        repo=wf.repo if wf.source == "github" else None,
        jira_url=wf.jira_url if wf.source == "jira" else None,
        jira_project=wf.jira_project if wf.source == "jira" else None,
        cache_dir=cache_dir, offline=offline,
        include_issues=include_issues,
        wip_labels=wip_labels,
    )


def _stages_from_workflow(wf) -> tuple[str, ...]:
    """The ordered stage tuple — WIP-marked steps if any exist,
    else every step in order. Used by aging + cumulative as the
    band / column order."""
    wip = tuple(s.name for s in wf.steps if s.wip)
    return wip or tuple(s.name for s in wf.steps)


def _build_source(
    *,
    repo: str | None,
    jira_url: str | None,
    jira_project: str | None,
    cache_dir: Path,
    offline: bool,
    wip_labels: WipLabels | None = None,
    include_issues: bool = False,
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
    if include_issues and not have_github:
        raise click.UsageError(
            "--include-issues is GitHub-only (Issues are a GitHub concept)."
        )
    if have_github:
        return make_github_source(
            repo,
            cache_dir=cache_dir,
            read_only=offline,
            wip_labels=wip_labels,
            include_issues=include_issues,
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
@click.version_option(
    # Resolve via importlib.metadata so the value matches what `uv
    # tool list` and `pip show` report — the canonical source of
    # truth for an installed package. hatch-vcs writes this value
    # into the dist's METADATA at build time.
    version=None,
    package_name="flowmetrics",
    message="%(prog)s %(version)s",
)
def cli() -> None:
    """Flow metrics and Monte Carlo forecasting from GitHub (PRs and
    Issues) and Atlassian Jira issue data.

    For agent use, pass `--format json` to any command. JSON output
    includes schema URI, raw data, chart data, interpretation, and
    captured stderr — nothing is silently dropped.
    """


@cli.group(short_help="Monte Carlo forecasting (date / throughput)")
def forecast() -> None:
    """Monte Carlo forecasting.

    `flow forecast date NAME --items N` — when will N items be done?
    `flow forecast throughput NAME --target-date YYYY-MM-DD` — how many
    items by a given date?

    Both subcommands take the workflow's source from
    `--workflow-name` (store lookup) or `--workflow-yaml` (direct
    file path) — same shape as `flow metric ...`.
    """


_HISTORY_OPTIONS = [
    click.option(
        "--history-start",
        type=str,
        default=None,
        help=(
            "First day of the training window (YYYY-MM-DD, UTC). "
            f"Defaults to {DEFAULT_TRAINING_DAYS - 1} days before --history-end, "
            f"giving the standard {DEFAULT_TRAINING_DAYS}-day rolling window."
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
        help="Monte Carlo iterations. ~1k gives the shape, ~10k stabilises.",
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


@forecast.command("date", short_help="When will N items be done? (date forecast)")
@_apply_workflow_options
@click.option(
    "--items", "items", type=int, required=True,
    help=(
        "Number of items to complete. We use 'items' rather than "
        "'backlog' because the latter is Scrum-loaded."
    ),
)
@click.option(
    "--start-date", type=str, default=None,
    help="Forecast horizon start (YYYY-MM-DD). Defaults to today.",
)
@_apply_history_options
@_FORMAT_OPTION
@_OUTPUT_OPTION
@_VERBOSE_OPTION
def forecast_date(
    workflow_name: str | None,
    workflow_yaml: Path | None,
    workflows_dir: Path,
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

    Date-axis forecast with forward percentiles. The workflow
    (`--workflow-name` or `--workflow-yaml`) supplies the source.

    For agent use, pass --format json (schema:
    flowmetrics.forecast.when_done.v1).
    """
    wf = _resolve_workflow(
        workflow_name=workflow_name, workflow_yaml=workflow_yaml,
        workflows_dir=workflows_dir,
    )
    src = _build_source_from_workflow(
        wf, cache_dir=cache_dir, offline=offline,
    )

    def build() -> WhenDoneReport:
        samples, train_start, train_end = _resolve_history(
            src, history_start, history_end,
        )
        if sum(samples) == 0:
            raise RuntimeError(
                f"No completed items in training window "
                f"{train_start}→{train_end}; cannot forecast."
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
            jira_url=wf.jira_url,
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


@forecast.command("throughput", short_help="How many items by a target date? (throughput forecast)")
@_apply_workflow_options
@click.option(
    "--target-date", type=str, required=True,
    help="Target date the forecast aims at (YYYY-MM-DD).",
)
@click.option(
    "--start-date", type=str, default=None,
    help="Forecast horizon start (YYYY-MM-DD). Defaults to today.",
)
@_apply_history_options
@_FORMAT_OPTION
@_OUTPUT_OPTION
@_VERBOSE_OPTION
def forecast_throughput(
    workflow_name: str | None,
    workflow_yaml: Path | None,
    workflows_dir: Path,
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

    Items-axis forecast with backward percentiles. The workflow
    (`--workflow-name` or `--workflow-yaml`) supplies the source.

    For agent use, pass --format json (schema:
    flowmetrics.forecast.how_many.v1).
    """
    wf = _resolve_workflow(
        workflow_name=workflow_name, workflow_yaml=workflow_yaml,
        workflows_dir=workflows_dir,
    )
    src = _build_source_from_workflow(
        wf, cache_dir=cache_dir, offline=offline,
    )

    def build() -> HowManyReport:
        samples, train_start, train_end = _resolve_history(
            src, history_start, history_end,
        )
        if sum(samples) == 0:
            raise RuntimeError(
                f"No completed items in training window "
                f"{train_start}→{train_end}; cannot forecast."
            )
        start = _parse_date(start_date) if start_date else date.today()
        end = _parse_date(target_date)
        rng = Random(seed) if seed is not None else Random()
        results = monte_carlo_how_many(
            samples, start_date=start, end_date=end, runs=runs, rng=rng,
        )
        hist: ResultsHistogram[int] = build_histogram(results)
        percentiles = {p: backward_percentile(hist, p) for p in (50, 70, 85, 95)}

        input_ = HowManyInput(
            repo=src.label,
            start_date=start,
            target_date=end,
            history_start=train_start,
            history_end=train_end,
            offline=offline,
            jira_url=wf.jira_url,
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


# ---------------------------------------------------------------------------
# `flow metric ...` — text + JSON metric extraction for agents.
#
# These commands expose the underlying lib metric computations (aging,
# CFD, throughput, cycle-time) in graphics-free shape. They were
# top-level commands (`flow aging` / `flow cfd` / `flow scatterplot`)
# until the CLI shrink — turns out agents still need the numbers; the
# right home is a metric group, not chart-primary top-level commands.
# ---------------------------------------------------------------------------


@cli.group(short_help="Extract metrics for agents / headless humans")
def metric() -> None:
    """Pull numeric metric data without rendering anything.

    Each subcommand takes a source (`--repo` OR `--jira-url` +
    `--jira-project`) and writes either a one-line text headline
    (default) or a versioned JSON envelope (`--format json`). No
    HTML, no charts — the web UI (`flow serve`) is the home for
    every chart.
    """


_METRIC_FORMAT_OPTION = click.option(
    "--format",
    "fmt",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="text=one-line headline (default); json=structured envelope.",
)


def _emit_metric(fmt: str, headline: str, payload: dict) -> None:
    """Emit a metric subcommand's result in the chosen format. Text
    mode prints the one-line headline; JSON mode emits the structured
    envelope (with a trailing newline so pipeable to `jq`)."""
    if fmt == "json":
        click.echo(json.dumps(payload, indent=2))
        return
    click.echo(headline)


@metric.command("throughput", short_help="Daily completion counts in a window")
@_apply_workflow_options
@click.option("--start", type=str, required=True, help="Window start (YYYY-MM-DD).")
@click.option("--stop", type=str, required=True, help="Window stop (YYYY-MM-DD).")
@click.option(
    "--cache-dir", type=click.Path(path_type=Path),
    default=DEFAULT_CACHE_DIR, show_default=True,
)
@click.option("--offline/--online", default=False)
@_METRIC_FORMAT_OPTION
def metric_throughput(
    workflow_name, workflow_yaml, workflows_dir,
    start, stop, cache_dir, offline, fmt,
) -> None:
    """Daily completion counts — items completed each day in the
    window. The workflow (`--workflow-name` or `--workflow-yaml`) supplies the source.

    Schema: `flowmetrics.metric.throughput.v1`."""

    from .throughput import daily_counts

    wf = _resolve_workflow(
        workflow_name=workflow_name, workflow_yaml=workflow_yaml,
        workflows_dir=workflows_dir,
    )
    src = _build_source_from_workflow(
        wf, cache_dir=cache_dir, offline=offline,
    )

    start_d = _parse_date(start)
    stop_d = _parse_date(stop)
    items = list(src.fetch_completed_in_window(start_d, stop_d))
    completion_dates: list[date] = [
        it.completed_at.date() for it in items if it.completed_at is not None
    ]
    samples = daily_counts(completion_dates, start_d, stop_d)
    total = sum(samples)
    days = (stop_d - start_d).days + 1
    avg = total / days if days else 0.0

    headline = (
        f"{wf.name} ({src.label}) {start_d} → {stop_d}: "
        f"{total} items completed across {days} days "
        f"({avg:.2f}/day)."
    )
    payload = {
        "schema": "flowmetrics.metric.throughput.v1",
        "input": {
            "workflow": wf.name,
            "source": wf.source,
            "repo": wf.repo,
            "jira_url": wf.jira_url,
            "jira_project": wf.jira_project,
            "start": start_d.isoformat(),
            "stop": stop_d.isoformat(),
            "offline": offline,
        },
        "summary": {
            "total_items": total,
            "days": days,
            "avg_per_day": round(avg, 4),
        },
        "daily_samples": samples,
        "headline": headline,
    }
    _emit_metric(fmt, headline, payload)


@metric.command("cumulative", short_help="Cumulative Flow Diagram — state counts over time")
@_apply_workflow_options
@click.option("--start", type=str, required=True, help="Window start (YYYY-MM-DD).")
@click.option("--stop", type=str, required=True, help="Window stop (YYYY-MM-DD).")
@click.option(
    "--interval-days", type=int, default=1, show_default=True,
    help="Sample interval in days.",
)
@click.option(
    "--cache-dir", type=click.Path(path_type=Path),
    default=DEFAULT_CACHE_DIR, show_default=True,
)
@click.option("--offline/--online", default=False)
@_METRIC_FORMAT_OPTION
def metric_cumulative(
    workflow_name, workflow_yaml, workflows_dir,
    start, stop, interval_days,
    cache_dir, offline, fmt,
) -> None:
    """Cumulative Flow Diagram data — cumulative state counts at
    each sample. The workflow (`--workflow-name` or `--workflow-yaml`) supplies both
    the source AND the stage order.

    Schema: `flowmetrics.metric.cumulative.v1`."""

    from .cfd import build_cfd
    from .service import fetch_items_active_in_window

    wf = _resolve_workflow(
        workflow_name=workflow_name, workflow_yaml=workflow_yaml,
        workflows_dir=workflows_dir,
    )
    src = _build_source_from_workflow(
        wf, cache_dir=cache_dir, offline=offline,
    )
    workflow_tuple = _stages_from_workflow(wf)
    if not workflow_tuple:
        raise click.UsageError(
            f"workflow {wf.name!r} has no steps — add stages via the "
            "wizard or in the YAML."
        )

    start_d = _parse_date(start)
    stop_d = _parse_date(stop)
    items = fetch_items_active_in_window(src, start_d, stop_d)
    points = build_cfd(
        items, workflow=workflow_tuple,
        start=start_d, stop=stop_d,
        interval=timedelta(days=interval_days),
    )

    end = points[-1] if points else None
    arrivals = end.counts_by_state.get(workflow_tuple[0], 0) if end else 0
    departures = end.counts_by_state.get(workflow_tuple[-1], 0) if end else 0
    end_wip = arrivals - departures
    headline = (
        f"{wf.name} ({src.label}) {start_d} → {stop_d}: "
        f"{arrivals} arrivals, {departures} departures, "
        f"end-of-window WIP {end_wip}."
    )
    payload = {
        "schema": "flowmetrics.metric.cumulative.v1",
        "input": {
            "workflow": wf.name,
            "source": wf.source,
            "repo": wf.repo,
            "jira_url": wf.jira_url,
            "jira_project": wf.jira_project,
            "start": start_d.isoformat(),
            "stop": stop_d.isoformat(),
            "stages": list(workflow_tuple),
            "interval_days": interval_days,
            "offline": offline,
        },
        "summary": {
            "arrivals_at_end": arrivals,
            "departures_at_end": departures,
            "end_of_window_wip": end_wip,
            "samples": len(points),
        },
        "points": [
            {
                "sampled_on": p.sampled_on.isoformat(),
                "counts_by_state": dict(p.counts_by_state),
            }
            for p in points
        ],
        "headline": headline,
    }
    _emit_metric(fmt, headline, payload)


@metric.command("aging", short_help="In-flight items × state × age")
@_apply_workflow_options
@click.option(
    "--asof", type=str, default=None,
    help="As-of date (YYYY-MM-DD). Defaults to today (UTC).",
)
@click.option(
    "--cache-dir", type=click.Path(path_type=Path),
    default=DEFAULT_CACHE_DIR, show_default=True,
)
@click.option("--offline/--online", default=False)
@_METRIC_FORMAT_OPTION
def metric_aging(
    workflow_name, workflow_yaml, workflows_dir,
    asof, cache_dir, offline, fmt,
) -> None:
    """In-flight items by current state × age, plus completed-item
    cycle-time percentiles as reference thresholds. The workflow
    (`--workflow-name` or `--workflow-yaml`) supplies both the source AND the stage
    order.

    Schema: `flowmetrics.metric.aging.v1`."""
    from datetime import date
    from datetime import timedelta as _td

    from .aging import compute_aging, cycle_time_percentiles
    from .compute import compute_pr_flow

    wf = _resolve_workflow(
        workflow_name=workflow_name, workflow_yaml=workflow_yaml,
        workflows_dir=workflows_dir,
    )
    src = _build_source_from_workflow(
        wf, cache_dir=cache_dir, offline=offline,
    )
    workflow_tuple = _stages_from_workflow(wf)
    if not workflow_tuple:
        raise click.UsageError(
            f"workflow {wf.name!r} has no steps — add stages via the "
            "wizard or in the YAML."
        )

    asof_d = _parse_date(asof) if asof else date.today()
    in_flight = list(src.fetch_in_flight(asof_d))
    aging_items = compute_aging(in_flight, asof=asof_d)

    # Cycle-time percentiles from the prior 30-day window — same
    # reference window the Aging chart uses on the web UI.
    hist_end = asof_d - _td(days=1)
    hist_start = hist_end - _td(days=DEFAULT_TRAINING_DAYS - 1)
    completed = list(src.fetch_for_percentile_training(hist_start, hist_end))
    flows = [
        compute_pr_flow(it, gap=DEFAULT_GAP, min_cluster=DEFAULT_MIN_CLUSTER)
        for it in completed
        if it.completed_at is not None
    ]
    pct = cycle_time_percentiles(flows)

    count = len(aging_items)
    oldest = max((it.age_days for it in aging_items), default=0)
    headline = (
        f"{wf.name} ({src.label}) as of {asof_d}: {count} in-flight "
        f"items; oldest {oldest}d "
        f"(P85={pct.get(85, 0):.1f}d from {len(flows)} completed)."
    )
    payload = {
        "schema": "flowmetrics.metric.aging.v1",
        "input": {
            "workflow": wf.name,
            "source": wf.source,
            "repo": wf.repo,
            "jira_url": wf.jira_url,
            "jira_project": wf.jira_project,
            "asof": asof_d.isoformat(),
            "stages": list(workflow_tuple),
            "offline": offline,
        },
        "summary": {
            "in_flight_count": count,
            "oldest_age_days": oldest,
            "completed_count_for_percentiles": len(flows),
        },
        "cycle_time_percentiles_days": {str(p): v for p, v in pct.items()},
        "items": [
            {
                "item_id": it.item_id,
                "title": it.title,
                "current_state": it.current_state,
                "age_days": it.age_days,
                "url": it.url,
            }
            for it in aging_items
        ],
        "headline": headline,
    }
    _emit_metric(fmt, headline, payload)


@metric.command("cycle-time", short_help="Per-item cycle times + P50/P85/P95")
@_apply_workflow_options
@click.option("--start", type=str, default=None, help="Window start (YYYY-MM-DD).")
@click.option("--stop", type=str, default=None, help="Window stop (YYYY-MM-DD).")
@click.option(
    "--cache-dir", type=click.Path(path_type=Path),
    default=DEFAULT_CACHE_DIR, show_default=True,
)
@click.option("--offline/--online", default=False)
@_METRIC_FORMAT_OPTION
def metric_cycle_time(
    workflow_name, workflow_yaml, workflows_dir,
    start, stop, cache_dir, offline, fmt,
) -> None:
    """Per-item cycle times + percentile thresholds. The workflow
    (`--workflow-name` or `--workflow-yaml`) supplies the source.

    Schema: `flowmetrics.metric.cycle_time.v1`."""
    from .charts.primitives import chart_percentiles
    from .service import default_history_end, default_history_start

    wf = _resolve_workflow(
        workflow_name=workflow_name, workflow_yaml=workflow_yaml,
        workflows_dir=workflows_dir,
    )
    src = _build_source_from_workflow(
        wf, cache_dir=cache_dir, offline=offline,
    )

    stop_d = _parse_date(stop) if stop else default_history_end()
    start_d = _parse_date(start) if start else default_history_start(stop_d)
    if start_d > stop_d:
        raise click.UsageError(
            f"--start ({start_d}) must be on or before --stop ({stop_d})"
        )

    items = list(src.fetch_for_percentile_training(start_d, stop_d))

    rows: list[dict] = []
    cycle_days: list[float] = []
    for it in items:
        if it.completed_at is None:
            continue
        cycle = (it.completed_at - it.created_at).total_seconds() / 86400
        cycle_days.append(cycle)
        rows.append({
            "item_id": it.item_id,
            "title": it.title,
            "completed_at": it.completed_at.date().isoformat(),
            "cycle_time_days": round(cycle, 4),
            "url": it.url,
        })

    pct = (
        chart_percentiles(cycle_days)
        if cycle_days else {50: 0.0, 70: 0.0, 85: 0.0, 95: 0.0}
    )
    headline = (
        f"{wf.name} ({src.label}) {start_d} → {stop_d}: "
        f"{len(rows)} completed items; "
        f"P50={pct.get(50, 0):.1f}d, "
        f"P85={pct.get(85, 0):.1f}d, "
        f"P95={pct.get(95, 0):.1f}d."
    )
    payload = {
        "schema": "flowmetrics.metric.cycle_time.v1",
        "input": {
            "workflow": wf.name,
            "source": wf.source,
            "repo": wf.repo,
            "jira_url": wf.jira_url,
            "jira_project": wf.jira_project,
            "start": start_d.isoformat(),
            "stop": stop_d.isoformat(),
            "offline": offline,
        },
        "summary": {"completed_count": len(rows)},
        "percentiles_days": {str(p): round(v, 4) for p, v in pct.items()},
        "items": rows,
        "headline": headline,
    }
    _emit_metric(fmt, headline, payload)


# ---------------------------------------------------------------------------
# Warehouse: `flow materialize <name>` — Slice 1.
# ---------------------------------------------------------------------------


@cli.command(short_help="Materialize a workflow — fetch + write Parquet")
@click.argument("name", type=str, required=False, default=None)
@click.option(
    "--all/--no-all",
    "all_workflows",
    default=False,
    help=(
        "Materialize every configured workflow (the daily-cron path). "
        "Mutually exclusive with a NAME positional. A single failing "
        "workflow doesn't block the rest; per-workflow detail lives in "
        "the manifest."
    ),
)
@click.option(
    "--data-dir",
    type=click.Path(path_type=Path),
    default=Path("./data"),
    show_default=True,
    help="Directory where work_items/, transitions/, runs/ Parquet land.",
)
@click.option(
    "--workflows-dir", "contracts_dir",
    type=click.Path(path_type=Path),
    default=Path("./contracts"),
    show_default=True,
    help=(
        "Directory holding workflows.db (the wizard's store, "
        "DB-first lookup) and any un-migrated workflow YAMLs."
    ),
)
@click.option(
    "--cache-dir",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Source-API response cache (read by GitHub/Jira adapters). "
        "Defaults to `<data-dir>/.cache/github` — one tree per "
        "`flow` install means launchd / cron / systemd never "
        "inherit a CWD-relative path."
    ),
)
@click.option(
    "--offline/--online",
    default=False,
    help="Offline reads cache only; online hits the source API on miss.",
)
@click.option(
    "--since",
    "since",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help=(
        "Override the workflow's `start` for this run only. "
        "ISO YYYY-MM-DD (UTC). Used for targeted backfills, e.g. "
        "the aging page's coverage-gap action."
    ),
)
@click.option(
    "--until",
    "until",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help=(
        "Override the workflow's `stop` for this run only. "
        "ISO YYYY-MM-DD (UTC)."
    ),
)
@click.option(
    "--status-file",
    "status_file",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Write a JSON status file (running → done/failed) at this "
        "path. The Data Source page polls it during a "
        "browser-triggered backfill. Single-workflow mode only."
    ),
)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "With --all: where to write the daily JSON manifest. "
        "Defaults to <data-dir>/_status/daily-<UTC-date>.json."
    ),
)
@click.option(
    "--bg/--no-bg",
    default=False,
    help=(
        "Install + activate a scheduled materialize job (macOS "
        "launchd). Requires --at HH:MM. Use `--bg --stop` to "
        "uninstall."
    ),
)
@click.option(
    "--at", "at_time", type=str, default=None,
    help=(
        "With --bg: local time of day to fire the scheduled "
        "materialize, in HH:MM format (e.g. 06:00 for 6 AM)."
    ),
)
@click.option(
    "--stop/--no-stop",
    default=False,
    help=(
        "With --bg: stop the scheduled job and remove its plist. "
        "Without --bg: error."
    ),
)
def materialize(
    name: str | None,
    all_workflows: bool,
    data_dir: Path,
    contracts_dir: Path,
    cache_dir: Path | None,
    offline: bool,
    since,  # click.DateTime → datetime | None
    until,
    status_file: Path | None,
    manifest_path: Path | None,
    bg: bool,
    at_time: str | None,
    stop: bool,
) -> None:
    """Fetch + canonicalise + write Parquet for one workflow, or for
    every configured workflow with `--all`.

    Two modes:

      \b
      flow materialize NAME    # single workflow
      flow materialize --all   # every workflow in --workflows-dir

    NAME is looked up DB-first in `<workflows-dir>/workflows.db`,
    then falls back to a `NAME.yaml` file in the same directory.
    `flow workflows list` shows what's resolvable.

    `--all` is the daily-cron / scheduled-ingest path: iterate every
    non-archived workflow, write a per-day JSON manifest at
    `<data-dir>/_status/daily-<UTC-date>.json`, and exit non-zero
    only when EVERY workflow failed. A single bad workflow doesn't
    block the others.

    `--since` and `--until` override the workflow's stored start/stop
    for this invocation only — single-workflow mode.

    `--status-file` (opt-in, single-workflow mode) writes a JSON
    running → done/failed record so the web Data Source page can
    poll a browser-triggered backfill.
    """
    # --stop only makes sense paired with --bg.
    if stop and not bg:
        raise click.ClickException(
            "--stop requires --bg (it's the inverse of --bg). "
            "Did you mean `flow materialize --bg --stop`?"
        )

    # Co-locate cache with the warehouse it feeds. The launchd /
    # cron / systemd path cannot rely on CWD-relative defaults — see
    # `_default_cache_dir`'s docstring.
    resolved_cache_dir = _default_cache_dir(cache_dir, data_dir)

    if bg:
        _materialize_bg(
            name=name,
            all_workflows=all_workflows,
            data_dir=data_dir,
            contracts_dir=contracts_dir,
            cache_dir=resolved_cache_dir,
            at_time=at_time,
            stop=stop,
        )
        return

    if name is None and not all_workflows:
        raise click.UsageError(
            "pass a workflow NAME or `--all` (use `flow workflows list` "
            "to see what's configured)."
        )
    if name is not None and all_workflows:
        raise click.UsageError(
            "NAME and `--all` are mutually exclusive."
        )
    if all_workflows and (since is not None or until is not None):
        raise click.UsageError(
            "--since / --until apply to a single workflow only; "
            "drop --all, or run materialize per workflow."
        )
    if all_workflows and status_file is not None:
        raise click.UsageError(
            "--status-file applies to a single workflow only; "
            "with --all, use --manifest instead."
        )

    if all_workflows:
        _materialize_all(
            data_dir=data_dir,
            contracts_dir=contracts_dir,
            cache_dir=resolved_cache_dir,
            offline=offline,
            manifest_path=manifest_path,
        )
    else:
        _materialize_one(
            name=name,
            data_dir=data_dir,
            contracts_dir=contracts_dir,
            cache_dir=resolved_cache_dir,
            offline=offline,
            since=since,
            until=until,
            status_file=status_file,
        )


def _parse_at(at_time: str) -> tuple[int, int]:
    """Parse `--at HH:MM` (24-hour, local time) into (hour, minute).
    Raises UsageError with a clear message on malformed input."""
    import re as _re
    m = _re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", at_time)
    if not m:
        raise click.UsageError(
            f"--at {at_time!r}: expected HH:MM format (e.g. 06:00, 14:30)."
        )
    hour = int(m.group(1))
    minute = int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise click.UsageError(
            f"--at {at_time!r}: hour must be 0–23 and minute 0–59."
        )
    return hour, minute


def _default_cache_dir(cache_dir: Path | None, data_dir: Path) -> Path:
    """Resolve `--cache-dir` for the materialize command.

    Returns the explicit value when set; otherwise derives
    `<data-dir>/.cache/github`. Co-locating with the warehouse means
    `flow materialize` works the same under launchd (CWD=`/`,
    sealed-system-volume read-only), under cron, and from any
    interactive shell — never inheriting whatever CWD the launcher
    happened to choose."""
    if cache_dir is not None:
        return cache_dir
    return data_dir / ".cache" / "github"


def _materialize_bg(
    *,
    name: str | None,
    all_workflows: bool,
    data_dir: Path,
    contracts_dir: Path,
    cache_dir: Path,
    at_time: str | None,
    stop: bool,
) -> None:
    """Install / uninstall a scheduled materialize job. Mirrors the
    `flow serve --bg` flag combinatorics: --stop tears it down,
    otherwise --at HH:MM installs."""
    from . import bg as bg_mod

    if stop:
        try:
            bg_mod.stop_materialize_schedule()
        except bg_mod.BgError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo("scheduled materialize stopped + uninstalled.")
        return

    if at_time is None:
        raise click.UsageError(
            "`--bg` requires `--at HH:MM` to schedule the run "
            "(or `--bg --stop` to uninstall a previous schedule)."
        )
    if name is None and not all_workflows:
        raise click.UsageError(
            "with `--bg --at HH:MM`, pass a workflow NAME or `--all` "
            "(use `flow workflows list` to see what's configured)."
        )
    if name is not None and all_workflows:
        raise click.UsageError(
            "NAME and `--all` are mutually exclusive."
        )

    hour, minute = _parse_at(at_time)

    # Resolve absolute paths — launchd doesn't inherit a CWD, and
    # encoding relative paths into a plist would break the moment
    # the scheduled job fires from a different directory.
    flow_bin_str = shutil.which("flow")
    if flow_bin_str is None:
        raise click.ClickException(
            "could not locate the `flow` executable on PATH. "
            "Re-run after `uv tool install` or with the absolute "
            "path on PATH."
        )
    flow_bin = Path(flow_bin_str).resolve()
    data_dir_abs = data_dir.resolve()
    contracts_dir_abs = contracts_dir.resolve()
    cache_dir_abs = cache_dir.resolve()
    log_dir = data_dir_abs / "_status"

    # The args carried INTO the scheduled command — everything that
    # follows `flow materialize`. Position the workflow selector
    # (NAME or --all) first, then the paths.
    materialize_args: list[str] = []
    if all_workflows:
        materialize_args.append("--all")
    else:
        # Single-workflow mode.
        materialize_args.append(name)  # type: ignore[arg-type]
    materialize_args.extend([
        "--workflows-dir", str(contracts_dir_abs),
        "--data-dir", str(data_dir_abs),
        # Pin the cache dir explicitly so launchd never inherits a
        # CWD-relative default (which on macOS resolves under `/`,
        # the read-only sealed system volume → OSError [Errno 30]).
        "--cache-dir", str(cache_dir_abs),
    ])

    try:
        unit_path = bg_mod.install_materialize_schedule(
            flow_bin=flow_bin,
            materialize_args=materialize_args,
            hour=hour, minute=minute,
            log_dir=log_dir,
        )
    except bg_mod.BgError as exc:
        raise click.ClickException(str(exc)) from exc

    selector = "--all" if all_workflows else name
    click.echo(
        f"scheduled materialize installed at {unit_path}\n"
        f"  fires:   daily at {hour:02d}:{minute:02d} local time\n"
        f"  command: flow materialize {selector} "
        f"--workflows-dir {contracts_dir_abs} "
        f"--data-dir {data_dir_abs} "
        f"--cache-dir {cache_dir_abs}\n"
        f"  logs:    {log_dir}/materialize.{{out,err}}.log\n"
        f"  stop:    flow materialize --bg --stop"
    )


def _materialize_one(
    *,
    name: str,
    data_dir: Path,
    contracts_dir: Path,
    cache_dir: Path,
    offline: bool,
    since,
    until,
    status_file: Path | None,
) -> None:
    """Single-workflow path. Invoked by external cron / systemd-timer
    / k8s CronJob (or by the wizard's browser-triggered backfill).
    Exits 0 on success, non-zero on any failure."""
    from .backfill import write_status
    from .materialize import materialize as run_materialize
    from .workflows_db import WorkflowStore

    since_iso = since.date().isoformat() if since is not None else None
    until_iso = until.date().isoformat() if until is not None else None
    started = datetime.now(UTC)

    def _status(state: str, message: str) -> None:
        if status_file is None:
            return
        write_status(
            status_file,
            {
                "workflow": name,
                "since": since_iso,
                "until": until_iso,
                "status": state,
                "started_at": started.isoformat(),
                "finished_at": (
                    None if state == "running"
                    else datetime.now(UTC).isoformat()
                ),
                "message": message,
            },
        )

    _status("running", "")

    # The store resolves DB-first then falls back to a YAML on disk
    # (cron / not-yet-migrated). Reads don't trigger the YAML→DB
    # migration — that's serve-time's job.
    workflow = WorkflowStore(contracts_dir).get(name)
    if workflow is None:
        msg = (
            f"workflow {name!r} not found under {contracts_dir} "
            "(no DB row and no matching YAML)"
        )
        _status("failed", msg)
        click.echo(f"error: {msg}", err=True)
        sys.exit(2)

    overrides: dict = {}
    if since is not None:
        overrides["start"] = since.date()
    if until is not None:
        overrides["stop"] = until.date()
    if overrides:
        workflow = workflow.model_copy(update=overrides)

    try:
        manifest = run_materialize(
            workflow=workflow,
            data_dir=data_dir,
            cache_dir=cache_dir,
            offline=offline,
        )
    except Exception as exc:
        _status("failed", f"{type(exc).__name__}: {exc}")
        if status_file is None:
            raise
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    msg = (
        f"materialized {manifest.contract_id} (run_id={manifest.run_id}): "
        f"{manifest.items_fetched} items in "
        f"{(manifest.completed_at - manifest.started_at).total_seconds():.1f}s"
    )
    _status("done", msg)
    click.echo(msg)


def _materialize_all_now() -> datetime:
    """Indirection so tests can pin the timestamp without touching
    the global `datetime.now`. Plain function, not a constant — the
    monkeypatch needs a name to rebind."""
    return datetime.now(UTC)


def _materialize_all(
    *,
    data_dir: Path,
    contracts_dir: Path,
    cache_dir: Path,
    offline: bool,
    manifest_path: Path | None,
) -> None:
    """All-workflows path. The daily-cron / scheduled-ingest target."""
    from .materialize import materialize as run_materialize
    from .workflow import WorkflowError
    from .workflows_db import WorkflowStore

    # Migrate any leftover YAMLs into the DB first so this single
    # command handles both first-boot and the steady-state cron path.
    store = WorkflowStore(contracts_dir)
    store.ensure_initialized()

    started = _materialize_all_now()

    # `list()` already excludes archived rows, so a retired workflow
    # isn't re-imported by the daily cron.
    live = store.list()

    results: list[dict] = []
    for meta in live:
        name = meta.workflow.name
        entry: dict = {"workflow": name, "status": "failed", "error": ""}
        try:
            manifest = run_materialize(
                workflow=meta.workflow,
                data_dir=data_dir,
                cache_dir=cache_dir,
                offline=offline,
            )
            entry["status"] = "ok"
            entry["items"] = manifest.items_fetched
            entry["run_id"] = manifest.run_id
        except WorkflowError as exc:
            entry["error"] = f"WorkflowError: {exc}"
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
        results.append(entry)

    finished = _materialize_all_now()
    payload = {
        "schema": "flowmetrics.materialize_all.v1",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "results": results,
    }

    if manifest_path is None:
        manifest_path = (
            data_dir / "_status" / f"daily-{started.date().isoformat()}.json"
        )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2))

    # Echo a one-line summary so cron mail / journal reads cleanly.
    ok = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] == "failed")
    click.echo(
        f"materialize --all: {ok} ok, {failed} failed, "
        f"manifest at {manifest_path}"
    )

    # Exit non-zero only when everything failed (or the dir was empty
    # AND someone explicitly expects something there — we treat the
    # empty case as success: "no workflows configured today" is the
    # cron-job's first day, not an error).
    if results and ok == 0:
        sys.exit(1)


# ---------------------------------------------------------------------------
# `flow contracts ...` — read-only peek at the configured workflows.
# Mirrors the home page of the dashboard for operators who never open
# the browser; closes a long-standing discoverability gap where the
# materialize commands silently read workflows.db but no CLI surfaced it.
# ---------------------------------------------------------------------------


@cli.group(short_help="Inspect configured workflows")
def workflows() -> None:
    """Read-only inspection of configured workflows (workflows.db +
    un-migrated YAML files in --workflows-dir)."""


@workflows.command("list", short_help="List configured workflows")
@click.option(
    "--workflows-dir", "contracts_dir",
    type=click.Path(path_type=Path),
    default=Path("./contracts"),
    show_default=True,
    help=(
        "Directory holding workflows.db (DB-first) and any "
        "un-migrated workflow YAMLs."
    ),
)
@click.option(
    "--data-dir",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Optional: also show a DATA column reporting whether "
        "`flow materialize NAME` has ever produced Parquet for "
        "each workflow under <data-dir>/work_items/."
    ),
)
@click.option(
    "--all/--no-all", "include_archived",
    default=False,
    help="Include archived workflows.",
)
def contracts_list(
    contracts_dir: Path,
    data_dir: Path | None,
    include_archived: bool,
) -> None:
    """Enumerate every workflow `flow materialize` / `flow serve`
    would resolve.

    Source markers: `db` for rows in workflows.db (wizard-managed);
    `yaml` for un-migrated YAML files in the workflows-dir. When
    both exist for the same name, the DB row wins — same precedence
    as `WorkflowStore.get()`.

    Pass `--data-dir PATH` to also surface a DATA column: `ready`
    when materialize has produced Parquet for that workflow, `—`
    otherwise (with a footer hint pointing at the recovery
    command).
    """
    from .workflows_db import WorkflowStore

    store = WorkflowStore(contracts_dir)
    # DB rows (active + archived as requested) come from the
    # underlying SQLite via WorkflowStore.list().
    db_rows = store.list(include_archived=include_archived)
    db_names = {m.name for m in db_rows}

    # Un-migrated YAMLs: scan the workflows-dir directly. Skip any
    # whose name is already shadowed by a DB row so output reflects
    # what `flow materialize` would actually resolve.
    yaml_rows: list[tuple[str, str]] = []
    if contracts_dir.is_dir():
        for path in sorted(contracts_dir.iterdir()):
            if path.suffix not in (".yaml", ".yml"):
                continue
            stem = path.stem
            if stem in db_names:
                continue
            meta = store.get_meta(stem)
            if meta is None:
                # Malformed YAML — skip silently; the materialize
                # command will surface a clear error if invoked.
                continue
            yaml_rows.append((stem, _contract_target(meta.workflow)))

    if not db_rows and not yaml_rows:
        click.echo(
            f"No workflows configured in {contracts_dir.resolve()}.\n"
            "\n"
            "To add one, run `flow serve` and click '+ New workflow' in\n"
            "the browser — the wizard probes your repo and writes a\n"
            "workflows.db row for you.\n"
            "\n"
            "Or drop a workflow YAML into the directory; see\n"
            "docs/HOWTO.md#write-a-workflow-yaml-by-hand."
        )
        return

    rows: list[tuple[str, str, str, bool]] = []
    for meta in db_rows:
        rows.append((
            meta.name,
            "db",
            _contract_target(meta.workflow),
            meta.archived_at is not None,
        ))
    for name, target in yaml_rows:
        rows.append((name, "yaml", target, False))

    # Optional DATA column: probe the warehouse for any Parquet under
    # the workflow's contract_id= partition. A single matching file
    # is enough — we're answering "has materialize EVER run?", not
    # "is this fresh?".
    data_cells: dict[str, str] = {}
    empty_names: list[str] = []
    if data_dir is not None:
        for name, _src, _target, _arch in rows:
            partition = data_dir / "work_items" / f"contract_id={name}"
            has_parquet = (
                partition.is_dir()
                and any(partition.rglob("*.parquet"))
            )
            data_cells[name] = "ready" if has_parquet else "—"
            if not has_parquet:
                empty_names.append(name)

    # Column widths sized from data, capped so a very long repo
    # name doesn't blow up the format.
    name_w = max(len("NAME"), *(len(r[0]) for r in rows))
    src_w = max(len("SOURCE"), *(len(r[1]) for r in rows))
    if data_dir is not None:
        data_w = max(len("DATA"), *(len(v) for v in data_cells.values()))
        click.echo(
            f"{'NAME':<{name_w}}  {'SOURCE':<{src_w}}  "
            f"{'DATA':<{data_w}}  TARGET"
        )
    else:
        click.echo(f"{'NAME':<{name_w}}  {'SOURCE':<{src_w}}  TARGET")
    for name, src, target, archived in sorted(rows):
        suffix = "  [archived]" if archived else ""
        if data_dir is not None:
            data_cell = data_cells[name]
            click.echo(
                f"{name:<{name_w}}  {src:<{src_w}}  "
                f"{data_cell:<{data_w}}  {target}{suffix}"
            )
        else:
            click.echo(f"{name:<{name_w}}  {src:<{src_w}}  {target}{suffix}")

    if empty_names:
        click.echo(
            "\nWorkflows without warehouse data: "
            f"{', '.join(empty_names)}\n"
            "Recover with one of:\n"
            "  - `flow serve` → open the Data Source page → Backfill\n"
            f"  - `flow materialize {empty_names[0]} "
            f"--workflows-dir {contracts_dir} --data-dir {data_dir}`"
        )


def _contract_target(workflow) -> str:
    """One-line summary of what the workflow is fetching — GitHub
    `repo` or Jira `jira_project @ jira_url`. Carried in the listing
    so an operator can match name → source without opening the YAML."""
    if workflow.source == "github":
        return workflow.repo or "(no repo set)"
    if workflow.source == "jira":
        return f"{workflow.jira_project} @ {workflow.jira_url}"
    return workflow.source


# ---------------------------------------------------------------------------
# `flow backup` / `flow restore` — warehouse portability.
# ---------------------------------------------------------------------------


@cli.command(short_help="Snapshot the warehouse into a single .tar.gz")
@click.option(
    "--data-dir",
    type=click.Path(path_type=Path),
    default=Path("./data"),
    show_default=True,
    help="Warehouse to back up.",
)
@click.option(
    "--workflows-dir", "contracts_dir",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Directory holding `workflows.db` (the config DB). Pass this "
        "to include config in the backup; omit it for a data-only "
        "archive. The DB is snapshotted via SQLite's online backup "
        "API so a running server can't corrupt it."
    ),
)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Where to write the tarball. Defaults to "
        "<data-dir>/_backups/flowmetrics-<UTC-timestamp>.tar.gz."
    ),
)
@click.option(
    "--include-cache/--no-include-cache",
    default=False,
    help=(
        "Include the source-API response cache. Off by default — "
        "the cache is regenerable and bloats the archive."
    ),
)
def backup(
    data_dir: Path,
    contracts_dir: Path | None,
    output: Path | None,
    include_cache: bool,
) -> None:
    """Snapshot the warehouse (and optionally the config DB) into a
    single timestamped .tar.gz.

    The archive carries every Parquet table + run manifest under
    `--data-dir` plus a `flowmetrics-backup.json` header with a
    SHA-256 of every payload file. Pass `--workflows-dir` to also
    include a consistent snapshot of `workflows.db` (taken via
    SQLite's online backup API so a live server can't tear it).
    `flow restore` verifies the header + checksums before extracting.
    """
    from .backup import write_backup

    if output is None:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output = data_dir / "_backups" / f"flowmetrics-{ts}.tar.gz"

    header = write_backup(
        data_dir,
        output,
        include_cache=include_cache,
        contracts_dir=contracts_dir,
    )
    size_mb = output.stat().st_size / (1024 * 1024)
    click.echo(
        f"wrote {output} ({size_mb:.1f} MB, "
        f"{len(header.files)} files, schema={header.schema})"
    )


@cli.command(short_help="Restore a warehouse from a flow backup tarball")
@click.option(
    "--input",
    "input_path",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Path to the .tar.gz written by `flow backup`.",
)
@click.option(
    "--data-dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Target directory to restore the warehouse into.",
)
@click.option(
    "--workflows-dir", "contracts_dir",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Target directory for the restored `workflows.db`. Required "
        "whenever the backup carries config (or when using "
        "--config-only)."
    ),
)
@click.option(
    "--force/--no-force",
    default=False,
    help=(
        "Overwrite a non-empty target. Off by default so a typo "
        "doesn't clobber a working warehouse."
    ),
)
@click.option(
    "--data-only/--no-data-only",
    default=False,
    help="Restore only the data warehouse (skip workflows.db).",
)
@click.option(
    "--config-only/--no-config-only",
    default=False,
    help="Restore only workflows.db (skip the data warehouse).",
)
def restore(
    input_path: Path,
    data_dir: Path,
    contracts_dir: Path | None,
    force: bool,
    data_only: bool,
    config_only: bool,
) -> None:
    """Verify + extract a `flow backup` tarball.

    Default extracts both the data warehouse and (if present)
    `workflows.db`. Use `--data-only` to leave config untouched or
    `--config-only` to leave the warehouse untouched. Refuses to
    touch a non-empty target without `--force`. Verifies every
    file's SHA-256 against the header before writing anything, so
    a corrupted or tampered archive fails before it can damage
    a half-restored install.
    """
    from .backup import BackupError, restore_backup

    if data_only and config_only:
        raise click.ClickException(
            "--data-only and --config-only are mutually exclusive."
        )

    restore_data = not config_only
    restore_config = not data_only

    try:
        header = restore_backup(
            input_path,
            data_dir,
            force=force,
            contracts_dir=contracts_dir,
            restore_data=restore_data,
            restore_config=restore_config,
        )
    except BackupError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"restored {len(header.files)} files "
        f"(from {input_path}, written {header.created_at})"
    )


# ---------------------------------------------------------------------------
# Warehouse: `flow serve` — Slice 2.
# ---------------------------------------------------------------------------


def _assert_port_available(host: str, port: int) -> None:
    """Check the port is free BEFORE handing off to uvicorn so an
    already-bound port surfaces as a readable message naming the
    port + the `--port N` escape hatch — not uvicorn's raw
    `[Errno 48] Address already in use` traceback."""
    import errno
    import socket as _socket

    probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES):
            raise click.ClickException(
                f"port {port} on {host} is already in use.\n"
                f"{_port_busy_hints(port)}"
            ) from exc
        raise
    finally:
        probe.close()


def _port_busy_hints(port: int, os_name: str | None = None) -> str:
    """OS-appropriate 'find/kill the holder' suggestion block for
    the port-busy error message. POSIX → `lsof` / `kill`; Windows →
    `netstat` / `taskkill`. The `--port N+1` escape hatch is the
    same on both."""
    import os as _os

    name = _os.name if os_name is None else os_name
    alt_port = port + 1
    if name == "nt":
        return (
            f"  - find what is holding it:  netstat -ano | findstr :{port}\n"
            f"  - free it:                  taskkill /F /PID <PID>\n"
            f"  - or pick another port:     flow serve --port {alt_port}"
        )
    return (
        f"  - find what is holding it:  lsof -ti:{port}\n"
        f"  - free it:                  kill $(lsof -ti:{port})\n"
        f"  - or pick another port:     flow serve --port {alt_port}"
    )


@cli.command(short_help="Serve the warehouse-backed web UI")
@click.option(
    "--port",
    type=int,
    default=8000,
    show_default=True,
)
@click.option(
    "--host",
    type=str,
    default="127.0.0.1",
    show_default=True,
    help="Bind address. Defaults to localhost; any other value requires --password.",
)
@click.option(
    "--data-dir",
    type=click.Path(path_type=Path),
    default=Path("./data"),
    show_default=True,
)
@click.option(
    "--workflows-dir", "contracts_dir",
    type=click.Path(path_type=Path),
    default=Path("./contracts"),
    show_default=True,
)
@click.option(
    "--password",
    type=str,
    envvar="FLOW_PASSWORD",
    default=None,
    help=(
        "HTTP Basic password. Required when --host is anything other "
        "than 127.0.0.1. Also readable from $FLOW_PASSWORD."
    ),
)
@click.option(
    "--bg/--no-bg",
    default=False,
    help=(
        "Install + start the dashboard as a persistent native "
        "service (macOS launchd / Linux systemd --user). "
        "Idempotent: re-running reloads with the latest flags. "
        "Use `--bg --stop` to tear it down."
    ),
)
@click.option(
    "--stop/--no-stop",
    default=False,
    help=(
        "With --bg: stop the service and remove its unit file. "
        "Without --bg: error."
    ),
)
def serve(
    port: int,
    host: str,
    data_dir: Path,
    contracts_dir: Path,
    password: str | None,
    bg: bool,
    stop: bool,
) -> None:
    """Serve the dashboard + per-metric detail pages.

    Reads from the local Parquet store under --data-dir (populated by
    `flow materialize`). Never touches GitHub or Jira during a request.

    Pass `--bg` to install + start as a persistent native service
    (macOS launchd or Linux systemd --user). `--bg --stop` tears it
    down. Windows operators: use the templated NSSM wrapper (see
    docs/HOWTO.md#run-as-a-persistent-web-server).
    """
    # --stop only makes sense alongside --bg (its inverse). Catch
    # `flow serve --stop` (no --bg) as an operator typo so we don't
    # silently start the dashboard in the foreground.
    if stop and not bg:
        raise click.ClickException(
            "--stop requires --bg (it's the inverse of --bg). "
            "Did you mean `flow serve --bg --stop`?"
        )

    if bg:
        from . import bg as bg_mod

        if stop:
            try:
                bg_mod.stop_and_uninstall()
            except bg_mod.BgError as exc:
                raise click.ClickException(str(exc)) from exc
            click.echo("flow serve --bg stopped + uninstalled.")
            return

        # Off-localhost binds are network-exposed; require a password.
        # Same rule as foreground — checked here too because the
        # service unit will encode the chosen flags as-is.
        if host != "127.0.0.1" and not password:
            raise click.ClickException(
                f"--host {host} is network-exposed and requires "
                "--password (or $FLOW_PASSWORD)."
            )
        # launchd / systemd don't inherit a CWD; resolve everything to
        # absolutes before writing the unit.
        flow_bin_str = shutil.which("flow")
        if flow_bin_str is None:
            raise click.ClickException(
                "could not locate the `flow` executable on PATH. "
                "Re-run after `uv tool install` or with the absolute "
                "path on PATH."
            )
        flow_bin = Path(flow_bin_str).resolve()
        data_dir_abs = data_dir.resolve()
        contracts_dir_abs = contracts_dir.resolve()
        log_dir = data_dir_abs / "_status"
        try:
            unit_path = bg_mod.install_and_start(
                flow_bin=flow_bin,
                workflows_dir=contracts_dir_abs,
                data_dir=data_dir_abs,
                port=port,
                host=host,
                password=password,
                log_dir=log_dir,
            )
        except bg_mod.BgError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(
            f"flow serve --bg installed at {unit_path}\n"
            f"  → http://{host}:{port}/\n"
            f"  logs:  {log_dir}/serve.{{out,err}}.log\n"
            f"  stop:  flow serve --bg --stop"
        )
        # Linux user-units die on session logout unless the user
        # has lingering enabled. We can't enable it ourselves (it's
        # root-only) but we can name the one-liner the operator
        # needs to run.
        if sys.platform.startswith("linux"):
            click.echo(
                "\nNote: a systemd --user service stops on logout. "
                "To keep the dashboard alive across logout, run once:\n"
                "  sudo loginctl enable-linger $USER"
            )
        return

    import uvicorn

    from .app import create_app

    # Off-localhost binds are network-exposed; require a password.
    if host != "127.0.0.1" and not password:
        click.echo(
            f"error: --host {host} is network-exposed and requires --password "
            "(or $FLOW_PASSWORD). Use --host 127.0.0.1 for local-only access.",
            err=True,
        )
        sys.exit(2)

    _assert_port_available(host, port)

    app = create_app(
        data_dir=data_dir,
        contracts_dir=contracts_dir,
        password=password,
    )
    # Banner names the resolved paths so a confused operator
    # ("why is the dashboard empty?") immediately sees what's
    # being scanned and can re-point with --data-dir /
    # --workflows-dir.
    click.echo(f"flow serve listening on http://{host}:{port}/")
    click.echo(f"  data_dir:      {data_dir.resolve()}")
    click.echo(f"  workflows_dir: {contracts_dir.resolve()}")
    uvicorn.run(app, host=host, port=port, log_level="info")
