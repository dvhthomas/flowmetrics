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

from .compute import WindowResult  # compute_pr_flow used in aging path
from .forecast import (
    ResultsHistogram,
    backward_percentile,
    build_histogram,
    forward_percentile,
    monte_carlo_how_many,
    monte_carlo_when_done,
)
from .interpretation import (
    interpret_efficiency,
    interpret_how_many,
    interpret_when_done,
)
from .logcapture import LogCapture
from .renderers import json_renderer, text_renderer
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
@click.option(
    "--include-issues/--no-include-issues",
    default=False,
    help="GitHub-only. Also include Issues closed in the window. For "
    "Issues closed by a PR-merge, cycle time uses the PR's mergedAt.",
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
    include_issues: bool,
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
        include_issues=include_issues,
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



@cli.group(short_help="Monte Carlo forecasting (when-done / how-many)")
def forecast() -> None:
    """Monte Carlo forecasting — when-done / how-many."""


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


@forecast.command("when-done")
@_apply_source_options
@click.option(
    "--items",
    "items",
    type=int,
    required=True,
    help=(
        "Number of items to complete. We use 'items' rather than 'backlog' "
        "because the latter is Scrum-loaded (used for the prioritized list)."
    ),
)
@click.option("--start-date", type=str, default=None)
@_apply_history_options
@click.option(
    "--include-issues/--no-include-issues",
    default=False,
    help="GitHub-only. Include Issues closed in the training window "
    "(with stitched PR-merge cycle times where applicable).",
)
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
    include_issues: bool,
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
        include_issues=include_issues,
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
@click.option(
    "--include-issues/--no-include-issues",
    default=False,
    help="GitHub-only. Include Issues closed in the training window "
    "(with stitched PR-merge cycle times where applicable).",
)
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
    include_issues: bool,
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
        include_issues=include_issues,
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


# ---------------------------------------------------------------------------
# Warehouse: `flow materialize <name>` — Slice 1.
# ---------------------------------------------------------------------------


@cli.command(short_help="Materialize a contract — fetch + write Parquet")
@click.argument("name", type=str)
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
    default=DEFAULT_CACHE_DIR,
    show_default=True,
    help="Source-API response cache (read by GitHub/Jira adapters).",
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
        "Override the contract's `start` for this run only. "
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
        "Override the contract's `stop` for this run only. "
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
        "browser-triggered backfill."
    ),
)
def materialize(
    name: str,
    data_dir: Path,
    contracts_dir: Path,
    cache_dir: Path,
    offline: bool,
    since,  # click.DateTime → datetime | None
    until,
    status_file: Path | None,
) -> None:
    """Fetch + canonicalise + write Parquet for one contract.

    NAME is looked up DB-first in `<workflows-dir>/workflows.db`
    (where the wizard writes), then falls back to a `NAME.yaml` file
    in the same directory. `flow workflows list` shows what's
    resolvable. Use `flow materialize-all` for the whole set.

    Invoked by external cron / systemd-timer / k8s CronJob. Exits 0
    on success, non-zero on any failure. Operators see the error in
    cron mail or systemd journal.

    `--since` and `--until` override the contract's stored
    start/stop for this invocation only — they don't mutate the
    DB row or YAML. Useful for targeted backfills when the
    warehouse needs to be brought forward without changing the
    contract's canonical window.

    `--status-file` (opt-in) writes a JSON running → done/failed
    record so the web Data Source page can poll a browser-triggered
    backfill. Without it, behaviour is unchanged (cron path).
    """

    from .backfill import write_status
    from .workflows_db import WorkflowStore
    from .materialize import materialize as run_materialize

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
    contract = WorkflowStore(contracts_dir).get(name)
    if contract is None:
        msg = (
            f"contract {name!r} not found under {contracts_dir} "
            "(no DB row and no matching YAML)"
        )
        _status("failed", msg)
        click.echo(f"error: {msg}", err=True)
        sys.exit(2)

    # Click's DateTime returns datetime; we want date.
    overrides: dict = {}
    if since is not None:
        overrides["start"] = since.date()
    if until is not None:
        overrides["stop"] = until.date()
    if overrides:
        # Pydantic's `model_copy` is the equivalent of
        # `dataclasses.replace` for the new Contract model.
        contract = contract.model_copy(update=overrides)

    try:
        manifest = run_materialize(
            contract=contract,
            data_dir=data_dir,
            cache_dir=cache_dir,
            offline=offline,
        )
    except Exception as exc:
        _status("failed", f"{type(exc).__name__}: {exc}")
        # No status file → preserve the cron path: let it raise.
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


