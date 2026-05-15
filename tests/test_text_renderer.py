"""Behavioural spec for the rich text renderer.

We test substring contents — rich-formatting bytes vary by terminal width
and color settings, so we focus on what a human reader needs to see.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from flowmetrics.aging import AgingItem
from flowmetrics.compute import FlowEfficiency, WindowResult
from flowmetrics.forecast import build_histogram
from flowmetrics.interpretation import interpret_aging
from flowmetrics.renderers import text_renderer
from flowmetrics.report import (
    AgingInput,
    AgingReport,
    CfdInput,
    CfdPoint,
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
        headline="Portfolio FE is 12.3% — typical for knowledge work.",
        key_insight="Slowest PR dominates the ratio.",
        next_actions=["Inspect PR #99.", "Compare to last 4 weeks."],
        caveats=["Per-engineer use is harmful."],
    )


def _efficiency_report() -> EfficiencyReport:
    pr = FlowEfficiency(
        item_id="#99",
        title="Slow PR",
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
        simulation=SimulationSummary(runs=10_000, seed=42),
        histogram=build_histogram([date(2026, 5, 19), date(2026, 5, 20)]),
        percentiles={
            50: date(2026, 5, 19),
            70: date(2026, 5, 19),
            85: date(2026, 5, 20),
            95: date(2026, 5, 20),
        },
        interpretation=_interp(),
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
        simulation=SimulationSummary(runs=10_000, seed=42),
        histogram=build_histogram([50, 60, 70]),
        percentiles={50: 60, 70: 55, 85: 51, 95: 50},
        interpretation=_interp(),
    )


# ---------------------------------------------------------------------------


class TestAsciiSafeOutput:
    """Text output must survive being redirected through a non-UTF-8
    locale, piped into a clipboard, dropped into an email or chat
    that auto-decodes as latin-1, etc. Unicode arrows (→) get
    rendered as `â†'` mojibake under those conditions. HTML is
    safe because the page declares charset=utf-8; text isn't."""

    def test_no_unicode_arrows_in_terse_default(self):
        out = text_renderer.render(_efficiency_report())
        assert "→" not in out

    def test_no_unicode_arrows_in_verbose(self):
        for report_fn in (_efficiency_report, _when_done_report, _how_many_report):
            out = text_renderer.render(report_fn(), verbose=True)
            assert "→" not in out, (
                f"Verbose output of {report_fn.__name__} contains "
                f"a unicode arrow — text mode must use ASCII '->'."
            )

    def test_ascii_arrows_present_where_unicode_arrows_used_to_be(self):
        """Sanity: the date-range / workflow-chain prose still
        carries an arrow — just as ASCII '->' instead of '→'."""
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "->" in out


