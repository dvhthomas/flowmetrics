"""Typed report objects shared by every renderer.

Each command builds a Report and hands it to a renderer (json / text /
html). Renderers never recompute; they only format what's in the Report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .compute import WindowResult
from .forecast import ResultsHistogram


@dataclass(frozen=True)
class Interpretation:
    headline: str
    key_insight: str
    next_actions: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EfficiencyInput:
    repo: str
    start: date
    stop: date
    gap_hours: float
    min_cluster_minutes: float
    offline: bool
    # Status names mapped to "active" when items carry named workflow
    # statuses (Jira). Ignored for GitHub. Captured here so the
    # interpretation layer can suggest a remap when observed statuses
    # don't overlap the configured set.
    active_statuses: tuple[str, ...] = ()


@dataclass(frozen=True)
class EfficiencyReport:
    input: EfficiencyInput
    result: WindowResult
    interpretation: Interpretation
    generated_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    schema: str = "flowmetrics.efficiency.v1"
    command: str = "efficiency week"


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainingSummary:
    window_start: date
    window_end: date
    daily_samples: list[int]
    total_merges: int
    avg_per_day: float
    min_per_day: int
    max_per_day: int
    zero_days: int


@dataclass(frozen=True)
class SimulationSummary:
    runs: int
    seed: int | None


@dataclass(frozen=True)
class WhenDoneInput:
    repo: str
    items: int  # Number of items to complete. "Items" follows Vacanti's
    # phrasing; we avoid "backlog" because Scrum overloads it.
    start_date: date
    history_start: date
    history_end: date
    offline: bool

    @property
    def history_days(self) -> int:
        """Inclusive day count of the training window."""
        return (self.history_end - self.history_start).days + 1


@dataclass(frozen=True)
class WhenDoneReport:
    input: WhenDoneInput
    training: TrainingSummary
    simulation: SimulationSummary
    histogram: ResultsHistogram[date]
    percentiles: dict[int, date]
    interpretation: Interpretation
    generated_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    schema: str = "flowmetrics.forecast.when_done.v1"
    command: str = "forecast when-done"


@dataclass(frozen=True)
class HowManyInput:
    repo: str
    start_date: date
    target_date: date
    history_start: date
    history_end: date
    offline: bool

    @property
    def history_days(self) -> int:
        return (self.history_end - self.history_start).days + 1


@dataclass(frozen=True)
class HowManyReport:
    input: HowManyInput
    training: TrainingSummary
    simulation: SimulationSummary
    histogram: ResultsHistogram[int]
    percentiles: dict[int, int]
    interpretation: Interpretation
    generated_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    schema: str = "flowmetrics.forecast.how_many.v1"
    command: str = "forecast how-many"


Report = EfficiencyReport | WhenDoneReport | HowManyReport


@dataclass(frozen=True)
class ForecastHorizon:
    """How far the forecast extends, vs. how much past data it's based on.

    Vacanti's recurring point: shorter-term forecasts are more reliable.
    The further `days_ahead` exceeds `training_window_days`, the more
    susceptible the forecast is to a regime change invalidating it.
    """

    days_ahead: int
    training_window_days: int
    ratio: float
    reading: str  # narrative explanation of the ratio


def forecast_horizon(report: WhenDoneReport | HowManyReport) -> ForecastHorizon:
    if isinstance(report, WhenDoneReport):
        # The 85% confidence date is the canonical "forecast endpoint".
        endpoint = report.percentiles.get(85) or max(report.percentiles.values())
        days_ahead = (endpoint - report.input.start_date).days
    else:
        days_ahead = (report.input.target_date - report.input.start_date).days

    training_days = (report.training.window_end - report.training.window_start).days + 1
    ratio = days_ahead / training_days if training_days else 0.0

    if ratio <= 1.0:
        reading = (
            "Forecast horizon is within the training window — relatively trusted. "
            "Shorter is better."
        )
    elif ratio <= 2.0:
        reading = (
            "Forecast horizon extends past the training window — treat with caution. "
            "Shorter is better; consider tightening the question."
        )
    else:
        reading = (
            "Forecast horizon is much further out than the training data covers. "
            "High risk of regime change invalidating the result. Shorter is better."
        )
    return ForecastHorizon(days_ahead, training_days, ratio, reading)


_EFFICIENCY_VOCABULARY = {
    "Cycle time": (
        "Wall-clock time from when a PR was opened until it was merged. "
        "The clock starts at `created_at` and stops at `merged_at`."
    ),
    "Active time": (
        "The share of cycle time covered by clusters of activity events "
        "(commits, reviews, comments). Events more than `gap-hours` apart "
        "form separate clusters; each cluster credits at least "
        "`min-cluster-minutes` of active time."
    ),
    "Wait time": (
        "cycle_time − active_time. Time the PR spent waiting in queues "
        "(awaiting review, blocked, etc.). Vacanti's actionable signal — "
        "queues are where the system bottlenecks live."
    ),
    "Flow efficiency": ("active_time / cycle_time. Reported per-PR and as a portfolio."),
    "Portfolio flow efficiency": (
        "Σ active / Σ cycle across all merged PRs in this window. Vacanti's "
        "system-level recipe — long-running PRs dominate, which is what you want."
    ),
}


_FORECAST_VOCABULARY = {
    "Throughput": (
        "Items completed per unit time (here: per day). The empirical input "
        "Monte Carlo Simulation draws from."
    ),
    "Training window": (
        "The recent period whose daily throughput we sample. Defaults to the "
        "last 30 calendar days ending yesterday-UTC — Vacanti's recommended "
        "horizon."
    ),
    "Monte Carlo Simulation": (
        "Draws daily throughput with replacement, simulates forward, and "
        "repeats (10,000 runs by default). The distribution of outcomes is "
        "the forecast."
    ),
    "Results Histogram": (
        "The empirical distribution produced by Monte Carlo. X-axis = "
        "outcome (date for when-done; item count for how-many); Y-axis = "
        "simulation-run frequency."
    ),
    "Percentile": (
        "Confidence level. For when-done (date axis): read FORWARD — 85% "
        "confidence is a later date. For how-many (items axis): read "
        "BACKWARD — 85% confidence is FEWER items, a more conservative "
        "commitment."
    ),
}


def report_vocabulary(report: Report) -> dict[str, str]:
    """Inline canonical definitions for the terms a reader will encounter."""
    if isinstance(report, EfficiencyReport):
        return dict(_EFFICIENCY_VOCABULARY)
    if isinstance(report, WhenDoneReport | HowManyReport):
        return dict(_FORECAST_VOCABULARY)
    raise TypeError(f"unknown report type: {type(report).__name__}")  # pragma: no cover


def report_definition(report: Report) -> str:
    """One-paragraph definition of what this report measures.

    Sits near the top of every rendered output so a reader can interpret
    the result without consulting docs/METRICS.md or docs/FORECAST.md.
    """
    if isinstance(report, EfficiencyReport):
        return (
            "Portfolio flow efficiency: the share of cycle time that was actively "
            "worked on, vs. waiting in review or other queues. Portfolio FE = "
            "Σ active / Σ cycle across all merged PRs in this window — Vacanti's "
            "recipe, the right number to act on."
        )
    if isinstance(report, WhenDoneReport):
        return (
            "Monte Carlo forecast of when N items will finish, drawing daily "
            "throughput samples from the training window. The histogram is the "
            "distribution of simulated completion dates; percentile lines mark "
            "confidence thresholds. Read forward: higher confidence = later date."
        )
    if isinstance(report, HowManyReport):
        return (
            "Monte Carlo forecast of how many items finish by a target date. "
            "The histogram is the distribution of simulated item counts; "
            "percentile lines mark confidence thresholds. Read BACKWARD: higher "
            "confidence = FEWER items, a more conservative commitment."
        )
    raise TypeError(f"unknown report type: {type(report).__name__}")  # pragma: no cover


def cli_invocation(report: Report) -> str:
    """Reconstruct the CLI command that would produce this report.

    Carries report provenance into every rendered artifact — both humans
    (copy-paste to reproduce) and agents (concrete command to suggest).
    """
    if isinstance(report, EfficiencyReport):
        parts = [
            "uv run flow efficiency week",
            f"--repo {report.input.repo}",
            f"--start {report.input.start.isoformat()}",
            f"--stop {report.input.stop.isoformat()}",
            f"--gap-hours {report.input.gap_hours}",
            f"--min-cluster-minutes {report.input.min_cluster_minutes}",
        ]
        if report.input.offline:
            parts.append("--offline")
        return " ".join(parts)

    if isinstance(report, WhenDoneReport):
        parts = [
            "uv run flow forecast when-done",
            f"--repo {report.input.repo}",
            f"--items {report.input.items}",
            f"--start-date {report.input.start_date.isoformat()}",
            f"--history-start {report.input.history_start.isoformat()}",
            f"--history-end {report.input.history_end.isoformat()}",
            f"--runs {report.simulation.runs}",
        ]
        if report.simulation.seed is not None:
            parts.append(f"--seed {report.simulation.seed}")
        if report.input.offline:
            parts.append("--offline")
        return " ".join(parts)

    if isinstance(report, HowManyReport):
        parts = [
            "uv run flow forecast how-many",
            f"--repo {report.input.repo}",
            f"--target-date {report.input.target_date.isoformat()}",
            f"--start-date {report.input.start_date.isoformat()}",
            f"--history-start {report.input.history_start.isoformat()}",
            f"--history-end {report.input.history_end.isoformat()}",
            f"--runs {report.simulation.runs}",
        ]
        if report.simulation.seed is not None:
            parts.append(f"--seed {report.simulation.seed}")
        if report.input.offline:
            parts.append("--offline")
        return " ".join(parts)

    raise TypeError(f"unknown report type: {type(report).__name__}")  # pragma: no cover


def build_training_summary(daily_samples: list[int], start: date, end: date) -> TrainingSummary:
    return TrainingSummary(
        window_start=start,
        window_end=end,
        daily_samples=list(daily_samples),
        total_merges=sum(daily_samples),
        avg_per_day=sum(daily_samples) / len(daily_samples) if daily_samples else 0.0,
        min_per_day=min(daily_samples) if daily_samples else 0,
        max_per_day=max(daily_samples) if daily_samples else 0,
        zero_days=sum(1 for s in daily_samples if s == 0),
    )
