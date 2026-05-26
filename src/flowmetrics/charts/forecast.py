"""Layer 2 — the forecast chart models.

Two Monte Carlo views:

  `build_when_done_model(...)`  → `WhenDoneModel`
      "When will it be done?" Distribution of completion DATES.

  `build_how_many_model(...)`   → `HowManyModel`
      "How many will be done by date X?" Distribution of COUNTS.

Both run M=10,000 simulations against the daily-throughput
distribution derived from the warehouse's completion history.
The Monte Carlo primitives live in `flowmetrics.forecast`
(stdlib-only, ~10–25ms per 10K runs); this layer wraps them with
percentile extraction, display formatting, and the headline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from random import Random

from ..forecast import (
    backward_percentile,
    build_histogram,
    forward_percentile,
    monte_carlo_how_many,
    monte_carlo_when_done,
)
from ..throughput import daily_counts
from ..utc_dates import attach_utc, to_utc_display_date
from ..warehouse.queries import CompletedItem
from ..windows import Window

# Number of simulations. 10K is the standard recommendation — enough
# for stable P95s, fast enough for interactive sliders.
DEFAULT_RUNS = 10_000

# Colour tokens — same neutrals + P85 accent the cycle-time chart
# uses. The view embeds these on percentile rules and on the
# small percentile-row table next to the chart.
_PCT_COLOR_P50 = "__theme:muted__"
_PCT_COLOR_P85 = "__theme:p-500__"
_PCT_COLOR_P95 = "__theme:fg__"


@dataclass(frozen=True)
class WhenDoneModel:
    """Fully-resolved 'when will N items be done' chart."""

    histogram: tuple[dict, ...]  # [{"date_iso", "count"}]
    p50_iso: str
    p85_iso: str
    p95_iso: str
    p50_display: str
    p85_display: str
    p95_display: str
    headline: str
    daily_throughput_n_days: int

    @property
    def percentile_rows(self) -> tuple[dict, ...]:
        return (
            {"label": "P50", "value_display": self.p50_display,
             "color": _PCT_COLOR_P50},
            {"label": "P85", "value_display": self.p85_display,
             "color": _PCT_COLOR_P85},
            {"label": "P95", "value_display": self.p95_display,
             "color": _PCT_COLOR_P95},
        )

    @property
    def is_empty(self) -> bool:
        return not self.histogram


@dataclass(frozen=True)
class HowManyModel:
    """Fully-resolved 'how many done in window' chart."""

    histogram: tuple[dict, ...]  # [{"count", "runs"}]
    p50: int
    p85: int
    p95: int
    headline: str
    daily_throughput_n_days: int

    @property
    def percentile_rows(self) -> tuple[dict, ...]:
        return (
            {"label": "P50", "value_display": f"≥ {self.p50} items",
             "color": _PCT_COLOR_P50},
            {"label": "P85", "value_display": f"≥ {self.p85} items",
             "color": _PCT_COLOR_P85},
            {"label": "P95", "value_display": f"≥ {self.p95} items",
             "color": _PCT_COLOR_P95},
        )

    @property
    def is_empty(self) -> bool:
        return not self.histogram


def _utc_date(dt: datetime) -> date:
    return attach_utc(dt).date()


def _display(d: date) -> str:
    return to_utc_display_date(datetime(d.year, d.month, d.day, tzinfo=UTC))


def _daily_counts(
    items: list[CompletedItem], reference: Window | None,
) -> list[int]:
    """Daily completion counts across the OBSERVED completion span,
    optionally narrowed to `reference`.

    The walk is bounded by the observed completion span — not by
    `reference`. Padding with phantom zero days outside the data
    would feed the Monte Carlo NODATA dressed up as zero throughput,
    biasing every forecast pessimistically. Reference scopes WHICH
    completions count; it never extends the sample.
    """
    dates = [_utc_date(it.completed_at) for it in items]
    if reference is not None:
        dates = [d for d in dates if reference.from_ <= d <= reference.to]
    if not dates:
        return []
    return daily_counts(dates, min(dates), max(dates))


def build_when_done_model(
    items: list[CompletedItem],
    *,
    backlog: int,
    start_date: date,
    runs: int = DEFAULT_RUNS,
    seed: int = 0,
    reference: Window | None = None,
) -> WhenDoneModel:
    """Run a Monte Carlo simulation: when will `backlog` items be
    done? Returns the histogram + the P50/P85/P95 completion dates."""
    samples = _daily_counts(items, reference)
    if not samples:
        return WhenDoneModel(
            histogram=(),
            p50_iso="", p85_iso="", p95_iso="",
            p50_display="", p85_display="", p95_display="",
            headline="No throughput data yet.",
            daily_throughput_n_days=0,
        )
    rng = Random(seed)
    results = monte_carlo_when_done(
        samples, backlog, start_date, runs=runs, rng=rng,
    )
    hist = build_histogram(results)
    # `forward_percentile` expects p in (0, 100] — percentages, NOT
    # probabilities. Passing 0.50/0.85/0.95 would make threshold a
    # 50/85/95-count threshold, collapsing all three dates onto the
    # earliest bin.
    p50 = forward_percentile(hist, 50)
    p85 = forward_percentile(hist, 85)
    p95 = forward_percentile(hist, 95)
    histogram = tuple(
        {"date_iso": d.isoformat(), "count": hist.counts[d]}
        for d in hist.sorted_keys
    )
    p50_d = _display(p50)
    p85_d = _display(p85)
    p95_d = _display(p95)
    headline = (
        f"{backlog} items from {_display(start_date)} · "
        f"P50 by {p50_d} · P85 by {p85_d} · P95 by {p95_d} "
        f"({runs:,} runs over {len(samples)} days of throughput history)"
    )
    return WhenDoneModel(
        histogram=histogram,
        p50_iso=p50.isoformat(),
        p85_iso=p85.isoformat(),
        p95_iso=p95.isoformat(),
        p50_display=p50_d,
        p85_display=p85_d,
        p95_display=p95_d,
        headline=headline,
        daily_throughput_n_days=len(samples),
    )


def build_how_many_model(
    items: list[CompletedItem],
    *,
    start_date: date,
    end_date: date,
    runs: int = DEFAULT_RUNS,
    seed: int = 0,
    reference: Window | None = None,
) -> HowManyModel:
    """Run a Monte Carlo simulation: how many items will be done in
    [start_date, end_date]? For counts the percentile convention is
    inverted: P85 is the count you'd hit AT LEAST 85% of the time
    (lower than P50). `backward_percentile` enforces the
    high-confidence-floor interpretation."""
    samples = _daily_counts(items, reference)
    days = (end_date - start_date).days + 1
    if not samples or days <= 0:
        return HowManyModel(
            histogram=(),
            p50=0, p85=0, p95=0,
            headline="No throughput data yet.",
            daily_throughput_n_days=0,
        )
    rng = Random(seed)
    results = monte_carlo_how_many(
        samples,
        start_date=start_date,
        end_date=end_date,
        runs=runs,
        rng=rng,
    )
    hist = build_histogram(results)
    p50 = int(backward_percentile(hist, 50))
    p85 = int(backward_percentile(hist, 85))
    p95 = int(backward_percentile(hist, 95))
    histogram = tuple(
        {"count": k, "runs": hist.counts[k]} for k in hist.sorted_keys
    )
    headline = (
        f"{days} days from {_display(start_date)} · "
        f"P50 ≥ {p50} items · P85 ≥ {p85} · P95 ≥ {p95} "
        f"({runs:,} runs over {len(samples)} days of throughput history)"
    )
    return HowManyModel(
        histogram=histogram,
        p50=p50,
        p85=p85,
        p95=p95,
        headline=headline,
        daily_throughput_n_days=len(samples),
    )
