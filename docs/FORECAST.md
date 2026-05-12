# Monte Carlo forecasting

This document explains both forecast scenarios this tool implements, exactly
how each one is computed from GitHub data, the assumptions baked in, and
how to read the output. It follows Vacanti's *When Will It Be Done?*
framing for both scenarios.

## 1. The two questions

There are two questions a delivery team typically asks. They use the same
historical data and the same simulator; they differ in what is held fixed
and what is forecast, and in how percentiles are read.

| Scenario          | You hold fixed... | You forecast... | Axis units    | Percentiles read |
| ----------------- | ----------------- | --------------- | ------------- | ---------------- |
| **When-done**     | N items | a completion date | dates       | forward          |
| **How-many**      | a target date     | items completed | items         | backward         |

If you remember nothing else: **dates use forward percentiles, items use
backward percentiles**. The next two sections explain why.

## 2. The simulator

Both scenarios use the same engine. The empirical training data is a list
of historical daily throughput samples — one count per day in the training
window, including zero-merge days. The simulator draws from this list with
replacement.

### 2.1 When-done

> "We have 50 items to deliver. When will it be done?"

```
For each of `runs` simulations:
    current_date := start_date
    remaining := items
    Repeat:
        sample := random.choice(daily_samples)
        remaining := remaining - sample
        if remaining <= 0:
            record current_date as the completion date for this run
            stop
        current_date := current_date + 1 day
```

After `runs` simulations you have `runs` completion dates. Tally them into
a Results Histogram with **dates on the x-axis and frequency on the y-axis**.

Vacanti's convergence rule of thumb: 1,000 runs gives the shape, 10,000
runs stabilises it. The default is 10,000.

### 2.2 How-many

> "We need delivery by 2026-05-25. How many items can we promise?"

```
For each of `runs` simulations:
    total := 0
    For each day from start_date to target_date (inclusive):
        sample := random.choice(daily_samples)
        total := total + sample
    record total as the items-completed outcome for this run
```

After `runs` simulations you have `runs` integer item counts. Tally them
into a Results Histogram with **items on the x-axis and frequency on the
y-axis**, ordered low-to-high left-to-right.

## 3. The Results Histogram

Both scenarios produce a Results Histogram with the same shape:

- The x-axis is the outcome (date or item count).
- The y-axis is the number of simulation runs that produced that outcome.

Both distributions are typically right-skewed: a long tail of "things went
slowly" outcomes. The mode (peak) is near the average of historical
throughput; the tail represents draws of consecutive bad days.

## 4. Why percentile direction flips

The whole reason both scenarios coexist is that the practical meaning of
"confidence" runs in opposite directions on the two axes.

### 4.1 When-done: forward

"85% confidence we will be done by date X" means: 85% of simulations
finished on or before X. As confidence rises, X moves **later**.

```
85th-percentile date = smallest X such that P(completion <= X) >= 85%
```

Why this direction: pushing the date later is more conservative because
more simulation runs have completed by a later date.

### 4.2 How-many: backward

"85% confidence we will deliver at least N items" means: 85% of simulations
delivered N or more items. As confidence rises, N moves **lower**.

```
85th-percentile items = largest N such that P(items_delivered >= N) >= 85%
```

Why this direction: promising fewer items is more conservative because
more simulation runs cleared the lower bar.

This is the trickiest part of Vacanti's framework and the single most
common mistake when reading the output. The CLI prints both directions
clearly so there is no ambiguity:

- when-done output says **"by what date will all items be done?"**
- how-many output says **"minimum items we can commit to"**.

## 5. Where the GitHub data feeds in

The training samples are real PR-merge counts from GitHub. Specifically:

1. Pick a training window. By default this is the **30 calendar days
   ending today** — Vacanti's recommended ~30 days of recent data.
2. Search GitHub for PRs in that repo merged in the window
   (`repo:X is:pr is:merged merged:START..END`).
3. Group by merge date in UTC.
4. Produce one integer per day in the window: the count of merges that
   day. **Zero-merge days are included.**

The samples list is what the simulator draws from. A 30-day training
window produces a 30-element list of integers (one per day). The
distribution of those integers — including all the zero days — is treated
as the empirical distribution of "what one day looks like" for this team
in this regime.

