from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import accumulate
from random import Random

# Safety limit on the inner loop of when-done so a regime of mostly-zero
# samples can't hang the process. 5 years is far past anything sane.
_MAX_SIMULATION_DAYS = 365 * 5

# Hoist out of the inner loop. A single allocation is reused for every
# `current + idx * _ONE_DAY` calculation instead of constructing a new
# `timedelta` on every run.
_ONE_DAY = timedelta(days=1)


@dataclass(frozen=True)
class ResultsHistogram[X]:
    """Tally of simulation outcomes — generic over outcome type.

    `counts` maps each distinct outcome to the number of runs that hit it.
    `sorted_keys` is the outcomes in ascending order; convenient for both
    rendering and percentile reading.
    """

    counts: dict[X, int]
    total: int
    sorted_keys: list[X]

    @property
    def min_outcome(self) -> X:
        return self.sorted_keys[0]

    @property
    def max_outcome(self) -> X:
        return self.sorted_keys[-1]


# ----------------------------------------------------------------------
# Simulation
# ----------------------------------------------------------------------


def _validate_samples(samples: Sequence[int]) -> None:
    if not samples:
        raise ValueError("daily throughput samples cannot be empty")
    if all(s <= 0 for s in samples):
        raise ValueError("every sample is zero or negative; cannot simulate progress")
    if any(s < 0 for s in samples):
        raise ValueError("daily throughput samples cannot be negative")


def monte_carlo_when_done(
    daily_samples: Sequence[int],
    items: int,
    start_date: date,
    *,
    runs: int,
    rng: Random,
) -> list[date]:
    """Forecast: "given N items to complete, when will they be done?"

    For each run we walk forward day by day from `start_date`, drawing a
    historical daily-throughput sample (with replacement) on each day, and
    stop as soon as cumulative throughput meets or exceeds `items`. The
    day on which it crosses is the completion date for that run.

    We use `items` rather than `backlog` because the latter is
    Scrum-loaded — it's used for "the prioritized list of work";
    here we just mean a count of work remaining.
    """
    _validate_samples(daily_samples)
    if items <= 0:
        raise ValueError("items must be positive")
    if runs <= 0:
        raise ValueError("runs must be positive")

    samples = list(daily_samples)

    # Heuristic initial batch size: how many days do we expect a run
    # to take? Cumulative_arrivals / mean_throughput, padded 1.5x to
    # cover the right tail without too many `extend` calls. Falls
    # back to the safety cap.
    positive = [s for s in samples if s > 0]
    nonzero_frac = len(positive) / len(samples)
    positive_mean = sum(positive) / max(1, len(positive))
    expected_per_day = positive_mean * nonzero_frac
    est_days = max(1, int(items / max(0.01, expected_per_day) * 1.5))
    batch_size = min(est_days, _MAX_SIMULATION_DAYS)

    results: list[date] = []
    for _ in range(runs):
        # One C-level call into `_random` for `batch_size` samples,
        # then prefix-sum via `accumulate` (also C-level) and find
        # the crossing day with `bisect_left`. Replaces a
        # Python-level loop with rng.choice per day.
        draws = rng.choices(samples, k=batch_size)
        cum = list(accumulate(draws))
        total_drawn = batch_size
        while cum[-1] < items:
            if total_drawn >= _MAX_SIMULATION_DAYS:
                raise RuntimeError(
                    f"Simulation exceeded {_MAX_SIMULATION_DAYS} days without "
                    "completing — your throughput samples may be effectively zero."
                )
            extra = min(batch_size, _MAX_SIMULATION_DAYS - total_drawn)
            more = rng.choices(samples, k=extra)
            base = cum[-1]
            # Extend cum with running totals starting from `base`,
            # skipping the seed value `accumulate` yields first.
            cum.extend(list(accumulate(more, initial=base))[1:])
            total_drawn += extra
        # idx = first day index where cumulative >= items.
        idx = bisect_left(cum, items)
        results.append(start_date + idx * _ONE_DAY)
    return results


def monte_carlo_how_many(
    daily_samples: Sequence[int],
    *,
    start_date: date,
    end_date: date,
    runs: int,
    rng: Random,
) -> list[int]:
    """Forecast: "given this window, how many items will be done?"

    For each run we draw one historical daily-throughput sample per day in
    `[start_date, end_date]` (inclusive) and sum them. That sum is the
    items-completed outcome for that run.
    """
    _validate_samples(daily_samples)
    if end_date < start_date:
        raise ValueError(f"end_date ({end_date}) must be >= start_date ({start_date})")
    if runs <= 0:
        raise ValueError("runs must be positive")

    days = (end_date - start_date).days + 1
    samples = list(daily_samples)
    # One batched `rng.choices(..., k=days)` per run, then `sum`.
    # Both calls drop into CPython's C-level implementations,
    # replacing `days` Python-level `rng.choice` calls per run.
    return [sum(rng.choices(samples, k=days)) for _ in range(runs)]


# ----------------------------------------------------------------------
# Histogram + percentiles
# ----------------------------------------------------------------------


def build_histogram[X](results: Sequence[X]) -> ResultsHistogram[X]:
    if not results:
        raise ValueError("cannot build a histogram from empty results")
    counter = Counter(results)
    return ResultsHistogram(
        counts=dict(counter),
        total=sum(counter.values()),
        sorted_keys=sorted(counter.keys()),
    )


def _validate_percentile(p: float) -> None:
    if not (0 < p <= 100):
        raise ValueError(f"percentile must be in (0, 100]; got {p}")


def forward_percentile[X](hist: ResultsHistogram[X], p: float) -> X:
    """Smallest outcome x such that P(outcome <= x) >= p/100.

    Use this for date forecasts: '85% confidence we'll be done by this date'.
    As p increases, the returned date moves later.
    """
    _validate_percentile(p)
    threshold = hist.total * p / 100
    cumulative = 0
    for key in hist.sorted_keys:
        cumulative += hist.counts[key]
        if cumulative >= threshold:
            return key
    return hist.sorted_keys[-1]  # pragma: no cover — defensive


def backward_percentile[X](hist: ResultsHistogram[X], p: float) -> X:
    """Largest outcome x such that P(outcome >= x) >= p/100.

    Use this for item forecasts: '85% confidence we'll deliver at least
    this many items'. As p increases, the returned count shrinks — more
    confidence means a more conservative commitment.
    """
    _validate_percentile(p)
    threshold = hist.total * p / 100
    cumulative = 0
    # Walk outcomes high → low. P(>= key) = (cumulative running from the top).
    for key in reversed(hist.sorted_keys):
        cumulative += hist.counts[key]
        if cumulative >= threshold:
            return key
    return hist.sorted_keys[0]  # pragma: no cover — defensive
