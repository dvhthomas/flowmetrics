"""Single-file HTML report built with jinja2 + matplotlib charts.

Aging additionally renders an interactive Vega-Lite chart from the
report data. The Vega + Vega-Lite + Vega-Embed JS bundles are vendored
under ``static/`` and inlined into the document so the HTML renders
with no network connection.
"""

from __future__ import annotations

import base64
import io
import json
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from jinja2 import Environment, PackageLoader, select_autoescape

from ..report import (
    AgingReport,
    CfdReport,
    EfficiencyReport,
    HowManyReport,
    Report,
    WhenDoneReport,
    cli_invocation,
    forecast_horizon,
    report_definition,
    report_vocabulary,
)

_STATIC_DIR = Path(__file__).parent / "static"


@lru_cache(maxsize=1)
def _vega_bundle() -> str:
    """Concatenated Vega + Vega-Lite + Vega-Embed minified JS.

    Cached because reading 830KB off disk on every report render is
    wasteful. The bundle is identical for every report — the spec
    that uses it is what changes.
    """
    return "\n".join(
        (_STATIC_DIR / name).read_text()
        for name in ("vega.min.js", "vega-lite.min.js", "vega-embed.min.js")
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
    if isinstance(report, CfdReport):
        return _render_cfd(report)
    if isinstance(report, AgingReport):
        return _render_aging(report)
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
    """Chart-friendly axis label: ``May 19`` (or ``May 19 2026`` when the
    chart spans a year boundary). Space separator is more legible at
    small axis-tick sizes than a dash.

    Distinct from prose date formatting (`Jan 12, 2026`) used in
    headlines/insights. ISO stays in code/JSON/`cli_invocation`.
    """
    if include_year:
        return f"{d.strftime('%b')} {d.day} {d.year}"
    return f"{d.strftime('%b')} {d.day}"


def _needs_year_in_labels(dates: list[date]) -> bool:
    return bool(dates) and len({d.year for d in dates}) > 1


def _chart_per_pr(report: EfficiencyReport) -> str:
    from ..percentiles import chart_percentiles

    per = sorted(report.result.per_pr, key=lambda p: p.efficiency)
    fig, ax = plt.subplots(figsize=(9, max(2.5, len(per) * 0.25)))
    labels = [f"{p.item_id}" for p in per]
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
    # By cycle time descending — Top slowest list. Different sort than
    # per_pr_sorted (which sorts by efficiency for the full detail table).
    per_pr_by_cycle = sorted(
        report.result.per_pr, key=lambda p: p.cycle_time, reverse=True
    )
    return template.render(
        title=f"flowmetrics — efficiency {report.input.repo}",
        generated_at=report.generated_at,
        interpretation=report.interpretation,
        definition=report_definition(report),
        invocation=cli_invocation(report),
        vocabulary=report_vocabulary(report),
        report=report,
        per_pr_sorted=sorted(report.result.per_pr, key=lambda p: p.efficiency),
        per_pr_by_cycle=per_pr_by_cycle,
        pr_urls=_github_pr_urls(report.input.repo, [p.item_id for p in report.result.per_pr]),
        chart_per_pr_b64=_chart_per_pr(report) if report.result.pr_count else "",
    )


def _github_pr_urls(repo: str, item_ids: list[str]) -> dict[str, str]:
    """Best-effort URL builder. Returns {} for non-GitHub sources;
    template falls through to plain text in that case."""
    if not repo or "/" not in repo:
        return {}
    out: dict[str, str] = {}
    for item_id in item_ids:
        if item_id.startswith("#"):
            out[item_id] = f"https://github.com/{repo}/pull/{item_id.lstrip('#')}"
    return out


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


def _chart_cfd(report: CfdReport) -> str:
    """Stacked area CFD chart per Vacanti: arrivals on top, departures on
    bottom, intermediate states in between. The lines are drawn directly
    (the area between consecutive lines = WIP in that band)."""
    workflow = list(report.input.workflow)
    sample_dates = [p.sampled_on for p in report.points]
    include_year = _needs_year_in_labels(sample_dates)
    iso_dates = [d.isoformat() for d in sample_dates]
    pretty = [_human_date_label(d, include_year) for d in sample_dates]

    fig, ax = plt.subplots(figsize=(10, 5))

    # One series per workflow state, drawn from top (arrivals) to bottom
    # (departures). fill_between consecutive lines yields the stacked
    # bands that visualize WIP.
    series: list[list[int]] = [
        [p.counts_by_state.get(state, 0) for p in report.points] for state in workflow
    ]
    palette = [
        "#2b7cff",  # top = arrivals (blue)
        "#5ab2ff",
        "#a0d7ff",
        "#d4a72c",
        "#cc3333",
    ]
    for i, state in enumerate(workflow):
        color = palette[i % len(palette)]
        if i + 1 < len(workflow):
            ax.fill_between(
                iso_dates, series[i], series[i + 1], color=color, alpha=0.55, label=state
            )
        else:
            # Bottom band = the "departed" floor — fill from 0 to the last line.
            ax.fill_between(iso_dates, series[i], 0, color=color, alpha=0.55, label=state)
        ax.plot(iso_dates, series[i], color=color, linewidth=1.2)

    ax.set_ylabel("Items (cumulative)")
    ax.set_title(
        f"Cumulative Flow Diagram — {report.input.repo} "
        f"({sample_dates[0].isoformat()} → {sample_dates[-1].isoformat()})"
    )
    ax.legend(loc="upper left", fontsize=8)
    _thin_xticks(ax, pretty)
    fig.autofmt_xdate(rotation=45)
    fig.tight_layout()
    return _png_b64(fig)


def _render_cfd(report: CfdReport) -> str:
    template = _env.get_template("cfd.html.jinja")
    end_counts: dict[str, int] = (
        dict(report.points[-1].counts_by_state) if report.points else {}
    )
    return template.render(
        title=f"flowmetrics — cfd {report.input.repo}",
        generated_at=report.generated_at,
        interpretation=report.interpretation,
        definition=report_definition(report),
        invocation=cli_invocation(report),
        vocabulary=report_vocabulary(report),
        report=report,
        end_counts=end_counts,
        wip_trend=_cfd_wip_trend(report),
        chart_cfd_b64=_chart_cfd(report) if report.points else "",
    )


def _cfd_wip_trend(report: CfdReport) -> dict:
    """WIP gap (arrivals minus departures) at the start vs end of the window.
    The actionable read on a CFD: is the queue growing or shrinking?"""
    if not report.points:
        return {"direction": "flat", "start_wip": 0, "end_wip": 0, "delta": 0}
    workflow = report.input.workflow
    arrivals_state = workflow[0]
    departures_state = workflow[-1]
    first = report.points[0].counts_by_state
    last = report.points[-1].counts_by_state
    start_wip = first.get(arrivals_state, 0) - first.get(departures_state, 0)
    end_wip = last.get(arrivals_state, 0) - last.get(departures_state, 0)
    delta = end_wip - start_wip
    if delta > 0:
        direction = "widening"
    elif delta < 0:
        direction = "narrowing"
    else:
        direction = "flat"
    return {
        "direction": direction,
        "start_wip": start_wip,
        "end_wip": end_wip,
        "delta": delta,
    }


def _chart_aging(report: AgingReport) -> str:
    """Aging WIP scatter per Vacanti (WWIBD Figure 3.2): columns =
    workflow states, y = Age in days, percentile lines from completed
    cycle time as horizontal references."""
    import random as _r

    workflow = list(report.input.workflow)
    state_to_x = {state: i for i, state in enumerate(workflow)}

    # Place each in-flight item in its current-state column. Jitter x
    # slightly so overlapping ages stay readable (Vacanti uses faint
    # horizontal jitter on his charts too).
    jitter = _r.Random(42)
    xs: list[float] = []
    ys: list[float] = []
    labels: list[str] = []
    for it in report.items:
        x = state_to_x.get(it.current_state)
        if x is None:
            # Item is in a state outside the configured workflow — drop it
            # but the count will still show up in the diagnostic table.
            continue
        xs.append(x + (jitter.random() - 0.5) * 0.35)
        ys.append(it.age_days)
        labels.append(it.item_id)

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.scatter(xs, ys, color="#2b7cff", s=60, alpha=0.7, edgecolors="white")

    # Percentile reference lines
    pct = report.cycle_time_percentiles
    for p, color, style in [
        (50, "#999", ":"),
        (70, "#888", "-."),
        (85, "#d4a72c", "--"),
        (95, "#cc3333", "-"),
    ]:
        value = pct.get(p, 0.0)
        if value > 0:
            ax.axhline(
                value,
                color=color,
                linestyle=style,
                linewidth=1.0,
                label=f"P{p}: {value:.1f}d",
            )

    # WIP count callouts above each column
    wip_by_state: dict[str, int] = {}
    for it in report.items:
        wip_by_state[it.current_state] = wip_by_state.get(it.current_state, 0) + 1
    y_top = (max(ys) if ys else max(pct.values(), default=10)) * 1.1 + 1
    for state, x in state_to_x.items():
        count = wip_by_state.get(state, 0)
        ax.text(
            x, y_top, f"WIP: {count}",
            ha="center", va="bottom", fontsize=9, color="#444",
        )

    # Column separators
    for i in range(1, len(workflow)):
        ax.axvline(i - 0.5, color="#ddd", linewidth=0.8)

    ax.set_xticks(range(len(workflow)))
    ax.set_xticklabels(workflow, rotation=20, ha="right")
    ax.set_ylabel("Age (days)")
    ax.set_xlim(-0.6, len(workflow) - 0.4)
    ax.set_ylim(0, y_top + 2)
    ax.set_title(
        f"Aging Work In Progress — {report.input.repo} as of "
        f"{report.input.asof.isoformat()}"
    )
    ax.legend(loc="upper left", fontsize=8)
    fig.tight_layout()
    return _png_b64(fig)


def _render_aging(report: AgingReport) -> str:
    from ..aging import (
        compute_aging_distribution,
        per_state_diagnostic,
        top_interventions,
    )
    from ..interpretation import _prose_date
    from . import vega_specs

    template = _env.get_template("aging.html.jinja")

    # All caveats — including the divergence one — render together in
    # the "About this report" footer. No top-of-page red banner: the
    # information lives, the alarm doesn't.
    other_caveats: list[str] = list(report.interpretation.caveats)

    repo = report.input.repo
    repo_url = f"https://github.com/{repo}" if "/" in repo else None

    # Aging-distribution colors — ColorBrewer YlOrRd, color-blind-safe
    # sequential. The P85–P95 background is darkened from #f03b20 to
    # #d6361a so white text on it clears the WCAG AA contrast ratio
    # (4.5:1 for normal text) instead of only AA-large.
    band_colors = [
        ("Below P50", "#ffeda0", "#2b2b2b"),  # (band label, bg, text)
        ("P50–P70",   "#fed976", "#2b2b2b"),
        ("P70–P85",   "#fd8d3c", "#2b2b2b"),
        ("P85–P95",   "#d6361a", "#ffffff"),
        ("Above P95", "#a30019", "#ffffff"),
    ]
    dist = compute_aging_distribution(report.items, report.cycle_time_percentiles)
    aging_distribution_styled = [
        {
            **band,
            "bg": bg,
            "fg": fg,
        }
        for band, (_label, bg, fg) in zip(dist, band_colors, strict=False)
    ]

    # Top N (50) items past P85, sorted by age descending. Useful subset
    # of the in-flight list — a flat dump of 500+ items isn't helpful.
    p85 = report.cycle_time_percentiles.get(85, 0.0)
    past_p85_top: list = []
    if p85 > 0:
        candidates = [it for it in report.items if it.age_days >= p85]
        past_p85_top = sorted(
            candidates, key=lambda i: i.age_days, reverse=True
        )[:50]
    past_p85_total = sum(1 for it in report.items if p85 > 0 and it.age_days >= p85)

    # Short prose summary of the distribution, used as the bar's
    # aria-label so screen readers announce the shape textually.
    bar_aria = "Aging distribution by percentile band: " + "; ".join(
        f"{band['label']} {band['count']} ({round(band['share'] * 100)}%)"
        for band in aging_distribution_styled
        if band["count"] > 0
    )

    return template.render(
        title="Aging Work In Progress",
        repo_url=repo_url,
        prose_asof=_prose_date(report.input.asof),
        generated_at=report.generated_at,
        interpretation=report.interpretation,
        definition=report_definition(report),
        invocation=cli_invocation(report),
        vocabulary=report_vocabulary(report),
        report=report,
        aging_distribution=aging_distribution_styled,
        bar_aria=bar_aria,
        per_state_diagnostic=per_state_diagnostic(
            items=report.items,
            workflow=report.input.workflow,
            percentiles=report.cycle_time_percentiles,
        ),
        top_interventions=top_interventions(
            items=report.items,
            workflow=report.input.workflow,
            percentiles=report.cycle_time_percentiles,
        ),
        past_p85_top=past_p85_top,
        past_p85_total=past_p85_total,
        other_caveats=other_caveats,
        vega_bundle=_vega_bundle() if report.items else "",
        vega_spec_json=json.dumps(vega_specs.aging_spec(report)) if report.items else "",
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
