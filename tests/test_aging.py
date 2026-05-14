"""Vacanti's Aging Work In Progress chart.

Aging plots in-flight items by their current workflow state (x-axis,
ordered earliest → latest) against their age in days (y-axis). Percentile
checkpoint lines are drawn from completed items' cycle times — same
distribution shown on the Scatterplot.

Properties under test:
1. Only in-flight items (not yet exited the workflow) appear.
2. Age is days elapsed since the item entered the workflow.
3. Current state = the workflow state the item is sitting in today.
4. Percentile checkpoint lines come from completed-item cycle times.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import ClassVar

from flowmetrics.aging import (
    AgingItem,
    compute_aging,
    compute_aging_distribution,
    cycle_time_percentiles,
    per_state_diagnostic,
    top_interventions,
)
from flowmetrics.compute import FlowEfficiency, StatusInterval, WorkItem


def ts(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, tzinfo=UTC)


def _in_flight(
    item_id: str,
    *,
    created: datetime,
    intervals: list[tuple[str, datetime, datetime]],
) -> WorkItem:
    return WorkItem(
        item_id=item_id,
        title=f"t-{item_id}",
        created_at=created,
        merged_at=None,  # in flight
        status_intervals=[StatusInterval(s, e, name) for name, s, e in intervals],
    )


def _completed(
    item_id: str,
    *,
    created: datetime,
    merged: datetime,
    intervals: list[tuple[str, datetime, datetime]] | None = None,
) -> WorkItem:
    return WorkItem(
        item_id=item_id,
        title=f"t-{item_id}",
        created_at=created,
        merged_at=merged,
        status_intervals=[
            StatusInterval(s, e, name) for name, s, e in (intervals or [])
        ],
    )


class TestComputeAgingFiltering:
    def test_completed_items_are_excluded(self):
        items = [
            _completed("DONE-1", created=ts(2026, 4, 1), merged=ts(2026, 4, 10)),
            _in_flight(
                "OPEN-1",
                created=ts(2026, 4, 20),
                intervals=[("In Progress", ts(2026, 4, 20), ts(2026, 5, 1))],
            ),
        ]
        out = compute_aging(items, asof=date(2026, 5, 12))
        ids = {p.item_id for p in out}
        assert ids == {"OPEN-1"}
        assert "DONE-1" not in ids


class TestAgeComputation:
    def test_age_days_is_today_minus_created_date(self):
        items = [
            _in_flight(
                "X-1",
                created=ts(2026, 5, 1),  # 11 days before asof
                intervals=[("Open", ts(2026, 5, 1), ts(2026, 5, 12))],
            )
        ]
        out = compute_aging(items, asof=date(2026, 5, 12))
        assert out[0].age_days == 11

    def test_age_is_zero_for_just_created_item(self):
        items = [
            _in_flight(
                "X-2",
                created=ts(2026, 5, 12),
                intervals=[("Open", ts(2026, 5, 12), ts(2026, 5, 12))],
            )
        ]
        out = compute_aging(items, asof=date(2026, 5, 12))
        assert out[0].age_days == 0


class TestCurrentStateDerivation:
    def test_current_state_is_last_interval_status(self):
        items = [
            _in_flight(
                "X-1",
                created=ts(2026, 5, 1),
                intervals=[
                    ("Open", ts(2026, 5, 1), ts(2026, 5, 3)),
                    ("In Progress", ts(2026, 5, 3), ts(2026, 5, 12)),
                ],
            )
        ]
        out = compute_aging(items, asof=date(2026, 5, 12))
        assert out[0].current_state == "In Progress"

    def test_no_intervals_falls_back_to_unknown(self):
        items = [
            _in_flight("X-1", created=ts(2026, 5, 1), intervals=[]),
        ]
        out = compute_aging(items, asof=date(2026, 5, 12))
        assert out[0].current_state == "Unknown"


class TestCycleTimePercentiles:
    """The Aging chart overlays percentile lines computed from completed
    items' cycle times. Same distribution as the Scatterplot — used as
    checkpoints to see if in-flight items are aging past historic norms."""

    def test_returns_p50_p70_p85_p95_in_days(self):
        # Cycle times: 1, 2, 3, ..., 10 days
        completed = [
            FlowEfficiency(
                item_id=f"#{i}",
                title=f"PR {i}",
                created_at=ts(2026, 4, 1),
                merged_at=ts(2026, 4, 1) + timedelta(days=i),
                cycle_time=timedelta(days=i),
                active_time=timedelta(days=i),
                efficiency=1.0,
            )
            for i in range(1, 11)
        ]
        pct = cycle_time_percentiles(completed)
        assert set(pct.keys()) == {50, 70, 85, 95}
        # Monotonically non-decreasing across percentiles
        assert pct[50] <= pct[70] <= pct[85] <= pct[95]
        # All values in days, positive
        for v in pct.values():
            assert v > 0

    def test_empty_input_yields_zero_percentiles(self):
        pct = cycle_time_percentiles([])
        assert pct == {50: 0.0, 70: 0.0, 85: 0.0, 95: 0.0}


class TestAgingItemFields:
    def test_carries_title_and_id(self):
        items = [
            _in_flight(
                "BIGTOP-42",
                created=ts(2026, 5, 1),
                intervals=[("In Progress", ts(2026, 5, 1), ts(2026, 5, 12))],
            )
        ]
        out = compute_aging(items, asof=date(2026, 5, 12))
        assert isinstance(out[0], AgingItem)
        assert out[0].item_id == "BIGTOP-42"
        assert out[0].title == "t-BIGTOP-42"


class TestPrUrl:
    """Aging items optionally carry a per-item URL so the interactive
    chart can make each circle clickable. The CLI builds the URL
    function based on the source backend (GitHub vs Jira)."""

    def test_pr_url_is_none_by_default(self):
        items = [
            _in_flight(
                "#42",
                created=ts(2026, 5, 1),
                intervals=[("Awaiting Review", ts(2026, 5, 1), ts(2026, 5, 12))],
            )
        ]
        out = compute_aging(items, asof=date(2026, 5, 12))
        assert out[0].pr_url is None

    def test_pr_url_populated_when_url_for_callable_provided(self):
        items = [
            _in_flight(
                "#42",
                created=ts(2026, 5, 1),
                intervals=[("Awaiting Review", ts(2026, 5, 1), ts(2026, 5, 12))],
            ),
            _in_flight(
                "#7",
                created=ts(2026, 5, 5),
                intervals=[("Approved", ts(2026, 5, 5), ts(2026, 5, 12))],
            ),
        ]
        out = compute_aging(
            items,
            asof=date(2026, 5, 12),
            url_for=lambda item_id: f"https://github.com/acme/widget/pull/{item_id.lstrip('#')}",
        )
        by_id = {it.item_id: it for it in out}
        assert by_id["#42"].pr_url == "https://github.com/acme/widget/pull/42"
        assert by_id["#7"].pr_url == "https://github.com/acme/widget/pull/7"

    def test_url_for_returning_none_keeps_pr_url_none(self):
        """A url_for callable that returns None for unknown ids leaves
        the AgingItem's pr_url untouched (still None)."""
        items = [
            _in_flight(
                "BIGTOP-42",
                created=ts(2026, 5, 1),
                intervals=[("In Progress", ts(2026, 5, 1), ts(2026, 5, 12))],
            )
        ]
        out = compute_aging(items, asof=date(2026, 5, 12), url_for=lambda _id: None)
        assert out[0].pr_url is None


