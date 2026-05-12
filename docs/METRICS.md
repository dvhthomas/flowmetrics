# How flow efficiency is calculated

This document defines every number this tool reports, where it comes from in
the GitHub API, and which real-world concept it is a proxy for. Read it
before you act on any of the numbers.

## 1. The concept

Flow efficiency, in Daniel Vacanti's framing, is one ratio:

```
Flow Efficiency = Active Time / Total Cycle Time
```

- **Total cycle time** is wall-clock time: from when work was committed to,
  until it was done.
- **Active time** is the subset of cycle time during which someone was
  actually working on the item. Everything else — waiting on review, blocked,
  sitting in a QA queue, waiting on a dependency — is **wait time**.

Vacanti's recurring point: real numbers are usually 5–15% in knowledge work,
and the value of the metric is **locating the biggest queue**, not hitting a
target.

This tool measures flow efficiency at the **pull request level**. It does not
measure feature-level or issue-level flow. See the limitations section for
what that means in practice.

## 2. The formula, end to end

For one pull request:

```
cycle_time(pr)        = pr.merged_at - pr.created_at
active_time(pr)       = sum_over_clusters( max(cluster_span, min_cluster) )
                        capped at cycle_time(pr)
efficiency(pr)        = active_time(pr) / cycle_time(pr)
```

For a window of N pull requests:

```
portfolio_efficiency  = sum(active_time)  / sum(cycle_time)
mean_efficiency       = mean( efficiency(pr_i) )
median_efficiency     = median( efficiency(pr_i) )
```

`portfolio_efficiency` is the right number. The mean and median are reported
because people ask for them, but `portfolio_efficiency` is what Vacanti means
by "the system's flow efficiency": one PR sitting in review for two weeks
matters more than ten one-hour PRs, and only the portfolio ratio reflects
that. See section 7 for a worked example showing them diverge.

## 3. How GitHub data proxies the concepts

GitHub does not directly expose "active time" or "wait time". We derive them.

### 3.1 Cycle time

```
cycle_time = pr.mergedAt - pr.createdAt
```

`createdAt` is the GraphQL field for when the PR was opened. `mergedAt` is
when it was merged.

**Concept it proxies:** the moment work was committed to, until done.

**What this misses:**
- Work that happened **before** the PR was opened (branch work, drafting in a
  local editor) is not counted. If your team opens PRs late, your cycle time
  will look artificially short.
- Time spent in a **draft** state is included in cycle time. Whether that is
  fair depends on your team's conventions — see section 6.2.
- Work that never resulted in a merge (closed PRs) is excluded entirely.

If you want to count earlier work, you need to pair PRs with the issues or
tickets that triggered them. That is not in this tool yet.

### 3.2 Active time

`active_time` is computed by:

1. Collecting every activity event timestamp on the PR.
2. Clustering events that are close together in time.
3. Summing the duration of each cluster (with a minimum floor per cluster).
4. Capping the total at `cycle_time` so floors can never push it past 100%.

**Concept it proxies:** the wall-clock time during which someone was actively
working on, reviewing, or discussing the PR.

The events we count and where each comes from in the GraphQL response:

| Event source              | GraphQL `__typename`         | Field used         | What it represents                                      |
| ------------------------- | ---------------------------- | ------------------ | ------------------------------------------------------- |
| Code pushed               | `PullRequestCommit`          | `commit.committedDate` | The author pushed work                              |
| Review submitted          | `PullRequestReview`          | `submittedAt`      | A reviewer read the diff and submitted a verdict       |
| Top-level comment         | `IssueComment`               | `createdAt`        | Someone wrote on the PR conversation                   |
| Review-thread comment     | `PullRequestReviewThread`    | `comments.nodes[].createdAt` | Inline comment on a code line              |
| Marked ready for review   | `ReadyForReviewEvent`        | `createdAt`        | PR transitioned out of draft                           |
| Converted back to draft   | `ConvertToDraftEvent`        | `createdAt`        | PR transitioned back to draft                          |
| Reviewer requested        | `ReviewRequestedEvent`       | `createdAt`        | Someone asked for a review (often automated)           |
| Merged                    | `MergedEvent`                | `createdAt`        | Final merge moment                                      |
| Closed                    | `ClosedEvent`                | `createdAt`        | PR was closed (we still include the timestamp)         |

We also implicitly include `createdAt` and `mergedAt` themselves, so a PR
with no other activity still has two events (open and merge) that the
clustering can work on.

Anything else in the timeline — labels, assignees, milestones, head-ref
deletions — is ignored. They are bookkeeping, not work.

### 3.3 Wait time

```
wait_time = cycle_time - active_time
```

Wait time is **never measured directly**. It is whatever cycle time is not
covered by an activity cluster. If a PR sits for three days with no commits,
no reviews, no comments, those three days are wait time by definition.

This is intentional. There is no GitHub event for "wait" — there are only
events for "something happened". The absence of events is the only signal we
have for queue time.

