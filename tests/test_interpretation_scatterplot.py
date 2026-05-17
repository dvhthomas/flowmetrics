"""Behavioural spec for interpret_scatterplot's headline framing.

The P85 metric is Vacanti's external-commitment threshold — perfect
for teams doing recent-work-only. For OSS Jira backlogs with
multi-year tickets occasionally cleaned up, P85 is dominated by the
deep tail and reads as 'team takes 893 days to do anything,' which
isn't true. When P85/median ratio is wide, the headline should
surface BOTH numbers so the reader knows the median is the typical
flow.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from flowmetrics.interpretation import interpret_scatterplot
from flowmetrics.report import ScatterplotInput, ScatterplotPoint


def _input(start=date(2026, 4, 17), stop=date(2026, 5, 16)):
    return ScatterplotInput(
        repo="acme/widget", start=start, stop=stop, offline=False,
    )


def _points(cycles_days: list[float]) -> list[ScatterplotPoint]:
    return [
        ScatterplotPoint(
            item_id=f"#{i}", title=f"t{i}",
            completed_at=datetime(2026, 5, 1, tzinfo=UTC),
            cycle_time_days=c, url=None,
        )
        for i, c in enumerate(cycles_days)
    ]


class TestScatterplotHeadline:
    def test_normal_spread_uses_p85_only(self):
        """When P85 is in the same order of magnitude as the median
        (typical synchronous workflow), the headline keeps the
        classic 'finished within P85' framing."""
        # P50 ~5, P85 ~10 — ratio 2x, well below the wide-spread threshold.
        points = _points([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        percentiles = {50: 5.0, 70: 7.0, 85: 10.0, 95: 12.0}
        result = interpret_scatterplot(_input(), points, percentiles)
        assert "85% finished within 10.0 days" in result.headline
        assert "median" not in result.headline.lower()
        assert "wide spread" not in result.headline.lower()

    def test_wide_spread_surfaces_both_median_and_p85(self):
        """When the deep tail dominates (P85 / median > 5x), reframe
        the headline to surface both numbers. The Cassandra-style
        OSS Jira backlog cleanup scenario."""
        # Cassandra-like: P50=27d (typical recent work), P85=893d
        # (driven by year-old tickets resolved this window).
        points = _points([27.0] * 30 + [893.0, 2543.0, 3999.0])
        percentiles = {50: 27.0, 70: 119.0, 85: 893.0, 95: 2543.0}
        result = interpret_scatterplot(_input(), points, percentiles)
        # Headline should name BOTH median and P85 so the reader knows
        # the median is what 'typical flow' looks like.
        assert "27.0" in result.headline, (
            f"wide-spread headline should name the median; got {result.headline!r}"
        )
        assert "893.0" in result.headline
        # And a phrase that explains why the spread is wide.
        text = result.headline.lower()
        assert "wide spread" in text or "deep tail" in text or "backlog" in text

    def test_zero_median_does_not_trigger_reframe(self):
        """Edge case: P50 = 0 (every item finished same-day) — don't
        divide by zero in the ratio check."""
        points = _points([0.0] * 10)
        percentiles = {50: 0.0, 70: 0.0, 85: 0.0, 95: 0.0}
        # Just verify it doesn't crash; headline can be anything.
        result = interpret_scatterplot(_input(), points, percentiles)
        assert result.headline  # not empty