class TestMaxAgeFilter:
    """Opt-in `max_age_days` excludes long-tail stalled items so the
    chart can focus on actionable WIP. Default behavior (no filter)
    matches Vacanti: every in-flight item is visible.
    """

    @staticmethod
    def _items_at_various_ages(asof: date) -> list[WorkItem]:
        """5 in-flight items at ages 1, 30, 100, 200, 500 days."""
        ages = [1, 30, 100, 200, 500]
        out = []
        for i, age in enumerate(ages):
            created = datetime.combine(
                asof - timedelta(days=age), datetime.min.time(), tzinfo=UTC
            ) + timedelta(hours=12)
            out.append(
                _in_flight(
                    f"OPEN-{i}",
                    created=created,
                    intervals=[("In Progress", created, created + timedelta(days=1))],
                )
            )
        return out

    def test_no_max_age_shows_everything(self):
        asof = date(2026, 5, 12)
        items = self._items_at_various_ages(asof)
        out = compute_aging(items, asof=asof)
        assert {it.item_id for it in out} == {f"OPEN-{i}" for i in range(5)}

    def test_max_age_days_excludes_items_above_threshold(self):
        """With max_age_days=180, ages 200 and 500 are dropped; 1/30/100 stay."""
        asof = date(2026, 5, 12)
        items = self._items_at_various_ages(asof)
        out = compute_aging(items, asof=asof, max_age_days=180)
        kept = {it.item_id for it in out}
        assert kept == {"OPEN-0", "OPEN-1", "OPEN-2"}
        # All retained items have age <= 180
        assert all(it.age_days <= 180 for it in out)

    def test_max_age_days_boundary_inclusive(self):
        """An item exactly at max_age_days is kept (≤, not <)."""
        asof = date(2026, 5, 12)
        created = datetime.combine(
            asof - timedelta(days=180), datetime.min.time(), tzinfo=UTC
        ) + timedelta(hours=12)
        items = [
            _in_flight(
                "EDGE",
                created=created,
                intervals=[("In Progress", created, created + timedelta(days=1))],
            )
        ]
        out = compute_aging(items, asof=asof, max_age_days=180)
        assert len(out) == 1 and out[0].age_days == 180

    def test_max_age_days_none_is_explicit_no_filter(self):
        """Passing None is the same as omitting — no filter applied."""
        asof = date(2026, 5, 12)
        items = self._items_at_various_ages(asof)
        out = compute_aging(items, asof=asof, max_age_days=None)
        assert len(out) == 5

    def test_max_age_days_zero_keeps_only_items_created_today(self):
        """max_age_days=0 is an edge case but should not crash; only
        items created today (age 0) survive."""
        asof = date(2026, 5, 12)
        items = self._items_at_various_ages(asof)
        out = compute_aging(items, asof=asof, max_age_days=0)
        assert out == []  # the youngest item in the fixture is age=1


