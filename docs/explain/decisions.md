---
title: Architectural decisions and known constraints
---

# Architectural decisions and known constraints

> **Diátaxis: Explanation.** Each section names a deliberate
> trade-off in how `flowmetrics` talks to GitHub: the constraint,
> the decision we made, the cost we accepted, and when the decision
> should be revisited.

> **Note.** Some entries below refer to a `flow efficiency` command
> that has since been removed (the active/wait heuristic wasn't a
> strong-enough signal). The rationale is kept here because the same
> query, cache, and pagination machinery now serves `flow metric *`
> and `flow forecast` — the trade-offs are unchanged; only the
> command name moved.

## 1. One shared GraphQL query for metric extraction and forecast

**What we do.** A single query (`PR_SEARCH_QUERY` in
`src/flowmetrics/github.py`) drives both the metric-extraction path
(`flow metric *`) and `flow forecast`. The query fetches PR metadata
*and* the first 100 timeline events per PR inline, in a single round
trip per page.

**What it costs.** The forecast path only reads `mergedAt`. The 100
timeline events per PR are fetched and discarded — about a 99%
over-fetch for the forecast scenario. On the `astral-sh/uv` 30-day
training window that means ~4 requests carrying ~20,000 timeline event
nodes we throw away.

**Why we accepted it.**

1. The disk cache (`src/flowmetrics/cache.py`) keys responses by
   sha256 of (query, variables). The first run pays the over-fetch
   cost once; every rerun for the same window is local-only and free.
   Forecasting iterates on item count, target date, runs count, and
   seed — none of which change the cache key. So in normal use the
   over-fetch happens once per window, ever.
2. Maintaining one query is cheaper than maintaining two when the
   incremental network cost is paid only once.
3. The GraphQL "cost" budget for a 30-day, ~200-merge window stays
   well under GitHub's 5,000-points-per-hour rate limit.

**When to revisit.** Add a slim forecast-only query if any of the
following becomes true:

- You point this tool at a monorepo where one window approaches the
  GraphQL points limit.
- You start running uncached against many repos in a short window
  (e.g. a fleet dashboard).
- Forecast becomes the dominant use case and metric extraction is
  rarely invoked.

The change would be ~50 lines: a second query constant + a second
fetcher that returns only `(number, mergedAt)`. The forecast service
function would call the slim fetcher; metric extraction would keep
the full one.

## 2. Page size is 100 — GitHub's max

**What we do.** `fetch_prs_merged_in_window` defaults to `page_size=100`,
the largest value the GitHub GraphQL `search` connection accepts.

**What it costs.** A page of 100 PRs × up to 100 timeline events each
is ~10,000 nodes per response. On slow networks the larger response
payload takes longer to download than the equivalent two 50-PR pages,
but in practice this is dominated by GitHub's response-generation
time, which is per-request rather than per-node.

**Why we accepted it.** Halves the request count for any window over
50 PRs. The cost difference is small in absolute terms but it's a
free win — there's no quality trade-off, only a payload-size trade-
off that almost never binds.

**When to revisit.** If you're seeing slow first-run responses on
small networks or hitting GraphQL points limits, drop back to 50.
Anywhere `cache_dir` is shared across many users, a smaller page
size also produces more granular cache entries which could be useful
for cache-sharing tools. Neither is currently relevant.

## 3. Timeline events are truncated at 100 per PR

**What we do.** `timelineItems(first: 100)` in `PR_SEARCH_QUERY`. No
inner pagination.

**What it costs.** PRs with more than 100 activity events have their
tail dropped. For the label-driven CFD / Aging path, this can drop
late label changes; the chart caption surfaces the per-PR truncation
count so the reader can see when it happens.

**Why we accepted it.** The vast majority of merged PRs in a typical
weekly window have fewer than 100 events. Forecast doesn't read
timelines at all, so this is invisible to that path.

**When to revisit.** If you find specific PRs whose under-counted
label history materially changes the chart, add an inner pagination
loop in `fetch_prs_merged_in_window` keyed off
`timelineItems.pageInfo.hasNextPage`. Expect a meaningful complexity
bump.

## 4. GitHub search caps results at 1,000

**What we do.** Use the `search` connection. We do not chunk windows
ourselves.

**What it costs.** A window with more than 1,000 merged PRs has its
tail silently dropped. Throughput averages will be under-counted,
metric-extraction numbers will exclude the dropped PRs entirely.

