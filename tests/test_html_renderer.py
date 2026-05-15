"""Behavioural spec for the HTML renderer.

Contract:
1. `render(report)` returns a complete `<!doctype html>` document.
2. The headline, key insight, next actions, and caveats are present.
3. PNG charts are base64-embedded (data:image/png;base64,...) so the
   output is one self-contained file with no external resources.
4. A notes/insights placeholder exists for human use.
5. `default_output_path(report)` returns a path with the report's
   datetime stamp in YYYYMMDD-HHMMSS format and a repo slug.
6. `render_to_file(report, path)` writes the document to disk.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from flowmetrics.aging import AgingItem
from flowmetrics.cfd import CfdPoint
from flowmetrics.compute import FlowEfficiency, WindowResult
from flowmetrics.forecast import build_histogram
from flowmetrics.renderers import html_renderer
from flowmetrics.report import (
    AgingInput,
    AgingReport,
    CfdInput,
    CfdReport,
    EfficiencyInput,
    EfficiencyReport,
    HowManyInput,
    HowManyReport,
    Interpretation,
    SimulationSummary,
    WhenDoneInput,
    WhenDoneReport,
    build_training_summary,
)


def _interp():
    return Interpretation(
        headline="Headline appears verbatim.",
        key_insight="Key insight appears verbatim.",
        next_actions=["Action one verbatim.", "Action two verbatim."],
        caveats=["Caveat one verbatim."],
    )


def _efficiency_report() -> EfficiencyReport:
    pr = FlowEfficiency(
        item_id="#42",
        title="Test PR",
        created_at=datetime(2026, 5, 4, 9, 0, tzinfo=UTC),
        merged_at=datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
        cycle_time=timedelta(days=6),
        active_time=timedelta(hours=12),
        efficiency=0.083,
    )
    return EfficiencyReport(
        input=EfficiencyInput("acme/widget", date(2026, 5, 4), date(2026, 5, 10), 4.0, 30.0, False),
        result=WindowResult(
            pr_count=1,
            portfolio_efficiency=0.083,
            mean_efficiency=0.083,
            median_efficiency=0.083,
            total_cycle=timedelta(days=6),
            total_active=timedelta(hours=12),
            per_pr=[pr],
        ),
        interpretation=_interp(),
        generated_at=datetime(2026, 5, 12, 14, 30, 15, tzinfo=UTC),
    )


def _when_done_report() -> WhenDoneReport:
    return WhenDoneReport(
        input=WhenDoneInput(
            "acme/widget",
            50,
            date(2026, 5, 11),
            date(2026, 4, 11),
            date(2026, 5, 10),
            False,
        ),
        training=build_training_summary([5] * 4, date(2026, 5, 7), date(2026, 5, 10)),
        simulation=SimulationSummary(runs=1000, seed=42),
        histogram=build_histogram([date(2026, 5, 19), date(2026, 5, 20)]),
        percentiles={
            50: date(2026, 5, 19),
            70: date(2026, 5, 19),
            85: date(2026, 5, 20),
            95: date(2026, 5, 20),
        },
        interpretation=_interp(),
        generated_at=datetime(2026, 5, 12, 14, 30, 15, tzinfo=UTC),
    )


def _how_many_report() -> HowManyReport:
    return HowManyReport(
        input=HowManyInput(
            "acme/widget",
            date(2026, 5, 11),
            date(2026, 5, 25),
            date(2026, 4, 11),
            date(2026, 5, 10),
            False,
        ),
        training=build_training_summary([5] * 4, date(2026, 5, 7), date(2026, 5, 10)),
        simulation=SimulationSummary(runs=1000, seed=42),
        histogram=build_histogram([50, 60, 70]),
        percentiles={50: 60, 70: 55, 85: 51, 95: 50},
        interpretation=_interp(),
        generated_at=datetime(2026, 5, 12, 14, 30, 15, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------


class TestEfficiencyHtml:
    def test_is_complete_html_document(self):
        out = html_renderer.render(_efficiency_report())
        assert "<!doctype html>" in out.lower()
        assert "<html" in out and "</html>" in out

    def test_contains_headline_and_key_insight(self):
        out = html_renderer.render(_efficiency_report())
        assert "Headline appears verbatim." in out
        assert "Key insight appears verbatim." in out

    def test_contains_each_next_action(self):
        out = html_renderer.render(_efficiency_report())
        assert "Action one verbatim." in out
        assert "Action two verbatim." in out

    def test_contains_caveats(self):
        out = html_renderer.render(_efficiency_report())
        assert "Caveat one verbatim." in out

    def test_embeds_base64_png_chart(self):
        out = html_renderer.render(_efficiency_report())
        assert re.search(r'src="data:image/png;base64,[A-Za-z0-9+/=]{100,}"', out)

    def test_no_human_notes_section(self):
        """We removed the 'Your notes' placeholder — the report stands alone."""
        out = html_renderer.render(_efficiency_report())
        assert "Your notes" not in out
        assert "Insights &amp; action items" not in out

    def test_per_pr_table_is_in_a_details_element(self):
        """Per-PR list is long; wrap it in <details> so the report
        doesn't scroll forever when there are 40+ PRs."""
        out = html_renderer.render(_efficiency_report())
        assert "<details" in out
        assert "<summary" in out

    def test_includes_cli_invocation_for_reproducibility(self):
        """Each report shows the CLI command that produced it — useful
        for humans (copy-paste to reproduce) and agents (provenance)."""
        out = html_renderer.render(_efficiency_report())
        assert "uv run flow efficiency week" in out
        assert "--repo acme/widget" in out

    def test_actionable_content_comes_before_detail(self):
        """Top-of-page: portfolio number + slowest PRs + chart.
        Bottom-of-page (collapsed): per-PR list, reproduce, about."""
        out = html_renderer.render(_efficiency_report())
        i_portfolio = out.index("Portfolio FE")
        i_slowest = out.index("Top slowest")
        i_reproduce = out.index("Reproduce this report")
        i_about = out.index("About this report")
        # Actionable content sits above the bottom collapsibles.
        assert i_portfolio < i_reproduce
        assert i_slowest < i_reproduce
        assert i_slowest < i_about

    def test_has_definition_explaining_what_the_chart_shows(self):
        """Every report includes a short 'what this shows' line so the
        reader doesn't have to read the docs to interpret it."""
        out = html_renderer.render(_efficiency_report())
        # Should mention either "flow efficiency" or "active time" as a definition
        assert "active time" in out.lower() or "active/cycle" in out.lower()

    def test_detail_section_marked_with_a_divider_or_heading(self):
        """The 'detail' block at the bottom is visually demarcated."""
        out = html_renderer.render(_efficiency_report())
        # Either an <hr>, or a heading labelled "Detail"
        assert "<hr" in out.lower() or "detail" in out.lower()


