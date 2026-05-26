"""Layer 2 — tests for `flowmetrics.charts.data_source`.

`build_data_source_model` is pure: per-day creation counts in, a
calendar-heatmap model out. Decisions — log-thirds bucketing, the
180-day cap, weekday/week-anchor, month-tick selection, headline —
are asserted here with no DuckDB and no Vega.
"""

from __future__ import annotations

from datetime import date, timedelta

from flowmetrics.charts.data_source import (
    MAX_DAYS,
    build_data_source_model,
)


class TestEmpty:
    def test_no_creations_yields_empty_model(self):
        m = build_data_source_model([])
        assert m.is_empty
        assert "No work items" in m.headline


class TestSpanAndZeroFill:
    def test_span_covers_every_day_between_first_and_last(self):
        per_day = [
            (date(2026, 1, 1), 2),
            (date(2026, 1, 5), 3),
        ]
        m = build_data_source_model(per_day)
        # 5 calendar days, including the 3 zero-fill days.
        assert len(m.days) == 5
        assert [d.day_iso for d in m.days] == [
            "2026-01-01", "2026-01-02", "2026-01-03",
            "2026-01-04", "2026-01-05",
        ]
        zero_days = [d for d in m.days if d.records == 0]
        assert len(zero_days) == 3

    def test_cap_at_most_recent_180_days(self):
        latest = date(2026, 6, 1)
        per_day = [
            (latest - timedelta(days=400), 1),  # ancient
            (latest, 2),
        ]
        m = build_data_source_model(per_day)
        assert len(m.days) == MAX_DAYS
        assert m.days[-1].day_iso == latest.isoformat()


class TestLevelBuckets:
    def test_zero_records_is_none(self):
        per_day = [(date(2026, 1, 1), 0), (date(2026, 1, 2), 1)]
        m = build_data_source_model(per_day)
        by_iso = {d.day_iso: d.level for d in m.days}
        assert by_iso["2026-01-01"] == "None"

    def test_log_thirds_distributes_levels(self):
        # max_records = 100 → log(records)/log(100) thresholds:
        #   t=1/3 ≈ 4.6 ; t=2/3 ≈ 21.5
        # records 1 → Low ; 10 → Medium ; 100 → High.
        per_day = [
            (date(2026, 1, 1), 1),
            (date(2026, 1, 2), 10),
            (date(2026, 1, 3), 100),
        ]
        m = build_data_source_model(per_day)
        by_iso = {d.day_iso: d.level for d in m.days}
        assert by_iso["2026-01-01"] == "Low"
        assert by_iso["2026-01-02"] == "Medium"
        assert by_iso["2026-01-03"] == "High"


class TestCalendarLayout:
    def test_each_day_carries_weekday_and_week_anchor(self):
        # 2026-01-05 is a Monday.
        per_day = [(date(2026, 1, 5), 1), (date(2026, 1, 8), 1)]
        m = build_data_source_model(per_day)
        d_mon = next(d for d in m.days if d.day_iso == "2026-01-05")
        d_thu = next(d for d in m.days if d.day_iso == "2026-01-08")
        assert d_mon.weekday == "Mon"
        assert d_thu.weekday == "Thu"
        # Both fall in the same Monday-anchored week.
        assert d_mon.week_iso == d_thu.week_iso == "2026-01-05"

    def test_month_ticks_pick_one_week_per_calendar_month(self):
        # Three calendar months touched by Monday-anchored weeks
        # (Jan 1 Thu → Dec 29 Mon, Feb 2 Mon, Feb 15 Sun in week
        # of Feb 9 Mon).
        per_day = [(date(2026, 1, 1), 1), (date(2026, 2, 15), 1)]
        m = build_data_source_model(per_day)
        # Exactly one tick per Year-Month that any anchored week
        # lands in.
        months = {t[:7] for t in m.month_ticks}
        assert months == {"2025-12", "2026-01", "2026-02"}
        assert len(m.month_ticks) == 3


class TestHeadline:
    def test_headline_names_count_earliest_and_most_recent_items(self):
        per_day = [(date(2026, 1, 1), 2), (date(2026, 1, 5), 3)]
        m = build_data_source_model(per_day)
        assert "5 work items" in m.headline
        assert "earliest work item Jan 01, 2026" in m.headline
        assert "most recent work item Jan 05, 2026" in m.headline
