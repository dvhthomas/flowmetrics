"""Behavioural spec for service-level defaults.

GitHub uses UTC for `mergedAt`. Today is always partial — including it
in the training window biases the simulator low because the rest of
today's merges haven't happened yet. So the default end of the training
window is yesterday-in-UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from flowmetrics.service import DEFAULT_TRAINING_DAYS, default_history_end, default_history_start


class TestDefaultHistoryEnd:
    def test_returns_yesterday_in_utc(self):
        today_utc = datetime.now(UTC).date()
        assert default_history_end() == today_utc - timedelta(days=1)

    def test_is_a_date_not_a_datetime(self):
        result = default_history_end()
        assert isinstance(result, date)
        assert not isinstance(result, datetime)


class TestDefaultHistoryStart:
    def test_default_is_29_days_before_default_end(self):
        # 30-day inclusive window: default_end - 29 days
        end = default_history_end()
        assert default_history_start() == end - timedelta(days=DEFAULT_TRAINING_DAYS - 1)

    def test_relative_to_given_end_date(self):
        end = date(2026, 5, 10)
        assert default_history_start(end) == date(2026, 4, 11)
        # Inclusive: end - start + 1 == 30
        assert (end - default_history_start(end)).days + 1 == DEFAULT_TRAINING_DAYS