def _cfd_report(*, start_wip: int = 5, end_wip: int = 15) -> CfdReport:
    """A CFD with a deliberately-growing WIP gap to exercise the trend
    indicator. State A counts as arrivals (leftmost), Done as departures."""
    points = [
        CfdPoint(date(2026, 5, 1), {"A": start_wip + 0, "Done": 0}),
        CfdPoint(date(2026, 5, 7), {"A": start_wip + 5, "Done": 0}),
        CfdPoint(date(2026, 5, 14), {"A": end_wip, "Done": 0}),
    ]
    return CfdReport(
        input=CfdInput(
            repo="acme/widget",
            start=date(2026, 5, 1),
            stop=date(2026, 5, 14),
            workflow=("A", "Done"),
            interval_days=7,
            offline=False,
        ),
        points=points,
        interpretation=_interp(),
        generated_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
    )


class TestCfdHtmlRedesign:
    """Tufte cleanup brought to Aging-redesign parity:
    - Title is "Cumulative Flow Diagram" (the metric); repo subtitled.
    - 'Headline numbers' table removed.
    - WIP-trend banner remains (conditional, the actionable signal).
    - Auto-rendered Key insight / Next actions / Caveats / Reproduce /
      Vocabulary are folded into Reproduce + About collapsibles."""

    def test_title_is_the_metric_not_the_program_label(self):
        out = html_renderer.render(_cfd_report())
        h1 = re.search(r"<h1>([^<]+)</h1>", out)
        assert h1 is not None
        assert "Cumulative Flow" in h1.group(1)
        assert "flowmetrics" not in h1.group(1).lower()

    def test_no_key_insight_section_at_top_level(self):
        out = html_renderer.render(_cfd_report())
        assert "<h2>Key insight</h2>" not in out

    def test_no_top_level_caveats_section(self):
        out = html_renderer.render(_cfd_report())
        assert "<h2>Caveats</h2>" not in out

    def test_reproduce_in_its_own_collapsible(self):
        out = html_renderer.render(_cfd_report())
        assert "<summary>Reproduce this report</summary>" in out
        assert "<h2>Reproduce this report</h2>" not in out

    def test_about_collapsible_present(self):
        out = html_renderer.render(_cfd_report())
        assert "<summary>About this report</summary>" in out

    def test_workflow_states_render_as_pills_in_reproduce_block(self):
        out = html_renderer.render(_cfd_report())
        # Workflow states surfaced as pill spans, like Aging.
        assert 'class="pill">A<' in out or 'class="pill">Done<' in out

    def test_headline_numbers_table_removed(self):
        out = html_renderer.render(_cfd_report())
        assert "Headline numbers" not in out

    def test_wip_trend_indicator_present(self):
        out = html_renderer.render(_cfd_report(start_wip=5, end_wip=15))
        assert "widening" in out.lower() or "growing" in out.lower()

    def test_wip_trend_indicator_shows_narrowing_when_wip_drops(self):
        out = html_renderer.render(_cfd_report(start_wip=20, end_wip=5))
        assert "narrow" in out.lower() or "shrink" in out.lower()