## 4. The clustering algorithm

This is where the active-time proxy gets opinionated. The naive choice —
"sum all activity intervals" — fails because most activity events are
instantaneous (a comment, a review submission). You cannot sum durations of
points.

### 4.1 What clustering does

Given a list of event timestamps and two parameters:

- `gap` — the inactivity threshold (default 4 hours)
- `min_cluster` — the minimum credit per cluster (default 30 minutes)

The algorithm:

1. Sort the events.
2. Walk them in order. If the next event is within `gap` of the previous
   one, it joins the same cluster. Otherwise it starts a new cluster.
3. Each cluster has a span: `(end - start)`. A cluster with a single event
   has a span of zero.
4. Each cluster's credited duration is `max(span, min_cluster)`.
5. Total active time is the sum of credited durations, then capped at
   `cycle_time`.

### 4.2 Why the `gap` parameter

If you commit at 09:00 and another commit lands at 14:00 with nothing in
between, are those two commits part of one stretch of work, or two? The
algorithm needs to decide. A 4-hour gap is a defensible default: short
enough that a typical morning of coding clusters together, long enough that
"morning work" and "afternoon work after a meeting" don't get joined into
one 5-hour cluster that includes the meeting.

You can tune it: `--gap-hours 2` is stricter (more wait time), `--gap-hours
8` is looser (more active time).

### 4.3 Why the `min_cluster` floor

A PR opened at 09:00 with two comments at 09:30 and a merge at 17:00 would
otherwise produce one cluster spanning 09:00–17:00 — eight hours of "active
time" because someone happened to comment in the middle. That overstates
the work.

Conversely, a PR opened and merged in the same minute with no other events
would otherwise produce zero active time, because two events at the same
moment have zero span.

The `min_cluster` floor is the compromise: every cluster represents at
least 30 minutes of credited work. It says: "if something happened, somebody
was paying attention to this for at least the minimum interval."

The 30-minute default is conservative; it credits more than most events
deserve in isolation, but it is small enough that a PR with sparse activity
across a long window still shows low flow efficiency. You can tune it:
`--min-cluster-minutes 15` is stricter, `--min-cluster-minutes 60` is more
generous.

### 4.4 Worked example: clustering

Consider a PR opened Monday 09:00, merged Wednesday 17:00 (56 hours), with:

- Monday 09:30 — commit
- Monday 10:00 — review comment
- Monday 14:00 — commit
- Wednesday 16:00 — commit
- Wednesday 17:00 — merge

With `gap=4h`, the algorithm produces two clusters:

1. **Monday 09:00 → 14:00**: spans 5h. The 09:00 (open), 09:30, 10:00,
   and 14:00 events are all within 4h of their neighbours, so they collapse
   into one cluster. Credited: `max(5h, 30min) = 5h`.
2. **Wednesday 16:00 → 17:00**: spans 1h. The 16:00 and 17:00 events are
   adjacent. Credited: `max(1h, 30min) = 1h`.

Active time: 6h. Cycle time: 56h. **Flow efficiency: 10.7%.**

The 50 hours between Monday 14:00 and Wednesday 16:00 are wait time. That is
likely "PR sat in review queue" or "author got pulled onto something else".
The metric does not tell you which. That is the point of the next step:
look at where wait time is concentrated across many PRs.

### 4.5 Worked example: the floor matters

Consider a PR opened Monday 09:00, merged Friday 09:00 (96 hours), with no
intermediate activity. There are exactly two events: open and merge.

With `gap=4h`, they form two separate clusters (96h apart). Each has zero
span. Each is floored to 30 minutes. Total active time: 1 hour.

**Flow efficiency: 1.0%.** This matches intuition: the PR existed for four
working days with no observable work, so it must have been waiting.

## 5. Aggregation

Reported per window:

- `pr_count` — number of merged PRs in the window.
- `total_cycle` — sum of cycle times.
- `total_active` — sum of active times.
- `portfolio_efficiency` — `total_active / total_cycle`.
- `mean_efficiency` — average of per-PR ratios.
- `median_efficiency` — median of per-PR ratios.

### 5.1 Why portfolio is the right number

The portfolio ratio weights every hour of cycle time equally, regardless of
which PR it belongs to. One PR sitting in review for two weeks contributes
two weeks of cycle time and (probably) very little active time, dragging
the portfolio efficiency down — which is correct, because two weeks of
delivery delay matters more than the speed of ten one-hour version-bump
PRs.

The mean-of-ratios treats every PR equally, regardless of size or duration.
A version-bump PR that goes open-to-merged in 30 minutes counts the same as
a 14-day PR with two weeks of waiting. That is misleading.

The recorded test data is concrete: for `astral-sh/uv` 2026-05-04 to
2026-05-10:

- `portfolio_efficiency = 3.8%`
- `mean_efficiency = 54.2%`

Both come from the same 43 PRs. The portfolio number is the honest one.
The mean is inflated by a long tail of tiny PRs that auto-bump-version and
merge within an hour, each scoring 100%.