### 5.1 Why zero-merge days are included

They are real observations. Excluding them would systematically overstate
throughput (you would only ever draw "good days"). If 3 of 30 days had no
merges, that 10% chance of a zero-merge day appears in the simulator and
shows up correctly in the tail of the forecast distribution.

### 5.2 What we are using as a proxy for "throughput"

We are using **merged PRs per day** as the throughput unit. The implicit
assumption is that one merged PR = one delivered "item of work". For most
teams that is good enough for forecasting. Cases where it breaks down:

- You batch many tiny changes into single merge commits (overstates work
  per item).
- You split one logical feature across many PRs (understates work per
  item).
- Your team's PR granularity has changed during the training window (the
  past does not represent the present).

The metric is most useful when PR size is roughly stable across the
training window and the forecast window.

## 6. Assumptions, in plain language

These are the assumptions baked into the simulator. They are defensible
but not the only choices. If your situation violates them badly, the
forecast will not survive contact with reality.

### 6.1 The future will look like the recent past

This is the foundational assumption of any Monte Carlo throughput
forecast. The simulator draws from past samples; it has no other signal.
If a major regime change is imminent (a re-org, a new launch, the holiday
period), the forecast does not know about it and will be wrong.

The recommended response is to **forecast both with and without the
expected change**, and treat the gap between the two as a budget for
uncertainty rather than tweaking the model.

### 6.2 Daily samples are independent and identically distributed

The simulator draws each day independently and gives every historical day
equal probability. That ignores:

- **Autocorrelation**: bad days often cluster (Christmas, sprint-planning
  weeks, on-call rotations). The simulator does not capture these
  clusters; it tends to under-estimate variance.
- **Trend**: if throughput is rising or falling within the training
  window, the simulator treats it as random scatter around a single mean.
  Use a shorter training window if you suspect a trend.
- **Weekday effects**: Mondays and Saturdays are different. The simulator
  is calendar-blind. If your weekend throughput is near-zero, those days
  are still represented in the training samples, so the average is pulled
  down correctly — but the simulator might assign weekend-style
  throughput to a Tuesday, which is wrong in detail. For 1-4 week
  forecasts the effect is usually small.

### 6.3 PR merges are a complete record of completed work

Closed-without-merge PRs are not counted as completed. Direct pushes to
the main branch (rare in most teams, common in some) are invisible.
Squashed-merge PRs count as one item regardless of how many commits they
contain.

### 6.4 30 days is "enough but not too much"

Default training window: 30 calendar days. Vacanti's reasoning is
twofold:

- **Long enough**: roughly four weeks captures within-week variance and a
  few rare events.
- **Short enough**: regime changes (re-orgs, team changes, the launch
  pattern shifting) drift in over months, so 30 days mostly reflects the
  current regime.

If your team's pattern has been very stable, longer windows give a
smoother distribution. If you have an obvious recent change in pace, use
a shorter window starting after the change with
`--history-start YYYY-MM-DD`.

### 6.5 Calendar days, not working days

The window is 30 calendar days. Weekends and holidays are real low-
throughput days; including them is more honest than pretending only
weekdays exist.

### 6.6 GitHub timestamps are accurate to the day

We bucket by the date portion of `mergedAt`. We do not adjust for time
zones. A PR merged at 23:55 UTC and one merged at 00:05 UTC are placed in
different days. For most teams this is acceptable noise; if your team is
entirely in one time zone, the bucketing should ideally be that zone, not
UTC. This tool currently does not support time-zone offsets — open an
issue if you need it.

### 6.7 Search returns at most ~1000 results

The GitHub search API caps results at 1,000. A 30-day window with >1,000
merges (very busy repos) will silently truncate, **under-counting
throughput and inflating zero-merge days**. If you suspect this matters,
narrow the window or paginate explicitly. For most teams this never
binds.

## 7. How to read the output

### 7.1 When-done

```
Confidence — by what date will all items be done?
  50% confidence:   2026-05-19
  70% confidence:   2026-05-21
  85% confidence:   2026-05-23
  95% confidence:   2026-05-25
```

How to use it:

- **50%** is the median outcome. Half the simulations finished on or
  before this date. **Do not commit to this.** You are equally likely to
  be late as on time.