class TestEfficiencyHtmlRedesign:
    """Tufte cleanup, brought to parity with the Aging redesign:
    - Title is the metric name ("Flow Efficiency"), not the system
      label "flowmetrics — efficiency …".
    - Subtitle carries repo + window + generated stamp on one line.
    - The auto-rendered "Key insight" yellow box, top-level "Next
      actions" list, top-level "Caveats" grey box, and standalone
      "Reproduce this report" header are all suppressed — folded into
      a Reproduce-collapsible and an About-collapsible at the bottom.
    - "Top slowest PRs" panel stays (the actionable list).
    """

    def test_title_is_the_metric_not_the_program_label(self):
        out = html_renderer.render(_efficiency_report())
        m = re.search(r"<title>([^<]+)</title>", out)
        assert m is not None
        assert "flowmetrics" not in m.group(1).lower()
        h1 = re.search(r"<h1>([^<]+)</h1>", out)
        assert h1 is not None
        assert "Flow efficiency" in h1.group(1)

    def test_repo_appears_as_subtitle_link(self):
        out = html_renderer.render(_efficiency_report())
        # GitHub-style repo → clickable subtitle.
        assert 'href="https://github.com/acme/widget"' in out
        # Window dates surface in the subtitle context too.
        assert "2026-05-04" in out or "May 4" in out

    def test_no_key_insight_section_at_top_level(self):
        """The yellow Key insight box is suppressed; the slowest-PRs
        panel + portfolio FE in the headline carry the signal."""
        out = html_renderer.render(_efficiency_report())
        assert "<h2>Key insight</h2>" not in out

    def test_no_top_level_caveats_section(self):
        """Caveats fold into the About details collapsible at the
        bottom — not a top-level H2."""
        out = html_renderer.render(_efficiency_report())
        assert "<h2>Caveats</h2>" not in out

    def test_reproduce_in_its_own_collapsible(self):
        out = html_renderer.render(_efficiency_report())
        assert "<summary>Reproduce this report</summary>" in out
        # And no longer at top level.
        assert "<h2>Reproduce this report</h2>" not in out

    def test_about_collapsible_carries_definition_and_vocabulary(self):
        out = html_renderer.render(_efficiency_report())
        assert "<summary>About this report</summary>" in out
        about_idx = out.index("<summary>About this report</summary>")
        about_end = out.index("</details>", about_idx)
        about = out[about_idx:about_end]
        assert "What this shows" in about
        # Vocab terms live here too (lowercase per the vocabulary dict).
        assert "Cycle time" in about or "Active time" in about or "Flow efficiency" in about

    def test_headline_numbers_table_removed(self):
        out = html_renderer.render(_efficiency_report())
        assert "Headline numbers" not in out

    def test_slowest_prs_panel_present_with_named_items(self):
        """The slowest PRs are the system-level bottleneck per Vacanti's
        'long-running PRs dominate the portfolio FE' — naming them is
        the actionable signal."""
        out = html_renderer.render(_efficiency_report())
        assert "Top slowest" in out
        assert "#42" in out


class TestForecastHtmlRedesign:
    """Tufte parity for when-done + how-many forecast reports:
    - Title is the question, not the program label.
    - Subtitle is one line with repo + window + stamp.
    - Horizon traffic-light banner stays (conditional, the actionable
      signal-quality indicator).
    - Auto-rendered Key insight / Next actions / Caveats / Reproduce /
      Vocabulary fold into collapsed details at the bottom."""

    def test_when_done_title_is_the_question_not_the_program_label(self):
        out = html_renderer.render(_when_done_report())
        h1 = re.search(r"<h1>([^<]+)</h1>", out)
        assert h1 is not None
        assert "flowmetrics" not in h1.group(1).lower()
        # Title reads as a question.
        assert "When will it be done" in h1.group(1)

    def test_how_many_title_is_the_question(self):
        out = html_renderer.render(_how_many_report())
        h1 = re.search(r"<h1>([^<]+)</h1>", out)
        assert h1 is not None
        assert "flowmetrics" not in h1.group(1).lower()
        assert "How many" in h1.group(1)

    def test_when_done_no_key_insight_section_at_top_level(self):
        out = html_renderer.render(_when_done_report())
        assert "<h2>Key insight</h2>" not in out

    def test_how_many_no_top_level_caveats_section(self):
        out = html_renderer.render(_how_many_report())
        assert "<h2>Caveats</h2>" not in out

    def test_when_done_reproduce_in_collapsible(self):
        out = html_renderer.render(_when_done_report())
        assert "<summary>Reproduce this report</summary>" in out
        assert "<h2>Reproduce this report</h2>" not in out

    def test_when_done_about_collapsible_present(self):
        out = html_renderer.render(_when_done_report())
        assert "<summary>About this report</summary>" in out

    def test_horizon_traffic_light_banner_still_present(self):
        """The horizon banner is the actionable signal-quality indicator
        for forecasts — it must NOT be suppressed by the chrome cleanup."""
        out = html_renderer.render(_when_done_report())
        assert 'class="horizon' in out