**Why we accepted it.** For 30-day windows on most repos, including
`astral-sh/uv` at ~6 merges/day, this is far from binding. The cap is
a `search`-API limitation, not something we can work around in one
query.

**When to revisit.** If your target repo merges more than 33 PRs/day
on average (1000 ÷ 30), the default 30-day forecast window will
truncate. Workaround: tighten the window with a closer `--history-start`,
or chunk the window into multiple sub-window queries and union the results.
The latter is not implemented; it would need a service-level wrapper
that walks daily/weekly slices and concatenates.

## 5. The cache is unconditional and never expires

**What we do.** `FileCache` keys by sha256(query + variables) and
stores responses as JSON files. There is no TTL, no LRU eviction, no
ETag/If-None-Match.

**What it costs.** A cache file for a *past* window will never be
refreshed. If GitHub retroactively changes data (rare but possible:
spam-account purges, deleted comments), the cache holds the stale
version. A cache file for *today's* window keeps the data from the
moment you first ran it; PRs merged since then are not reflected
until you delete the file.

**Why we accepted it.**

- For historical windows (>1 day old), GitHub data is effectively
  immutable. A stale cache is the same as fresh data.
- For today's window the user is expected to re-run with intent;
  blowing away `.cache/github/<hash>.json` is one `rm` away.
- TTLs encourage cache-mismatched accidents — code says "fresh
  enough" but the user disagrees. Explicit invalidation is simpler.
- Tests commit cache files as fixtures, and TTL would invalidate
  fixtures.

**When to revisit.** If you wire `flowmetrics` into a dashboard that
needs same-day freshness, add a `--max-age` flag that ignores cache
entries older than N minutes for windows that include `today`.

## 6. Credentials come from `gh auth token`, not a managed config

**What we do.** `resolve_token()` in `src/flowmetrics/github.py`
checks `$GITHUB_TOKEN`, then falls back to `gh auth token`. Most
users have already run `gh auth login`, so no separate token
management is required.

**What it costs.** Requires the `gh` CLI on PATH for users who
haven't set `$GITHUB_TOKEN`. A subprocess call per first uncached
request (cheap; happens once per process).

**Why we accepted it.** It removes the most common friction point
("where do I put my token, what scope does it need") for engineers
who already use `gh`. The fallback to `$GITHUB_TOKEN` keeps it CI-
friendly.

**When to revisit.** If we add OAuth-app or GitHub-App auth (for
multi-tenant deployments), the auth surface needs to grow.

## 7. We never paginate `search.search` past what the user asked for

**What we do.** `fetch_prs_merged_in_window` paginates the outer
search until `pageInfo.hasNextPage` is false, but is bounded only by
GitHub's 1,000-result cap.

**What it costs.** A query whose result set is in the hundreds will
make multiple sequential round-trips. We do not parallelise.

**Why we accepted it.** Sequential pagination is the only safe
default — GitHub rate-limits hard, and parallel requests on the same
search are an easy way to get throttled. The cache makes the second-
run cost zero anyway.

**When to revisit.** Only relevant if you remove the cache or run
many fresh windows concurrently.

## 8. We do not retry transient failures

**What we do.** Single attempt per request. `response.raise_for_status()`
propagates network or HTTP errors immediately.

**What it costs.** A flaky connection or a transient 502 from GitHub
fails the whole run.

**Why we accepted it.** Retries are a layer best added by the user
(via shell loops, Make targets, or a wrapping script) since the
right behaviour depends on context. For a tool that's mostly cache-
hits, the failure mode is rare enough that explicit retry logic
isn't worth the maintenance cost.

**When to revisit.** If you find yourself manually re-running on
transient failures more than once or twice per week, add an
`httpx.HTTPTransport(retries=N)` wrapper at construction time.

---

## 9. WIP-tracking source is per-system, not generalized

**The question.** The CFD and Aging charts assume named workflow
states. Where do those come from? In Jira, status transitions live in
each issue's changelog. In GitHub, there is no native multi-state
workflow on PRs — tools like [gh-velocity] reconstruct WIP from issue
*labels* (with per-project configuration). The two models are not
interchangeable; either covers some teams' reality and not others'.

**What we do.**

- **Jira issues**: native workflow. Each issue's `changelog.histories`
  provides status transitions; CFD and Aging consume `status_intervals`
  directly. This is the canonical kanban use case and our reference
  surface for those charts.

