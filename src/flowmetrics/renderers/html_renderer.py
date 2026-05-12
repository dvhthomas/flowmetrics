"""Single-file HTML report built with jinja2 + matplotlib charts."""

from __future__ import annotations

import base64
import io
from datetime import date, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from jinja2 import Environment, PackageLoader, select_autoescape

from ..report import (
    EfficiencyReport,
    HowManyReport,
    Report,
    WhenDoneReport,
    cli_invocation,
    forecast_horizon,
    report_definition,
    report_vocabulary,
)


def _fmt_duration(td: timedelta) -> str:
    hours = td.total_seconds() / 3600
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


_env = Environment(
    loader=PackageLoader("flowmetrics.renderers", "templates"),
    autoescape=select_autoescape(["html", "jinja"]),
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.globals["fmt_duration"] = _fmt_duration  # ty: ignore[invalid-assignment]


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def default_output_path(report: Report, directory: Path | str = Path("reports")) -> Path:
    directory = Path(directory)
    repo_slug = report.input.repo.replace("/", "_")
    cmd_slug = report.command.replace(" ", "-")
    stamp = report.generated_at.strftime("%Y%m%d-%H%M%S")
    return directory / f"flow-{cmd_slug}-{repo_slug}-{stamp}.html"


def render(report: Report) -> str:
    if isinstance(report, EfficiencyReport):
        return _render_efficiency(report)
    if isinstance(report, WhenDoneReport):
        return _render_when_done(report)
    if isinstance(report, HowManyReport):
        return _render_how_many(report)
    raise TypeError(f"unknown report type: {type(report).__name__}")  # pragma: no cover


def render_to_file(report: Report, path: Path | str | None = None) -> Path:
    out = Path(path) if path else default_output_path(report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(report), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------


def _png_b64(fig: plt.Figure) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", dpi=120)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _thin_xticks(ax, labels: list[str], n_max: int = 10) -> None:
    """Show at most n_max evenly-spaced ticks. Prevents the date axis
    from collapsing into an overlapping wall of slanted labels when the
    series is long (forecast histograms can span 60+ days)."""
    if len(labels) <= n_max:
        return
    step = max(1, len(labels) // n_max)
    positions = list(range(0, len(labels), step))
    ax.set_xticks(positions)
    ax.set_xticklabels([labels[i] for i in positions])


def _human_date_label(d: date, include_year: bool) -> str:
    """Chart-friendly date label: `May-19` (or `May-19 2026` across year boundaries).

    ISO dates stay in code / JSON / `cli_invocation`; human-readable goes
    on chart axes and legend labels.
    """
    return d.strftime("%b-%d %Y" if include_year else "%b-%d")


def _needs_year_in_labels(dates: list[date]) -> bool:
    return bool(dates) and len({d.year for d in dates}) > 1


def _chart_per_pr(report: EfficiencyReport) -> str:
    from ..percentiles import chart_percentiles

    per = sorted(report.result.per_pr, key=lambda p: p.efficiency)
    fig, ax = plt.subplots(figsize=(9, max(2.5, len(per) * 0.25)))
    labels = [f"#{p.pr_number}" for p in per]
    values = [p.efficiency * 100 for p in per]

    def _color(v: float, is_bot: bool) -> str:
        if is_bot:
            return "#bbbbbb"  # bot: muted grey
        return "#cc3333" if v < 10 else "#d4a72c" if v < 50 else "#2ca02c"

    colors = [_color(v, p.is_bot) for v, p in zip(values, per, strict=True)]
    ax.barh(labels, values, color=colors)
    ax.set_xlabel("Flow efficiency (%)")
    ax.set_title("Per-PR flow efficiency (slowest at top; bot PRs in grey)")
    ax.invert_yaxis()

    # Portfolio FE: the system-level reference
    pf = report.result.portfolio_efficiency * 100
    ax.axvline(
        pf, color="#2b7cff", linestyle="--", linewidth=1.8, label=f"Portfolio FE ({pf:.1f}%)"
    )
    # Per-PR percentile lines (50/70/85/95 of the FE distribution itself)
    pct = chart_percentiles(values)
    for p, color, style in [
        (50, "#999", ":"),
        (70, "#888", "-."),
        (85, "#d4a72c", "--"),
        (95, "#cc3333", "-"),
    ]:
        ax.axvline(
            pct[p], color=color, linestyle=style, linewidth=1.0, label=f"P{p}: {pct[p]:.1f}%"
        )
    ax.legend(loc="lower right", fontsize=8)
    fig.tight_layout()
    return _png_b64(fig)


def _chart_when_done_histogram(report: WhenDoneReport) -> str:
    sorted_dates = report.histogram.sorted_keys
    # x-axis values stay categorical (one bar per outcome day) so the
    # percentile axvlines line up with bar positions; but the *labels*
    # are human-friendly MON-dd.
    pct_dates = list(report.percentiles.values())
    include_year = _needs_year_in_labels(sorted_dates + pct_dates)
    iso_keys = [d.isoformat() for d in sorted_dates]
    pretty_keys = [_human_date_label(d, include_year) for d in sorted_dates]
    counts = [report.histogram.counts[d] for d in sorted_dates]

    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.bar(iso_keys, counts, color="#2b7cff", alpha=0.7)
    ax.set_xlabel("Completion date")
    ax.set_ylabel("Frequency")
    t = report.training
    ax.set_title(
        f"When will it be done? — trained on {len(t.daily_samples)} days of "
        f"throughput ({t.total_merges} items)"
    )
    for p, color, style in [
        (50, "#999", ":"),
        (70, "#888", "-."),
        (85, "#d4a72c", "--"),
        (95, "#cc3333", "-"),
    ]:
        d_p = report.percentiles[p]
        ax.axvline(
            d_p.isoformat(),  # ty: ignore[invalid-argument-type]
            color=color,
            linestyle=style,
            label=f"P{p}: {_human_date_label(d_p, include_year)}",
        )
    ax.legend(loc="upper right", fontsize=8)
    _thin_xticks(ax, pretty_keys)
    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    return _png_b64(fig)


def _chart_how_many_histogram(report: HowManyReport) -> str:
    keys = list(report.histogram.sorted_keys)
    counts = [report.histogram.counts[n] for n in keys]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.bar(keys, counts, color="#2b7cff", alpha=0.7)
    ax.set_xlabel("Items completed")
    ax.set_ylabel("Frequency")
    t = report.training
    ax.set_title(
        f"How many items? — trained on {len(t.daily_samples)} days of "
        f"throughput ({t.total_merges} items) — read percentiles BACKWARD"
    )
    for p, color, style in [
        (50, "#999", ":"),
        (70, "#888", "-."),
        (85, "#d4a72c", "--"),
        (95, "#cc3333", "-"),
    ]:
        ax.axvline(
            report.percentiles[p],
            color=color,
            linestyle=style,
            label=f"{p}%: {report.percentiles[p]} items",
        )
    ax.legend(loc="upper right")
    fig.tight_layout()
    return _png_b64(fig)


def _chart_training(report: WhenDoneReport | HowManyReport) -> str:
    from datetime import timedelta as _td

    from ..percentiles import chart_percentiles

    samples = report.training.daily_samples
    day_dates = [report.training.window_start + _td(days=i) for i in range(len(samples))]
    include_year = _needs_year_in_labels(day_dates)
    iso_days = [d.isoformat() for d in day_dates]
    pretty_days = [_human_date_label(d, include_year) for d in day_dates]
    fig, ax = plt.subplots(figsize=(10, 3))
    ax.bar(iso_days, samples, color="#888", alpha=0.7)
    ax.axhline(
        report.training.avg_per_day,
        color="#2b7cff",
        linestyle="--",
        linewidth=1.5,
        label=f"Avg: {report.training.avg_per_day:.2f}/day",
    )
    # Throughput percentile bands across the historical sample
    pct = chart_percentiles(samples)
    for p, color, style in [
        (50, "#999", ":"),
        (70, "#888", "-."),
        (85, "#d4a72c", "--"),
        (95, "#cc3333", "-"),
    ]:
        ax.axhline(pct[p], color=color, linestyle=style, linewidth=1.0, label=f"P{p}: {pct[p]}")
    ax.set_title("Training window: daily throughput")
    ax.set_ylabel("Items merged")
    ax.legend(loc="upper right", fontsize=8)
    _thin_xticks(ax, pretty_days)
    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    return _png_b64(fig)


# ---------------------------------------------------------------------------
# Per-report
# ---------------------------------------------------------------------------


def _render_efficiency(report: EfficiencyReport) -> str:
    template = _env.get_template("efficiency.html.jinja")
    return template.render(
        title=f"flowmetrics — efficiency {report.input.repo}",
        generated_at=report.generated_at,
        interpretation=report.interpretation,
        definition=report_definition(report),
        invocation=cli_invocation(report),
        vocabulary=report_vocabulary(report),
        report=report,
        per_pr_sorted=sorted(report.result.per_pr, key=lambda p: p.efficiency),
        chart_per_pr_b64=_chart_per_pr(report) if report.result.pr_count else "",
    )


def _render_when_done(report: WhenDoneReport) -> str:
    template = _env.get_template("when_done.html.jinja")
    return template.render(
        title=f"flowmetrics — when-done {report.input.repo}",
        generated_at=report.generated_at,
        interpretation=report.interpretation,
        definition=report_definition(report),
        invocation=cli_invocation(report),
        vocabulary=report_vocabulary(report),
        horizon=forecast_horizon(report),
        report=report,
        training=report.training,
        chart_training_b64=_chart_training(report),
        chart_histogram_b64=_chart_when_done_histogram(report),
    )


def _render_how_many(report: HowManyReport) -> str:
    template = _env.get_template("how_many.html.jinja")
    return template.render(
        title=f"flowmetrics — how-many {report.input.repo}",
        generated_at=report.generated_at,
        interpretation=report.interpretation,
        definition=report_definition(report),
        invocation=cli_invocation(report),
        vocabulary=report_vocabulary(report),
        horizon=forecast_horizon(report),
        report=report,
        training=report.training,
        chart_training_b64=_chart_training(report),
        chart_histogram_b64=_chart_how_many_histogram(report),
    )
