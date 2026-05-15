"""Single-file HTML report built with jinja2.

Every chart is rendered client-side from a Vega-Lite spec. Vega +
Vega-Lite + Vega-Embed are loaded from the jsdelivr CDN — pinned to
major version so a future minor/patch upgrade on their side is
automatic, but a major bump (which can break specs) requires us to
flip the pin.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

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
    report_title,
    report_vocabulary,
)


def _repo_url(repo: str) -> str | None:
    """GitHub URL for a `repo` label, or None for Jira / no-repo cases.

    Consolidates the duplicated guard scattered across `_render_*` —
    same predicate, same fallback. Jira labels carry `jira:` prefix
    and no slash, so the test is `/ in repo and not jira:`-prefixed.
    """
    if not repo or "/" not in repo or repo.startswith("jira:"):
        return None
    return f"https://github.com/{repo}"


def _safe_json_for_script_tag(obj: object) -> str:
    """JSON-serialize for inlining inside an HTML <script> tag.

    `json.dumps()` does not escape `<` or `>`, so a string containing
    `</script>` (e.g. a malicious PR title) would close the surrounding
    script tag and inject arbitrary JS. Escape `<`, `>`, and `&` to
    their `\\uXXXX` JSON escape sequences — valid JSON, safe HTML.
    """
    return (
        # ensure_ascii=False keeps multibyte chars (arrows, glyphs)
        # readable in the source HTML; we still escape the three chars
        # that would break out of a script tag.
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
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
# Per-report
# ---------------------------------------------------------------------------


def _render_efficiency(report: EfficiencyReport) -> str:
    from . import vega_specs

    template = _env.get_template("efficiency.html.jinja")
    per_pr_by_cycle = sorted(
        report.result.per_pr, key=lambda p: p.cycle_time, reverse=True
    )
    repo_url = _repo_url(report.input.repo)
    return template.render(
        title=report_title(report),
        repo_url=repo_url,
        generated_at=report.generated_at,
        interpretation=report.interpretation,
        definition=report_definition(report),
        invocation=cli_invocation(report),
        vocabulary=report_vocabulary(report),
        report=report,
        per_pr_sorted=sorted(report.result.per_pr, key=lambda p: p.efficiency),
        per_pr_by_cycle=per_pr_by_cycle,
        pr_urls=_github_pr_urls(report.input.repo, [p.item_id for p in report.result.per_pr]),
        vega_spec_json=(
            _safe_json_for_script_tag(vega_specs.efficiency_spec(report))
            if report.result.pr_count else ""
        ),
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
    from . import vega_specs

    template = _env.get_template("when_done.html.jinja")
    repo_url = _repo_url(report.input.repo)
    return template.render(
        title=report_title(report),
        repo_url=repo_url,
        generated_at=report.generated_at,
        interpretation=report.interpretation,
        definition=report_definition(report),
        invocation=cli_invocation(report),
        vocabulary=report_vocabulary(report),
        horizon=forecast_horizon(report),
        report=report,
        training=report.training,
        vega_spec_json=(
            _safe_json_for_script_tag(vega_specs.when_done_spec(report))
            if report.histogram.counts else ""
        ),
    )


def _render_cfd(report: CfdReport) -> str:
    from . import vega_specs

    template = _env.get_template("cfd.html.jinja")
    repo_url = _repo_url(report.input.repo)
    return template.render(
        title=report_title(report),
        repo_url=repo_url,
        generated_at=report.generated_at,
        interpretation=report.interpretation,
        definition=report_definition(report),
        invocation=cli_invocation(report),
        vocabulary=report_vocabulary(report),
        report=report,
        vega_spec_json=(
            _safe_json_for_script_tag(vega_specs.cfd_spec(report))
            if report.points else ""
        ),
    )


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

    repo_url = _repo_url(report.input.repo)

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
        title=report_title(report),
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
        vega_spec_json=(
            _safe_json_for_script_tag(vega_specs.aging_spec(report))
            if report.items else ""
        ),
        aging_distribution_spec_json=(
            _safe_json_for_script_tag(vega_specs.aging_distribution_spec(report))
            if report.items else ""
        ),
    )


def _render_how_many(report: HowManyReport) -> str:
    from . import vega_specs

    template = _env.get_template("how_many.html.jinja")
    repo_url = _repo_url(report.input.repo)
    return template.render(
        title=report_title(report),
        repo_url=repo_url,
        generated_at=report.generated_at,
        interpretation=report.interpretation,
        definition=report_definition(report),
        invocation=cli_invocation(report),
        vocabulary=report_vocabulary(report),
        horizon=forecast_horizon(report),
        report=report,
        training=report.training,
        vega_spec_json=(
            _safe_json_for_script_tag(vega_specs.how_many_spec(report))
            if report.histogram.counts else ""
        ),
    )
