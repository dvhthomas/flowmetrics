"""Tests for `flowmetrics.windows` — the date-range types and the
single date-resolution model behind the filter bar.

`parse_windows` is the ONE place date math happens: the filter
bar emits a `period` choice, `parse_windows` resolves it into a
`WindowSelection`, and every view reads that model.

  - **View window** — navigation. What date range the charts
    show. Anchored to today by default.
  - **Reference period** — the statistical sample for aging
    thresholds and forecasts. Anchored to the most recent data
    (`data_max`), NOT the view — so scrolling the view never
    strands the percentile sample.

This suite uses raw query dicts on purpose: it IS the test of
the `?period=` / `?anchor=` / `?ref_days=` param contract.
Both windows are inclusive on both endpoints.
"""

from __future__ import annotations

from datetime import date

from flowmetrics.windows import (
    DEFAULT_VIEW_DAYS,
    Window,
    WindowSelection,
    last_completed_week,
    parse_windows,
)


class TestWindow:
    def test_from_and_to_are_inclusive(self):
        w = Window(from_=date(2026, 5, 4), to=date(2026, 5, 10))
        assert w.days_inclusive == 7  # May 4..10 inclusive

    def test_same_day_is_one_day_inclusive(self):
        w = Window(from_=date(2026, 5, 4), to=date(2026, 5, 4))
        assert w.days_inclusive == 1, (
            "single-day window must report 1 day (inclusive endpoints)"
        )

    def test_last_n_days_anchors_at_today(self):
        today = date(2026, 5, 20)
        w = Window.last_n_days(30, today=today)
        assert w.to == today
        assert w.from_ == date(2026, 4, 21)  # 30 days inclusive
        assert w.days_inclusive == 30

    def test_last_n_days_with_1_day_is_today_only(self):
        today = date(2026, 5, 20)
        w = Window.last_n_days(1, today=today)
        assert w.from_ == today
        assert w.to == today
        assert w.days_inclusive == 1


class TestParseWindowsViewPresets:
    TODAY = date(2026, 5, 20)  # a Wednesday

    def test_no_period_defaults_to_last_30_days(self):
        sel = parse_windows({}, today=self.TODAY)
        assert sel.period == "last-30-days"
        assert sel.view.to == self.TODAY
        assert sel.view.days_inclusive == 30

    def test_period_last_7_days(self):
        sel = parse_windows({"period": "last-7-days"}, today=self.TODAY)
        assert sel.view.to == self.TODAY
        assert sel.view.days_inclusive == 7

    def test_period_last_90_days(self):
        sel = parse_windows({"period": "last-90-days"}, today=self.TODAY)
        assert sel.view.to == self.TODAY
        assert sel.view.days_inclusive == 90

    def test_period_last_week_is_the_completed_sun_sat_week(self):
        # Wed 2026-05-20 → last completed week = Sun May 10 – Sat May 16.
        sel = parse_windows({"period": "last-week"}, today=self.TODAY)
        assert sel.view.from_ == date(2026, 5, 10)
        assert sel.view.to == date(2026, 5, 16)

    def test_period_last_2_weeks_ends_on_last_completed_saturday(self):
        sel = parse_windows({"period": "last-2-weeks"}, today=self.TODAY)
        assert sel.view.to == date(2026, 5, 16)
        assert sel.view.days_inclusive == 14

    def test_period_all_time_spans_the_full_data(self):
        """`all-time` resolves the view to the whole data span —
        data_min → data_max."""
        sel = parse_windows(
            {"period": "all-time"},
            today=self.TODAY,
            data_min=date(2025, 1, 1),
            data_max=date(2025, 4, 5),
        )
        assert sel.period == "all-time"
        assert sel.view.from_ == date(2025, 1, 1)
        assert sel.view.to == date(2025, 4, 5)

    def test_period_all_time_falls_back_when_warehouse_empty(self):
        """No data bounds → all-time falls back to the default
        preset rather than producing a degenerate window."""
        sel = parse_windows({"period": "all-time"}, today=self.TODAY)
        assert sel.view.to == self.TODAY
        assert sel.view.days_inclusive == 30

    def test_unknown_period_resolves_to_the_default(self):
        sel = parse_windows({"period": "bogus"}, today=self.TODAY)
        assert sel.period == "last-30-days"
        assert sel.view.to == self.TODAY
        assert sel.view.days_inclusive == 30

    def test_anchor_is_ignored_for_preset_periods(self):
        """`anchor` only applies to Custom — a preset is relative
        to today, ignoring any stray anchor param."""
        sel = parse_windows(
            {"period": "last-7-days", "anchor": "2020-01-01"},
            today=self.TODAY,
        )
        assert sel.view.to == self.TODAY


class TestParseWindowsCustom:
    TODAY = date(2026, 5, 20)

    def test_custom_uses_anchor_and_view_days(self):
        sel = parse_windows(
            {"period": "custom", "anchor": "2025-04-05", "view_days": "14"},
            today=self.TODAY,
        )
        assert sel.period == "custom"
        assert sel.view.to == date(2025, 4, 5)
        assert sel.view.days_inclusive == 14

    def test_custom_without_anchor_falls_back_to_today(self):
        sel = parse_windows(
            {"period": "custom", "view_days": "7"}, today=self.TODAY,
        )
        assert sel.view.to == self.TODAY
        assert sel.view.days_inclusive == 7

    def test_custom_invalid_anchor_falls_back_to_today(self):
        sel = parse_windows(
            {"period": "custom", "anchor": "not-a-date"}, today=self.TODAY,
        )
        assert sel.view.to == self.TODAY

    def test_custom_invalid_view_days_falls_back_to_default(self):
        sel = parse_windows(
            {"period": "custom", "view_days": "abc"}, today=self.TODAY,
        )
        assert sel.view.days_inclusive == DEFAULT_VIEW_DAYS


