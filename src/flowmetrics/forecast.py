from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from random import Random

# Safety limit on the inner loop of when-done so a regime of mostly-zero
# samples can't hang the process. 5 years is far past anything sane.
_MAX_SIMULATION_DAYS = 365 * 5


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

    @property
    def min_date(self) -> X:  # back-compat alias for date outcomes
        return self.min_outcome

    @property
    def max_date(self) -> X:
        return self.max_outcome


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

    We use `items` rather than `backlog` because Vacanti's framework
    flags "backlog" as overloaded — Scrum uses it for "the prioritized
    list of work"; here we just mean a count of work remaining.
    """
    _validate_samples(daily_samples)
    if items <= 0:
        raise ValueError("items must be positive")
    if runs <= 0:
        raise ValueError("runs must be positive")

    results: list[date] = []
    samples = list(daily_samples)
    for _ in range(runs):
        current = start_date
        remaining = items
        for _day in range(_MAX_SIMULATION_DAYS):
            sample = rng.choice(samples)
            remaining -= sample
            if remaining <= 0:
                results.append(current)
                break
            current += timedelta(days=1)
        else:
            raise RuntimeError(
                f"Simulation exceeded {_MAX_SIMULATION_DAYS} days without "
                "completing — your throughput samples may be effectively zero."
            )
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
    results: list[int] = []
    for _ in range(runs):
        total = 0
        for _ in range(days):
            total += rng.choice(samples)
        results.append(total)
    return results


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


def render_histogram[X](
    hist: ResultsHistogram[X],
    *,
    width: int = 50,
    label: str = "outcome",
) -> str:
    """Render a Results Histogram as plain ASCII for terminal output.

    Each row: outcome value, bar of `#` proportional to count, count number.
    """
    max_count = max(hist.counts.values())
    label_width = max(len(str(k)) for k in hist.sorted_keys)
    label_width = max(label_width, len(label))
    lines = [f"  {label:<{label_width}}  freq  histogram"]
    for key in hist.sorted_keys:
        count = hist.counts[key]
        bar_len = max(1, round(count / max_count * width)) if count > 0 else 0
        bar = "#" * bar_len
        lines.append(f"  {key!s:<{label_width}}  {count:>4}  {bar}")
    return "\n".join(lines)


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
