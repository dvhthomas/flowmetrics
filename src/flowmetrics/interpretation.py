"""Turns raw results into headlines, key insights, and next actions."""

from __future__ import annotations

from datetime import date

from .compute import WindowResult
from .forecast import ResultsHistogram
from .report import (
    EfficiencyInput,
    HowManyInput,
    Interpretation,
    TrainingSummary,
    WhenDoneInput,
)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


# ---------------------------------------------------------------------------
# Efficiency
# ---------------------------------------------------------------------------


def interpret_efficiency(input: EfficiencyInput, result: WindowResult) -> Interpretation:
    caveats_common = [
        "Never use this metric per engineer — it reflects the system, not individuals.",
        "Per-PR FE is a directional indicator, not a precise measurement.",
        f"Active time uses gap={input.gap_hours}h and "
        f"min_cluster={input.min_cluster_minutes}min — tuning these moves the number.",
    ]

    if result.pr_count == 0:
        return Interpretation(
            headline=f"No PRs merged in {input.repo} between {input.start} and {input.stop}.",
            key_insight="An empty window means nothing to measure — not a bad signal.",
            next_actions=[
                "Widen the date window with --start/--stop.",
                "Confirm the repo name is correct.",
            ],
            caveats=caveats_common,
        )

    portfolio = result.portfolio_efficiency
    bot_suffix = ""
    if result.bot_pr_count:
        bot_suffix = f" ({result.human_pr_count} human, {result.bot_pr_count} bot)"
    headline = (
        f"Portfolio flow efficiency for {input.repo} {input.start}→{input.stop}: "
        f"{_pct(portfolio)} across {result.pr_count} completed items{bot_suffix}."
    )

    slowest = max(result.per_pr, key=lambda p: p.cycle_time)

    if portfolio < 0.10:
        key_insight = (
            f"This is in Vacanti's typical 5-15% range for knowledge work — most clock time "
            f"is wait, not active. Slowest PR ({slowest.item_id})  ran "
            f"{slowest.cycle_time.total_seconds() / 86400:.1f}d and dominates the ratio."
        )
    elif portfolio < 0.25:
        key_insight = (
            f"Portfolio FE ({_pct(portfolio)}) sits above the typical 5-15% knowledge-work "
            "range. Sanity-check by looking at the per-PR breakdown — a few near-100% "
            "version-bump PRs can pull the ratio up."
        )
    else:
        key_insight = (
            f"Portfolio FE of {_pct(portfolio)} is unusually high — verify the data. "
            "Common causes: dependabot automation, tiny version-bump PRs that "
            "merge in minutes, or stale draft handling. Cross-check by sampling a "
            "few PRs by hand."
        )

    next_actions: list[str] = []
    long_runners = sorted(
        (p for p in result.per_pr if p.efficiency < 0.10),
        key=lambda p: p.cycle_time,
        reverse=True,
    )[:3]
    if long_runners:
        ids = ", ".join(f"{p.item_id}" for p in long_runners)
        next_actions.append(
            f"Inspect the three slowest PRs ({ids}) — they likely sat in review queue."
        )
    next_actions.append("Compare to the previous 2-4 weeks to spot a trend.")
    next_actions.append(
        "Look at where the wait time concentrates (pre-first-review, post-approval, blocked) "
        "rather than setting a target on the number itself."
    )

    return Interpretation(
        headline=headline,
        key_insight=key_insight,
        next_actions=next_actions,
        caveats=caveats_common,
    )


# ---------------------------------------------------------------------------
# Forecast: when-done
# ---------------------------------------------------------------------------


def interpret_when_done(
    input: WhenDoneInput,
    training: TrainingSummary,
    histogram: ResultsHistogram[date],
    percentiles: dict[int, date],
) -> Interpretation:
    p50 = percentiles[50]
    p85 = percentiles[85]
    p95 = percentiles[95]

    headline = (
        f"Forecast for {input.repo}: 85% confidence {input.items} items are done "
        f"by {p85}, starting {input.start_date}."
    )

    key_insight = (
        f"Median outcome is {p50}. 95% confidence is {p95}. The gap between 50% and 85% is "
        "the wait-cost of being predictable — wider gap = more daily-throughput variance."
    )

    next_actions = [
        f"Commit externally on the 85% date ({p85}), not the 50% date ({p50}).",
        "Re-run weekly to track drift in the forecast.",
    ]
    if input.items > training.total_merges:
        next_actions.append(
            f"Items to complete ({input.items}) exceeds total recent throughput "
            f"({training.total_merges} merges in {input.history_days}d). Forecast is "
            "extrapolating past the training horizon — treat the tail with skepticism."
        )
    # Vacanti: shorter-term forecasts are better. If the 85% date lands
    # well past the training window, suggest a shorter forecast.
    horizon_days = (p85 - input.start_date).days if hasattr(p85, "year") else 0
    if horizon_days > input.history_days * 1.5:
        next_actions.append(
            f"This forecast reaches {horizon_days} days out — {horizon_days / input.history_days:.1f}× "
            f"the training window. Shorter forecasts are more reliable; consider fewer "
            "items in a near-term commitment and re-forecasting after delivering them."
        )

    caveats = [
        "Assumes the next weeks will look like the recent past — no regime change.",
        "Zero-merge days are included as real samples; bad days happen in the simulator.",
        "Throughput unit is 'completed items', not 'features' — works only if item size is stable.",
    ]

    return Interpretation(
        headline=headline,
        key_insight=key_insight,
        next_actions=next_actions,
        caveats=caveats,
    )


# ---------------------------------------------------------------------------
# Forecast: how-many
# ---------------------------------------------------------------------------


def interpret_how_many(
    input: HowManyInput,
    training: TrainingSummary,
    histogram: ResultsHistogram[int],
    percentiles: dict[int, int],
) -> Interpretation:
    p50 = percentiles[50]
    p85 = percentiles[85]
    p95 = percentiles[95]

    days = (input.target_date - input.start_date).days + 1
    headline = (
        f"Forecast for {input.repo}: 85% confidence we deliver at least {p85} items by "
        f"{input.target_date} (window: {days} days)."
    )

    key_insight = (
        f"Median outcome is {p50} items; 95% commitment floor is {p95}. Higher confidence "
        f"= fewer items — promising {p95} leaves throughput on the table to buy certainty."
    )

    next_actions = [
        f"For external commitments, promise {p85} items (or {p95} if stakes are high).",
        f"For internal planning, treat {p50} as the realistic target — half the time you "
        "deliver less.",
        "Re-run weekly; the floor moves as historical throughput shifts.",
    ]
    if days > input.history_days:
        next_actions.append(
            f"Forecast horizon ({days}d) is longer than training window "
            f"({input.history_days}d) — treat the tail of the histogram with extra skepticism."
        )

    caveats = [
        "Confidence reads BACKWARD: higher % means FEWER items, not more.",
        "Throughput unit is 'completed items' — assumes item size is stable across windows.",
        "No accounting for known disruptions (holidays, on-call rotations, launches).",
    ]

    return Interpretation(
        headline=headline,
        key_insight=key_insight,
        next_actions=next_actions,
        caveats=caveats,
    )
