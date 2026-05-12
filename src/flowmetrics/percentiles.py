"""Vacanti's canonical chart-annotation percentiles.

Every chart in this tool annotates its data with the 50/70/85/95
percentile lines. This module turns a value sequence into the exact
positions to draw those lines.

The percentile rule: the p-th percentile is the smallest value x such
that at least p% of the sorted values are <= x. That matches the
forward-percentile reading we use elsewhere (`forecast.forward_percentile`)
and is more conservative than linear interpolation for the small
sample sizes typical of weekly windows.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T", int, float)

CANONICAL_PERCENTILES = (50, 70, 85, 95)


def chart_percentiles[T: (int, float)](values: Sequence[T]) -> dict[int, T]:
    """Return {50: v50, 70: v70, 85: v85, 95: v95} for `values`."""
    if not values:
        raise ValueError("chart_percentiles requires at least one value")
    sorted_values = sorted(values)
    n = len(sorted_values)
    return {p: sorted_values[min(n - 1, max(0, _index_at(p, n)))] for p in CANONICAL_PERCENTILES}


def _index_at(percentile: int, n: int) -> int:
    """Index into sorted list for the p-th percentile (1-based ⇒ 0-based)."""
    # Use ceil(p/100 * n) so 50th of 10 → index 5 (value at position 5,
    # which is the 5th item — value 5 in 1..10). Subtract 1 for 0-indexing.
    from math import ceil

    return max(0, ceil(percentile / 100 * n) - 1)
