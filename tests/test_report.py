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

from flowmetrics.cfd import CfdPoint
from flowmetrics.compute import FlowEfficiency, WindowResult
from flowmetrics.forecast import build_histogram
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
    TrainingSummary,
    WhenDoneInput,
    WhenDoneReport,
    build_training_summary,
    cli_invocation,
    report_vocabulary,
)


def _eff(item_id="#1", eff=0.5):
    return FlowEfficiency(
        item_id=item_id,
        title=f"PR {item_id}",
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
            "Results histogram",
            "Percentile",
        ]:
            assert term in vocab, f"missing term: {term}"


class TestCfdReport:
    def _report(self, *, repo="acme/widget", offline=False) -> CfdReport:
        return CfdReport(
            input=CfdInput(
                repo=repo,
                start=date(2026, 5, 4),
                stop=date(2026, 5, 10),
                workflow=("Open", "In Progress", "Done"),
                interval_days=1,
                offline=offline,
            ),
            points=[
                CfdPoint(
                    sampled_on=date(2026, 5, 4),
                    counts_by_state={"Open": 0, "In Progress": 0, "Done": 0},
                ),
                CfdPoint(
                    sampled_on=date(2026, 5, 10),
                    counts_by_state={"Open": 5, "In Progress": 2, "Done": 3},
                ),
            ],
            interpretation=_interp(),
        )

    def test_schema_and_command_pinned(self):
        r = self._report()
        assert r.schema == "flowmetrics.cfd.v1"
        assert r.command == "cfd"

    def test_frozen(self):
        r = self._report()
        with pytest.raises(FrozenInstanceError):
            r.command = "nope"  # type: ignore[misc]

    def test_vocabulary_defines_cfd_terms(self):
        vocab = report_vocabulary(self._report())
        for term in ["Arrivals", "Departures", "WIP", "Cumulative Flow Diagram"]:
            assert term in vocab, f"missing term: {term}"

    def test_cli_invocation_round_trips_inputs(self):
        cmd = cli_invocation(self._report())
        assert cmd.startswith("uv run flow cfd")
        assert "--repo acme/widget" in cmd
        assert "--start 2026-05-04" in cmd
        assert "--stop 2026-05-10" in cmd
        assert "--workflow 'Open,In Progress,Done'" in cmd
        assert "--interval-days 1" in cmd
        assert "--offline" not in cmd

    def test_cli_invocation_includes_offline_when_set(self):
        assert "--offline" in cli_invocation(self._report(offline=True))


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

    @staticmethod
    def _aging_report(*, from_wip_labels: bool = False):
        return AgingReport(
            input=AgingInput(
                repo="acme/widget",
                asof=date(2026, 5, 14),
                workflow=("shaping", "in-progress", "in-review"),
                history_start=date(2026, 4, 14),
                history_end=date(2026, 5, 13),
                offline=False,
                from_wip_labels=from_wip_labels,
            ),
            items=[],
            cycle_time_percentiles={50: 1.0, 70: 2.0, 85: 3.0, 95: 5.0},
            completed_count=10,
            interpretation=_interp(),
        )

    def test_aging_invocation_review_cycle_mode_uses_workflow(self):
        cmd = cli_invocation(self._aging_report(from_wip_labels=False))
        assert cmd.startswith("uv run flow aging")
        assert "--repo acme/widget" in cmd
        assert "--workflow 'shaping,in-progress,in-review'" in cmd
        assert "--wip-labels" not in cmd

    def test_aging_invocation_label_mode_uses_wip_labels(self):
        cmd = cli_invocation(self._aging_report(from_wip_labels=True))
        # The labels ARE the workflow in label mode — same tuple, just
        # surfaced under the flag the user actually typed.
        assert "--wip-labels 'shaping,in-progress,in-review'" in cmd
        assert "--workflow" not in cmd

    @staticmethod
    def _aging_report_with_max_age(max_age_days: int | None) -> AgingReport:
        return AgingReport(
            input=AgingInput(
                repo="acme/widget",
                asof=date(2026, 5, 14),
                workflow=("Awaiting Review", "Approved"),
                history_start=date(2026, 4, 14),
                history_end=date(2026, 5, 13),
                offline=False,
                max_age_days=max_age_days,
            ),
            items=[],
            cycle_time_percentiles={50: 1.0, 70: 2.0, 85: 3.0, 95: 5.0},
            completed_count=10,
            interpretation=_interp(),
        )

    def test_aging_invocation_includes_max_age_days_when_set(self):
        cmd = cli_invocation(self._aging_report_with_max_age(180))
        assert "--max-age-days 180" in cmd

    def test_aging_invocation_omits_max_age_days_when_unset(self):
        cmd = cli_invocation(self._aging_report_with_max_age(None))
        assert "--max-age-days" not in cmd

    @staticmethod
    def _aging_report_jira(jira_url: str | None = "https://issues.apache.org/jira"):
        """Aging fixture for the Jira reproducer round-trip test."""
        return AgingReport(
            input=AgingInput(
                repo="jira:BIGTOP",
                asof=date(2026, 5, 14),
                workflow=("Open", "In Progress", "Patch Available"),
                history_start=date(2026, 4, 14),
                history_end=date(2026, 5, 13),
                offline=False,
                jira_url=jira_url,
            ),
            items=[],
            cycle_time_percentiles={50: 1.0, 70: 2.0, 85: 3.0, 95: 5.0},
            completed_count=10,
            interpretation=_interp(),
        )

    def test_report_titles_are_centralised_and_consistent(self):
        """A single `report_title(report)` helper returns the metric
        title used by the HTML renderer. Centralising it means metric
        names live in one place — same place as report_definition and
        cli_invocation."""
        from flowmetrics.report import report_title
        eff = EfficiencyReport(
            input=EfficiencyInput("acme/x", date(2026, 5, 4), date(2026, 5, 10),
                                  4.0, 30.0, False),
            result=_window_result(), interpretation=_interp(),
        )
        cfd = CfdReport(
            input=CfdInput("acme/x", date(2026, 5, 4), date(2026, 5, 10),
                           ("Open", "Done"), 1, False),
            points=[CfdPoint(date(2026, 5, 4), {"Open": 1, "Done": 0})],
            interpretation=_interp(),
        )
        wd = WhenDoneReport(
            input=WhenDoneInput("acme/x", 50, date(2026, 5, 11),
                                date(2026, 4, 11), date(2026, 5, 10), False),
            training=TrainingSummary(
                window_start=date(2026, 4, 11), window_end=date(2026, 5, 10),
                daily_samples=[1], total_merges=1, avg_per_day=1.0,
                min_per_day=1, max_per_day=1, zero_days=0,
            ),
            simulation=SimulationSummary(runs=1000, seed=None),
            histogram=build_histogram([date(2026, 5, 19)]),
            percentiles={50: date(2026, 5, 19)},
            interpretation=_interp(),
        )
        hm = HowManyReport(
            input=HowManyInput("acme/x", date(2026, 5, 11), date(2026, 5, 25),
                               date(2026, 4, 11), date(2026, 5, 10), False),
            training=TrainingSummary(
                window_start=date(2026, 4, 11), window_end=date(2026, 5, 10),
                daily_samples=[1], total_merges=1, avg_per_day=1.0,
                min_per_day=1, max_per_day=1, zero_days=0,
            ),
            simulation=SimulationSummary(runs=1000, seed=None),
            histogram=build_histogram([60]),
            percentiles={50: 60},
            interpretation=_interp(),
        )
        aging = self._aging_report(from_wip_labels=False)
        assert report_title(eff) == "Flow efficiency"
        assert report_title(cfd) == "Cumulative Flow Diagram"
        assert report_title(wd) == "When will it be done?"
        assert report_title(hm) == "How many items?"
        assert report_title(aging) == "Aging Work In Progress"

    def test_efficiency_jira_invocation_emits_jira_url(self):
        """Pre-existing bug class: every report's cli_invocation must
        emit --jira-url / --jira-project for Jira sources, not
        --repo jira:PROJECT."""
        report = EfficiencyReport(
            input=EfficiencyInput(
                repo="jira:BIGTOP",
                start=date(2026, 5, 4),
                stop=date(2026, 5, 10),
                gap_hours=4.0,
                min_cluster_minutes=30.0,
                offline=False,
                jira_url="https://issues.apache.org/jira",
            ),
            result=_window_result(),
            interpretation=_interp(),
        )
        cmd = cli_invocation(report)
        assert "--jira-url https://issues.apache.org/jira" in cmd
        assert "--jira-project BIGTOP" in cmd
        assert "--repo jira:" not in cmd

    def test_cfd_jira_invocation_emits_jira_url(self):
        report = CfdReport(
            input=CfdInput(
                repo="jira:BIGTOP",
                start=date(2026, 5, 4),
                stop=date(2026, 5, 10),
                workflow=("Open", "Done"),
                interval_days=1,
                offline=False,
                jira_url="https://issues.apache.org/jira",
            ),
            points=[
                CfdPoint(date(2026, 5, 4), {"Open": 1, "Done": 0}),
                CfdPoint(date(2026, 5, 10), {"Open": 2, "Done": 1}),
            ],
            interpretation=_interp(),
        )
        cmd = cli_invocation(report)
        assert "--jira-url https://issues.apache.org/jira" in cmd
        assert "--jira-project BIGTOP" in cmd
        assert "--repo jira:" not in cmd

    def test_when_done_jira_invocation_emits_jira_url(self):
        report = WhenDoneReport(
            input=WhenDoneInput(
                "jira:BIGTOP", 50, date(2026, 5, 11),
                date(2026, 4, 11), date(2026, 5, 10), False,
                jira_url="https://issues.apache.org/jira",
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
            simulation=SimulationSummary(runs=1000, seed=None),
            histogram=build_histogram([date(2026, 5, 19)]),
            percentiles={50: date(2026, 5, 19)},
            interpretation=_interp(),
        )
        cmd = cli_invocation(report)
        assert "--jira-url https://issues.apache.org/jira" in cmd
        assert "--jira-project BIGTOP" in cmd
        assert "--repo jira:" not in cmd

    def test_how_many_jira_invocation_emits_jira_url(self):
        report = HowManyReport(
            input=HowManyInput(
                "jira:BIGTOP", date(2026, 5, 11), date(2026, 5, 25),
                date(2026, 4, 11), date(2026, 5, 10), False,
                jira_url="https://issues.apache.org/jira",
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
            simulation=SimulationSummary(runs=1000, seed=None),
            histogram=build_histogram([60]),
            percentiles={50: 60},
            interpretation=_interp(),
        )
        cmd = cli_invocation(report)
        assert "--jira-url https://issues.apache.org/jira" in cmd
        assert "--jira-project BIGTOP" in cmd
        assert "--repo jira:" not in cmd

    def test_aging_jira_invocation_emits_jira_url_and_project_not_repo(self):
        """Pre-existing bug: when the source is Jira, the reproducer
        emitted `--repo jira:BIGTOP` which is not a runnable command.
        Fix: emit `--jira-url ... --jira-project ...` instead."""
        cmd = cli_invocation(self._aging_report_jira())
        assert "--jira-url https://issues.apache.org/jira" in cmd
        assert "--jira-project BIGTOP" in cmd
        # And the broken `--repo jira:BIGTOP` form is GONE.
        assert "--repo jira:" not in cmd
        assert "--repo BIGTOP" not in cmd

    def test_aging_jira_invocation_falls_back_when_url_missing(self):
        """Defensive: a Jira-prefixed repo without a stored URL still
        emits a runnable-looking command rather than crashing. The user
        will have to fill in --jira-url manually."""
        cmd = cli_invocation(self._aging_report_jira(jira_url=None))
        # Project still extracted; URL is a placeholder the user fixes.
        assert "--jira-project BIGTOP" in cmd
        assert "--jira-url" in cmd
