"""Cycle-time definition: Vacanti's strict calendar-day formula.

    CT = FD - SD + 1

where SD and FD are the calendar DATES of `created_at` and
`completed_at` (UTC). Same-day work = 1 day. Yesterday-to-today =
2 days. Inclusive of both endpoints — "we'd never say it took
zero days to complete" (Vacanti, Actionable Agile Metrics for
Predictability, 10th Anniversary Edition, p. 59).

This is a whole-day metric. The exact wall-clock elapsed time
(hours and minutes between events) is still available on the
lifecycle/timeline component, which reads the raw datetime
timestamps from the transitions Parquet — but the cycle-time
column itself is integer days. Earlier iterations of this codebase
tried `(elapsed_datetime) + 1 day` which preserved sub-day
precision; the user reverted to the strict whole-day formula
because cycle time is reported per-day everywhere it's used
(forecasting, percentile checks, throughput cadence).

The single source of truth lives in `materialise.cycle_time_days`;
the column is read straight from Parquet by every UI consumer.
`test_cycle_time_function_is_defined_exactly_once` enforces that.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from flowmetrics.materialise import cycle_time_days


class TestCycleTimeDays:
    def test_same_day_pr_is_one_day(self):
        """Vacanti: same calendar day → 0 + 1 = 1.

        A 64-minute PR opened and merged on May 04 reports 1d.
        Sub-day elapsed time doesn't enter the formula — only the
        calendar dates do."""
        created = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        completed = datetime(2026, 5, 4, 12, 4, tzinfo=UTC)
        assert cycle_time_days(created, completed) == 1.0

    def test_two_minute_pr_across_midnight_is_two_days(self):
        """Calendar boundary crossed → 2 calendar days.

        Wall-clock elapsed is 2 minutes; the formula doesn't care.
        SD = May 04, FD = May 05; CT = 1 + 1 = 2."""
        created = datetime(2026, 5, 4, 23, 59, tzinfo=UTC)
        completed = datetime(2026, 5, 5, 0, 1, tzinfo=UTC)
        assert cycle_time_days(created, completed) == 2.0

    def test_zero_duration_is_exactly_one_day(self):
        """A PR that opened and merged at the exact same moment is
        the minimum legal value: same date → 0 + 1 = 1.0d."""
        created = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        assert cycle_time_days(created, created) == 1.0

    def test_week_long_pr_counts_every_calendar_day_inclusive(self):
        """Apr 26 → May 04: 8 calendar days difference, +1 = 9 days.

        The sub-day component (the PR ran from 9 AM to 3 PM on the
        last day — 6 extra hours) doesn't add to the count. Whole
        days only."""
        created = datetime(2026, 4, 26, 9, 0, tzinfo=UTC)
        completed = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)
        assert cycle_time_days(created, completed) == 9.0

    def test_in_flight_returns_none(self):
        created = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
        assert cycle_time_days(created, None) is None

    def test_tuesday_to_wednesday_is_two_days(self):
        """Vacanti's canonical example: Jan 01 → Jan 02 = 2 days.
        Crossed one calendar boundary; +1 for the inclusive count."""
        # 2026-05-05 is a Tuesday.
        created = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
        completed = datetime(2026, 5, 6, 15, 0, tzinfo=UTC)
        assert cycle_time_days(created, completed) == 2.0

    def test_result_is_always_integer_valued(self):
        """The column type is DOUBLE for storage consistency, but
        every legal value is whole. This invariant is what
        downstream tools (forecasting, percentile checks) rely on."""
        cases = [
            # (created, completed, expected)
            (
                datetime(2026, 5, 4, 0, 0, tzinfo=UTC),
                datetime(2026, 5, 4, 23, 59, tzinfo=UTC),
                1.0,
            ),
            (
                datetime(2026, 5, 4, 23, 59, tzinfo=UTC),
                datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
                2.0,
            ),
            (
                datetime(2026, 4, 1, 9, 0, tzinfo=UTC),
                datetime(2026, 4, 15, 15, 30, tzinfo=UTC),
                15.0,  # 14 + 1
            ),
        ]
        for created, completed, expected in cases:
            result = cycle_time_days(created, completed)
            assert result is not None
            assert result == int(result), (
                f"cycle_time_days must be integer-valued for "
                f"created={created}, completed={completed}; "
                f"got {result}"
            )
            assert result == expected, (
                f"created={created}, completed={completed}: "
                f"expected {expected}, got {result}"
            )

    def test_cycle_time_function_is_defined_exactly_once(self):
        """Single source of truth: `cycle_time_days` is defined in
        `materialise.py` only. Every UI consumer reads the
        pre-computed `cycle_time_days` column from Parquet — no
        component re-derives the value. Drift between definitions
        has caused past metric-mismatch bugs (e.g. lifecycle showing
        ~10h while the table reports 1.41d).

        This guards against the regression by scanning the codebase
        for a `def cycle_time_days(` outside materialise.py. The
        formula's specific Vacanti adjustment is too easy to
        "helpfully" re-implement; the test forbids that.
        """
        src_root = Path(__file__).parent.parent / "src" / "flowmetrics"
        offenders: list[tuple[str, int, str]] = []
        for path in src_root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if "def cycle_time_days(" in line:
                    rel = str(path.relative_to(src_root))
                    if rel != "materialise.py":
                        offenders.append((rel, lineno, line.strip()))
        assert not offenders, (
            "`def cycle_time_days(` defined OUTSIDE materialise.py — "
            "the function must exist in exactly one place. UI "
            "consumers read the stored column instead. Offenders:\n"
            + "\n".join(f"  {p}:{ln}: {ll}" for p, ln, ll in offenders)
        )

    def test_completion_before_creation_surfaces_bad_data(self):
        """If a source-data bug delivers completed < created, the
        formula yields a non-positive value. Valid minimum is 1.0;
        anything below 1.0 (zero or negative) is the impossible
        zone — downstream code can flag those as data quality."""
        created = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
        completed = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
        result = cycle_time_days(created, completed)
        assert result is not None
        # FD - SD + 1 = -1 + 1 = 0
        assert result < 1.0, (
            f"completion before creation must land in the sub-1.0 "
            f"'impossible' zone so bad data surfaces; got {result}"
        )