# ---------------------------------------------------------------------------
# `flow materialize-all` — daily-ingest wrapper for cron / launchd / Task
# Scheduler. Iterates every YAML in --workflows-dir; one bad contract
# doesn't block the others. Writes a JSON manifest the user's monitoring
# tool can grep for failures.
# ---------------------------------------------------------------------------


def _materialize_all_now() -> datetime:
    """Indirection so tests can pin the timestamp without touching
    the global `datetime.now`. Plain function, not a constant — the
    monkeypatch needs a name to rebind."""
    return datetime.now(UTC)


@cli.command(
    name="materialize-all",
    short_help="Run materialize for every configured workflow",
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
    "--cache-dir",
    type=click.Path(path_type=Path),
    default=DEFAULT_CACHE_DIR,
    show_default=True,
)
@click.option("--offline/--online", default=False)
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Where to write the daily JSON manifest. Defaults to "
        "<data-dir>/_status/daily-<UTC-date>.json."
    ),
)
def materialize_all(
    data_dir: Path,
    contracts_dir: Path,
    cache_dir: Path,
    offline: bool,
    manifest_path: Path | None,
) -> None:
    """Iterate every configured workflow and materialize each one.

    Workflows come from workflows.db (the wizard's store) plus any
    un-migrated YAML files in --workflows-dir. `flow workflows list`
    shows what would run.

    Scheduler-friendly: a single failing contract doesn't block the
    rest. Exit code is 0 when at least one workflow succeeded (so
    monitoring only pages when EVERYTHING is broken); the manifest
    holds per-workflow detail for finer-grained alerting.
    """

    from .workflow import WorkflowError
    from .workflows_db import WorkflowStore
    from .materialize import materialize as run_materialize

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
        name = meta.contract.name
        entry: dict = {"workflow": name, "status": "failed", "error": ""}
        try:
            manifest = run_materialize(
                contract=meta.contract,
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
        f"materialize-all: {ok} ok, {failed} failed, manifest at {manifest_path}"
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
    "--all/--no-all", "include_archived",
    default=False,
    help="Include archived workflows.",
)
def contracts_list(contracts_dir: Path, include_archived: bool) -> None:
    """Enumerate every workflow `flow materialize` / `flow serve`
    would resolve.

    Source markers: `db` for rows in workflows.db (wizard-managed);
    `yaml` for un-migrated YAML files in the workflows-dir. When
    both exist for the same name, the DB row wins — same precedence
    as `WorkflowStore.get()`.
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
            yaml_rows.append((stem, _contract_target(meta.contract)))

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

    # One row per workflow, three columns: name, source, target.
    # `archived` rows carry a suffix so a glance is unambiguous.
    rows: list[tuple[str, str, str, bool]] = []
    for meta in db_rows:
        rows.append((
            meta.name,
            "db",
            _contract_target(meta.contract),
            meta.archived_at is not None,
        ))
    for name, target in yaml_rows:
        rows.append((name, "yaml", target, False))

    # Column widths sized from data, capped so a very long repo
    # name doesn't blow up the format.
    name_w = max(len("NAME"), *(len(r[0]) for r in rows))
    src_w = max(len("SOURCE"), *(len(r[1]) for r in rows))
    click.echo(f"{'NAME':<{name_w}}  {'SOURCE':<{src_w}}  TARGET")
    for name, src, target, archived in sorted(rows):
        suffix = "  [archived]" if archived else ""
        click.echo(f"{name:<{name_w}}  {src:<{src_w}}  {target}{suffix}")


def _contract_target(contract) -> str:
    """One-line summary of what the contract is fetching — GitHub
    `repo` or Jira `jira_project @ jira_url`. Carried in the listing
    so an operator can match name → source without opening the YAML."""
    if contract.source == "github":
        return contract.repo or "(no repo set)"
    if contract.source == "jira":
        return f"{contract.jira_project} @ {contract.jira_url}"
    return contract.source


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