- **85%** is a usual default for external commitments. There is a 1-in-6
  chance you miss this date in the simulator.
- **95%** is for high-stakes commitments. The remaining 5% is the tail
  risk; it is not zero.

### 7.2 How-many

```
Confidence — minimum items we can commit to by the target date:
  50% confidence:   89 items
  70% confidence:   76 items
  85% confidence:   64 items
  95% confidence:   51 items
```

How to use it:

- **50% confidence: 89 items** means: half the simulations delivered 89
  or more. You are equally likely to over-deliver or under-deliver. Do
  not commit to 89.
- **85% confidence: 64 items** means: 85% of simulations delivered 64 or
  more. This is a defensible commitment number.
- **95% confidence: 51 items** is what you would tell an external
  stakeholder who needs certainty. You are leaving items on the table to
  buy that certainty.

The "more confidence = fewer items" trade-off is the entire point. If
you find yourself committing to the 50%-confidence number, you are
committing to the median; you will miss it about half the time.

## 8. Tuning

| Flag                  | Default | Purpose                                                |
| --------------------- | ------- | ------------------------------------------------------ |
| `--history-start`     | 29 days before `--history-end` | First day of training window         |
| `--history-end`       | yesterday-UTC | End of training window (today's data is partial) |
| `--start-date`        | today   | First day of forecast work                             |
| `--runs`              | 10000   | Monte Carlo runs (1k for shape, 10k stabilises)        |
| `--seed`              | random  | Optional RNG seed for reproducibility                  |
| `--cache-dir`         | .cache/github | Where GraphQL responses are cached            |
| `--offline / --online`| online  | Offline reads cache only; online hits GitHub on miss   |

### 8.1 Reproducibility

Pass `--seed N` for deterministic output. This is important when you want
to compare runs ("does adding two more engineers move the 85% number?")
because the noise floor between two random 10k-run simulations is small
but non-zero.

### 8.2 The cache makes reruns free

The training-window GraphQL response is cached by query+variables hash.
Rerunning the same `--history-start --history-end` against the same repo
makes zero requests — the data is already on disk. You can iterate on
`--items`, `--target-date`, `--runs`, and `--seed` essentially for free.

## 9. What this is not

- **Not a guarantee.** The output is a distribution of possible futures
  conditioned on the recent past resembling the near future. Treat the
  numbers as ranges, not promises.
- **Not a substitute for talking to people.** If the team knows a holiday
  block is coming, that information overrides the model. The model is a
  starting point for the conversation.
- **Not an estimate of effort.** Throughput-based forecasting deliberately
  ignores "how hard each item is". It assumes the next 50 items will, on
  average, look like the last few weeks of merged work. If item size is
  changing dramatically (e.g. you are about to start a large architectural
  rewrite), the forecast will be too optimistic.
- **Not for per-engineer use.** Same warning as the flowmetrics
  metric: a system-level forecast does not say anything about any single
  contributor's velocity. Using it that way is harmful.

## 10. Example workflows

### 10.1 "When can I commit to this 50-item commitment?"

```
uv run flow forecast when-done \
    --repo astral-sh/uv --items 50
```

Reads the last 30 calendar days of merges, runs 10,000 Monte Carlo
simulations, prints the 50/70/85/95 percentile completion dates and an
ASCII histogram.

### 10.2 "How many can we commit to by quarter-end?"

```
uv run flow forecast how-many \
    --repo astral-sh/uv --target-date 2026-06-30
```

Same training window, same simulator, but the histogram is items-on-x and
percentiles read backward. Print the 50/70/85/95 minimum-items
commitments.

### 10.3 "What about a shorter history because we just reorganised?"

```
uv run flow forecast when-done \
    --repo astral-sh/uv --items 50 --history-start 2026-04-28
    # ↑ a closer start date narrows the training window
```

Two weeks of history will be noisier but will not include the pre-reorg
regime.

### 10.4 "Compare two scenarios reproducibly"

```
uv run flow forecast when-done \
    --repo astral-sh/uv --items 50 --seed 42
uv run flow forecast when-done \
    --repo astral-sh/uv --items 75 --seed 42
```

Same seed; the only difference in the output is the effect of changing
the item count. The simulator is deterministic given a seed.
