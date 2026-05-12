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

from flowmetrics.compute import FlowEfficiency, WindowResult
from flowmetrics.forecast import build_histogram
from flowmetrics.renderers import html_renderer
from flowmetrics.report import (
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
        """Top-of-page: answer + key insight + chart + next actions.
        Bottom-of-page: input params, reproduce command, caveats, per-PR list."""
        out = html_renderer.render(_efficiency_report())
        i_key_insight = out.index("Key insight")
        i_next_actions = out.index("Next actions") if "Next actions" in out else len(out)
        i_input = out.index("Input")
        i_reproduce = out.index("Reproduce")
        i_caveats = out.index("Caveats")
        # The actionable block sits above the detail block
        assert i_key_insight < i_input
        assert i_key_insight < i_reproduce
        assert i_key_insight < i_caveats
        # The chart and next actions also sit above detail
        if i_next_actions < len(out):
            assert i_next_actions < i_input

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