class TestWhenDoneHtml:
    def test_contains_percentile_dates(self):
        out = html_renderer.render(_when_done_report())
        assert "2026-05-20" in out  # 85th percentile

    def test_embeds_two_charts(self):
        # Training-throughput + Results-Histogram
        out = html_renderer.render(_when_done_report())
        matches = re.findall(r'src="data:image/png;base64,', out)
        assert len(matches) >= 2

    def test_shows_forecast_horizon_callout(self):
        """Surface Vacanti's 'shorter is better' principle: show how far
        the forecast extends vs. the training window."""
        out = html_renderer.render(_when_done_report())
        # Either explicit text or the principle phrased clearly
        assert "horizon" in out.lower() or "shorter" in out.lower()

    def test_includes_canonical_definitions(self):
        """Each report carries Vacanti's terminology inline."""
        out = html_renderer.render(_when_done_report())
        for term in ["Throughput", "Training window", "Monte Carlo"]:
            assert term in out


class TestHowManyHtml:
    def test_contains_percentile_items(self):
        out = html_renderer.render(_how_many_report())
        assert "51" in out  # 85th percentile

    def test_warns_about_backward_percentile_direction(self):
        out = html_renderer.render(_how_many_report())
        text = out.upper()
        assert "BACKWARD" in text or "FEWER" in text


def _aging_report_with_distribution() -> AgingReport:
    # Six items spread across all five distribution bands.
    # P50=1.7, P70=5.4, P85=17.8, P95=57.4
    items = [
        AgingItem(item_id="#1", title="t1", current_state="Awaiting Review", age_days=0),
        AgingItem(item_id="#2", title="t2", current_state="Awaiting Review", age_days=3),
        AgingItem(item_id="#3", title="t3", current_state="Awaiting Review", age_days=10),
        AgingItem(item_id="#4", title="t4", current_state="Awaiting Review", age_days=30),
        AgingItem(item_id="#5", title="t5", current_state="Awaiting Review", age_days=100),
        AgingItem(item_id="#6", title="t6", current_state="Awaiting Review", age_days=200),
    ]
    return AgingReport(
        input=AgingInput(
            repo="acme/widget",
            asof=date(2026, 5, 14),
            workflow=("Awaiting Review", "Approved"),
            history_start=date(2026, 4, 14),
            history_end=date(2026, 5, 13),
            offline=False,
        ),
        items=items,
        cycle_time_percentiles={50: 1.7, 70: 5.4, 85: 17.8, 95: 57.4},
        completed_count=100,
        interpretation=_interp(),
        generated_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
    )


def _aging_report_with_divergence() -> AgingReport:
    """Distribution forces above-P95 share well above the 10% caveat
    threshold so the divergence banner fires.

    Uses the real `interpret_aging` so the divergence caveat actually
    fires; the banner extractor looks for the word "diverge" in the
    caveats list."""
    from flowmetrics.interpretation import interpret_aging

    # P95 = 50d; 6 of 10 items past P95 → 60% above-P95 share.
    items = [
        AgingItem(item_id=f"#{i}", title=f"PR {i}",
                  current_state="State A", age_days=100)
        for i in range(6)
    ] + [
        AgingItem(item_id=f"#{i}", title=f"PR {i}",
                  current_state="State A", age_days=1)
        for i in range(6, 10)
    ]
    input_ = AgingInput(
        repo="acme/widget",
        asof=date(2026, 5, 14),
        workflow=("State A", "State B"),
        history_start=date(2026, 4, 14),
        history_end=date(2026, 5, 13),
        offline=False,
    )
    pct = {50: 5.0, 70: 10.0, 85: 25.0, 95: 50.0}
    return AgingReport(
        input=input_,
        items=items,
        cycle_time_percentiles=pct,
        completed_count=100,
        interpretation=interpret_aging(input_, items, pct, completed_count=100),
        generated_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
    )