class TestComputeAgingDistribution:
    """Bands an in-flight age distribution against the percentile-line
    thresholds. Five bands (Below P50, P50-P70, P70-P85, P85-P95,
    Above P95). The diagnostic that makes the survivorship-bias story
    visible at a glance.
    """

    PCT: ClassVar[dict[int, float]] = {50: 1.7, 70: 5.4, 85: 17.8, 95: 57.4}

    @staticmethod
    def _item(age: int) -> AgingItem:
        return AgingItem(
            item_id=f"#{age}",
            title=f"PR {age}",
            current_state="Awaiting Review",
            age_days=age,
        )

    def test_each_band_has_label_lower_upper_count_share(self):
        items = [self._item(a) for a in [0, 3, 10, 30, 100]]
        dist = compute_aging_distribution(items, self.PCT)
        assert [b["label"] for b in dist] == [
            "Below P50",
            "P50–P70",
            "P70–P85",
            "P85–P95",
            "Above P95",
        ]
        for b in dist:
            assert set(b) >= {"label", "lower", "upper", "count", "share"}

    def test_band_bounds_use_percentile_values(self):
        items = [self._item(0)]
        dist = compute_aging_distribution(items, self.PCT)
        bounds = [(b["lower"], b["upper"]) for b in dist]
        # Below P50: (None, 1.7); P50–P70: [1.7, 5.4); P70–P85: [5.4, 17.8);
        # P85–P95: [17.8, 57.4); Above P95: [57.4, None)
        assert bounds == [
            (None, 1.7),
            (1.7, 5.4),
            (5.4, 17.8),
            (17.8, 57.4),
            (57.4, None),
        ]

    def test_counts_assign_items_to_correct_bands(self):
        # Build items at carefully chosen ages so we can hand-check.
        items = [
            self._item(0),    # Below P50
            self._item(1),    # Below P50 (< P50 = 1.7)
            self._item(3),    # P50–P70 (1.7 ≤ 3 < 5.4)
            self._item(10),   # P70–P85 (5.4 ≤ 10 < 17.8)
            self._item(30),   # P85–P95 (17.8 ≤ 30 < 57.4)
            self._item(100),  # Above P95 (≥ 57.4)
            self._item(57),   # P85–P95 (17.8 ≤ 57 < 57.4)  ← boundary check
        ]
        dist = compute_aging_distribution(items, self.PCT)
        counts = {b["label"]: b["count"] for b in dist}
        assert counts == {
            "Below P50": 2,
            "P50–P70": 1,
            "P70–P85": 1,
            "P85–P95": 2,
            "Above P95": 1,
        }
        # Shares are fractions of the total (7 items).
        for b in dist:
            assert b["share"] == b["count"] / 7

    def test_boundary_at_p95_belongs_to_above_p95(self):
        """An item at exactly P95 belongs to the Above-P95 band, matching
        the interpretation layer's >= P95 comparison."""
        items = [self._item(57)]  # < 57.4 → P85–P95
        items_at_95 = [self._item(58)]  # > 57.4 → Above P95
        # 57 < 57.4 → P85–P95
        d1 = compute_aging_distribution(items, self.PCT)
        assert {b["label"]: b["count"] for b in d1}["P85–P95"] == 1
        # 58 > 57.4 → Above P95
        d2 = compute_aging_distribution(items_at_95, self.PCT)
        assert {b["label"]: b["count"] for b in d2}["Above P95"] == 1

    def test_empty_items_returns_zero_counts(self):
        dist = compute_aging_distribution([], self.PCT)
        for b in dist:
            assert b["count"] == 0
            assert b["share"] == 0.0

    def test_missing_percentile_keys_does_not_crash(self):
        """If percentiles are zeros (no completed items fed training),
        bands collapse — everything is in the topmost defined band."""
        dist = compute_aging_distribution(
            [self._item(5)], {50: 0.0, 70: 0.0, 85: 0.0, 95: 0.0}
        )
        # All thresholds at 0 → any positive age is Above P95.
        counts = {b["label"]: b["count"] for b in dist}
        assert counts["Above P95"] == 1
        # And the other bands are empty.
        for label in ["Below P50", "P50–P70", "P70–P85", "P85–P95"]:
            assert counts[label] == 0


