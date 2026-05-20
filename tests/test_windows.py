"""Tests for `flowmetrics.windows` — the (from, to) date-range
types that drive the dashboard's two user-facing windows:

  - **View window**: clamps chart x-axes. Display-only.
  - **Reference period**: the statistical sample. Drives
    percentile thresholds (cycle-time, aging) and the MCS
    throughput sampling distribution (forecast).

Both windows are inclusive on both endpoints. The UI labels
match: "From" and "To" both inclusive — same-day = 1-day window,
not 0.

Defaults are anchored to today UTC:
  - View window: 30 days inclusive (today − 29 .. today)
  - Reference period: 14 days inclusive (today − 13 .. today)
"""

from __future__ import annotations

from datetime import date

import pytest

from flowmetrics.windows import (
    DEFAULT_REFERENCE_DAYS,
    DEFAULT_VIEW_DAYS,
    Window,
    parse_windows,
)


class TestWindow:
    def test_from_and_to_are_inclusive(self):
        w = Window(from_=date(2026, 5, 4), to=date(2026, 5, 10))
        # 7-day window (May 4..10 inclusive)
        assert w.days_inclusive == 7

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


class TestParseWindows:
    def test_defaults_when_no_query_params(self):
        today = date(2026, 5, 20)
        view, ref = parse_windows({}, today=today)
        # View defaults to 30 days inclusive ending today.
        assert view.to == today
        assert view.days_inclusive == DEFAULT_VIEW_DAYS
        # Reference defaults to 14 days inclusive ending today.
        assert ref.to == today
        assert ref.days_inclusive == DEFAULT_REFERENCE_DAYS

    def test_view_from_and_to_override_defaults(self):
        today = date(2026, 5, 20)
        view, _ref = parse_windows(
            {"view_from": "2025-01-01", "view_to": "2025-01-31"},
            today=today,
        )
        assert view.from_ == date(2025, 1, 1)
        assert view.to == date(2025, 1, 31)

    def test_ref_from_and_to_override_defaults(self):
        today = date(2026, 5, 20)
        _view, ref = parse_windows(
            {"ref_from": "2025-04-01", "ref_to": "2025-04-07"},
            today=today,
        )
        assert ref.from_ == date(2025, 4, 1)
        assert ref.to == date(2025, 4, 7)

    def test_view_and_ref_are_independent(self):
        today = date(2026, 5, 20)
        view, ref = parse_windows(
            {
                "view_from": "2025-01-01", "view_to": "2025-01-31",
                "ref_from": "2025-02-01", "ref_to": "2025-02-07",
            },
            today=today,
        )
        assert view.from_ == date(2025, 1, 1)
        assert view.to == date(2025, 1, 31)
        assert ref.from_ == date(2025, 2, 1)
        assert ref.to == date(2025, 2, 7)

    def test_only_one_endpoint_falls_back_to_default(self):
        """Partial input (only from, only to) is treated as no
        input. Avoids surprising user with a half-window."""
        today = date(2026, 5, 20)
        view, _ref = parse_windows(
            {"view_from": "2025-01-01"},  # no view_to
            today=today,
        )
        assert view.days_inclusive == DEFAULT_VIEW_DAYS
        assert view.to == today

    def test_invalid_date_falls_back_to_default(self):
        """A malformed date doesn't crash the request — it just
        means the operator typed something wrong and gets the
        default back. UI can re-validate visibly."""
        today = date(2026, 5, 20)
        view, _ref = parse_windows(
            {"view_from": "not-a-date", "view_to": "2025-01-31"},
            today=today,
        )
        assert view.days_inclusive == DEFAULT_VIEW_DAYS

    def test_default_constants_are_reasonable(self):
        """Pin the documented defaults so a future widening
        doesn't go unnoticed."""
        assert DEFAULT_VIEW_DAYS == 30
        assert DEFAULT_REFERENCE_DAYS == 14

    def test_anchor_plus_view_days_derives_view_window(self):
        """The common-case URL shape: `?anchor=YYYY-MM-DD&view_days=N`
        derives a view window of N inclusive days ending on the
        anchor. Same for reference via `ref_days`."""
        view, ref = parse_windows(
            {"anchor": "2026-05-20",
             "view_days": "30",
             "ref_days": "14"},
            today=date(2026, 5, 20),
        )
        assert view.to == date(2026, 5, 20)
        assert view.from_ == date(2026, 4, 21)
        assert view.days_inclusive == 30
        assert ref.to == date(2026, 5, 20)
        assert ref.from_ == date(2026, 5, 7)
        assert ref.days_inclusive == 14

    def test_anchor_only_uses_default_durations(self):
        """`?anchor=X` without view_days/ref_days should still
        derive both windows using DEFAULT_VIEW_DAYS and
        DEFAULT_REFERENCE_DAYS — just shift the anchor."""
        view, ref = parse_windows(
            {"anchor": "2025-04-05"},
            today=date(2026, 5, 20),
        )
        assert view.to == date(2025, 4, 5)
        assert view.days_inclusive == DEFAULT_VIEW_DAYS
        assert ref.to == date(2025, 4, 5)
        assert ref.days_inclusive == DEFAULT_REFERENCE_DAYS

    def test_explicit_view_from_to_overrides_anchor(self):
        """Advanced/custom mode wins: when both `view_from` and
        `view_to` are set, ignore anchor + view_days for the view
        window. Reference is unaffected."""
        view, ref = parse_windows(
            {
                "anchor": "2026-05-20",
                "view_days": "30",
                "view_from": "2025-01-01",
                "view_to": "2025-01-15",
                "ref_days": "7",
            },
            today=date(2026, 5, 20),
        )
        assert view.from_ == date(2025, 1, 1)
        assert view.to == date(2025, 1, 15)
        # Reference still uses anchor + ref_days.
        assert ref.to == date(2026, 5, 20)
        assert ref.days_inclusive == 7

    def test_invalid_anchor_or_days_falls_back_to_defaults(self):
        """Garbage in user params shouldn't 4xx the request —
        defaults still apply."""
        view, ref = parse_windows(
            {"anchor": "nope", "view_days": "abc"},
            today=date(2026, 5, 20),
        )
        assert view.to == date(2026, 5, 20)
        assert view.days_inclusive == DEFAULT_VIEW_DAYS

    def test_last_completed_week_sun_to_sat(self):
        """`last_completed_week` returns the most-recent COMPLETED
        Sunday-to-Saturday week (excludes the current partial
        week the viewer is in). Used by the 'Last week (Sun-Sat)'
        preset to anchor the windows."""
        from flowmetrics.windows import last_completed_week
        # Thu 2026-05-21 → last completed week = Sun May 10 – Sat May 16
        w = last_completed_week(today=date(2026, 5, 21))
        assert w.from_ == date(2026, 5, 10)
        assert w.to == date(2026, 5, 16)
        # Sunday 2026-05-24 → last completed week = Sun May 17 – Sat May 23
        # (today is Sun, so the JUST-FINISHED week is the answer)
        w = last_completed_week(today=date(2026, 5, 24))
        assert w.from_ == date(2026, 5, 17)
        assert w.to == date(2026, 5, 23)
        # Saturday 2026-05-23 → last completed week ends previous Sat
        # (today is Sat, current week not yet done)
        w = last_completed_week(today=date(2026, 5, 23))
        assert w.from_ == date(2026, 5, 10)
        assert w.to == date(2026, 5, 16)
