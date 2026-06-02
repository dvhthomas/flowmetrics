"""Shared percentile-rank filter for chart renders.

The page-level Percentile Filter slider narrows BOTH the chart's
scatter and the table's rows. The table uses DuckDB's
`PERCENT_RANK()`; this module gives the chart renders an
equivalent Python filter so the two views agree on the same
rows, even when the underlying data has ties (which it does
routinely — `cycle_time = 1d` is the dominant value for small
PRs).

DuckDB's `PERCENT_RANK()` returns `(rank - 1) / (n - 1)` with
ties sharing the rank of their first occurrence. We multiply
by 100 and round to match the integer-bound slider; this
module's `filter_by_rank` does the same.
"""

from __future__ import annotations

from collections.abc import Callable

# Snap stops on the two-handle slider: 0 then the 5%-step ladder
# from P50 upward. The same ladder feeds `PERCENTILE_CONT` so
# the slider's readout can show "P50 (4d)" etc. without an
# extra round-trip.
PTILE_STOPS: tuple[int, ...] = (
    0, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100,
)

# The four standard chip buckets shown in the page-level toolbar.
# Each maps a `(lo, hi)` percentile band to a name for the kind
# of threshold comparison it implies on the metric VALUE — the
# raw cycle-time / age / whatever the chart's dashed reference
# lines draw against. Keeping this mapping in one place is what
# lets the SQL filter and the Python `filter_by_rank` agree on
# what each chip means.
#   "lte"  → x <= p50 (the chip ≤ P50)
#   "p50_p85" → p50 < x <= p85
#   "p85_p95" → p85 < x <= p95
#   "gt"   → x > p95
STANDARD_BUCKETS: dict[tuple[int, int], str] = {
    (0, 50): "lte",
    (50, 85): "p50_p85",
    (85, 95): "p85_p95",
    (95, 100): "gt",
}


def threshold_predicate(
    lo: int, hi: int,
    thresholds: tuple[float, float, float] | None,
) -> Callable[[float], bool] | None:
    """Return a callable `value -> bool` for the standard chip
    bucket `(lo, hi)` when `thresholds=(p50, p85, p95)` is
    supplied. Returns None for non-standard ranges or when
    thresholds aren't available — the caller falls back to
    percentile-rank semantics."""
    if thresholds is None or not all(t is not None for t in thresholds):
        return None
    kind = STANDARD_BUCKETS.get((lo, hi))
    if kind is None:
        return None
    p50, p85, p95 = thresholds
    if kind == "lte":
        return lambda v: v <= p50
    if kind == "p50_p85":
        return lambda v: p50 < v <= p85
    if kind == "p85_p95":
        return lambda v: p85 < v <= p95
    if kind == "gt":
        return lambda v: v > p95
    return None


def threshold_sql(
    lo: int, hi: int,
    thresholds: tuple[float, float, float] | None,
    column: str,
) -> tuple[str, list[float]] | None:
    """Return `(sql_fragment, params)` for the standard chip
    bucket `(lo, hi)` evaluated against the named `column` (the
    metric-value column in the caller's CTE). Mirrors
    `threshold_predicate` exactly; SQL/Python share the bucket
    semantics through `STANDARD_BUCKETS`."""
    if thresholds is None or not all(t is not None for t in thresholds):
        return None
    kind = STANDARD_BUCKETS.get((lo, hi))
    if kind is None:
        return None
    p50, p85, p95 = thresholds
    if kind == "lte":
        return f"{column} <= ?", [p50]
    if kind == "p50_p85":
        return f"{column} > ? AND {column} <= ?", [p50, p85]
    if kind == "p85_p95":
        return f"{column} > ? AND {column} <= ?", [p85, p95]
    if kind == "gt":
        return f"{column} > ?", [p95]
    return None

def filter_by_rank[T](
    items: list[T],
    *,
    key: Callable[[T], float],
    ranges: list[tuple[int, int]] | None = None,
    ptile_min: int = 0,
    ptile_max: int = 100,
    metric_thresholds: tuple[float, float, float] | None = None,
) -> list[T]:
    """Keep `items` whose percentile rank lands in any of
    `ranges` (each a `(lo, hi)` pair). When `ranges` is None,
    fall back to the single `[ptile_min, ptile_max]` band.

    When `metric_thresholds=(p50, p85, p95)` is supplied, the
    four standard chip ranges (0-50, 50-85, 85-95, 95-100)
    filter by ABSOLUTE `key(item)` value against those
    thresholds — so the chart and table agree with what the
    user sees as the P50/P85/P95 reference lines on the chart.
    Custom ranges still use PERCENT_RANK so chart and table
    counts stay aligned with tie-grouping semantics.
    """
    if not items:
        return []
    if ranges is None:
        ranges = [(ptile_min, ptile_max)]
    # Resolve each range to its predicate ONCE. Standard buckets
    # with thresholds use a metric-value predicate; everything else
    # gets a rank-band check evaluated inside the tie-aware loop.
    predicates: list[
        Callable[[T, float], bool]  # (item, rank) -> bool
    ] = []
    for lo, hi in ranges:
        if lo == 0 and hi == 100:
            predicates.append(lambda _it, _r: True)
            continue
        value_pred = threshold_predicate(lo, hi, metric_thresholds)
        if value_pred is not None:
            predicates.append(
                # Closure trap: bind value_pred fresh for each loop.
                lambda it, _r, vp=value_pred: vp(key(it))
            )
        else:
            # Fall back to PERCENT_RANK semantics — tie-aware,
            # matching DuckDB.
            predicates.append(
                lambda _it, rank, lo=lo, hi=hi: lo <= rank <= hi
            )
    ordered = sorted(items, key=key)
    n = len(ordered)
    kept: list[T] = []
    i = 0
    while i < n:
        # Find the run of equal-key items starting at i.
        j = i
        anchor_key = key(ordered[i])
        while j < n and key(ordered[j]) == anchor_key:
            j += 1
        # PERCENT_RANK puts the whole run at the rank of the
        # first occurrence. `n == 1` falls back to rank 0.
        rank = round((i / max(1, n - 1)) * 100) if n > 1 else 0
        for run_item in ordered[i:j]:
            if any(p(run_item, rank) for p in predicates):
                kept.append(run_item)
        i = j
    return kept


def parse_ranges(s: str | None) -> list[tuple[int, int]] | None:
    """Parse the URL `ptile_ranges` param — comma-separated
    `min-max` pairs (e.g. `"0-50,85-95"`) — into a list of
    clamped `(lo, hi)` tuples. Returns None on missing input,
    [] when the string had no valid pair."""
    if not s:
        return None
    out: list[tuple[int, int]] = []
    for chunk in s.split(","):
        chunk = chunk.strip()
        if not chunk or "-" not in chunk:
            continue
        try:
            lo_str, hi_str = chunk.split("-", 1)
            lo = max(0, min(100, int(lo_str)))
            hi = max(0, min(100, int(hi_str)))
            if lo > hi:
                lo, hi = hi, lo
            out.append((lo, hi))
        except ValueError:
            continue
    return out