class TestParseWindowsReference:
    """The reference window is the statistical sample — aging
    percentiles and the Monte Carlo forecast. Its LENGTH follows
    the chosen Period by default (pick 7 days → a 7-day sample);
    `?ref_days=` (Advanced) overrides it. It is always anchored to
    `data_max`, never the view anchor."""

    TODAY = date(2026, 5, 20)

    def test_reference_length_follows_the_period(self):
        """Pick 'Last 7 days' → the reference sample is 7 days too,
        not a fixed 30."""
        sel = parse_windows(
            {"period": "last-7-days"},
            today=self.TODAY, data_max=date(2025, 4, 5),
        )
        assert sel.reference.to == date(2025, 4, 5)
        assert sel.reference.days_inclusive == 7
        assert sel.reference.days_inclusive == sel.view.days_inclusive

    def test_default_period_gives_a_30_day_reference(self):
        sel = parse_windows(
            {}, today=self.TODAY, data_max=date(2025, 4, 5),
        )
        # No period → last-30-days → 30-day view → 30-day reference.
        assert sel.reference.days_inclusive == 30

    def test_reference_anchors_to_data_max_not_the_view(self):
        """A 'Last 2 weeks' view far from the data must not strand
        the percentile sample — reference ends on the data, and
        its length follows the period (14 days)."""
        sel = parse_windows(
            {"period": "last-2-weeks"},
            today=self.TODAY,
            data_max=date(2025, 4, 5),
        )
        assert sel.reference.to == date(2025, 4, 5)
        assert sel.reference.days_inclusive == 14

    def test_ref_days_overrides_the_period_length(self):
        """`?ref_days=` is the Advanced escape hatch — it decouples
        the sample length from the Period."""
        sel = parse_windows(
            {"period": "last-7-days", "ref_days": "60"},
            today=self.TODAY,
            data_max=date(2025, 4, 5),
        )
        assert sel.view.days_inclusive == 7
        assert sel.reference.to == date(2025, 4, 5)
        assert sel.reference.days_inclusive == 60

    def test_reference_falls_back_to_today_when_no_data(self):
        """An empty warehouse has no `data_max`; the reference
        then ends at today so the math still has a window."""
        sel = parse_windows({}, today=self.TODAY, data_max=None)
        assert sel.reference.to == self.TODAY
        assert sel.reference.days_inclusive == 30  # follows 30-day view

    def test_invalid_ref_days_falls_back_to_the_period_length(self):
        sel = parse_windows(
            {"period": "last-7-days", "ref_days": "lots"},
            today=self.TODAY, data_max=None,
        )
        assert sel.reference.days_inclusive == 7


class TestWindowSelection:
    """The resolved model `parse_windows` emits — every view and
    the filter bar read these properties; nothing re-derives."""

    TODAY = date(2026, 5, 20)

    def test_returns_a_window_selection(self):
        sel = parse_windows({}, today=self.TODAY)
        assert isinstance(sel, WindowSelection)

    def test_anchor_is_the_view_end(self):
        sel = parse_windows(
            {"period": "custom", "anchor": "2025-04-05"}, today=self.TODAY,
        )
        assert sel.anchor == date(2025, 4, 5)
        assert sel.anchor == sel.view.to

    def test_view_days_and_ref_days_mirror_the_windows(self):
        sel = parse_windows(
            {"period": "last-7-days", "ref_days": "60"},
            today=self.TODAY,
            data_max=date(2025, 4, 5),
        )
        assert sel.view_days == 7
        assert sel.ref_days == 60

    def test_is_custom_only_for_the_custom_period(self):
        assert parse_windows({"period": "custom"}, today=self.TODAY).is_custom
        assert not parse_windows(
            {"period": "last-7-days"}, today=self.TODAY,
        ).is_custom

    def test_is_advanced_when_reference_decoupled_from_period(self):
        # Default: reference follows the period → not advanced.
        assert parse_windows({}, today=self.TODAY).is_advanced is False
        # Picking a preset is not "advanced" — the reference just
        # follows along.
        assert parse_windows(
            {"period": "last-7-days"}, today=self.TODAY,
        ).is_advanced is False
        # An explicit ref_days that differs from the view → advanced.
        tweaked = parse_windows({"ref_days": "60"}, today=self.TODAY)
        assert tweaked.is_advanced is True


class TestConstantsAndHelpers:
    def test_default_constants_are_reasonable(self):
        """Pin the documented defaults so a change doesn't go
        unnoticed."""
        assert DEFAULT_VIEW_DAYS == 30

    def test_last_completed_week_sun_to_sat(self):
        """`last_completed_week` returns the most-recent COMPLETED
        Sunday-to-Saturday week (excludes the current partial
        week). Drives the 'Last week' / 'Last 2 weeks' presets."""
        # Thu 2026-05-21 → last completed week = Sun May 10 – Sat May 16
        w = last_completed_week(today=date(2026, 5, 21))
        assert w.from_ == date(2026, 5, 10)
        assert w.to == date(2026, 5, 16)
        # Sunday 2026-05-24 → the just-finished week is the answer.
        w = last_completed_week(today=date(2026, 5, 24))
        assert w.from_ == date(2026, 5, 17)
        assert w.to == date(2026, 5, 23)
        # Saturday 2026-05-23 → current week not yet done.
        w = last_completed_week(today=date(2026, 5, 23))
        assert w.from_ == date(2026, 5, 10)
        assert w.to == date(2026, 5, 16)
