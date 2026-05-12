from .compute import (
    FlowEfficiency,
    PullRequestEvents,
    WindowResult,
    aggregate,
    compute_pr_flow,
)
from .forecast import (
    ResultsHistogram,
    backward_percentile,
    build_histogram,
    forward_percentile,
    monte_carlo_how_many,
    monte_carlo_when_done,
)
from .service import (
    DEFAULT_CACHE_DIR,
    DEFAULT_GAP,
    DEFAULT_MIN_CLUSTER,
    DEFAULT_TRAINING_DAYS,
    flowmetrics_for_window,
    historical_throughput_samples,
    this_week_window,
)
from .throughput import daily_throughput

__all__ = [
    "DEFAULT_CACHE_DIR",
    "DEFAULT_GAP",
    "DEFAULT_MIN_CLUSTER",
    "DEFAULT_TRAINING_DAYS",
    "FlowEfficiency",
    "PullRequestEvents",
    "ResultsHistogram",
    "WindowResult",
    "aggregate",
    "backward_percentile",
    "build_histogram",
    "compute_pr_flow",
    "daily_throughput",
    "flowmetrics_for_window",
    "forward_percentile",
    "historical_throughput_samples",
    "monte_carlo_how_many",
    "monte_carlo_when_done",
    "this_week_window",
]


def main() -> None:  # entry point declared in pyproject.toml
    from .cli import cli

    cli()