class TestPerStateDiagnostic:
    """Per-state aging breakdown — the bottleneck diagnostic. For each
    state in workflow order, surface count, median age, oldest age,
    and count past P85/P95. Empty states get a row with all zeros so
    the chart and table agree on column membership.
    """

    PCT: ClassVar[dict[int, float]] = {50: 1.7, 70: 5.4, 85: 17.8, 95: 57.4}

    @staticmethod
    def _item(item_id: str, state: str, age: int) -> AgingItem:
        return AgingItem(
            item_id=item_id, title=f"PR {item_id}", current_state=state, age_days=age
        )

    def test_rows_one_per_workflow_state_in_order(self):
        rows = per_state_diagnostic(
            items=[
                self._item("#1", "Awaiting Review", 3),
                self._item("#2", "Approved", 10),
            ],
            workflow=("Awaiting Review", "Approved"),
            percentiles=self.PCT,
        )
        assert [r["state"] for r in rows] == ["Awaiting Review", "Approved"]

    def test_empty_state_has_zero_row(self):
        """A state with zero items still appears so the table and chart
        agree on column membership."""
        rows = per_state_diagnostic(
            items=[self._item("#1", "Awaiting Review", 3)],
            workflow=("Awaiting Review", "Approved"),
            percentiles=self.PCT,
        )
        approved = next(r for r in rows if r["state"] == "Approved")
        assert approved["count"] == 0
        assert approved["median_age_days"] is None
        assert approved["oldest_age_days"] is None
        assert approved["past_p85"] == 0
        assert approved["past_p95"] == 0

    def test_aggregates_count_median_oldest_per_state(self):
        items = [
            self._item("#1", "Awaiting Review", 1),
            self._item("#2", "Awaiting Review", 5),
            self._item("#3", "Awaiting Review", 100),
            self._item("#4", "Approved", 50),
        ]
        rows = per_state_diagnostic(
            items=items, workflow=("Awaiting Review", "Approved"), percentiles=self.PCT
        )
        ar = next(r for r in rows if r["state"] == "Awaiting Review")
        assert ar["count"] == 3
        assert ar["oldest_age_days"] == 100
        assert ar["median_age_days"] == 5  # of [1, 5, 100]

    def test_past_p85_and_p95_counts(self):
        # P85 = 17.8, P95 = 57.4 in self.PCT.
        items = [
            self._item("#1", "Awaiting Review", 5),    # under P85
            self._item("#2", "Awaiting Review", 20),   # past P85, under P95
            self._item("#3", "Awaiting Review", 100),  # past P95
        ]
        rows = per_state_diagnostic(
            items=items, workflow=("Awaiting Review",), percentiles=self.PCT
        )
        ar = rows[0]
        assert ar["past_p85"] == 2  # 20 and 100 (>= 17.8)
        assert ar["past_p95"] == 1  # 100 (>= 57.4)

    def test_at_risk_count_for_p50_to_p85_cohort(self):
        """Per Vacanti's doubling math: items between P50 and P85 are
        in the 'elevated risk' cohort — they've crossed at least the
        50% threshold (risk has doubled from 15% to 30%) but haven't
        yet missed the 85th-percentile forecast. Surfaced as a column
        so managers can name the items that need a conversation NOW."""
        # PCT has P50=1.7, P85=17.8.
        items = [
            self._item("#a", "Awaiting Review", 1),    # under P50 — calm
            self._item("#b", "Awaiting Review", 3),    # P50-P85 — at risk
            self._item("#c", "Awaiting Review", 10),   # P50-P85 — at risk
            self._item("#d", "Awaiting Review", 20),   # past P85 — already missed
        ]
        rows = per_state_diagnostic(
            items=items, workflow=("Awaiting Review",), percentiles=self.PCT
        )
        ar = rows[0]
        assert ar["at_risk_p50_to_p85"] == 2  # #b and #c


