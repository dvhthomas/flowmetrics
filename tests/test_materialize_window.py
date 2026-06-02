"""The contract's start/stop are an *optional* default fetch window.

The web builder no longer asks for a window — data is fetched via the
Data Source page's backfill (which passes its own --since/--until). So a
contract created in the UI has no window at all. `materialize` must still
run: when start/stop are absent it falls back to a rolling window (the
most recent N days up to today) instead of refusing with an assertion.
"""

from __future__ import annotations

from datetime import date, timedelta

from flowmetrics.workflow import Contract
from flowmetrics.materialize import DEFAULT_FETCH_WINDOW_DAYS, _resolve_window


def _c(**kw) -> Contract:
    return Contract(name="c", source="github", repo="o/r", **kw)


def test_explicit_window_is_used_verbatim():
    c = _c(start=date(2026, 5, 4), stop=date(2026, 5, 10))
    assert _resolve_window(c, today=date(2026, 5, 28)) == (
        date(2026, 5, 4),
        date(2026, 5, 10),
    )


def test_missing_window_defaults_to_a_rolling_window():
    start, stop = _resolve_window(_c(), today=date(2026, 5, 28))
    assert stop == date(2026, 5, 28)
    assert start == date(2026, 5, 28) - timedelta(days=DEFAULT_FETCH_WINDOW_DAYS)


def test_missing_stop_defaults_to_today():
    c = _c(start=date(2026, 1, 1))
    assert _resolve_window(c, today=date(2026, 5, 28)) == (
        date(2026, 1, 1),
        date(2026, 5, 28),
    )
