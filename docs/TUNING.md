# Tuning flow-metric parameters per repo

flowmetrics ships defaults tuned for corporate-synchronous teams:
4-hour activity-clustering gap, no stale-item filtering, no Issue+PR
stitching. These read low for OSS, where the rhythm of work is
different.

This doc explains the three knobs and how to choose values that match
your team's actual rhythm — without changing the canonical data model
or the math.

## What the metrics are actually measuring

The math is always the same:

```
cycle_time   = completed_at − created_at         (wall clock)
active_time  = sum of timeline-event clusters    (events within --gap-hours)
efficiency   = active_time / cycle_time          (in [0, 1])
```

**Cycle is cycle.** The wall-clock time from creation to merge is what
it is — invariant under tuning. If your PR sat open for 426 days, that
is its cycle, and the percentile lines on the scatterplot reflect that
distribution honestly.

The tuning question is: **what counts as a 'session of active work'?**
That's the `--gap-hours` knob. And separately: **which items are we
talking about at all?** That's `--exclude-stale-days` (noise filter)
and `--include-issues` (GitHub-specific cross-source stitching).

## `--gap-hours` — the activity-clustering window

The default is **4 hours**, which means events more than 4h apart are
considered to belong to different sessions. That assumes a
corporate-synchronous workflow: if someone hasn't touched the PR for
half a day they probably context-switched off it.

For **async OSS workflow** this is too tight. A typical OSS review
cycle:

- Author pushes
- Reviewer reads next morning (8–12 h gap)
- Author responds the following day (12–24 h gap)
- ...

With `--gap-hours=4` each of those events becomes its own "session,"
which collapses the active time to a series of `min_cluster` floors
(typically 30 minutes each). Result: an asynchronously-but-actively-
worked PR scores 0.1% efficient.

**Empirical tuning: look at the inter-event-gap distribution.** Pick a
threshold near the natural break between in-session work and
between-session waits.

For `astral-sh/uv` (202 PRs, 914 gaps over a 30-day window):

| Gap bucket    | Share  | Interpretation     |
|--------------:|-------:|--------------------|
| < 30 min      | 67.8%  | in-session         |
| 30 min – 1 h  | 7.1%   | in-session         |
| 1 – 4 h       | 7.2%   | in-session         |
| 4 – 12 h      | 3.3%   | in-session         |
| 12 – 24 h     | 6.7%   | overnight pickup   |
| 1 – 2 d       | 1.9%   | next-day pickup    |
| 2 d – 1 wk    | 3.3%   | between sessions   |
| 1 wk – 1 mo   | 2.0%   | dormant            |
| > 1 mo        | 0.8%   | abandoned          |

The cliff between "next-day pickup" and "between sessions" sits around
24–48 hours. **`--gap-hours=24` is the right value for uv** — it
captures 92% of intra-session gaps without conflating cross-week
pickups with active work.

This is per-repo. A team that works synchronously in business hours
will see a tighter cliff (probably around 4–8 h). A globally
distributed team with high time-zone spread will see a wider one
(24–48 h).

## `--exclude-stale-days` — the noise filter

Default: **off.** When set, CFD + aging drop items whose most recent
real event (commit, comment, review, label change) is more than N
days before the window stop.

This is for **OSS repos with large external-contribution backlogs.**
huggingface/transformers has 1233 items "in flight" in a 30-day
window. ~95% of those are PRs from external contributors that no
maintainer has touched in months. They aren't part of the team's flow
— they're a queue.

`--exclude-stale-days=14` cuts huggingface from 1233 → 155 items.
The CFD's bands become legible; the aging chart shows engaged WIP
instead of a sea of zombie PRs.

**The cost:** you're hiding real data. The zombies exist; they just
aren't being worked. If you want to track the backlog itself, run
without the filter and read the chart as "queue depth" rather than
"WIP."

**The lower bound:** don't go below 7 days unless you have a clear
reason. Below a week you risk dropping PRs that are genuinely under
slow async review.

## `--include-issues` — Issue+PR stitching (GitHub only)

Default: **off.** When set, GitHub commands fold open + closed Issues
into the same WorkItem stream as PRs. For Issues closed by a merged
PR, cycle time uses the PR's `mergedAt` (the causal "done" instant),
not the Issue's own `closedAt`.

This matters when **your workflow uses Issues for work requests and
PRs for implementation.** Without it, you only see the
implementation-phase cycle time; with it, you see the full
discussion-then-implementation arc.

For `CalcMark/go-calcmark`: scatterplot goes from 80 items (P85 =
0.0 days, PR-only) to 137 items (P85 = 0.7 days, with Issues).
The Issue-discussion phase that PR-only views silently truncate
becomes visible.

**When NOT to use it:** repos where Issues are filed by external
users for triage (and have no relationship to actual development
work). You'd be measuring intake latency, not engineering flow.

## Jira: tuning is mostly unnecessary

Jira's changelog gives every issue an explicit `status_intervals`
list. When `--active-statuses` matches the user's workflow (e.g.
`"In Progress,Patch Available"`), efficiency is computed
**directly** from time-in-status — no clustering, no gap.

The clustering path only kicks in for items without
`status_intervals` (which Jira items always have) or when no
`active_statuses` match any visited interval. Tune `--active-statuses`
to whatever your team's workflow actually uses; that's the only Jira
"tuning" that matters.

## Per-repo defaults in the sample set

| Sample | Tuning | What it demonstrates |
|---|---|---|
| `astral-sh/uv` | `--gap-hours=24` | Async OSS PR workflow; per-repo clustering tuning |
| `pytest-dev/pytest` | default 4h | Conventional review cadence; default works |
| `huggingface/transformers` | `--exclude-stale-days=14` | Signal-vs-noise at large OSS scale |
| `pre-commit/pre-commit` | none | Baseline; default settings are correct |
| `CalcMark/go-calcmark` | `--include-issues` | Issue+PR stitching for small-team workflow |
| `ASF/CASSANDRA` | rich Jira workflow | `status_intervals` drive efficiency directly |
| `ASF/BIGTOP` | rich Jira workflow | Same approach, smaller scale |

The three knobs are deliberately limited. **Don't add new metric
abstractions** (e.g. "dormancy detection," "engagement scoring")
when re-parameterizing the existing model captures the same
signal. The canonical data — `cycle_time`, `active_time`,
`status_intervals` — is what it is; tuning the threshold that
separates "in a session" from "between sessions" is enough to
adapt the metric to your team's rhythm.