- **GitHub PRs (Aging)**: a deliberately simple four-state review
  lifecycle derived from GitHub's own native fields — `isDraft` and
  `reviewDecision`:

  | Phase             | Condition                                   |
  | ----------------- | ------------------------------------------- |
  | Draft             | `isDraft == true`                           |
  | Awaiting Review   | `reviewDecision in (null, REVIEW_REQUIRED)` |
  | Changes Requested | `reviewDecision == "CHANGES_REQUESTED"`     |
  | Approved          | `reviewDecision == "APPROVED"`              |

  Age = today − `createdAt`. This is not a substitute for full WIP
  tracking — it's a review-cycle lens. Useful for spotting stalled PRs
  in the review queue; not useful for tracking development phases that
  happen *before* a PR is opened.

- **GitHub PRs (label-driven CFD + Aging)**: opt-in via the YAML's
  `wip_labels` field. The caller names which labels constitute WIP;
  the materializer in `src/flowmetrics/github_labels.py` walks
  `LabeledEvent` / `UnlabeledEvent` timeline events into
  `status_intervals` consumed by the same CFD/Aging code as Jira.
  Design notes: [GitHub label-driven CFD and Aging](github-labels.md).

- **GitHub issues + labels**: not supported. That's [gh-velocity]'s
  domain — they handle the per-repo label-to-state configuration
  honestly (it must be configured, because conventions vary). We point
  users there from the `flow metric aging --help`.

**What we accept.**

1. GitHub Aging surfaces review-cycle phase only, unless `wip_labels`
   is configured. Teams that track real development phases via labels
   should set `wip_labels`; teams without label conventions get the
   review-cycle view.
2. CFD on GitHub PRs is degenerate (arrivals + departures only) absent
   `wip_labels`. With `wip_labels` it gains one band per named label.
3. GitHub issues remain out of scope (see §10 below).

**When to revisit.**

- If a user wants CFD/Aging on GitHub issues, the path is a new
  `GitHubIssuesSource` adapter that takes an explicit label-to-state
  mapping per repo (config file or repeated `--label-state` flags).
  It should sit alongside the Jira source as a peer, not replace
  anything.

[gh-velocity]: https://gh-velocity.org/guides/cycle-time-setup/

---

## 10. For GitHub, only pull requests count as work — issues are invisible

**The assumption.** Every GitHub source in flowmetrics treats the
*pull request* as the unit of work. Issues are never queried. If a team
tracks WIP on issues (with or without labels), opens issues that don't
result in a PR, or closes issues by hand, none of that flows into any
report — forecast, CFD, or Aging.

**What it means in practice.**

- Cycle time, throughput, and percentiles are PR-only series.
- A team that fixes bugs via direct pushes or settings changes has its
  output silently dropped.
- The Aging chart, on GitHub, surfaces PR review state (Draft →
  Awaiting Review → Changes Requested → Approved) or — with
  `wip_labels` — PR label progression. It is *not* a view of issue
  progress.
- "Items" in the forecast vocabulary means "PRs" for GitHub sources
  and "issues" for Jira sources. The unit is consistent within a
  report but the noun changes by backend.

**Why we accept it.**

PRs have unambiguous start (`createdAt`) and end (`mergedAt`)
timestamps via the GraphQL API, plus a rich timeline of events. Issues,
by contrast, have no native concept of "started" — that signal lives
in per-team conventions (labels, project boards, milestones, custom
fields). Trying to be right across teams means making per-team
decisions configurable, which is exactly what [gh-velocity] is for. We
deliberately do not duplicate that.

**When to revisit.**

If a user with a labelled-issue workflow opens an issue here, the
remediation is a new `GitHubIssuesSource` with explicit per-repo
label-to-state mapping passed in config. It would sit alongside the
existing PR source as a separate source, not replace it. The two
answer different questions; both should be available.

[gh-velocity]: https://gh-velocity.org/guides/cycle-time-setup/

---

## Summary: the cache is doing the heavy lifting

Every decision above leans on one assumption: **the disk cache
absorbs the cost of over-fetching, smaller pages, single-attempt
network calls, and inflexible queries**. First run is a few seconds
of GraphQL traffic; every run after that is local file reads. As
long as users care about reproducibility and reruns (which they do
when iterating on forecast parameters), this is the right shape.

If the cache assumption breaks — e.g. fleet-wide dashboards, hot-
path automation, fresh-every-time CI — most of these decisions need
to be revisited together, not individually.
