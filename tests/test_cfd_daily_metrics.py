"""Per-day flow metrics derived from the CFD's cumulative counts.

For each day the CFD already knows the cumulative arrivals per stage;
`daily_flow_metrics` turns that into the basic flow numbers a reader
wants when hovering a day: WIP per stage (band heights), total WIP,
arrivals/departures that day, average throughput (departures/day to
date), and Little's-Law average cycle time (WIP / throughput).
"""

from __future__ import annotations

from flowmetrics.charts.cfd import (
    CfdDailyPoint,
    CfdModel,
    daily_flow_metrics,
)


def _model() -> CfdModel:
    stages = ("Open", "Review", "Done")
    daily = (
        CfdDailyPoint("2026-05-01", "May 01, 2026",
                      {"Open": 5, "Review": 2, "Done": 1}),
        CfdDailyPoint("2026-05-02", "May 02, 2026",
                      {"Open": 8, "Review": 4, "Done": 3}),
    )
    return CfdModel(
        daily=daily, stages=stages, headline="",
        first_date_iso="2026-05-01", last_date_iso="2026-05-02", crop=None,
    )


def test_empty_model_yields_no_metrics():
    empty = CfdModel(
        daily=(), stages=(), headline="",
        first_date_iso=None, last_date_iso=None, crop=None,
    )
    assert daily_flow_metrics(empty) == ()


def test_band_heights_are_wip_per_stage():
    d1, _ = daily_flow_metrics(_model())
    # Open = 5-2, Review = 2-1, Done = terminal cumulative = 1.
    assert d1.wip_by_stage == {"Open": 3, "Review": 1, "Done": 1}


def test_total_wip_is_arrivals_minus_departures():
    d1, d2 = daily_flow_metrics(_model())
    assert d1.total_wip == 4   # cumA 5 - cumD 1
    assert d2.total_wip == 5   # cumA 8 - cumD 3


def test_arrivals_and_departures_are_daily_deltas():
    d1, d2 = daily_flow_metrics(_model())
    # First day: the cumulative-to-date (carry-in + first-day arrivals).
    assert d1.arrivals == 5 and d1.departures == 1
    # Subsequent days: the delta.
    assert d2.arrivals == 3 and d2.departures == 2


def test_throughput_is_avg_departures_per_day_to_date():
    d1, d2 = daily_flow_metrics(_model())
    assert d1.throughput == 1.0       # 1 done / 1 day
    assert d2.throughput == 1.5       # 3 done / 2 days


def test_avg_cycle_time_is_littles_law():
    d1, d2 = daily_flow_metrics(_model())
    assert d1.avg_cycle_time == 4.0           # WIP 4 / tp 1.0
    assert abs(d2.avg_cycle_time - 5 / 1.5) < 1e-9
