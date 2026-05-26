"""Layer 2 — shared chart primitives.

The chart decisions that recur across more than one chart, defined
once. Extracted at the second use (the refactor's abstraction
gate) — see docs/PLAN-chart-model.md. Pure functions and frozen
dataclasses: no DuckDB, no Vega.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# Canonical chart-annotation percentiles — the ceil-index
# `chart_percentiles` variant returns this set.
CANONICAL_PERCENTILES = (50, 70, 85, 95)


def percentile_cont(values: Sequence[float], p: float) -> float:
    """Linear-interpolation percentile — matches DuckDB
    `percentile_cont`. `p` in [0, 1]. Empty sample → 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = p * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    return ordered[lo] + (rank - lo) * (ordered[hi] - ordered[lo])


@dataclass(frozen=True)
class Percentiles:
    """The 50/85/95 flow percentiles plus how many items the
    sample was drawn from. Empirical — drawn from observed cycle
    times, not a Monte Carlo model."""

    p50: float
    p85: float
    p95: float
    source_count: int


def percentiles_from(values: Sequence[float]) -> Percentiles:
    """Resolve the 50/85/95 percentiles of `values`."""
    return Percentiles(
        p50=percentile_cont(values, 0.50),
        p85=percentile_cont(values, 0.85),
        p95=percentile_cont(values, 0.95),
        source_count=len(values),
    )


@dataclass(frozen=True)
class RangeControl:
    """A filter slider. The view drops items above the slider
    value and lets the axis re-scale. Runs `floor`..`ceiling` and
    opens at `default`."""

    floor: int
    ceiling: int
    default: int


def _index_at(percentile: int, n: int) -> int:
    """Index into a sorted list for the p-th percentile. Uses
    ceil(p/100*n) so the 50th of 10 → index 5 (value at the 5th
    position, i.e. 5 in 1..10). Subtract 1 for 0-indexing."""
    return max(0, math.ceil(percentile / 100 * n) - 1)


def chart_percentiles[T: (int, float)](values: Sequence[T]) -> dict[int, T]:
    """Canonical chart-annotation percentiles —
    `{50: v50, 70: v70, 85: v85, 95: v95}` from `values`.

    Uses the *ceil-index* rule: the p-th percentile is the
    smallest value x such that at least p% of the sorted values
    are <= x. More conservative than linear interpolation for the
    small sample sizes typical of weekly windows; matches the
    forward-percentile reading used elsewhere
    (`forecast.forward_percentile`).

    Note: this is a DIFFERENT method from `percentile_cont` (above)
    which does linear interpolation. The web chart-model layer
    uses `percentile_cont` to match DuckDB; the CLI compute path
    uses `chart_percentiles` for the more conservative reading on
    small samples.
    """
    if not values:
        raise ValueError("chart_percentiles requires at least one value")
    sorted_values = sorted(values)
    n = len(sorted_values)
    return {
        p: sorted_values[min(n - 1, max(0, _index_at(p, n)))]
        for p in CANONICAL_PERCENTILES
    }


def range_control(
    floor: float, values: Sequence[float]
) -> RangeControl | None:
    """A filter slider from `floor` up to the max of `values`,
    opening at the max (shows everything). Absent when there is
    nothing to crop — fewer than two values, or the floor already
    at or above the maximum."""
    if len(values) < 2:
        return None
    rounded_floor = math.ceil(floor)
    ceiling = math.ceil(max(values))
    if rounded_floor >= ceiling:
        return None
    return RangeControl(floor=rounded_floor, ceiling=ceiling, default=ceiling)