class TestEfficiencyText:
    """Verbose efficiency mirrors the Aging text contract: headline +
    actionable numbers + slowest-PR table + reproduce. No more Key
    insight panel, no numbered Next actions list, no Vocabulary."""

    def test_contains_headline(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "Portfolio FE is 12.3%" in out

    def test_contains_repo_and_window(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "acme/widget" in out
        assert "2026-05-04" in out
        assert "2026-05-10" in out

    def test_contains_portfolio_fe_number(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "8.3%" in out

    def test_contains_per_pr_breakdown(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "#99" in out
        assert "Slow PR" in out

    def test_no_key_insight_panel(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "Key insight" not in out

    def test_no_next_actions_header(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "Next actions" not in out

    def test_no_vocabulary_block(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "Vocabulary used" not in out

    def test_no_what_this_shows_panel(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "What this shows" not in out

    def test_reproduce_command_present(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "uv run flow efficiency" in out


class TestWhenDoneText:
    def test_contains_headline(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "Portfolio FE is 12.3%" in out  # the headline we passed in

    def test_contains_percentiles(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "50" in out and "85" in out and "95" in out
        assert "2026-05-19" in out
        assert "2026-05-20" in out

    def test_contains_training_summary(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "Training" in out or "training" in out
        # 5/day * 4 days = 20 total
        assert "20" in out

    def test_does_not_include_ascii_histogram(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "########" not in out

    def test_no_key_insight_panel(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "Key insight" not in out

    def test_no_next_actions_header(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "Next actions" not in out

    def test_no_vocabulary_block(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "Vocabulary used" not in out


def _cfd_report() -> CfdReport:
    points = [
        CfdPoint(date(2026, 5, 1), {"A": 5, "Done": 0}),
        CfdPoint(date(2026, 5, 14), {"A": 15, "Done": 0}),
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


class TestCfdText:
    def test_headline_present(self):
        out = text_renderer.render(_cfd_report(), verbose=True)
        assert "Portfolio FE" in out  # the placeholder headline

    def test_end_of_window_wip_visible(self):
        out = text_renderer.render(_cfd_report(), verbose=True)
        assert "A" in out
        assert "15" in out  # end WIP for A

    def test_no_key_insight_panel(self):
        out = text_renderer.render(_cfd_report(), verbose=True)
        assert "Key insight" not in out

    def test_no_next_actions_header(self):
        out = text_renderer.render(_cfd_report(), verbose=True)
        assert "Next actions" not in out

    def test_no_vocabulary_block(self):
        out = text_renderer.render(_cfd_report(), verbose=True)
        assert "Vocabulary used" not in out

    def test_reproduce_command_present(self):
        out = text_renderer.render(_cfd_report(), verbose=True)
        assert "uv run flow cfd" in out


class TestHowManyText:
    def test_contains_percentiles_with_items(self):
        out = text_renderer.render(_how_many_report(), verbose=True)
        # 85% confidence should show 51 items (backward percentile)
        assert "51" in out
        assert "50" in out

    def test_no_key_insight_panel(self):
        out = text_renderer.render(_how_many_report(), verbose=True)
        assert "Key insight" not in out

    def test_no_next_actions_header(self):
        out = text_renderer.render(_how_many_report(), verbose=True)
        assert "Next actions" not in out

    def test_no_vocabulary_block(self):
        out = text_renderer.render(_how_many_report(), verbose=True)
        assert "Vocabulary used" not in out


class TestTerseDefault:
    """Default text output is one-line: just the headline answer.
    Full report is opt-in via verbose=True."""

    def test_default_render_is_a_single_line(self):
        out = text_renderer.render(_efficiency_report())
        lines = [line for line in out.strip().splitlines() if line.strip()]
        assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {lines}"
        assert "Portfolio FE" in lines[0] or "flow efficiency" in lines[0].lower()

    def test_default_render_does_not_include_input_block(self):
        out = text_renderer.render(_efficiency_report())
        assert "Repo" not in out
        assert "Reproduce" not in out
        assert "Vocabulary" not in out

    def test_verbose_render_includes_full_detail(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "Repo" in out
        assert "Reproduce" in out
        # Key insight is now carried by the headline panel — no separate
        # "Key insight" sub-panel any more.
        assert "Portfolio FE" in out

    def test_terse_when_done_is_one_line(self):
        out = text_renderer.render(_when_done_report())
        lines = [line for line in out.strip().splitlines() if line.strip()]
        assert len(lines) == 1
        # Terse output IS the report's headline string, modulo ASCII-
        # safe substitution of unicode arrows / dashes (text mode
        # writes mojibake-resistant output).
        expected = _interp().headline.replace("→", "->").replace("—", "--")
        assert lines[0] == expected


class TestAnswerFirstOrdering:
    """Text output mirrors HTML: headline → definition → key numbers →
    key insight → next actions → caveats — then detail (input + repro)."""

    def test_definition_appears_before_input(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        # The definition mentions "active" (efficiency) or "Monte Carlo" (forecasts)
        assert "active" in out.lower()
        # And it appears before the input/parameters block, not after
        i_def = out.lower().index("active")
        # "Repo" appears in the input table at the bottom (per new layout)
        assert "Repo" in out
        i_repo = out.index("Repo")
        assert i_def < i_repo

    def test_headline_appears_before_input(self):
        # The headline carries the insight — it must come before the
        # detail block. (No more separate "Key insight" panel.)
        out = text_renderer.render(_efficiency_report(), verbose=True)
        i_headline = out.index("Portfolio FE")
        i_repo = out.index("Repo")
        assert i_headline < i_repo


class TestNoEmptyOutput:
    def test_efficiency_render_is_substantial(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert len(out) > 200  # not just a header

    def test_when_done_render_is_substantial(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert len(out) > 200


def _aging_report(*, divergent: bool = True) -> AgingReport:
    """Aging fixture. divergent=True: 60% past P95 — survivorship-bias
    banner should fire. divergent=False: a healthy distribution."""
    if divergent:
        items = (
            [AgingItem(item_id=f"#{i}", title=f"PR {i}",
                       current_state="State A", age_days=100)
             for i in range(6)]
            + [AgingItem(item_id=f"#{i}", title=f"PR {i}",
                         current_state="State A", age_days=1)
               for i in range(6, 10)]
        )
    else:
        items = [
            AgingItem(item_id=f"#{i}", title=f"PR {i}",
                      current_state="State A", age_days=1)
            for i in range(10)
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


class TestAgingTextMinimal:
    """Text output for Aging is trimmed to highest-value signals:
    headline + divergence warning + interventions + per-state diagnostic
    + reproducer. No more redundant percentile-only table, no separate
    WIP-per-state table, no per-PR dump, no vocabulary block.
    """

    def test_headline_present(self):
        out = text_renderer.render(_aging_report(), verbose=True)
        assert "WIP Aging" in out

    def test_divergence_warning_present_when_triggered(self):
        out = text_renderer.render(_aging_report(divergent=True), verbose=True)
        assert "diverge" in out.lower()

    def test_per_state_diagnostic_table_present(self):
        out = text_renderer.render(_aging_report(), verbose=True)
        # The table has Past P85 / Past P95 columns — those headings
        # are distinctive to the new diagnostic.
        assert "Past P85" in out or "P85" in out
        assert "State A" in out

    def test_removed_separate_wip_per_state_table(self):
        """The old WIP-per-state table is now subsumed by the
        per-state diagnostic. The standalone title shouldn't appear."""
        out = text_renderer.render(_aging_report(), verbose=True)
        assert "WIP per workflow state" not in out

    def test_removed_separate_cycle_time_percentile_table(self):
        """Percentile values are inside the diagnostic table now;
        the separate 'Cycle-time percentile checkpoints' header is gone."""
        out = text_renderer.render(_aging_report(), verbose=True)
        assert "Cycle-time percentile checkpoints" not in out

    def test_removed_per_pr_dump_table(self):
        """`In-flight items (oldest first)` was a 20-row dump that
        competed with the interventions list. Removed from text."""
        out = text_renderer.render(_aging_report(), verbose=True)
        assert "In-flight items (oldest first)" not in out

    def test_removed_vocabulary_block(self):
        """Vacanti glossary is reference, not signal — removed from
        the verbose text output."""
        out = text_renderer.render(_aging_report(), verbose=True)
        assert "Vocabulary used" not in out

    def test_reproduce_command_present(self):
        out = text_renderer.render(_aging_report(), verbose=True)
        assert "uv run flow aging" in out