### 5.2 What the median is useful for

The median tells you what a typical PR's experience looks like in
isolation, which is sometimes useful for engineer-facing conversations
("most PRs ship in under a day, but our portfolio is dragged down by a few
long-runners"). It is not the system-level number.

## 6. Assumptions, in plain language

These are the choices baked into the implementation. Each one is defensible;
none is the only possible choice.

### 6.1 We only measure merged PRs

Closed-without-merge PRs are excluded. Vacanti would argue these still
represent flow (work was committed to, then abandoned) and ideally should be
counted with `closed_at` as the end. We exclude them because most teams
treat them as not-real work, and including them noisily inflates cycle time.

### 6.2 Draft PRs are treated as part of cycle time

If a PR is opened as a draft Monday and marked ready-for-review Friday, the
Monday→Friday span counts as cycle time. We do not distinguish "drafting"
from "waiting". This is the simplest defensible choice but you may disagree
if your team uses drafts to mean "work in progress, do not look yet".

### 6.3 Any timeline event counts as "activity"

We do not try to weight a one-word "lgtm" comment differently from a
five-paragraph code review. We cannot — the metadata isn't there. The
`min_cluster` floor is our compromise: every event credits the same minimum
duration, no more.

This means a sneaky "ping?" comment can inflate active time. In aggregate
across many PRs this averages out; for any single PR, treat the per-PR FE
as a directional indicator, not a precise measurement.

### 6.4 Author and reviewer activity are not distinguished

A review submission counts as activity the same as a commit. From a flow
perspective this is correct (the system is doing work), but if you want to
distinguish "author is blocked" from "reviewer is the bottleneck" you need
to separate the two and this tool does not.

### 6.5 The window filter uses merge date

`merged:START..STOP` in the GitHub search query. We measure PRs **merged
in the window**, regardless of when they were opened. A PR opened months
ago and merged this week counts as this week's flow.

This matches the "throughput this week" intuition. If you want
"PRs-still-in-flight" rather than "PRs-completed", you need a different
query.

### 6.6 Timestamps are taken at face value

GitHub timestamps are server time at the moment of the event. We assume
they are accurate. Force-pushes that rewrite commit dates can in principle
distort `committedDate`; we use it anyway because it is the best signal
available.

### 6.7 Timeline pagination is bounded

We fetch up to 100 timeline events per PR. PRs with more than 100 events
will have their tail truncated, slightly under-counting activity (and thus
inflating wait time). For typical week-long windows this rarely binds. If
you have a long-running PR with hundreds of comments, the cycle time is
already enormous and the FE is already close to zero; the truncation moves
it from "close to zero" to "very close to zero" without changing the
conclusion.

## 7. What you should and should not do with these numbers

**Useful:**
- Track the trend over weeks. Is portfolio FE going up, down, or flat?
- Look at where the wait time is concentrated. Across PRs in a window, is
  it mostly pre-first-review? Mostly post-approval? That points at the
  queue.
- Find the long-tail PRs that dominate `total_cycle` and ask why.

**Not useful — and harmful if you try:**
- Per-engineer flow efficiency. Individual numbers reflect the system they
  are working in, not their effort. Measuring them rewards gaming the
  metric and punishes the engineers stuck with the messiest queues.
- Setting a target ("we will hit 25% FE next quarter"). Vacanti is explicit
  about this: targets cause people to inflate active time by reclassifying
  wait states. The metric only works as a diagnostic.
- Comparing across teams or repos. Different conventions, different review
  cultures, different draft-PR patterns make raw comparisons meaningless.
  Compare a team to its own past.

## 8. Tuning knobs

The defaults are reasonable starting points, not the right answer for every
team:

| Flag                       | Default | What it controls                                  |
| -------------------------- | ------- | ------------------------------------------------- |
| `--gap-hours`              | 4.0     | Inactivity threshold between activity clusters    |
| `--min-cluster-minutes`    | 30.0    | Minimum credit per cluster                        |

Tighten the gap and shrink the floor when you want a stricter measurement
("only honest minutes count"). Loosen them when sparse events feel like
they should anchor more time.

The right answer is to **pick one set of values and stick with them**. A
moving baseline makes trend analysis impossible.

## 9. Re-recording the test fixture

The recorded GraphQL response in `tests/fixtures/cache/` pins the schema
shape we expect. If GitHub changes the response shape, or you change
`PR_SEARCH_QUERY` in `src/flowmetrics/github.py`, the cache key changes
and the fixture test fails with `CacheMiss`. Re-record with:

```
uv run flow efficiency week \
    --repo astral-sh/uv \
    --start 2026-05-04 --stop 2026-05-10 \
    --cache-dir tests/fixtures/cache
```

Commit the new cache file. The fixture test asserts only structural
invariants (PR count > 0, FE in (0, 1], at least one fast PR and one slow
PR), so refreshes are safe — but inspect the new file before committing.
