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

from .compute import WindowResult
from .forecast import (
    ResultsHistogram,
    backward_percentile,
    build_histogram,
    forward_percentile,
    monte_carlo_how_many,
    monte_carlo_when_done,
)
from .interpretation import interpret_efficiency, interpret_how_many, interpret_when_done
from .logcapture import LogCapture
from .renderers import html_renderer, json_renderer, text_renderer
from .report import (
    EfficiencyInput,
    EfficiencyReport,
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
    flowmetrics_for_window,
    historical_throughput_samples,
    make_github_source,
    make_jira_source,
    this_week_window,
)
from .sources import Source


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
) -> Source:
    """Pick the source backend from whichever flag set the user provided.

    `--repo` ⇒ GitHub. `--jira-url` + `--jira-project` ⇒ Jira. Mutually
    exclusive; exactly one set must be present.
    """
    have_github = bool(repo)
    have_jira = bool(jira_url or jira_project)
    if have_github and have_jira:
        raise click.UsageError(
            "Pass either --repo (GitHub) or --jira-url + --jira-project (Jira), not both."
        )
    if have_github:
        return make_github_source(repo, cache_dir=cache_dir, read_only=offline)
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


@cli.group(short_help="Flow efficiency (active vs. wait time)")
def efficiency() -> None:
    """Flow efficiency: active vs. wait time on merged PRs."""


@efficiency.command()
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
@_FORMAT_OPTION
@_OUTPUT_OPTION
@_VERBOSE_OPTION
def week(

    repo: str | None,
    jira_url: str | None,
    jira_project: str | None,
    start: str | None,
    stop: str | None,
    cache_dir: Path,
    offline: bool,
    gap_hours: float,
    min_cluster_minutes: float,
    fmt: str,
    output: Path | None,
    verbose: bool,
) -> None:
    """Compute flow efficiency for a date window (defaults to this week).

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

    def build() -> EfficiencyReport:
        result: WindowResult = flowmetrics_for_window(
            src, start_d, stop_d,
            gap=timedelta(hours=gap_hours),
            min_cluster=timedelta(minutes=min_cluster_minutes),
        )
        input_ = EfficiencyInput(
            repo=src.label,
            start=start_d,
            stop=stop_d,
            gap_hours=gap_hours,
            min_cluster_minutes=min_cluster_minutes,
            offline=offline,
        )
        return EfficiencyReport(
            input=input_,
            result=result,
            interpretation=interpret_efficiency(input_, result),
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
