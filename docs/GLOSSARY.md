# Glossary

flowmetrics follows Daniel Vacanti's terminology from *Actionable Agile
Metrics for Predictability* and *When Will It Be Done?*. We're explicit
about what each term means here so there's no drift between the code,
the docs, and the user.

## Vacanti-approved terms

### Items

The unit of work the simulator counts. In this tool an item is one
merged pull request (GitHub) or one resolved issue (Jira, future).

We use **items**, not **backlog**. Vacanti is explicit that "backlog"
is contaminated — Scrum overloads it for "the prioritized list of
work-yet-to-be-done". We mean "the count of things still to do" and
nothing more. The CLI flag is `--items N`; the dataclass field is
`WhenDoneInput.items`. The string "backlog" appears nowhere in
user-facing narrative copy (`tests/test_interpretation.py` asserts
this).

### Cycle time

Wall-clock time from when work was committed to until it was done.
For a PR: `merged_at - created_at`. See
[`docs/METRICS.md`](METRICS.md) for the GitHub-specific proxy and
its limitations.

### Active time

The subset of cycle time during which someone was actually working
on the item. Derived from event clustering on the activity timeline.

### Wait time

`cycle_time - active_time`. Never measured directly — it's whatever
isn't active. Wait time is the actionable signal: large wait means a
queue, which is where flow efficiency points you.

### Flow efficiency

`active_time / cycle_time`. Always reported in two flavours:

- **Portfolio flow efficiency** = `Σ active / Σ cycle` across all
  items in the window. Vacanti's recipe. The right number for system-
  level conversations.
- **Per-item flow efficiency** = the ratio for one item. Directional;
  not a precise measurement.

We also report mean and median per-item ratios; both are inferior to
portfolio efficiency and we say so in the output. Mean of ratios is
particularly misleading when a long tail of fast PRs distorts it.

**Concrete example.** Fifty trivial 5-minute version-bump PRs at 100%
FE, plus one big 30-day refactor PR at 5% FE. Mean per-PR FE ≈ 98%.
Portfolio FE ≈ 5%. The portfolio number reflects the system you're
trying to improve — the one big PR is where the wait time actually
lives. Optimizing for the 98% number rewards the people churning out
small PRs and punishes the engineer working through the queue.

### Throughput

Count of items completed per unit time (we use days). The empirical
input to Monte Carlo Simulation.

### Training window

The historical window from which throughput samples are drawn. Default
30 calendar days ending yesterday-UTC. Vacanti's recommendation in *When
Will It Be Done?* — long enough to capture variance, short enough not
to drag in stale regimes. CLI flags: `--history-start` and
`--history-end`.

### Forecast window

The future window we're forecasting *into*. Used by `forecast
how-many`. `--start-date` (default: today) → `--target-date`
(required).

### Monte Carlo Simulation (MCS)

Draw daily throughput samples from the training window with
replacement, simulate forward, repeat 10,000 times. The distribution
of outcomes is the forecast. CLI: `--runs N`, default 10,000.

### Results Histogram

The empirical distribution produced by Monte Carlo. X-axis = outcome
(date for when-done, item count for how-many); Y-axis = simulation-run
frequency. Vacanti calls this the **Results Histogram**.

### Forward percentile

For `when-done` (date-axis): "smallest date X such that P(complete by
X) >= p%". Higher confidence ⇒ later date.

### Backward percentile

For `how-many` (items-axis): "largest item count N such that P(deliver
>= N) >= p%". Higher confidence ⇒ FEWER items.

This is the trickiest part of Vacanti's framework — *more confidence
means commit to fewer items, not more*. The HTML and text renderers
spell this out next to every how-many forecast.

## Terms we deliberately avoid

### Backlog

Overloaded by Scrum. We say "items" or "items to complete".

### Velocity

Vacanti rejects velocity as a flow metric: it's a Scrum-specific
story-point summing convention, gameable, and confused with
throughput. We don't compute it.

### Burndown / burnup

Different framework (release-scope tracking). Out of scope.

### Story points

Effort estimation; not a flow metric. We count completed items, full
stop.

### Sprint

A scheduling unit, not a flow concept. We use absolute dates.

## Source-specific terminology

### PR (pull request)

GitHub-specific. The unit our `efficiency week` command measures and
the unit `forecast` counts.

### Issue

Jira-specific. The unit our Jira source measures (same role in the
math as a PR for GitHub).

**Important: GitHub issues are *not* consulted by any flowmetrics
report.** For GitHub sources, the unit of work is the pull request,
period. A team that tracks WIP on GitHub issues without opening
corresponding PRs will have that work silently dropped. See
`docs/DECISIONS.md` #10 for the full reasoning, and `gh-velocity.org`
for a tool that does read GitHub issues with label-based workflows.

When a renderer string says "items" it means "PRs OR Jira issues" —
the math is identical, but the noun differs by source.

## Configurable defaults

| Term | Default | Source |
|------|---------|--------|
| Activity-cluster gap | 4 hours | `--gap-hours` |
| Minimum credit per cluster | 30 minutes | `--min-cluster-minutes` |
| Training window length | 30 calendar days | derived from `--history-start` / `--history-end` |
| Training window end | Yesterday-UTC | `--history-end` |
| Forecast runs | 10,000 | `--runs` |

## How this glossary stays accurate

`tests/test_interpretation.py` asserts that "backlog" never appears in
any user-facing narrative output. `tests/test_samples_helpers.py`
asserts the CLI surface (the configured repos, the --items flag).
Changing one of the avoided terms requires changing a test.
