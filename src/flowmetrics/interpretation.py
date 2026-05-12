"""Turns raw results into headlines, key insights, and next actions."""

from __future__ import annotations

from datetime import date

from .aging import AgingItem
from .cfd import CfdPoint
from .compute import WindowResult
from .forecast import ResultsHistogram
from .report import (
    AgingInput,
    CfdInput,
    EfficiencyInput,
    HowManyInput,
    Interpretation,
    TrainingSummary,
    WhenDoneInput,
)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _prose_date(d: date) -> str:
    """Prose date for headlines/insights/actions: ``Jan 12, 2026``.

    Always includes the year — prose has no implicit context to fall
    back on. Distinct from chart axis labels (`Jan 12`, no year unless
    spanning a year boundary).
    """
    return f"{d.strftime('%b')} {d.day}, {d.year}"


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
            headline=(
                f"No PRs merged in {input.repo} between "
                f"{_prose_date(input.start)} and {_prose_date(input.stop)}."
            ),
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
        f"Portfolio flow efficiency for {input.repo} "
        f"{_prose_date(input.start)} → {_prose_date(input.stop)}: "
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

    # Diagnostic: zero active time AND configured --active-statuses don't
    # appear in this workflow → name the mismatch and suggest a remap.
    # (Vacanti-flavored Jira projects often use non-standard status names.)
    from datetime import timedelta as _td

    if (
        result.observed_statuses
        and result.total_active == _td(0)
        and not (set(input.active_statuses) & set(result.observed_statuses))
    ):
        observed = ", ".join(repr(s) for s in result.observed_statuses)
        configured = ", ".join(repr(s) for s in input.active_statuses) or "(none)"
        next_actions.insert(
            0,
            f"Configured --active-statuses ({configured}) don't appear in this "
            f"workflow. Statuses actually observed: {observed}. Pick whichever "
            "represent active development and pass them as "
            "`--active-statuses 'X,Y'` — or accept that work happens outside "
            "the issue tracker.",
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
        f"by {_prose_date(p85)}, starting {_prose_date(input.start_date)}."
    )

    key_insight = (
        f"Median outcome is {_prose_date(p50)}. 95% confidence is "
        f"{_prose_date(p95)}. The gap between 50% and 85% is the wait-cost of "
        "being predictable — wider gap = more daily-throughput variance."
    )

    next_actions = [
        f"Commit externally on the 85% date ({_prose_date(p85)}), "
        f"not the 50% date ({_prose_date(p50)}).",
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
        f"{_prose_date(input.target_date)} (window: {days} days)."
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


# ---------------------------------------------------------------------------
# CFD
# ---------------------------------------------------------------------------


def interpret_cfd(input: CfdInput, points: list[CfdPoint]) -> Interpretation:
    caveats = [
        "Past data only — Vacanti's CFD property #5. The chart does not "
        "project forward.",
        "Items still in flight may be under-represented when the source "
        "only fetches completed work. Wider bands today may grow tomorrow.",
        f"Workflow used: {' → '.join(input.workflow)}. Items that visit "
        "statuses outside this list are silently skipped at those points.",
    ]

    if not points:
        return Interpretation(
            headline=(
                f"No CFD samples for {input.repo} between "
                f"{_prose_date(input.start)} and {_prose_date(input.stop)}."
            ),
            key_insight="Empty result set — nothing to plot.",
            next_actions=[
                "Widen the date window with --start/--stop.",
                "Confirm the workflow states match those used by the source.",
            ],
            caveats=caveats,
        )

    first = input.workflow[0]
    last = input.workflow[-1]
    end_point = points[-1]
    arrivals = end_point.counts_by_state.get(first, 0)
    departures = end_point.counts_by_state.get(last, 0)
    wip = arrivals - departures

    headline = (
        f"CFD for {input.repo} {_prose_date(input.start)} → "
        f"{_prose_date(input.stop)}: {arrivals} items arrived, "
        f"{departures} completed, {wip} still in flight at end."
    )

    # Vacanti property #3: the widest vertical band identifies where WIP
    # concentrates — that's where the bottleneck is. Compute per-band
    # WIP at the latest sample and surface the largest.
    bands: list[tuple[str, int]] = []
    for i in range(len(input.workflow) - 1):
        state = input.workflow[i]
        next_state = input.workflow[i + 1]
        band_wip = end_point.counts_by_state.get(state, 0) - end_point.counts_by_state.get(
            next_state, 0
        )
        bands.append((state, band_wip))

    next_actions: list[str] = []
    if bands and any(w > 0 for _, w in bands):
        widest = max(bands, key=lambda b: b[1])
        if widest[1] > 0:
            key_insight = (
                f"Largest WIP band is '{widest[0]}' with {widest[1]} items at "
                f"{_prose_date(end_point.sampled_on)}. Per Vacanti property "
                "#3 (vertical distance = WIP in that band), the widest band "
                "is where work piles up — look there for the bottleneck. "
                "Property #6 (slope = average arrival rate) tells you the "
                "rate that band is filling."
            )
            next_actions.append(
                f"Investigate why items accumulate in '{widest[0]}' — that's "
                "the system's current constraint."
            )
        else:
            key_insight = (
                f"No WIP at end of window — every arrival has departed. "
                f"The slope of the arrivals line is the average arrival rate "
                f"(property #6): {arrivals} items over "
                f"{(input.stop - input.start).days + 1} days."
            )
    else:
        key_insight = (
            "Workflow has a single state — CFD collapses to one line of "
            "cumulative arrivals."
        )

    if departures == 0 and arrivals > 0:
        key_insight = (
            f"Zero departures over the window: {arrivals} items arrived but "
            "none completed. Either throughput has stalled or the data "
            "source doesn't include enough recent completions."
        )
        next_actions.append(
            "Verify completed items appear in the data source for this window."
        )

    next_actions.append(
        "Look for places where one line flattens — that's a stall in that band."
    )
    next_actions.append(
        "Compare band widths to the previous window to spot accumulation trends."
    )

    return Interpretation(
        headline=headline,
        key_insight=key_insight,
        next_actions=next_actions,
        caveats=caveats,
    )


# ---------------------------------------------------------------------------
# Aging
# ---------------------------------------------------------------------------


def interpret_aging(
    input: AgingInput,
    items: list[AgingItem],
    cycle_time_percentiles: dict[int, float],
    completed_count: int,
) -> Interpretation:
    """Narrate a WIP-Aging snapshot per Vacanti.

    Calls out items already past P85 (likely to miss forecast) and the
    state column with the most accumulated WIP.
    """
    caveats = [
        "Aging is a snapshot as of "
        f"{_prose_date(input.asof)} — not a trend over time.",
        "Percentile lines reflect the completed-item distribution from "
        f"{_prose_date(input.history_start)} → {_prose_date(input.history_end)} "
        f"({completed_count} items). If recent throughput has changed, "
        "those checkpoints may have drifted.",
        "Items in early workflow states (Open, To Do) often age longest in "
        "OSS — that's queue, not work. Treat the chart as a queue-depth "
        "indicator first, a productivity signal second.",
    ]

    if not items:
        return Interpretation(
            headline=(
                f"No in-flight items for {input.repo} as of "
                f"{_prose_date(input.asof)}."
            ),
            key_insight=(
                "Empty WIP can mean a clean board, or a stale source. "
                "Cross-check by widening the workflow states queried."
            ),
            next_actions=[
                "Confirm the workflow states match those used by the source.",
                "If items exist but are missing, verify the source's open-issue "
                "query (Jira: `resolution = Unresolved`).",
            ],
            caveats=caveats,
        )

    p85 = cycle_time_percentiles.get(85, 0.0)
    p95 = cycle_time_percentiles.get(95, 0.0)
    past_p85 = sorted(
        (i for i in items if i.age_days >= p85 and p85 > 0),
        key=lambda i: i.age_days,
        reverse=True,
    )
    past_p95 = [i for i in items if i.age_days >= p95 and p95 > 0]

    # WIP per state column
    wip_per_state: dict[str, int] = {}
    for it in items:
        wip_per_state[it.current_state] = wip_per_state.get(it.current_state, 0) + 1
    biggest_state, biggest_count = max(wip_per_state.items(), key=lambda kv: kv[1])

    headline = (
        f"WIP Aging for {input.repo} as of {_prose_date(input.asof)}: "
        f"{len(items)} in-flight items, {len(past_p85)} already past P85 "
        f"({p85:.1f}d), {len(past_p95)} past P95 ({p95:.1f}d)."
    )

    if past_p85:
        oldest = past_p85[:3]
        ids = ", ".join(i.item_id for i in oldest)
        key_insight = (
            f"{len(past_p85)} item(s) have aged past the P85 cycle time "
            f"({p85:.1f}d) — Vacanti's threshold for likely forecast miss. "
            f"Oldest: {ids}. Concentrate decision-making here."
        )
    else:
        key_insight = (
            f"All in-flight items are still inside the P85 cycle-time "
            f"threshold ({p85:.1f}d) — pipeline is on track relative to "
            "recent history."
        )

    next_actions: list[str] = []
    if past_p95:
        next_actions.append(
            f"{len(past_p95)} item(s) past P95 ({p95:.1f}d): decide explicitly "
            "to expedite, split, or drop. They are now outliers in your own "
            "historical distribution."
        )
    if past_p85 and not past_p95:
        oldest_ids = ", ".join(i.item_id for i in past_p85[:5])
        next_actions.append(
            f"Triage past-P85 items today: {oldest_ids}. The longer they "
            "sit, the further out they push the next forecast."
        )

    next_actions.append(
        f"Biggest WIP column is '{biggest_state}' with {biggest_count} "
        "item(s). If that band is upstream (queue), pull policy. If "
        "downstream (review/test), capacity."
    )
    next_actions.append(
        "Re-check daily; Aging is a leading indicator of forecast slip."
    )

    return Interpretation(
        headline=headline,
        key_insight=key_insight,
        next_actions=next_actions,
        caveats=caveats,
    )
