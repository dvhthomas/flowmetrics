"""Behavioural spec for the typed Report dataclasses.

Reports are the contract between commands and renderers. The spec is
deliberately thin — we test the surface that renderers actually use:
schema string, command label, generated_at timestamp, fields are present
and immutable.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta

import pytest

from flowmetrics.compute import FlowEfficiency, WindowResult
from flowmetrics.forecast import build_histogram
from flowmetrics.report import (
    EfficiencyInput,
    EfficiencyReport,
    HowManyInput,
    HowManyReport,
    Interpretation,
    SimulationSummary,
    TrainingSummary,
    WhenDoneInput,
    WhenDoneReport,
    build_training_summary,
    cli_invocation,
    report_vocabulary,
)


def _eff(pr_number=1, eff=0.5):
    return FlowEfficiency(
        pr_number=pr_number,
        title=f"PR {pr_number}",
        created_at=datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
        merged_at=datetime(2026, 5, 5, 17, 0, tzinfo=UTC),
        cycle_time=timedelta(hours=8),
        active_time=timedelta(hours=4),
        efficiency=eff,
    )


def _window_result(prs=None):
    prs = prs or [_eff()]
    return WindowResult(
        pr_count=len(prs),
        portfolio_efficiency=0.5,
        mean_efficiency=0.5,
        median_efficiency=0.5,
        total_cycle=timedelta(hours=8 * len(prs)),
        total_active=timedelta(hours=4 * len(prs)),
        per_pr=prs,
    )


def _interp():
    return Interpretation(headline="h", key_insight="k", next_actions=["a"], caveats=["c"])


class TestInterpretation:
    def test_defaults_for_lists_are_empty(self):
        i = Interpretation(headline="h", key_insight="k")
        assert i.next_actions == []
        assert i.caveats == []

    def test_frozen(self):
        i = Interpretation(headline="h", key_insight="k")
        with pytest.raises(FrozenInstanceError):
            i.headline = "new"  # type: ignore[misc]


class TestEfficiencyReport:
    def test_schema_and_command_pinned(self):
        r = EfficiencyReport(
            input=EfficiencyInput(
                repo="x/y",
                start=date(2026, 5, 4),
                stop=date(2026, 5, 10),
                gap_hours=4.0,
                min_cluster_minutes=30.0,
                offline=False,
            ),
            result=_window_result(),
            interpretation=_interp(),
        )
        assert r.schema == "flowmetrics.efficiency.v1"
        assert r.command == "efficiency week"

    def test_generated_at_defaults_to_now(self):
        before = datetime.now().astimezone()
        r = EfficiencyReport(
            input=EfficiencyInput("x/y", date.today(), date.today(), 4.0, 30.0, False),
            result=_window_result(),
            interpretation=_interp(),
        )
        after = datetime.now().astimezone()
        assert before <= r.generated_at <= after

    def test_frozen(self):
        r = EfficiencyReport(
            input=EfficiencyInput("x/y", date.today(), date.today(), 4.0, 30.0, False),
            result=_window_result(),
            interpretation=_interp(),
        )
        with pytest.raises(FrozenInstanceError):
            r.command = "nope"  # type: ignore[misc]


class TestForecastReports:
    def test_when_done_schema(self):
        hist = build_histogram([date(2026, 5, 19)])
        r = WhenDoneReport(
            input=WhenDoneInput(
                "x/y",
                10,
                date(2026, 5, 11),
                date(2026, 4, 11),
                date(2026, 5, 10),
                False,
            ),
            training=TrainingSummary(
                window_start=date(2026, 4, 11),
                window_end=date(2026, 5, 10),
                daily_samples=[1],
                total_merges=1,
                avg_per_day=1.0,
                min_per_day=1,
                max_per_day=1,
                zero_days=0,
            ),
            simulation=SimulationSummary(runs=10, seed=42),
            histogram=hist,
            percentiles={50: date(2026, 5, 19)},
            interpretation=_interp(),
        )
        assert r.schema == "flowmetrics.forecast.when_done.v1"
        assert r.command == "forecast when-done"

    def test_how_many_schema(self):
        hist = build_histogram([10, 12])
        r = HowManyReport(
            input=HowManyInput(
                "x/y",
                date(2026, 5, 11),
                date(2026, 5, 25),
                date(2026, 4, 11),
                date(2026, 5, 10),
                False,
            ),
            training=TrainingSummary(
                window_start=date(2026, 4, 11),
                window_end=date(2026, 5, 10),
                daily_samples=[1],
                total_merges=1,
                avg_per_day=1.0,
                min_per_day=1,
                max_per_day=1,
                zero_days=0,
            ),
            simulation=SimulationSummary(runs=10, seed=42),
            histogram=hist,
            percentiles={50: 10, 95: 12},
            interpretation=_interp(),
        )
        assert r.schema == "flowmetrics.forecast.how_many.v1"
        assert r.command == "forecast how-many"


class TestBuildTrainingSummary:
    def test_aggregates_samples(self):
        samples = [0, 2, 5, 0, 3]
        s = build_training_summary(samples, date(2026, 5, 1), date(2026, 5, 5))
        assert s.window_start == date(2026, 5, 1)
        assert s.window_end == date(2026, 5, 5)
        assert s.daily_samples == samples
        assert s.total_merges == 10
        assert s.avg_per_day == 2.0
        assert s.min_per_day == 0
        assert s.max_per_day == 5
        assert s.zero_days == 2

    def test_empty_samples_safe(self):
        s = build_training_summary([], date(2026, 5, 1), date(2026, 5, 1))
        assert s.total_merges == 0
        assert s.avg_per_day == 0.0
        assert s.min_per_day == 0
        assert s.max_per_day == 0


class TestReportVocabulary:
    """Each report carries Vacanti's canonical term definitions inline,
    so a reader (human or agent) can interpret the numbers without
    leaving the document."""

    def test_efficiency_defines_cycle_active_wait_flow(self):
        report = EfficiencyReport(
            input=EfficiencyInput("x/y", date(2026, 5, 4), date(2026, 5, 10), 4.0, 30.0, False),
            result=_window_result(),
            interpretation=_interp(),
        )
        vocab = report_vocabulary(report)
        for term in [
            "Cycle time",
            "Active time",
            "Wait time",
            "Flow efficiency",
            "Portfolio flow efficiency",
        ]:
            assert term in vocab, f"missing term: {term}"
            assert len(vocab[term]) > 10  # non-trivial definition

    def test_forecast_defines_throughput_training_mcs(self):
        report = WhenDoneReport(
            input=WhenDoneInput(
                "x/y",
                50,
                date(2026, 5, 11),
                date(2026, 4, 11),
                date(2026, 5, 10),
                False,
            ),
            training=TrainingSummary(
                window_start=date(2026, 4, 11),
                window_end=date(2026, 5, 10),
                daily_samples=[5],
                total_merges=5,
                avg_per_day=5.0,
                min_per_day=5,
                max_per_day=5,
                zero_days=0,
            ),
            simulation=SimulationSummary(runs=1000, seed=42),
            histogram=build_histogram([date(2026, 5, 19)]),
            percentiles={50: date(2026, 5, 19)},
            interpretation=_interp(),
        )
        vocab = report_vocabulary(report)
        for term in [
            "Throughput",
            "Training window",
            "Monte Carlo Simulation",
            "Results Histogram",
            "Percentile",
        ]:
            assert term in vocab, f"missing term: {term}"


class TestCliInvocation:
    """Each Report can reconstruct the CLI command that produced it.

    The point: every sample artifact carries its provenance. A user
    looking at an HTML report or an agent reading the JSON should be
    able to copy-paste the command to reproduce or modify the run.
    """

    def test_efficiency_invocation(self):
        report = EfficiencyReport(
            input=EfficiencyInput(
                repo="acme/widget",
                start=date(2026, 5, 4),
                stop=date(2026, 5, 10),
                gap_hours=4.0,
                min_cluster_minutes=30.0,
                offline=False,
            ),
            result=_window_result(),
            interpretation=_interp(),
        )
        cmd = cli_invocation(report)
        assert cmd.startswith("uv run flow efficiency week")
        assert "--repo acme/widget" in cmd
        assert "--start 2026-05-04" in cmd
        assert "--stop 2026-05-10" in cmd
        assert "--gap-hours 4.0" in cmd
        assert "--min-cluster-minutes 30.0" in cmd
        # offline=False → flag should be omitted
        assert "--offline" not in cmd

    def test_efficiency_invocation_includes_offline_when_set(self):
        report = EfficiencyReport(
            input=EfficiencyInput(
                repo="acme/widget",
                start=date(2026, 5, 4),
                stop=date(2026, 5, 10),
                gap_hours=4.0,
                min_cluster_minutes=30.0,
                offline=True,
            ),
            result=_window_result(),
            interpretation=_interp(),
        )
        assert "--offline" in cli_invocation(report)

    def test_when_done_invocation(self):
        report = WhenDoneReport(
            input=WhenDoneInput(
                "acme/widget",
                50,
                date(2026, 5, 11),
                date(2026, 4, 11),
                date(2026, 5, 10),
                False,
            ),
            training=TrainingSummary(
                window_start=date(2026, 4, 11),
                window_end=date(2026, 5, 10),
                daily_samples=[1],
                total_merges=1,
                avg_per_day=1.0,
                min_per_day=1,
                max_per_day=1,
                zero_days=0,
            ),
            simulation=SimulationSummary(runs=10000, seed=42),
            histogram=build_histogram([date(2026, 5, 19)]),
            percentiles={50: date(2026, 5, 19)},
            interpretation=_interp(),
        )
        cmd = cli_invocation(report)
        assert cmd.startswith("uv run flow forecast when-done")
        assert "--repo acme/widget" in cmd
        assert "--items 50" in cmd
        assert "--start-date 2026-05-11" in cmd
        assert "--history-start 2026-04-11" in cmd
        assert "--history-end 2026-05-10" in cmd
        assert "--runs 10000" in cmd
        assert "--seed 42" in cmd
        # We must NOT use the contaminated 'backlog' name in the reproducer
        assert "--backlog" not in cmd

    def test_when_done_omits_seed_when_unset(self):
        report = WhenDoneReport(
            input=WhenDoneInput(
                "acme/widget",
                50,
                date(2026, 5, 11),
                date(2026, 4, 11),
                date(2026, 5, 10),
                False,
            ),
            training=TrainingSummary(
                window_start=date(2026, 4, 11),
                window_end=date(2026, 5, 10),
                daily_samples=[1],
                total_merges=1,
                avg_per_day=1.0,
                min_per_day=1,
                max_per_day=1,
                zero_days=0,
            ),
            simulation=SimulationSummary(runs=10000, seed=None),
            histogram=build_histogram([date(2026, 5, 19)]),
            percentiles={50: date(2026, 5, 19)},
            interpretation=_interp(),
        )
        assert "--seed" not in cli_invocation(report)

    def test_how_many_invocation(self):
        report = HowManyReport(
            input=HowManyInput(
                "acme/widget",
                date(2026, 5, 11),
                date(2026, 5, 25),
                date(2026, 4, 11),
                date(2026, 5, 10),
                False,
            ),
            training=TrainingSummary(
                window_start=date(2026, 4, 11),
                window_end=date(2026, 5, 10),
                daily_samples=[1],
                total_merges=1,
                avg_per_day=1.0,
                min_per_day=1,
                max_per_day=1,
                zero_days=0,
            ),
            simulation=SimulationSummary(runs=10000, seed=42),
            histogram=build_histogram([60]),
            percentiles={50: 60},
            interpretation=_interp(),
        )
        cmd = cli_invocation(report)
        assert cmd.startswith("uv run flow forecast how-many")
        assert "--target-date 2026-05-25" in cmd
        assert "--start-date 2026-05-11" in cmd
        assert "--history-start 2026-04-11" in cmd
        assert "--history-end 2026-05-10" in cmd
