---
title: Glossary
---

# Glossary

> **Diátaxis: Reference.** Definitions of every domain term, plus
> the terms we deliberately avoid and why.

flowmetrics uses the kanban-flow vocabulary popularised by *Actionable
Agile Metrics for Predictability* and *When Will It Be Done?* (Daniel
Vacanti). We're explicit about what each term means here so there's no
drift between the code, the docs, and the user.

## Terms we use

### Items

The unit of work the simulator counts. In this tool an item is one
merged pull request (GitHub) or one resolved issue (Jira).

We use **items**, not **backlog**. "Backlog" is Scrum-loaded — it
overloads the word for "the prioritized list of work-yet-to-be-done".
We mean "the count of things still to do" and nothing more. The CLI
flag is `--items N`; the dataclass field is `WhenDoneInput.items`.
The string "backlog" appears nowhere in user-facing narrative copy
(`tests/test_interpretation.py` asserts this).

### Cycle time

Wall-clock time from when work was committed to until it was done.
For a PR: `merged_at - created_at`. Reported as a per-item series and
summarised with empirical P50/P85/P95 percentiles by
`flow metric cycle-time`.

### Throughput

Count of items completed per unit time (we use days). The empirical
input to Monte Carlo Simulation. Reported by `flow metric throughput`.

### Aging

For an in-flight item: `today − started_at`. Plotted by current
workflow state against age. Percentile reference lines on the Aging
chart are **empirical** percentiles drawn from completed-item cycle
times, used as risk thresholds — they are not derived from Monte
Carlo simulation. Reported by `flow metric aging`.

### Cumulative Flow Diagram (CFD)

Stacked-band chart of state counts over time. Band height = items in
that state on that date; total stack height = items in the system;
slope of the top = arrival rate; slope of the bottom = throughput
(departures). Reported by `flow metric cumulative`.

### Training window

The historical window from which throughput samples are drawn. Default
30 calendar days ending yesterday-UTC — long enough to capture variance,
short enough not to drag in stale regimes. CLI flags: `--history-start`
and `--history-end`.

### Forecast window

The future window we're forecasting *into*. Used by `flow forecast
throughput`. `--start-date` (default: today) → `--target-date`
(required).

### Monte Carlo Simulation (MCS)

Draw daily throughput samples from the training window with
replacement, simulate forward, repeat 10,000 times. The distribution
of outcomes is the forecast. CLI: `--runs N`, default 10,000.

### Results Histogram

The empirical distribution produced by Monte Carlo. X-axis = outcome
(date for `flow forecast date`, item count for `flow forecast
throughput`); Y-axis = simulation-run frequency.

### Forward percentile

For `flow forecast date` (date-axis): "smallest date X such that
P(complete by X) >= p%". Higher confidence ⇒ later date.

### Backward percentile

For `flow forecast throughput` (items-axis): "largest item count N
such that P(deliver >= N) >= p%". Higher confidence ⇒ FEWER items.

This is the trickiest part of the framework — *more confidence
means commit to fewer items, not more*. The renderers spell this
out next to every `flow forecast throughput` output. See
[Monte Carlo forecasting](explain/forecasting.md) for the worked
math.

## Terms we deliberately avoid

### Backlog

Overloaded by Scrum. We say "items" or "items to complete".

### Velocity

Not a flow metric. It's a Scrum-specific story-point summing convention,
gameable, and frequently confused with throughput. We don't compute it.

### Burndown / burnup

Different framework (release-scope tracking). Out of scope.

### Story points

Effort estimation; not a flow metric. We count completed items, full
stop.

### Sprint

A scheduling unit, not a flow concept. We use absolute dates.

## Source-specific terminology

### PR (pull request)

GitHub-specific. The unit our GitHub source measures.

### Issue

Jira-specific. The unit our Jira source measures (same role in the
math as a PR for GitHub).

**Important: GitHub issues are *not* consulted by any flowmetrics
report.** For GitHub sources, the unit of work is the pull request,
period. A team that tracks WIP on GitHub issues without opening
corresponding PRs will have that work silently dropped. See
[Decisions § 10](explain/decisions.md#10-for-github-only-pull-requests-count-as-work--issues-are-invisible)
for the full reasoning.

When a renderer string says "items" it means "PRs OR Jira issues" —
the math is identical, but the noun differs by source.

## Deprecated terms

The following terms were associated with the now-removed
`flow efficiency` command. They are kept here only to explain the
content of archived design docs.

### Active time

The subset of cycle time during which someone was actually working
on the item. Derived from event clustering on the activity timeline.

### Wait time

`cycle_time - active_time`. Never measured directly — whatever isn't
active.

### Flow efficiency

`active_time / cycle_time`. The heuristic split between active and
wait wasn't a strong enough signal to be useful; the `flow
efficiency` command was removed.

## Configurable defaults

| Term | Default | Source |
|------|---------|--------|
| Training window length | 30 calendar days | derived from `--history-start` / `--history-end` |
| Training window end | Yesterday-UTC | `--history-end` |
| Forecast runs | 10,000 | `--runs` |
| Cycle-time window | last 30 days | `--start` / `--stop` on `flow metric cycle-time` |

## How this glossary stays accurate

`tests/test_interpretation.py` asserts that "backlog" never appears in
any user-facing narrative output. Changing one of the avoided terms
requires changing a test.
