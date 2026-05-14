"""Human-readable terminal output using `rich`.

Layout for every report type — answer first, detail last:
    1. Headline (one-sentence panel)
    2. Definition (what this report measures)
    3. Key numbers (percentiles or summary stats)
    4. Key insight (actionable interpretation)
    5. Next actions
    6. Caveats
    ─── detail divider ───
    7. Input parameters
    8. Training window (forecast reports only)
    9. Reproduce command
   10. Per-PR breakdown (efficiency reports only)
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from ..report import (
    AgingReport,
    CfdReport,
    EfficiencyReport,
    HowManyReport,
    Report,
    WhenDoneReport,
    cli_invocation,
    report_definition,
    report_vocabulary,
)


def _fmt_duration(td: timedelta) -> str:
    hours = td.total_seconds() / 3600
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def render(
    report: Report,
    console: Console | None = None,
    *,
    verbose: bool = False,
) -> str:
    """Render to a string. Default is a one-line headline (terse, pipeable).

    Pass `verbose=True` for the full report (tables, interpretation,
    detail block, vocabulary). The verbose form is what you'd pipe to a
    file or read in a terminal.
    """
    buf = StringIO()
    console = console or Console(file=buf, force_terminal=False, width=100)
    if not verbose:
        # Terse: just the one-sentence headline. No rich styling, no panel
        # borders — pipeable to less / grep / a file viewer.
        console.print(report.interpretation.headline)
        return buf.getvalue()

    if isinstance(report, EfficiencyReport):
        _render_efficiency(report, console)
    elif isinstance(report, WhenDoneReport):
        _render_when_done(report, console)
    elif isinstance(report, HowManyReport):
        _render_how_many(report, console)
    elif isinstance(report, CfdReport):
        _render_cfd(report, console)
    elif isinstance(report, AgingReport):
        _render_aging(report, console)
    else:  # pragma: no cover
        raise TypeError(f"unknown report type: {type(report).__name__}")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------


def _top(console: Console, report: Report) -> None:
    """Headline + definition — every report starts here."""
    console.print(Panel.fit(report.interpretation.headline, style="bold cyan"))
    console.print(Panel(report_definition(report), title="What this shows", style="blue"))


def _insight_and_actions(console: Console, report: Report) -> None:
    console.print(Panel(report.interpretation.key_insight, title="Key insight", style="yellow"))
    if report.interpretation.next_actions:
        console.print("[bold]Next actions[/bold]")
        for i, action in enumerate(report.interpretation.next_actions, 1):
            console.print(f"  {i}. {action}")
    if report.interpretation.caveats:
        console.print("[dim]Caveats[/dim]")
        for caveat in report.interpretation.caveats:
            console.print(f"  - {caveat}")


def _detail_divider(console: Console) -> None:
    console.print(Rule(title="Detail", style="dim", align="left"))


def _input_table(report: Report, rows: list[tuple[str, str]]) -> Table:
    inp = Table(title="Input parameters", show_header=False)
    inp.add_column("k", style="dim")
    inp.add_column("v")
    for label, value in rows:
        inp.add_row(label, value)
    return inp


def _reproduce(console: Console, report: Report) -> None:
    console.print("[dim]Reproduce this report[/dim]")
    console.print(f"  {cli_invocation(report)}", style="cyan")


def _vocabulary(console: Console, report: Report) -> None:
    console.print("[dim]Vocabulary used in this report (Vacanti's terms)[/dim]")
    for term, defn in report_vocabulary(report).items():
        console.print(f"  [bold]{term}[/bold] — {defn}", style="dim")


# ---------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------


def _render_efficiency(report: EfficiencyReport, console: Console) -> None:
    _top(console, report)
    r = report.result

    if r.pr_count == 0:
        _insight_and_actions(console, report)
        _detail_divider(console)
        _reproduce(console, report)
        return

    # ── Key numbers ──────────────────────────────────────────────
    summary = Table(title="Headline numbers")
    summary.add_column("Metric")
    summary.add_column("Value", justify="right")
    summary.add_row("PRs merged", str(r.pr_count))
    summary.add_row("Portfolio flow efficiency", f"{r.portfolio_efficiency * 100:.1f}%")
    summary.add_row("Total cycle time", _fmt_duration(r.total_cycle))
    summary.add_row("Total active time", _fmt_duration(r.total_active))
    console.print(summary)

    _insight_and_actions(console, report)
    _detail_divider(console)

    # ── Detail block ─────────────────────────────────────────────
    rows = [
        ("Repo", report.input.repo),
        ("Window", f"{report.input.start} → {report.input.stop}"),
    ]
    console.print(_input_table(report, rows))
    console.print(
        f"How active time was calculated: events more than "
        f"[bold]{report.input.gap_hours}h[/bold] apart start a new work session; "
        f"a single isolated event counts as at least "
        f"[bold]{report.input.min_cluster_minutes}min[/bold] of active time.",
        style="dim",
    )
    _reproduce(console, report)
    _vocabulary(console, report)

    # ── Appendix: per-PR distribution + slowest list ─────────────
    console.print("[dim]Per-PR distribution (the portfolio number above is the right one)[/dim]")
    appendix = Table(show_header=False, box=None)
    appendix.add_column("k", style="dim")
    appendix.add_column("v")
    appendix.add_row("Median per-PR FE", f"{r.median_efficiency * 100:.1f}%")
    appendix.add_row("Mean per-PR FE", f"{r.mean_efficiency * 100:.1f}%  (noisy — see docs)")
    console.print(appendix)

    pr_table = Table(title="Per-PR breakdown (slowest first)")
    pr_table.add_column("#")
    pr_table.add_column("Cycle", justify="right")
    pr_table.add_column("Active", justify="right")
    pr_table.add_column("FE", justify="right")
    pr_table.add_column("Title")
    for p in sorted(r.per_pr, key=lambda p: p.efficiency):
        pr_table.add_row(
            f"{p.item_id}",
            _fmt_duration(p.cycle_time),
            _fmt_duration(p.active_time),
            f"{p.efficiency * 100:.1f}%",
            p.title[:60],
        )
    console.print(pr_table)


# ---------------------------------------------------------------------------
# Forecast: when-done
# ---------------------------------------------------------------------------


def _render_when_done(report: WhenDoneReport, console: Console) -> None:
    _top(console, report)

    pct = Table(title="Confidence — by what date will all items be done? (read FORWARD)")
    pct.add_column("Confidence")
    pct.add_column("Completion date")
    for p in [50, 70, 85, 95]:
        pct.add_row(f"{p}%", str(report.percentiles[p]))
    console.print(pct)
    console.print("(For the full distribution chart, use --format html.)", style="dim")

    _insight_and_actions(console, report)
    _detail_divider(console)

    rows = [
        ("Repo", report.input.repo),
        ("Items to complete", str(report.input.items)),
        ("Forecast start", report.input.start_date.isoformat()),
        ("Runs", f"{report.simulation.runs:,}"),
        ("Seed", str(report.simulation.seed) if report.simulation.seed is not None else "random"),
    ]
    console.print(_input_table(report, rows))

    t = report.training
    train = Table(title="Training window — historical throughput we sampled from")
    train.add_column("Metric")
    train.add_column("Value", justify="right")
    train.add_row("Window", f"{t.window_start} → {t.window_end} ({len(t.daily_samples)} days)")
    train.add_row("Total throughput (items)", str(t.total_merges))
    train.add_row("Average throughput / day", f"{t.avg_per_day:.2f}")
    train.add_row("Zero-throughput days", f"{t.zero_days} of {len(t.daily_samples)}")
    console.print(train)

    _reproduce(console, report)
    _vocabulary(console, report)


# ---------------------------------------------------------------------------
# Forecast: how-many
# ---------------------------------------------------------------------------


def _render_cfd(report: CfdReport, console: Console) -> None:
    _top(console, report)

    if not report.points:
        _insight_and_actions(console, report)
        _detail_divider(console)
        _reproduce(console, report)
        return

    end = report.points[-1]
    summary = Table(title="Headline numbers")
    summary.add_column("Metric")
    summary.add_column("Value", justify="right")
    for state in report.input.workflow:
        summary.add_row(f"At end — {state}", str(end.counts_by_state.get(state, 0)))
    arrivals = end.counts_by_state.get(report.input.workflow[0], 0)
    departures = end.counts_by_state.get(report.input.workflow[-1], 0)
    summary.add_row("WIP at end", str(arrivals - departures))
    console.print(summary)
    console.print("(For the stacked-area chart, use --format html.)", style="dim")

    _insight_and_actions(console, report)
    _detail_divider(console)

    rows = [
        ("Repo", report.input.repo),
        ("Workflow", " → ".join(report.input.workflow)),
        ("Window", f"{report.input.start} → {report.input.stop}"),
        ("Sample interval (days)", str(report.input.interval_days)),
        ("Samples", str(len(report.points))),
    ]
    console.print(_input_table(report, rows))
    _reproduce(console, report)
    _vocabulary(console, report)


def _render_aging(report: AgingReport, console: Console) -> None:
    from ..aging import per_state_diagnostic, top_interventions

    _top(console, report)

    if not report.items:
        _detail_divider(console)
        _reproduce(console, report)
        return

    # Promote the divergence caveat — the most important signal-quality
    # warning — out of the caveat list and into a prominent banner.
    other_caveats: list[str] = []
    for c in report.interpretation.caveats:
        low = c.lower()
        if "diverge" in low or "doesn't resemble" in low:
            console.print(Panel(c, title="⚠ Signal quality", style="red"))
        else:
            other_caveats.append(c)

    # Interventions — the actionable list. One PR per stuck workflow
    # stage, rightmost-first, capped at 5.
    interventions = top_interventions(
        items=report.items,
        workflow=report.input.workflow,
        percentiles=report.cycle_time_percentiles,
    )
    if interventions:
        iv_table = Table(title="Highest-leverage interventions")
        iv_table.add_column("State")
        iv_table.add_column("#")
        iv_table.add_column("Age", justify="right")
        iv_table.add_column("Title")
        for iv in interventions:
            iv_table.add_row(
                iv["current_state"],
                iv["item_id"],
                f"{iv['age_days']}d",
                iv["title"][:60],
            )
        console.print(iv_table)
    else:
        console.print("[green]✓ No items past P85 — pipeline on track.[/green]")

    # Per-state diagnostic — bottleneck where age is accumulating.
    diag_rows = per_state_diagnostic(
        items=report.items,
        workflow=report.input.workflow,
        percentiles=report.cycle_time_percentiles,
    )
    diag = Table(title="Per-state aging")
    diag.add_column("State")
    diag.add_column("Count", justify="right")
    diag.add_column("Age P50", justify="right")
    diag.add_column("Oldest", justify="right")
    diag.add_column("Past P85", justify="right")
    diag.add_column("Past P95", justify="right")
    for row in diag_rows:
        diag.add_row(
            row["state"],
            str(row["count"]),
            "—" if row["median_age_days"] is None else f"{row['median_age_days']}d",
            "—" if row["oldest_age_days"] is None else f"{row['oldest_age_days']}d",
            str(row["past_p85"]),
            str(row["past_p95"]),
        )
    console.print(diag)
    console.print(
        f"[dim](Percentile thresholds from {report.completed_count} PRs completed "
        f"{report.input.history_start} → {report.input.history_end}; "
        f"P85={report.cycle_time_percentiles.get(85, 0):.1f}d, "
        f"P95={report.cycle_time_percentiles.get(95, 0):.1f}d.)[/dim]"
    )
    console.print("[dim](Interactive chart: use --format html.)[/dim]")

    # Remaining caveats (max-age exclusion notes, etc.) — kept terse.
    if other_caveats:
        console.print("[dim]Caveats[/dim]")
        for caveat in other_caveats:
            console.print(f"  - {caveat}", style="dim")

    _detail_divider(console)
    _reproduce(console, report)


def _render_how_many(report: HowManyReport, console: Console) -> None:
    _top(console, report)

    pct = Table(title="Confidence — minimum items we can commit to (read BACKWARD)")
    pct.add_column("Confidence")
    pct.add_column("Items", justify="right")
    for p in [50, 70, 85, 95]:
        pct.add_row(f"{p}%", str(report.percentiles[p]))
    console.print(pct)
    console.print("(For the full distribution chart, use --format html.)", style="dim")

    _insight_and_actions(console, report)
    _detail_divider(console)

    days = (report.input.target_date - report.input.start_date).days + 1
    rows = [
        ("Repo", report.input.repo),
        (
            "Forecast window",
            f"{report.input.start_date} → {report.input.target_date}  ({days} days)",
        ),
        ("Runs", f"{report.simulation.runs:,}"),
        ("Seed", str(report.simulation.seed) if report.simulation.seed is not None else "random"),
    ]
    console.print(_input_table(report, rows))

    t = report.training
    train = Table(title="Training window — historical throughput we sampled from")
    train.add_column("Metric")
    train.add_column("Value", justify="right")
    train.add_row("Window", f"{t.window_start} → {t.window_end} ({len(t.daily_samples)} days)")
    train.add_row("Total throughput (items)", str(t.total_merges))
    train.add_row("Average throughput / day", f"{t.avg_per_day:.2f}")
    train.add_row("Zero-throughput days", f"{t.zero_days} of {len(t.daily_samples)}")
    console.print(train)

    _reproduce(console, report)
    _vocabulary(console, report)
