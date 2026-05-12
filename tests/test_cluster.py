from datetime import UTC, datetime, timedelta

import pytest

from flowmetrics.cluster import cluster_activity


def ts(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestClusterActivity:
    def test_empty_input_yields_no_clusters(self):
        assert cluster_activity([], gap=timedelta(hours=4)) == []

    def test_single_event_is_a_point_cluster(self):
        t = ts(2026, 5, 5, 10, 0)
        assert cluster_activity([t], gap=timedelta(hours=4)) == [(t, t)]

    def test_two_events_within_gap_merge_into_one(self):
        a = ts(2026, 5, 5, 10, 0)
        b = ts(2026, 5, 5, 13, 30)  # 3.5h later, within 4h gap
        assert cluster_activity([a, b], gap=timedelta(hours=4)) == [(a, b)]

    def test_two_events_beyond_gap_are_separate(self):
        a = ts(2026, 5, 5, 10, 0)
        b = ts(2026, 5, 5, 15, 0)  # 5h later, beyond 4h gap
        assert cluster_activity([a, b], gap=timedelta(hours=4)) == [
            (a, a),
            (b, b),
        ]

    def test_unsorted_input_is_handled(self):
        a = ts(2026, 5, 5, 10, 0)
        b = ts(2026, 5, 5, 11, 0)
        c = ts(2026, 5, 5, 12, 0)
        assert cluster_activity([c, a, b], gap=timedelta(hours=4)) == [(a, c)]

    def test_chained_events_within_gap_form_one_cluster(self):
        # Each step is < gap, but total span exceeds gap; still one cluster
        events = [ts(2026, 5, 5, 9 + i, 0) for i in range(6)]
        clusters = cluster_activity(events, gap=timedelta(hours=2))
        assert clusters == [(events[0], events[-1])]

    def test_mixed_clusters(self):
        events = [
            ts(2026, 5, 5, 9, 0),
            ts(2026, 5, 5, 10, 0),  # cluster 1: 9-10
            ts(2026, 5, 5, 15, 0),  # cluster 2: 15 (alone)
            ts(2026, 5, 6, 9, 0),  # cluster 3: next day
            ts(2026, 5, 6, 9, 30),
        ]
        clusters = cluster_activity(events, gap=timedelta(hours=4))
        assert clusters == [
            (events[0], events[1]),
            (events[2], events[2]),
            (events[3], events[4]),
        ]

    def test_gap_must_be_positive(self):
        with pytest.raises(ValueError):
            cluster_activity([ts(2026, 5, 5, 9, 0)], gap=timedelta(0))
