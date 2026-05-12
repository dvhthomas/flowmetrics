"""Behavioural spec for the interpretation layer.

interpret_* functions turn raw results into agent-actionable narrative.
Contract:

- A non-empty `headline` that names the repo and the headline metric.
- A `key_insight` that explains the result.
- A list of `next_actions` (always at least one).
- A list of `caveats` (always at least one, since these are statistical
  estimates with known limitations).
- Behaviour is data-sensitive: empty windows, low-FE vs high-FE,
  forecasts where items-to-complete > total historical throughput, etc. each
  produce a different narrative.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from flowmetrics.cfd import CfdPoint
from flowmetrics.compute import FlowEfficiency, WindowResult
from flowmetrics.forecast import build_histogram
from flowmetrics.interpretation import (
    interpret_cfd,
    interpret_efficiency,
    interpret_how_many,
    interpret_when_done,
)
from flowmetrics.report import (
    CfdInput,
    EfficiencyInput,
    HowManyInput,
    TrainingSummary,
    WhenDoneInput,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _eff_input(repo="acme/widget"):
    return EfficiencyInput(
        repo=repo,
        start=date(2026, 5, 4),
        stop=date(2026, 5, 10),
        gap_hours=4.0,
        min_cluster_minutes=30.0,
        offline=False,
    )


def _pr(n: int, cycle_hours: float, eff: float) -> FlowEfficiency:
    return FlowEfficiency(
        item_id=f"#{n}",
        title=f"PR {n}",
        created_at=datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
        merged_at=datetime(2026, 5, 5, 9, 0, tzinfo=UTC) + timedelta(hours=cycle_hours),
        cycle_time=timedelta(hours=cycle_hours),
        active_time=timedelta(hours=cycle_hours * eff),
        efficiency=eff,
    )


def _window_result(prs):
    if not prs:
        return WindowResult(0, 0.0, 0.0, 0.0, timedelta(), timedelta(), [])
    total_cycle = sum((p.cycle_time for p in prs), start=timedelta())
    total_active = sum((p.active_time for p in prs), start=timedelta())
    portfolio = total_active.total_seconds() / total_cycle.total_seconds()
    ratios = [p.efficiency for p in prs]
    return WindowResult(
        pr_count=len(prs),
        portfolio_efficiency=portfolio,
        mean_efficiency=sum(ratios) / len(ratios),
        median_efficiency=sorted(ratios)[len(ratios) // 2],
        total_cycle=total_cycle,
        total_active=total_active,
        per_pr=prs,
    )


# ---------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------


class TestInterpretEfficiency:
    def test_empty_window_advises_widening(self):
        result = _window_result([])
        i = interpret_efficiency(_eff_input(), result)
        assert "acme/widget" in i.headline
        assert "No PRs" in i.headline or "no prs" in i.headline.lower()
        assert any("widen" in a.lower() or "window" in a.lower() for a in i.next_actions)

    def test_low_fe_flags_vacanti_typical_band(self):
        # Slow tail dominates: 1 huge PR + 4 tiny ones → portfolio < 10%
        prs = [_pr(1, cycle_hours=200, eff=0.01)] + [
            _pr(n, cycle_hours=0.5, eff=1.0) for n in range(2, 6)
        ]
        i = interpret_efficiency(_eff_input(), _window_result(prs))
        # Headline includes the FE %
        assert "%" in i.headline
        # Mentions Vacanti's range
        assert "5" in i.key_insight and "15" in i.key_insight

    def test_high_fe_flags_data_quality(self):
        prs = [_pr(n, cycle_hours=0.5, eff=1.0) for n in range(5)]
        i = interpret_efficiency(_eff_input(), _window_result(prs))
        assert i.key_insight  # non-empty
        # When FE is suspiciously high, narrative names a likely cause
        text = (i.key_insight + " ".join(i.next_actions) + " ".join(i.caveats)).lower()
        assert any(
            token in text for token in ["dependabot", "automation", "version bump", "verify"]
        )

    def test_slowest_pr_called_out_when_long_runners_exist(self):
        prs = [
            _pr(1, cycle_hours=400, eff=0.01),  # very slow
            _pr(2, cycle_hours=1.0, eff=1.0),
            _pr(3, cycle_hours=1.0, eff=1.0),
        ]
        i = interpret_efficiency(_eff_input(), _window_result(prs))
        assert any("#1" in a for a in i.next_actions)

    def test_caveats_always_present(self):
        prs = [_pr(1, cycle_hours=10, eff=0.3)]
        i = interpret_efficiency(_eff_input(), _window_result(prs))
        assert len(i.caveats) >= 1
        # Always warn about per-engineer misuse
        assert any("engineer" in c.lower() or "individual" in c.lower() for c in i.caveats)


# ---------------------------------------------------------------------------
# Forecast: when-done
# ---------------------------------------------------------------------------


def _training(samples, start, end):
    return TrainingSummary(
        window_start=start,
        window_end=end,
        daily_samples=samples,
        total_merges=sum(samples),
        avg_per_day=sum(samples) / len(samples) if samples else 0.0,
        min_per_day=min(samples) if samples else 0,
        max_per_day=max(samples) if samples else 0,
        zero_days=sum(1 for s in samples if s == 0),
    )


class TestProseDateFormat:
    """Prose dates in headlines/insights/actions use `Jan 12, 2026` —
    always with year and comma. ISO stays in code/JSON/cli_invocation."""

    def test_efficiency_headline_uses_prose_dates(self):
        prs = [_pr(1, cycle_hours=24, eff=0.5)]
        i = interpret_efficiency(
            _eff_input(repo="acme/widget"),
            _window_result(prs),
        )
        # Original ISO ("2026-05-04") should be replaced by prose ("May 4, 2026")
        assert "May 4, 2026" in i.headline or "May 04, 2026" in i.headline
        assert "May 10, 2026" in i.headline or "May 10, 2026" in i.headline
        # And the old ISO form must be absent from prose
        assert "2026-05-04" not in i.headline
        assert "2026-05-10" not in i.headline

    def test_when_done_headline_uses_prose_dates(self):
        input_ = WhenDoneInput(
            repo="acme/widget", items=50, start_date=date(2026, 5, 11),
            history_start=date(2026, 4, 11), history_end=date(2026, 5, 10),
            offline=False,
        )
        training = _training([5] * 30, date(2026, 4, 11), date(2026, 5, 10))
        percentiles = {
            50: date(2026, 5, 19),
            70: date(2026, 5, 21),
            85: date(2026, 5, 23),
            95: date(2026, 5, 25),
        }
        hist = build_histogram([date(2026, 5, 20)])
        i = interpret_when_done(input_, training, hist, percentiles)
        assert "May 23, 2026" in i.headline
        assert "2026-05-23" not in i.headline


class TestStatusMismatchDiagnostic:
    """When a Jira-style item set shows zero active time and the observed
    statuses don't intersect the configured --active-statuses, the
    interpretation calls out the mismatch and suggests a fix."""

    def _input_with_active(self, active: tuple[str, ...]) -> EfficiencyInput:
        return EfficiencyInput(
            repo="jira:BIGTOP",
            start=date(2026, 5, 4),
            stop=date(2026, 5, 10),
            gap_hours=4.0,
            min_cluster_minutes=30.0,
            offline=False,
            active_statuses=active,
        )

    def test_zero_efficiency_with_unmatched_statuses_suggests_remap(self):
        prs = [_pr(1, cycle_hours=24, eff=0.0)]
        result = WindowResult(
            pr_count=1, portfolio_efficiency=0.0,
            mean_efficiency=0.0, median_efficiency=0.0,
            total_cycle=timedelta(hours=24),
            total_active=timedelta(0),
            per_pr=prs,
            observed_statuses=["Open", "Patch Available"],
        )
        i = interpret_efficiency(
            self._input_with_active(("In Progress", "In Development")),
            result,
        )
        text = " ".join(i.next_actions).lower()
        assert "active-statuses" in text
        # Concrete suggestion names an observed status
        assert "patch available" in text or "open" in text

    def test_zero_efficiency_with_overlap_does_not_suggest_remap(self):
        prs = [_pr(1, cycle_hours=24, eff=0.0)]
        result = WindowResult(
            pr_count=1, portfolio_efficiency=0.0,
            mean_efficiency=0.0, median_efficiency=0.0,
            total_cycle=timedelta(hours=24),
            total_active=timedelta(0),
            per_pr=prs,
            observed_statuses=["In Progress", "Open"],
        )
        i = interpret_efficiency(
            self._input_with_active(("In Progress",)),
            result,
        )
        text = " ".join(i.next_actions).lower()
        assert "active-statuses" not in text


class TestInterpretWhenDone:
    def test_headline_names_repo_85th_percentile_and_item_count(self):
        input_ = WhenDoneInput(
            repo="acme/widget",
            items=50,
            start_date=date(2026, 5, 11),
            history_start=date(2026, 4, 11),
            history_end=date(2026, 5, 10),
            offline=False,
        )
        training = _training([5] * 30, date(2026, 4, 11), date(2026, 5, 10))
        hist = build_histogram([date(2026, 5, 20)] * 10)
        percentiles = {
            50: date(2026, 5, 19),
            70: date(2026, 5, 21),
            85: date(2026, 5, 23),
            95: date(2026, 5, 25),
        }
        i = interpret_when_done(input_, training, hist, percentiles)
        assert "acme/widget" in i.headline
        assert "50" in i.headline  # number of items
        assert "May 23, 2026" in i.headline  # 85th percentile (prose format)

    def test_next_actions_warn_when_items_exceeds_training_throughput(self):
        input_ = WhenDoneInput(
            repo="acme/widget",
            items=10_000,
            start_date=date(2026, 5, 11),
            history_start=date(2026, 4, 11),
            history_end=date(2026, 5, 10),
            offline=False,
        )
        training = _training([1] * 30, date(2026, 4, 11), date(2026, 5, 10))
        hist = build_histogram([date(2026, 5, 19)])
        percentiles = {p: date(2026, 5, 19) for p in (50, 70, 85, 95)}
        i = interpret_when_done(input_, training, hist, percentiles)
        text = " ".join(i.next_actions).lower()
        assert "items" in text or "extrapolat" in text
        # Must NOT use the overloaded "backlog" term anywhere
        assert "backlog" not in text.lower()

    def test_no_interpretation_text_uses_the_overloaded_term_backlog(self):
        """Vacanti is explicit that 'backlog' is contaminated (Scrum
        overloads it). We never use it in user-facing narrative copy."""
        input_ = WhenDoneInput(
            "acme/widget",
            50,
            date(2026, 5, 11),
            date(2026, 4, 11),
            date(2026, 5, 10),
            False,
        )
        training = _training([5] * 30, date(2026, 4, 11), date(2026, 5, 10))
        hist = build_histogram([date(2026, 5, 19)])
        percentiles = {p: date(2026, 5, 19) for p in (50, 70, 85, 95)}
        i = interpret_when_done(input_, training, hist, percentiles)
        blob = " ".join([i.headline, i.key_insight, *i.next_actions, *i.caveats]).lower()
        assert "backlog" not in blob

    def test_long_horizon_triggers_constraint_suggestion(self):
        """Vacanti: shorter forecasts are better. When the 85% percentile
        date lands further out than the training window, the next-actions
        should suggest constraining (smaller --items)."""
        input_ = WhenDoneInput(
            repo="acme/widget",
            items=500,
            start_date=date(2026, 5, 11),
            history_start=date(2026, 4, 11),
            history_end=date(2026, 5, 10),
            offline=False,
        )
        training = _training([5] * 30, date(2026, 4, 11), date(2026, 5, 10))
        # p85 lands 90 days out — far past the 30-day training window
        percentiles = {
            50: date(2026, 7, 1),
            70: date(2026, 7, 15),
            85: date(2026, 8, 9),
            95: date(2026, 8, 31),
        }
        hist = build_histogram([date(2026, 7, 1)])
        i = interpret_when_done(input_, training, hist, percentiles)
        text = " ".join(i.next_actions).lower()
        # Should suggest shorter forecast — smaller items or re-running
        assert "shorter" in text or "fewer items" in text or "horizon" in text

    def test_caveats_warn_about_regime_change(self):
        input_ = WhenDoneInput(
            "acme/widget",
            50,
            date(2026, 5, 11),
            date(2026, 4, 11),
            date(2026, 5, 10),
            False,
        )
        training = _training([5] * 30, date(2026, 4, 11), date(2026, 5, 10))
        hist = build_histogram([date(2026, 5, 19)])
        percentiles = {p: date(2026, 5, 19) for p in (50, 70, 85, 95)}
        i = interpret_when_done(input_, training, hist, percentiles)
        text = " ".join(i.caveats).lower()
        assert "regime" in text or "past" in text


# ---------------------------------------------------------------------------
# Forecast: how-many
# ---------------------------------------------------------------------------


class TestInterpretHowMany:
    def test_headline_names_85th_percentile_items(self):
        input_ = HowManyInput(
            "acme/widget",
            date(2026, 5, 11),
            date(2026, 5, 25),
            date(2026, 4, 11),
            date(2026, 5, 10),
            False,
        )
        training = _training([5] * 30, date(2026, 4, 11), date(2026, 5, 10))
        hist = build_histogram([60] * 10)
        percentiles = {50: 89, 70: 76, 85: 64, 95: 51}
        i = interpret_how_many(input_, training, hist, percentiles)
        assert "acme/widget" in i.headline
        assert "64" in i.headline  # 85th percentile commitment

    def test_caveat_calls_out_backward_percentile_direction(self):
        input_ = HowManyInput(
            "acme/widget",
            date(2026, 5, 11),
            date(2026, 5, 25),
            date(2026, 4, 11),
            date(2026, 5, 10),
            False,
        )
        training = _training([5] * 30, date(2026, 4, 11), date(2026, 5, 10))
        hist = build_histogram([60])
        percentiles = {50: 89, 70: 76, 85: 64, 95: 51}
        i = interpret_how_many(input_, training, hist, percentiles)
        text = " ".join(i.caveats).upper()
        assert "BACKWARD" in text or "BACKWARDS" in text or "FEWER" in text.upper()

    def test_long_horizon_warning(self):
        # Forecast 60 days from 30 days of history → narrative should flag it
        input_ = HowManyInput(
            "acme/widget",
            date(2026, 5, 11),
            date(2026, 7, 11),
            date(2026, 4, 11),
            date(2026, 5, 10),
            False,
        )
        training = _training([5] * 30, date(2026, 4, 11), date(2026, 5, 10))
        hist = build_histogram([100])
        percentiles = {50: 100, 70: 90, 85: 80, 95: 70}
        i = interpret_how_many(input_, training, hist, percentiles)
        text = " ".join(i.next_actions).lower()
        assert "horizon" in text or "longer" in text


# ---------------------------------------------------------------------------
# CFD
# ---------------------------------------------------------------------------


def _cfd_input(repo="acme/widget"):
    return CfdInput(
        repo=repo,
        start=date(2026, 5, 4),
        stop=date(2026, 5, 10),
        workflow=("Open", "In Progress", "Done"),
        interval_days=1,
        offline=False,
    )


def _cfd_points_growing() -> list[CfdPoint]:
    return [
        CfdPoint(date(2026, 5, 4), {"Open": 0, "In Progress": 0, "Done": 0}),
        CfdPoint(date(2026, 5, 5), {"Open": 2, "In Progress": 1, "Done": 0}),
        CfdPoint(date(2026, 5, 6), {"Open": 4, "In Progress": 2, "Done": 1}),
        CfdPoint(date(2026, 5, 10), {"Open": 8, "In Progress": 5, "Done": 3}),
    ]


class TestInterpretCfd:
    def test_headline_names_repo_arrivals_and_departures(self):
        i = interpret_cfd(_cfd_input(), _cfd_points_growing())
        assert "acme/widget" in i.headline
        # arrivals=8, departures=3, WIP=5 at end
        assert "8" in i.headline
        assert "3" in i.headline

    def test_key_insight_names_biggest_wip_band(self):
        # At end: Open-band = Open - In Progress = 8 - 5 = 3
        # In Progress-band = In Progress - Done = 5 - 3 = 2
        # Open-band is wider → bottleneck is at "Open"
        i = interpret_cfd(_cfd_input(), _cfd_points_growing())
        assert "Open" in i.key_insight or "WIP" in i.key_insight

    def test_caveats_warn_past_data_only(self):
        i = interpret_cfd(_cfd_input(), _cfd_points_growing())
        text = " ".join(i.caveats).lower()
        assert "past" in text or "history" in text or "projection" in text

    def test_empty_points_handled_safely(self):
        i = interpret_cfd(_cfd_input(), [])
        assert "acme/widget" in i.headline
        assert i.next_actions  # must always give the reader something to do

    def test_no_completed_items_calls_out_zero_throughput(self):
        points = [
            CfdPoint(date(2026, 5, 4), {"Open": 1, "In Progress": 0, "Done": 0}),
            CfdPoint(date(2026, 5, 10), {"Open": 5, "In Progress": 5, "Done": 0}),
        ]
        i = interpret_cfd(_cfd_input(), points)
        text = (i.key_insight + " " + " ".join(i.next_actions)).lower()
        assert "0" in i.headline or "no" in text or "zero" in text