class TestTopInterventions:
    """Per-state oldest-past-P85, ordered rightmost-first. The list
    surfaces the highest-leverage actions: one per stuck stage of the
    pipeline, biased toward states closer to done."""

    PCT: ClassVar[dict[int, float]] = {50: 1.7, 70: 5.4, 85: 17.8, 95: 57.4}

    @staticmethod
    def _item(item_id: str, state: str, age: int, url: str | None = None) -> AgingItem:
        return AgingItem(
            item_id=item_id,
            title=f"PR {item_id}",
            current_state=state,
            age_days=age,
            pr_url=url,
        )

    def test_returns_one_oldest_past_p85_per_state(self):
        items = [
            self._item("#1", "Awaiting Review", 20),
            self._item("#2", "Awaiting Review", 50),  # oldest past P85 in this state
            self._item("#3", "Approved", 25),
            self._item("#4", "Approved", 60),         # oldest past P85 in this state
        ]
        out = top_interventions(
            items=items, workflow=("Awaiting Review", "Approved"), percentiles=self.PCT
        )
        ids = [it["item_id"] for it in out]
        assert "#2" in ids
        assert "#4" in ids

    def test_ordered_rightmost_first_highest_leverage(self):
        """Rightmost workflow state (most progress) wins ordering —
        items closest to ship are the highest-leverage interventions."""
        items = [
            self._item("#1", "Awaiting Review", 100),  # leftmost state
            self._item("#2", "Approved", 50),          # rightmost state
        ]
        out = top_interventions(
            items=items, workflow=("Awaiting Review", "Approved"), percentiles=self.PCT
        )
        # Approved appears first despite having the younger item.
        assert out[0]["current_state"] == "Approved"
        assert out[1]["current_state"] == "Awaiting Review"

    def test_skips_states_with_no_past_p85_items(self):
        """A healthy state contributes nothing to interventions."""
        items = [
            self._item("#1", "Awaiting Review", 50),  # past P85
            self._item("#2", "Approved", 5),          # under P85 (healthy)
        ]
        out = top_interventions(
            items=items, workflow=("Awaiting Review", "Approved"), percentiles=self.PCT
        )
        assert len(out) == 1
        assert out[0]["item_id"] == "#1"

    def test_empty_when_pipeline_healthy(self):
        """No items past P85 → empty list (page shows healthy)."""
        items = [self._item("#1", "Awaiting Review", 1)]
        out = top_interventions(
            items=items, workflow=("Awaiting Review",), percentiles=self.PCT
        )
        assert out == []

    def test_per_state_n_one_collapses_back_to_one_per_state(self):
        """Backwards-compat behavior — per_state_n=1 matches the
        original 'oldest-per-state' heuristic."""
        workflow = ("S0", "S1", "S2")
        items = [self._item(f"#{i}", f"S{i % 3}", 100 - i) for i in range(9)]
        out = top_interventions(
            items=items, workflow=workflow, percentiles=self.PCT, per_state_n=1
        )
        assert len(out) == 3
        assert {iv["current_state"] for iv in out} == {"S0", "S1", "S2"}

    def test_default_returns_three_per_state(self):
        """Default per_state_n=3 gives a more useful action list than
        1-per-state, especially when one state has the lion's share
        of stuck items."""
        items = [
            self._item(f"#{i}", "Awaiting Review", 50 + i)
            for i in range(5)
        ] + [self._item("#999", "Approved", 80)]
        out = top_interventions(
            items=items, workflow=("Awaiting Review", "Approved"),
            percentiles=self.PCT,
        )
        # 3 from Awaiting Review + 1 from Approved = 4 items.
        states = [iv["current_state"] for iv in out]
        assert states.count("Awaiting Review") == 3
        assert states.count("Approved") == 1
        # And within each state, ordered oldest first.
        ar_ages = [iv["age_days"] for iv in out if iv["current_state"] == "Awaiting Review"]
        assert ar_ages == sorted(ar_ages, reverse=True)

    def test_global_cap_of_fifteen(self):
        """Even with many states and 3 per state, total cap is 15."""
        workflow = tuple(f"S{i}" for i in range(6))
        items = [
            self._item(f"#{s}-{i}", f"S{s}", 100 - i)
            for s in range(6)
            for i in range(3)
        ]
        # 6 states × 3 per state = 18; capped at 15.
        out = top_interventions(
            items=items, workflow=workflow, percentiles=self.PCT,
        )
        assert len(out) == 15

    def test_carries_pr_url_when_set(self):
        items = [self._item("#1", "Approved", 50, url="https://x/1")]
        out = top_interventions(
            items=items, workflow=("Approved",), percentiles=self.PCT
        )
        assert out[0]["pr_url"] == "https://x/1"

    def test_unknown_state_does_not_crash_and_is_skipped(self):
        """If an item's current_state isn't in the workflow tuple
        (e.g. 'Unknown' fallback), skip it — workflow position is
        undefined, so it can't be ordered."""
        items = [
            self._item("#1", "Unknown", 100),     # not in workflow
            self._item("#2", "Approved", 50),     # in workflow
        ]
        out = top_interventions(
            items=items, workflow=("Approved",), percentiles=self.PCT
        )
        assert [it["item_id"] for it in out] == ["#2"]


class TestEmptyInputs:
    def test_no_items_returns_empty(self):
        out = compute_aging([], asof=date(2026, 5, 12))
        assert out == []