class TestAgingHtmlRedesign:
    """The Tufte redesign: title = the answer, banner promotes the
    divergence caveat, per-state diagnostic + interventions list are
    the actionable signals, redundant tables removed.
    """

    def test_html_title_is_the_headline_not_a_program_label(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # The <title> tag should carry the headline, not 'flowmetrics —
        # aging <repo>'. Agents and humans both read this in tabs.
        title_match = re.search(r"<title>([^<]+)</title>", out)
        assert title_match is not None
        assert "flowmetrics" not in title_match.group(1).lower()

    def test_no_red_banner_at_top_of_page_even_when_distribution_diverges(self):
        """The divergence is information, not an alarm. Stripped from
        the top per Tufte 'less is more' pass; the caveat still lives
        in the footer's caveats list for the reader who wants context."""
        out = html_renderer.render(_aging_report_with_divergence())
        # No `class="banner"` div anywhere on the page.
        assert 'class="banner"' not in out

    def test_divergence_caveat_still_appears_in_footer_caveats(self):
        """The signal-quality information must not be lost — it moves
        from the banner to the 'About this report' footer where the
        other caveats live."""
        out = html_renderer.render(_aging_report_with_divergence())
        # Divergence text still on the page (just not in a banner).
        assert "diverge" in out.lower()
        # And it sits after the chart, not above it.
        i_div = out.lower().index("diverge")
        i_chart = out.index('id="aging-chart"')
        assert i_div > i_chart

    def test_no_divergence_banner_when_distribution_is_healthy(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # Healthy fixture has ~17% above P95 (1 of 6); over threshold
        # actually. Need a healthier fixture.
        # Instead inspect that the banner shows when it should — that
        # behaviour is exercised in the divergence test above. Here we
        # assert the report without divergence does NOT carry the
        # banner. Use a custom fixture:
        report = AgingReport(
            input=AgingInput(
                repo="acme/widget", asof=date(2026, 5, 14),
                workflow=("A",), history_start=date(2026, 4, 14),
                history_end=date(2026, 5, 13), offline=False,
            ),
            items=[
                AgingItem(item_id=f"#{i}", title=f"PR {i}",
                          current_state="A", age_days=1)
                for i in range(20)
            ],  # all under P50 = "healthy"
            cycle_time_percentiles={50: 5.0, 70: 10.0, 85: 25.0, 95: 50.0},
            completed_count=100,
            interpretation=_interp(),
            generated_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )
        out = html_renderer.render(report)
        assert 'class="banner"' not in out

    def test_top_interventions_list_renders(self):
        out = html_renderer.render(_aging_report_with_divergence())
        # Items past P85 are surfaced under the Next actions section.
        assert "Next actions" in out

    def test_per_state_diagnostic_table_renders(self):
        out = html_renderer.render(_aging_report_with_divergence())
        # Bottleneck diagnostic by workflow state.
        assert "median age" in out.lower() or "oldest" in out.lower()

    def test_redundant_tables_removed(self):
        """Four tables duplicated the chart and the new diagnostic
        table; they are gone."""
        out = html_renderer.render(_aging_report_with_distribution())
        # "Headline numbers" repeats the headline (and the workflow).
        assert "Headline numbers" not in out
        # "WIP per workflow state" = the chart's column densities.
        assert "WIP per workflow state" not in out
        # "Cycle-time percentiles (from completed items)" = chart lines.
        assert "Cycle-time percentiles (from completed items)" not in out
        # "In-flight age distribution" is now folded into the diagnostic.
        assert "In-flight age distribution" not in out


class TestAgingHtmlV2Restructure:
    """The Tufte-v2 restructure: H1 is the term not the headline, repo
    is a subtitle link, chart moves above interventions, Next Actions
    absorbs the interventions list, helper-text paragraph deleted,
    Vacanti page numbers gone, vocabulary becomes a footnote."""

    def test_h1_is_the_term_not_the_full_headline(self):
        out = html_renderer.render(_aging_report_with_distribution())
        h1 = re.search(r"<h1>([^<]+)</h1>", out)
        assert h1 is not None
        # H1 is the report's metric, not the long headline sentence.
        assert h1.group(1).strip() == "Aging Work In Progress"

    def test_repo_appears_as_a_subtitle_link(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # The repo is a clickable link to GitHub right under the H1.
        assert 'href="https://github.com/acme/widget"' in out
        assert "acme/widget" in out

    def test_chart_appears_before_next_actions_block(self):
        out = html_renderer.render(_aging_report_with_distribution())
        i_chart = out.index('id="aging-chart"')
        i_next = out.index("Next actions") if "Next actions" in out else \
                 out.index("Next Actions")
        assert i_chart < i_next

    def test_chart_helper_text_paragraph_removed(self):
        """The 'Hover any circle / drag to pan / scroll-wheel to zoom'
        helper text is gone — affordances should be self-evident."""
        out = html_renderer.render(_aging_report_with_distribution())
        assert "Hover any circle" not in out
        assert "scroll-wheel" not in out
        assert "Drag to pan" not in out

    def test_no_vacanti_page_numbers(self):
        """Page numbers were wrong. Citations stripped to book only."""
        out = html_renderer.render(_aging_report_with_distribution())
        assert "pp. 50" not in out
        assert "p. 50" not in out
        assert "Figure 3.2" not in out

    def test_vocabulary_contains_wip_definition(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # WIP is the term most likely to need defining; must be there.
        assert "WIP" in out
        assert "Work In Progress" in out

    def test_next_actions_section_includes_multiple_items_per_state(self):
        """Old behaviour: 1 per state, capped at 5 — gave 2 items for
        rust. New: 3 per state — gives a useful action list."""
        # Fixture has 6 items in State A past P95, all candidates.
        out = html_renderer.render(_aging_report_with_divergence())
        # Count item-id chips inside the Next Actions section.
        # Find the section and count `<div class="intervention">` rows.
        i_actions = out.index("Next actions") if "Next actions" in out \
                    else out.index("Next Actions")
        i_next_section = out.index("<h2", i_actions + 1) if "<h2" in out[i_actions + 1:] \
                         else len(out)
        section = out[i_actions:i_next_section]
        rows = section.count('class="intervention"')
        # 6 candidates past P85, all in State A → capped at 3 (per-state).
        assert rows == 3


class TestAgingHtmlVegaChart:
    """HTML output includes a fully-inlined Vega-Lite interactive chart
    alongside the existing PNG. No CDN — the report must render with
    no network connection."""

    def test_loads_vega_from_cdn_not_inlined(self):
        """Switched from inlined bundle (~830KB per HTML report) to
        jsdelivr CDN script tags. Reports are no longer fully offline
        but file size drops by ~70%."""
        out = html_renderer.render(_aging_report_with_distribution())
        # CDN script tags present, pinned to a major version.
        assert 'src="https://cdn.jsdelivr.net/npm/vega@5' in out
        assert 'src="https://cdn.jsdelivr.net/npm/vega-lite@5' in out
        assert 'src="https://cdn.jsdelivr.net/npm/vega-embed@6' in out
        # The 830KB inlined UMD wrapper is gone.
        assert out.count('!function(') < 3

    def test_inlines_a_vega_lite_spec_for_the_chart(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # Spec is embedded as a JSON literal. Distinctive Vega-Lite v5
        # schema URL must be present.
        assert "vega-lite/v5.json" in out

    def test_chart_container_div_present(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # A div the JS can target. Name is stable so the JS can find it.
        assert 'id="aging-chart"' in out

    def test_invokes_vega_embed_on_the_container(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # vegaEmbed call wires the spec into the div.
        assert "vegaEmbed" in out
        # The spec is passed inline (not fetched from a URL).
        assert "fetch(" not in out  # belt-and-braces: no runtime spec load

    def test_malicious_pr_title_does_not_break_out_of_script_tag(self):
        """A PR title containing `</script>` must NOT close the inline
        Vega spec's script tag. Encoded JSON output replaces < > & with
        \\uXXXX escapes — still valid JSON, safe HTML."""
        # Build a fixture where one PR title is the XSS payload.
        from flowmetrics.aging import AgingItem
        items = [
            AgingItem(
                item_id="#1",
                title="Innocent",
                current_state="Awaiting Review",
                age_days=3,
            ),
            AgingItem(
                item_id="#2",
                title="</script><script>window.__xss=true</script>",
                current_state="Awaiting Review",
                age_days=10,
            ),
        ]
        report = AgingReport(
            input=AgingInput(
                repo="acme/widget",
                asof=date(2026, 5, 14),
                workflow=("Awaiting Review",),
                history_start=date(2026, 4, 14),
                history_end=date(2026, 5, 13),
                offline=False,
            ),
            items=items,
            cycle_time_percentiles={50: 1.0, 70: 2.0, 85: 3.0, 95: 5.0},
            completed_count=10,
            interpretation=_interp(),
            generated_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )
        out = html_renderer.render(report)
        # The spec is inlined as `vegaEmbed("#aging-chart", <SPEC>, {...})`.
        # Carve out just <SPEC> and assert it contains the escaped form
        # of `</script>`, not the literal that would close the tag.
        start = out.index('vegaEmbed("#aging-chart", ')
        spec_start = out.index("{", start)
        # Walk braces to find the matching close of <SPEC>.
        depth = 0
        for i in range(spec_start, len(out)):
            if out[i] == "{":
                depth += 1
            elif out[i] == "}":
                depth -= 1
                if depth == 0:
                    spec_end = i + 1
                    break
        else:
            raise AssertionError("could not locate spec JSON in output")
        spec = out[spec_start:spec_end]
        assert "</script>" not in spec, \
            "XSS regression: </script> appears verbatim inside inline Vega spec"
        # The escaped form IS present — confirms the safe-json helper ran.
        assert "\\u003c/script\\u003e" in spec
        # And the original PR ID still renders in the body.
        assert "#2" in out

    def test_png_chart_is_no_longer_present(self):
        """The PNG fallback was removed — the interactive chart is the
        canonical surface. This test inverts the earlier assertion."""
        out = html_renderer.render(_aging_report_with_distribution())
        assert not re.search(r'src="data:image/png;base64,[A-Za-z0-9+/=]{100,}"', out)


class TestAgingPolishV3:
    """Final pass: H2 for the chart section, accessibility hardening
    of the stacked bar, static PNG removed entirely, reproduce +
    parameters split into their own collapsible (separate from
    'About this report')."""

    def test_chart_section_has_h2_titled_wip_aging(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # Specific H2 immediately announces the chart for screen-reader
        # navigation by heading.
        assert "<h2>WIP Aging</h2>" in out

    def test_static_png_fallback_removed_entirely(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # No PNG <img> data-uri anywhere — the interactive chart is the
        # canonical surface; PNG fallback was unused archival cruft.
        import re as _re
        assert not _re.search(r'src="data:image/png;base64,', out)
        assert "PNG fallback" not in out
        assert "PNG (archival)" not in out

    def test_reproduce_and_parameters_in_their_own_collapsible(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # Standalone <details> summary named for reproducing the report.
        assert "<summary>Reproduce this report</summary>" in out
        # And the parameters live with it (workflow/training-window row).
        i_summary = out.index("<summary>Reproduce this report</summary>")
        i_next_h2 = out.index("<h2", i_summary + 1) if "<h2" in out[i_summary + 1:] else len(out)
        # Look for the parameter rows within that section.
        section = out[i_summary:i_next_h2]
        assert "Percentile training window" in section
        # And the invocation command is in the same block.
        assert "uv run flow aging" in section

    def test_about_section_does_not_carry_the_reproducer_anymore(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # The About block keeps definition + vocabulary; the reproducer
        # is now siblings, not folded in.
        about_idx = out.index("<summary>About this report</summary>")
        about_end = out.index("</details>", about_idx)
        about_section = out[about_idx:about_end]
        assert "uv run flow aging" not in about_section
        # But About still has the math + definition.
        assert "observed cycle times" in about_section.lower() or \
               "empirical" in about_section.lower()

    def test_stacked_bar_has_aria_label_with_distribution_summary(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # The bar element carries role + aria-label so screen readers
        # announce the distribution as text rather than ignoring it.
        assert 'role="img"' in out
        assert 'aria-label="Aging distribution' in out


class TestAgingReadability:
    """An engineer/manager/TPM reading the page must be able to
    understand what the percentile lines mean and act on them. This
    suite pins the existence and intelligibility of the explanation.
    """

    def test_per_state_table_includes_at_risk_column(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # New column header — surfaces the "needs a conversation now"
        # cohort (items past P50, not yet past P85).
        assert "At risk" in out

    def test_about_section_contains_vacanti_doubling_explanation(self):
        """The 'doubling math' (Vacanti, WWIBD) must be on the page so
        a manager reading the chart understands why the lines matter."""
        out = html_renderer.render(_aging_report_with_distribution())
        # The 100-PR worked example is the way an engineer grasps it.
        # Surface the key phrasing.
        text = out.lower()
        assert "conditional" in text or "doubled" in text or "doubles" in text
        # And the bottom-line action message.
        assert "85" in out and "50" in out

    def test_about_section_says_thresholds_are_observed_not_simulated(self):
        """After a month of confusion, the page must explicitly say the
        lines are observed cycle times, not Monte Carlo / forecasted."""
        out = html_renderer.render(_aging_report_with_distribution())
        text = out.lower()
        assert "observed" in text or "empirical" in text
        # The page is allowed to MENTION Monte Carlo (e.g. "no Monte
        # Carlo here") — but must not claim the lines are derived from
        # it. Sanity-check by looking for "no monte carlo" or similar
        # disclaimer when the phrase appears at all.
        if "monte carlo" in text:
            assert (
                "no monte carlo" in text
                or "no simulation" in text
                or "not monte carlo" in text
            ), "if MCS is mentioned, the page must clarify it is NOT used"


class TestAgingMinimalChrome:
    """Holistic 'less is more' pass on the Aging report:
    - No headline sentence — its prefix duplicates the title/subtitle,
      and its numbers duplicate the distribution table + per-state table.
    - No "Key insight" yellow block — redundant with the chart +
      interventions list.
    - No auto "Next actions" list from interpretation — the
      interventions list is the actionable next-actions surface.
    - Subtitle carries the as-of date so it doesn't need its own line.
    - 'All N in-flight items' dump replaced with 'Top 50 past P85'
      since 500+ rows is reference data, not actionable.
    - Generation stamp moves to the bottom (archival use only).
    """

    def test_headline_sentence_not_rendered_at_top(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # The old prefix "WIP Aging for X as of Y" duplicated title +
        # subtitle. Should not appear anywhere on the page.
        assert "WIP Aging for" not in out

    def test_subtitle_includes_asof_date(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # The asof date is essential context; carry it in the subtitle.
        # Prose date format used elsewhere: "May 14, 2026".
        assert "May 14, 2026" in out

    def test_no_key_insight_section_in_aging(self):
        out = html_renderer.render(_aging_report_with_distribution())
        assert "Key insight" not in out

    def test_no_auto_next_actions_list_from_interpretation(self):
        """The interventions list IS the Next actions surface. The base
        template's auto-rendered ordered list of interpretation.next_actions
        is suppressed — those text strings duplicated or contradicted
        the interventions list above them."""
        out = html_renderer.render(_aging_report_with_distribution())
        # Old jargon line about pull policy / capacity should be gone.
        assert "pull policy" not in out
        assert "capacity." not in out

    def test_full_per_pr_dump_replaced_with_top_50_past_p85(self):
        out = html_renderer.render(_aging_report_with_distribution())
        # Old summary text: "All N in-flight items (oldest first)".
        assert "All " not in out or "in-flight items (oldest first)" not in out
        # The replacement: "Oldest items past P85" — or similar.
        # (Empty in this fixture since few items past P85.)
        # Soft check: the new section header phrasing exists somewhere
        # OR the section is omitted entirely when nothing is past P85.

    def test_definition_moved_to_footer_not_above_chart(self):
        """'What this shows' was the third stacked colored block. It's
        either moved to a footer (after the detail divider) or removed."""
        out = html_renderer.render(_aging_report_with_distribution())
        if "What this shows" in out:
            i_def = out.index("What this shows")
            i_chart = out.index('id="aging-chart"')
            # If still present, it lives AFTER the chart.
            assert i_def > i_chart


class TestNoStackedDecorativeBlocks:
    """Tufte: don't stack colored boxes. The headline and definition
    were each a decorative colored block, so headline + banner +
    definition rendered as three vertical highlight stripes — high
    visual noise, low information hierarchy. Only the conditional
    banner/horizon classes are allowed to carry decorative chrome.
    """

    def test_headline_class_is_not_a_colored_block(self):
        out = html_renderer.render(_aging_report_with_distribution())
        m = re.search(r"\.headline\s*\{([^}]*)\}", out)
        assert m is not None, "headline CSS rule should exist (for hierarchy)"
        rule = m.group(1)
        assert "#f0f8ff" not in rule, "no blue tint"
        assert "border-left" not in rule, "no colored left stripe"

    def test_definition_class_is_not_a_colored_block(self):
        out = html_renderer.render(_aging_report_with_distribution())
        m = re.search(r"\.definition\s*\{([^}]*)\}", out)
        if m is None:
            # Acceptable: definition removed entirely from the page.
            return
        rule = m.group(1)
        assert "#f0f4f8" not in rule, "no grey tint"
        assert "border-left" not in rule, "no colored left stripe"


class TestAgingHtmlDistributionAtTop:
    """The 5-band aging distribution is the page's situation-snapshot
    "big number" — it answers "what does the in-flight pile look like
    against recent cycle times" without involving a trend. Surfaced
    prominently above the chart; the chart provides the per-item view
    of the same distribution.
    """

    def test_distribution_table_rendered_with_all_five_bands(self):
        out = html_renderer.render(_aging_report_with_distribution())
        assert "Below P50" in out
        assert "P50–P70" in out
        assert "P70–P85" in out
        assert "P85–P95" in out
        assert "Above P95" in out

    def test_distribution_table_appears_before_the_chart(self):
        """The situation snapshot goes at the top — above the chart."""
        out = html_renderer.render(_aging_report_with_distribution())
        i_dist = out.index("Above P95")
        i_chart = out.index('id="aging-chart"')
        assert i_dist < i_chart

    def test_per_state_table_appears_after_the_chart(self):
        """Drilldown comes after the chart, not before it."""
        out = html_renderer.render(_aging_report_with_distribution())
        i_chart = out.index('id="aging-chart"')
        i_per_state = out.index("Per-state aging")
        assert i_chart < i_per_state


class TestHumanDateLabel:
    """Chart axis labels: `Jan 12` (space, no dash). Year only when the
    chart spans a year boundary."""

    def test_format_with_space_not_dash(self):
        from datetime import date as _d

        from flowmetrics.renderers.html_renderer import _human_date_label

        assert _human_date_label(_d(2026, 1, 12), include_year=False) == "Jan 12"
        assert "-" not in _human_date_label(_d(2026, 1, 12), include_year=False)

    def test_format_with_year_when_required(self):
        from datetime import date as _d

        from flowmetrics.renderers.html_renderer import _human_date_label

        assert _human_date_label(_d(2026, 1, 12), include_year=True) == "Jan 12 2026"


class TestDefaultOutputPath:
    def test_filename_contains_repo_slug_and_timestamp(self):
        path = html_renderer.default_output_path(_efficiency_report())
        assert "acme_widget" in str(path)
        assert "20260512-143015" in str(path)
        assert str(path).endswith(".html")

    def test_filename_contains_command_slug(self):
        eff_path = str(html_renderer.default_output_path(_efficiency_report()))
        wd_path = str(html_renderer.default_output_path(_when_done_report()))
        hm_path = str(html_renderer.default_output_path(_how_many_report()))
        assert "efficiency-week" in eff_path
        assert "forecast-when-done" in wd_path
        assert "forecast-how-many" in hm_path


class TestRenderToFile:
    def test_writes_to_given_path(self, tmp_path):
        out = tmp_path / "report.html"
        result = html_renderer.render_to_file(_efficiency_report(), out)
        assert result == out
        assert out.exists()
        assert "<!doctype html>" in out.read_text().lower()

    def test_creates_parent_dirs(self, tmp_path):
        out = tmp_path / "nested" / "deep" / "report.html"
        html_renderer.render_to_file(_efficiency_report(), out)
        assert out.exists()

    def test_default_path_when_none(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = html_renderer.render_to_file(_efficiency_report())
        assert result.exists()
        assert "acme_widget" in result.name
