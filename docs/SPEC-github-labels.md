# Spec: GitHub label-driven CFD and Aging

Status: Draft v3 — needs human review before any code changes.
Owner: dvhthomas
Date: 2026-05-14
Related: [DECISIONS.md #9 and #10](DECISIONS.md), [METRICS.md](METRICS.md)

## 1. Objective

Make GitHub a first-class source for `flow cfd` and `flow aging` by
**letting the caller name the labels that constitute WIP**. Everything
else is "not WIP" by exclusion. The user supplies the WIP-label set
per invocation; flowmetrics applies it.

The GitHub Timeline API exposes `LabeledEvent` and `UnlabeledEvent`
with ISO-8601 timestamps. A sequence of those events on a PR, filtered
to the user's WIP labels, materializes the same
`[StatusInterval(start, end, status)]` series the rest of the codebase
already consumes from Jira changelogs.

### What this unlocks

- **CFD** for GitHub PRs goes from two-state degenerate (arrivals /
  departures) to a real Vacanti-style stack: one band per WIP label,
  in the order the caller named them, plus implicit arrivals
  (pre-WIP) and departures (no WIP label applied).
- **Aging** for GitHub PRs gains a label-driven mode that surfaces
  the team's *actual* workflow, alongside the existing review-cycle
  mode that uses `isDraft` + `reviewDecision`.

### Inputs — and only these inputs

Per invocation, on the CLI:

```
--wip-labels "label_a,label_b,label_c"
```

That's the entire user-facing surface. The list is **ordered, with
most progress on the right**. Order drives column/band placement and
breaks ties when a PR concurrently holds more than one WIP label —
**most progress wins** (rightmost in the list).

Re-defining "WIP" is a one-flag, one-run change. The caller decides
per invocation. There is no persistent config, no in-repo file, no
remembered state between runs. Want to see what happens if `in-review`
isn't counted as WIP? Run the command again without it. Want to add
`blocked` to your WIP set just for one chart? Add it to the flag.

Flowmetrics makes no other assumption about labels. It does not read
any in-repo config (`.github/labels.yml`, `project-label-sync.yml`,
GitHub Projects v2, etc.). It does not assume the target repo has any
particular convention, or any convention at all. Target repos may
have arbitrary label sets — the caller picks which subset counts as
WIP for the chart they want right now.

### Out of scope

- **Per-repo config files.** Per-invocation only.
- **GitHub issues.** PRs only. (See `DECISIONS.md` #10.)
- **Auto-detection of WIP labels.** The caller names them; we don't
  guess.
- **Multiple WIP classes** (pre-wip / wip / done as separate
  categories). One class only: WIP, by name. Not-WIP, by exclusion.
- **Lead-time vs cycle-time markers.** Out of scope here; that's an
  efficiency-mode concern.
- **Replacing the existing review-cycle Aging.** Label mode is opt-in
  via `--wip-labels`; absent the flag, behavior is unchanged.

## 2. Success criteria

1. `flow aging --repo OWNER/NAME --wip-labels "a,b,c"` renders an
   Aging chart with one column per named label, in the order given,
   plotting only PRs whose **current** label set intersects
   `{a, b, c}`. PRs with no WIP labels currently applied are excluded
   from Aging.
2. `flow cfd --repo OWNER/NAME --wip-labels "a,b,c"` renders a CFD
   with one band per named label, ordered as given, between an
   implicit pre-WIP arrivals band and a departures band. Vertical
   distance across the named bands at any date equals WIP count at
   that date.
3. The label-driven mode produces a per-PR `status_intervals` list
   that round-trips through the existing `compute_pr_flow` and
   `compute_aging` functions with no source-specific branching
   inside `cfd.py` or `aging.py`.
4. When a PR concurrently holds more than one label from the WIP
   list, its current/historical column is the **most-progress match**
   — i.e. the rightmost in the user-supplied order. Deterministic;
   documented in chart captions.
5. A PR with no WIP label ever applied appears in the pre-WIP band
   in the CFD for its full visible lifetime, and is absent from
   Aging.
6. A merged PR that still carries a WIP label at merge time stays
   in that column until the label is removed (or never leaves it).
   This is intentional: it surfaces merged-but-not-shipped work as
   aging WIP, which is the symptom worth seeing.
7. `flow aging --repo astral-sh/uv` (no `--wip-labels`) continues to
   produce the same review-cycle output as today. No regression to
   the zero-config path.

## 3. Tech stack

No new runtime dependencies.

- **Language:** Python 3.11+, managed by `uv`.
- **HTTP / GraphQL:** existing `httpx` client.
- **Cache:** existing `FileCache`. A new query string yields a new
  cache key automatically.
- **CLI:** existing `click`/`typer`-style entrypoint.
- **Renderers:** existing HTML/text/JSON renderers. They already
  accept arbitrary `workflow` tuples.

## 4. Commands

```
Build:  uv pip install -e .
Test:   uv run pytest
Lint:   uv run ruff check .
Format: uv run ruff format .
Type:   uv run mypy src/flowmetrics
Run:    uv run flow <subcommand> ...
```

New CLI surface — additive:

```
uv run flow aging --repo dvhthomas/kno \
    --wip-labels "shaping,in-progress,in-review"

uv run flow cfd --repo dvhthomas/kno \
    --wip-labels "shaping,in-progress,in-review" \
    --window 30d
```

The flag is identical on both commands. Parsing is shared: split on
commas, strip whitespace, preserve order, reject empties / dupes /
overlaps with a clear error before any network call.

## 5. Project structure

```
src/flowmetrics/
  github.py            ← extend: new query + new fetcher for labels
  github_labels.py     ← NEW: pure functions
                          - parse --wip-labels
                          - materialize StatusIntervals from LabelEvents
                          - tie-break concurrent labels
  cli.py               ← extend: --wip-labels on `cfd` and `aging`
  cfd.py               ← unchanged — consumes status_intervals
  aging.py             ← small extension: drop items whose current
                          interval status is the implicit pre-WIP /
                          departures bucket
  compute.py           ← unchanged — StatusInterval is the contract

tests/
  test_github_labels.py        ← materializer unit tests (TDD this)
  test_github_label_fetch.py   ← cached-fixture integration test
  test_cli_wip_labels.py       ← argument parsing + wiring
  fixtures/
    kno_pr_labels.json         ← recorded GraphQL response

docs/
  SPEC-github-labels.md        ← this file
  DECISIONS.md                 ← back-link from §9 and §10
  HOWTO.md                     ← add label-mode example after ship
```

## 6. Data model

The codebase's contract is `WorkItem.status_intervals:
list[StatusInterval]`. This spec adds a new producer; consumers are
effectively unchanged.

### Inputs (parsed from `--wip-labels`)

```python
@dataclass(frozen=True)
class WipLabels:
    """Ordered, deduped list of label names the caller has declared
    as WIP. Position in the tuple drives column/band order and
    tie-breaks concurrent labels (rightmost wins)."""
    ordered: tuple[str, ...]

    def index_of(self, label: str) -> int | None: ...
    def contains(self, label: str) -> bool: ...
```

### Inputs (from the GraphQL response)

```python
@dataclass(frozen=True)
class LabelEvent:
    at: datetime
    label: str
    kind: Literal["added", "removed"]
```

### Constants

```python
PRE_WIP_STATUS   = "Pre-WIP"     # arrivals band on CFD; excluded from Aging
DEPARTED_STATUS  = "Departed"    # merged-and-cleaned; excluded from Aging
ABANDONED_STATUS = "Abandoned"   # closed-not-merged; excluded from Aging
```

All three names are caption-only; the CLI doesn't expose them. If
real users want to rename them later, add knobs then. The semantic
difference between `DEPARTED_STATUS` and `ABANDONED_STATUS` is laid
out in §8b.

### Materialization

The function now takes a richer event stream — both label events and
lifecycle events (merge / close / reopen). Lifecycle events drive the
state machine in §8b; label events drive the `active` set.

```python
@dataclass(frozen=True)
class LabelEvent:
    at: datetime
    label: str
    kind: Literal["added", "removed"]

@dataclass(frozen=True)
class LifecycleEvent:
    at: datetime
    kind: Literal["merged", "closed", "reopened"]

def materialize_status_intervals(
    *,
    created_at: datetime,
    asof: datetime,
    label_events: Sequence[LabelEvent],
    lifecycle_events: Sequence[LifecycleEvent],
    wip: WipLabels,
) -> list[StatusInterval]:
    """Walk a merged timeline of label and lifecycle events and emit
    mutually-exclusive [start, end, status] intervals. Resolution
    follows §8b precisely:

    - `active` set is mutated by label events whose label is in `wip`.
      Non-WIP labels are filtered out before the walk.
    - Lifecycle state (`OPEN` → `CLOSED` → `MERGED` with reopen
      reverting `CLOSED` → `OPEN`) gates the status mapping.
    - Status mapping is the table in §8b: empty+OPEN → PRE_WIP,
      non-empty+OPEN → rightmost(active), *+CLOSED → ABANDONED,
      empty+MERGED → DEPARTED, non-empty+MERGED → rightmost(active).
    - A status change OR a lifecycle-state change emits a new interval.
    - Zero-length intervals are coalesced.
    - For an open PR, the final interval ends at `asof`.
    """
```

`closed_at` / `merged_at` from the PR snapshot are NOT passed to the
materializer — the lifecycle events from the timeline are
authoritative. This is the only way to handle reopens correctly
(snapshot `closedAt` would be the *latest* close, missing earlier
reopen cycles).

### Aging WIP filter

```python
_NON_WIP = {PRE_WIP_STATUS, DEPARTED_STATUS, ABANDONED_STATUS}

def is_aging_wip(item: WorkItem) -> bool:
    """True iff the item's current status is one of the user's WIP
    labels — i.e. NOT in `_NON_WIP`. Used by `flow aging` in label
    mode to drop rows that aren't currently WIP."""
```

This is the single touch point in `aging.py`. Existing code paths
remain.

## 7. GraphQL query

A new query, separate from `PR_SEARCH_QUERY`. Adding label events to
the existing query would invalidate cached forecast/efficiency
responses.

```graphql
query($q: String!, $first: Int!, $after: String) {
  search(query: $q, type: ISSUE, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    issueCount
    nodes {
      ... on PullRequest {
        number
        title
        createdAt
        mergedAt
        closedAt
        author { __typename login }
        labels(first: 50) { nodes { name } }
        timelineItems(
          first: 100,
          itemTypes: [
            LABELED_EVENT, UNLABELED_EVENT,
            MERGED_EVENT, CLOSED_EVENT, REOPENED_EVENT
          ]
        ) {
          pageInfo { hasNextPage }
          nodes {
            __typename
            ... on LabeledEvent   { createdAt label { name } }
            ... on UnlabeledEvent { createdAt label { name } }
            ... on MergedEvent    { createdAt }
            ... on ClosedEvent    { createdAt }
            ... on ReopenedEvent  { createdAt }
          }
        }
      }
    }
  }
  rateLimit { remaining limit resetAt cost }
}
```

Notes:

- `itemTypes` filter keeps payload small.
- `first: 100` truncation matches `DECISIONS.md` #3.
- `labels` snapshot is a cheap-skip for PRs that hold no WIP label
  right now AND in their history (rough heuristic; correctness still
  comes from the timeline walk).
- Two queries, not one. `PR_SEARCH_QUERY` is byte-for-byte unchanged
  to preserve cache stability.

## 8. Code style

Pure function; no I/O; keyword-only public args.

```python
def materialize_status_intervals(
    *,
    created_at: datetime,
    closed_at: datetime | None,
    asof: datetime,
    events: Sequence[LabelEvent],
    wip: WipLabels,
) -> list[StatusInterval]:
    relevant = sorted(
        (ev for ev in events if wip.contains(ev.label)),
        key=lambda e: e.at,
    )
    end = closed_at or asof
    if not relevant:
        return [StatusInterval(created_at, end, PRE_WIP_STATUS)]

    intervals: list[StatusInterval] = []
    active: set[str] = set()
    current_status = PRE_WIP_STATUS
    interval_start = created_at

    for ev in relevant:
        if ev.kind == "added":
            active.add(ev.label)
        else:
            active.discard(ev.label)
        new_status = _rightmost(active, wip) or PRE_WIP_STATUS
        if new_status != current_status and ev.at > interval_start:
            intervals.append(StatusInterval(interval_start, ev.at, current_status))
            interval_start = ev.at
            current_status = new_status

    # Termination: PRE_WIP at close becomes DEPARTED; an active WIP
    # label at close stays in its column (merged-but-not-shipped).
    final_status = DEPARTED_STATUS if current_status == PRE_WIP_STATUS else current_status
    intervals.append(StatusInterval(interval_start, end, final_status))
    return intervals
```

Conventions:

- `from __future__ import annotations` at the top of every new module.
- Frozen dataclasses for data carriers.
- One-liner comments only where the *why* is non-obvious (the
  termination rule above is one such place).
- No comments restating what a well-named function already does.

## 8b. Signal conflicts and deterministic resolution

The GitHub API exposes multiple overlapping signals of PR state, and
they routinely disagree. This section names the conflicts, fixes the
resolution rule, and lists what we surface to the user so signal
quality is visible — not hidden behind a clean-looking chart.

### The signals we see

| Signal                                | Shape                | Source                          |
| ------------------------------------- | -------------------- | ------------------------------- |
| `mergedAt` / `closedAt`               | Terminal timestamp   | PR snapshot                     |
| `MergedEvent` / `ClosedEvent`         | Timeline event       | Timeline                        |
| `ReopenedEvent`                       | Timeline event       | Timeline                        |
| `LabeledEvent` / `UnlabeledEvent`     | Timeline event       | Timeline                        |
| `isDraft` / `reviewDecision`          | Snapshot             | PR snapshot (review-cycle mode) |
| `ReadyForReviewEvent` / `ConvertToDraftEvent` | Timeline event | Timeline                |
| `labels` connection                   | Current snapshot     | PR snapshot                     |

In label mode, the materializer reads **timeline events only**:
`LabeledEvent`, `UnlabeledEvent`, `MergedEvent`, `ClosedEvent`,
`ReopenedEvent`. Snapshot fields and review-cycle events are not
consulted — that lens is already covered by the existing review-cycle
Aging.

### Resolution rule (the single source of truth)

For each PR, walk the merged timeline in strict chronological order
and maintain:

- `active`: the set of WIP labels currently applied.
- `state`: one of `OPEN` → `CLOSED` → `MERGED`. Transitions: a
  `ClosedEvent` moves OPEN→CLOSED; a `ReopenedEvent` moves CLOSED→OPEN;
  a `MergedEvent` moves any→MERGED (terminal; reopens after a merge
  are not surfaceable through GitHub anyway).
- `column`: the rightmost-in-`--wip-labels` member of `active`, or
  `PRE_WIP_STATUS` when `active` is empty.

The emitted interval at each transition is determined by the table
below. There are no other rules.

| (active WIP labels, lifecycle state) | Interval status               |
| ------------------------------------ | ----------------------------- |
| empty, OPEN                          | `PRE_WIP_STATUS`              |
| non-empty, OPEN                      | rightmost(active)             |
| empty, CLOSED-not-merged             | `ABANDONED_STATUS`            |
| non-empty, CLOSED-not-merged         | `ABANDONED_STATUS` *(see #2)* |
| empty, MERGED                        | `DEPARTED_STATUS`             |
| non-empty, MERGED                    | rightmost(active) *(see #1)*  |

Two rules that look surprising and are deliberate:

1. **Merged with a WIP label still on → stays in the WIP column.**
   This is the merged-but-not-shipped signal. The user wants to see
   it as aging WIP, not as departed. The Aging chart will surface
   these PRs; the CFD will show them as a band that hasn't dropped to
   Departed.

2. **Closed-not-merged is always `ABANDONED_STATUS`, regardless of
   labels.** This is the v4 fix for the closed-not-merged leak.
   GitHub's `closedAt` without `mergedAt` means the PR was discarded
   — labels on it are stale by definition. Forcing it to Abandoned at
   `closedAt` prevents abandoned PRs leaking into WIP forever. If the
   PR is later reopened (`ReopenedEvent`), it re-enters `active`-based
   resolution at the reopen timestamp.

### Three new implicit columns / bands

| Column              | Visible in CFD | Visible in Aging |
| ------------------- | -------------- | ---------------- |
| `PRE_WIP_STATUS`    | Yes (arrivals) | No               |
| `DEPARTED_STATUS`   | Yes (departures, merged-and-cleaned) | No |
| `ABANDONED_STATUS`  | Yes (departures, closed-not-merged) | No |

`DEPARTED_STATUS` and `ABANDONED_STATUS` are separate so the CFD can
distinguish "merged work shipped" from "work abandoned." Some teams
care; conflating them would lose information cheaply recovered.

### Conflict matrix — outcomes are now spec'd

| Conflict                                                   | Outcome                                                                                     |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| PR `mergedAt` with WIP label still applied                 | Stays in WIP column. Captioned: "merged-but-not-shipped: N PRs."                            |
| PR `closedAt`-not-merged with WIP label still applied      | Moves to `ABANDONED_STATUS` at `closedAt`. The label is ignored from that moment on.        |
| PR reopened (`ClosedEvent` then `ReopenedEvent`)           | Returns to label-driven resolution at the reopen timestamp. The closed gap appears in `ABANDONED_STATUS`. |
| Same-instant `add(a)` + `remove(b)`                        | Apply in GraphQL response order. Document that ordering is GitHub's, not ours.              |
| Label renamed mid-history (`in-progress` → `wip`)          | **User must list both names** in `--wip-labels`. We warn at startup if any name in `--wip-labels` is absent from both the current `labels(first: 50)` snapshot of any PR in the window AND every historical timeline event. |
| `--wip-labels` names a label not present in repo at all    | Warn at startup with `gh`-style "did you mean …" suggestion using the repo's label list.    |
| Bot-authored PR with no labels                             | Default behavior: visible in `PRE_WIP_STATUS` band. The chart caption will say "N of M PRs are bot-authored and carry no WIP label." Optional `--exclude-bots` (already exists in `flow efficiency`) drops them. |
| `isDraft: true` PR carrying a WIP label                    | Label wins; draft state ignored. Caption explicitly says "label mode does not consult `isDraft`." |
| Timeline >100 events (truncation)                          | The fetcher counts these per-PR and the chart caption says "N PRs had truncated timelines (tail dropped)." Materializer behavior is correct given truncated input. |
| Search cap of 1000                                         | Existing warning from `DECISIONS.md` #4 is reused.                                          |

### What we surface to the user (signal-quality contract)

Every label-mode chart and JSON envelope **must** include these
counts. This is non-negotiable — the whole point of being explicit
about resolution is that the user can see what we resolved.

```text
WIP labels (left → right, most progress wins):
  shaping → in-progress → in-review

Signal quality for window 2026-04-14 → 2026-05-14:
  PRs in window:                  124
  Carried ≥1 WIP label:           102  (82%)
  Merged-but-not-shipped:           7  (still carry a WIP label)
  Closed-not-merged (abandoned):    9
  Reopened at least once:           3
  Bot-authored (no WIP labels):    13
  Timelines truncated (>100 evt):   0
  Labels you named that don't
    appear anywhere in window:      0
```

In the JSON envelope under `summary.signal_quality`:

```json
{
  "wip_labels": ["shaping", "in-progress", "in-review"],
  "tiebreaker": "rightmost_wins",
  "counts": {
    "prs_in_window": 124,
    "with_any_wip_label": 102,
    "merged_not_shipped": 7,
    "abandoned": 9,
    "reopened": 3,
    "bot_no_wip": 13,
    "timeline_truncated": 0,
    "missing_wip_labels": []
  }
}
```

These numbers are *the contract* by which a downstream reader can
trust the chart. Without them the chart is a pretty lie.

### What the materializer will NOT do

- **Not consult `isDraft` / `reviewDecision`** in label mode.
  Surprises users who confuse the two Aging modes; caption it.
- **Not auto-resolve renamed labels.** GitHub's API keeps the
  historical name on past events. We do not heuristically alias.
- **Not infer state from `ReadyForReviewEvent` / `PullRequestReview`.**
  Those would create *another* shadow signal; we deliberately use
  one signal (labels) per chart.
- **Not collapse `DEPARTED_STATUS` and `ABANDONED_STATUS`.** Separate
  bands; user can read them.
- **Not drop PRs that hold no WIP labels.** They are visible in the
  Pre-WIP band — the size of that band is itself diagnostic.

## 9. Testing strategy

The materializer is a small, pure function with rich edge cases. TDD
it.

### Unit tests (`tests/test_github_labels.py`)

Each is a separate `pytest` case. Cover the lifecycle state machine
explicitly — these are the cases that v3 of the spec got wrong.

1. **Open PR, no events** → single `PRE_WIP_STATUS` interval ending
   at `asof`.
2. **Open PR, only non-WIP labels** (`bug`, `area:web`) → same as
   (1).
3. **Linear progression** through three WIP labels with a final
   `MergedEvent` after all labels cleared → four intervals (Pre-WIP,
   a, b, c) plus a `DEPARTED_STATUS` interval ending at merge.
4. **Merged-but-not-shipped**: WIP label still applied at
   `MergedEvent` → final interval ends at merge, status is that WIP
   label.
5. **Merged after last WIP label removed** → final interval is
   `DEPARTED_STATUS` ending at merge.
6. **Closed-not-merged with WIP label still applied** (`ClosedEvent`
   only, no `MergedEvent`) → final interval is `ABANDONED_STATUS`
   ending at close. *(v4 fix — caught the abandoned-PR-leaks-WIP bug
   from v3.)*
7. **Closed-not-merged with no WIP label applied** → final interval
   is `ABANDONED_STATUS` ending at close.
8. **Reopen cycle**: `Created → label_a → ClosedEvent → ReopenedEvent
   → label_b → MergedEvent`. Emits intervals: Pre-WIP, a,
   ABANDONED (the closed gap), a-or-b-resolution at reopen, b,
   final status at merge. Reopens are a known correctness hazard;
   test the boundary carefully.
9. **Two reopen cycles** → idempotent; we never enter ABANDONED twice
   for the same close-reopen pair without an intervening close.
10. **Backward move**: label_b → label_a (no concurrent labels). The
    chart must show this; intervals are non-monotonic.
11. **Concurrent labels** label_a and label_b at one instant →
    resolves to whichever appears later in `--wip-labels`.
12. **Same-instant add-then-remove** of label_a → no zero-length
    interval.
13. **Open PR with last WIP label removed** but PR still open →
    final interval is `PRE_WIP_STATUS` (not `DEPARTED_STATUS` or
    `ABANDONED_STATUS`).
14. **Single-label `--wip-labels "a"`** → two-column behavior
    (Pre-WIP + a) plus terminal bands as appropriate.
15. **Label name appearing only in historical events** (renamed
    label scenario): caller passes only the new name; historical
    events of the old name are silently filtered out. Confirms the
    materializer does *not* alias — the warning is the caller's
    discoverability path, not the materializer's responsibility.
16. **Lifecycle event with no label events**: PR opened and merged
    without any labels ever applied → two intervals: Pre-WIP for the
    open span, Departed for the instantaneous transition. (Or a
    single Departed interval if we coalesce — implementer's choice;
    document which.)

### Signal-quality summary tests (`tests/test_signal_quality.py`)

The counts surfaced in chart captions and JSON envelopes (§8b) are
themselves a public contract. They need tests.

1. `merged_not_shipped` count includes only PRs whose final interval
   in the window is a WIP-label column AND whose lifecycle is MERGED.
2. `abandoned` count = PRs in window with `ABANDONED_STATUS` as final
   interval.
3. `reopened` count = PRs with ≥1 `ReopenedEvent` in window.
4. `bot_no_wip` count = PRs where `is_bot=True` AND no WIP-label
   event ever fires.
5. `timeline_truncated` count = PRs where `timelineItems.pageInfo
   .hasNextPage` is true on the response. (Truncation is observable.)
6. `missing_wip_labels` = `--wip-labels` entries that appear in
   neither any PR's current label set nor any timeline event in the
   window. (Useful for catching typos and renames.)

### CLI parsing tests (`tests/test_cli_wip_labels.py`)

1. `--wip-labels "a,b,c"` → `WipLabels(ordered=("a","b","c"))`.
2. Whitespace tolerance: `--wip-labels " a , b ,c "` → same as (1).
3. Empty string → clear error at parse time.
4. Duplicate labels (`"a,b,a"`) → clear error at parse time.
5. Invalid characters / empty entries (`"a,,b"`) → clear error.

### Fixture-backed fetcher test (`tests/test_github_label_fetch.py`)

Record one real GraphQL response from a PR-rich repo (synthetic
PR-shaped fixture derived from `kno`'s issue timelines is acceptable
for now, since `kno` PRs don't yet carry workflow labels). Commit to
`tests/fixtures/`. Run with `read_only=True`. Same shape as existing
GitHub fetcher tests.

### Coverage

Materializer hits 100% line + branch coverage. Fetcher and CLI glue
sit at typical project coverage.

## 10. Boundaries

### Always

- Treat `WorkItem.status_intervals` as the contract between sources
  and reports.
- Validate `--wip-labels` at parse time, before any network call.
- Keep `OPEN_PR_QUERY` and `PR_SEARCH_QUERY` byte-for-byte identical.
- Surface the WIP-label list on the rendered chart caption ("WIP
  labels: shaping, in-progress, in-review") so the reader can verify.
- Silently ignore labels outside `--wip-labels`. They're someone
  else's organizing principle.

### Ask first

- Adding a way to rename `Pre-WIP` / `Departed` on the chart.
- Letting `flow efficiency` consume `--wip-labels` for active/wait
  clustering. Touches `compute.py`'s clustering math.
- Reading labels from linked issues (`Closes #N`). That's the
  `GitHubIssuesSource` path; out of scope here.

### Never

- Read any in-repo config to infer WIP labels.
- Auto-detect "state-like" labels.
- Mutate `PR_SEARCH_QUERY`.
- Drop PRs from the CFD because they hold no WIP label. They are
  visible in the Pre-WIP / Departed bands; that's the *signal*, not
  noise.
- Force a merged PR into Departed if a WIP label is still applied.
  The merged-but-not-shipped case is the whole point of this design.
- Hard-code any label name.

## 11. Risks and mitigations

| Risk                                                          | Likelihood | Mitigation                                                                                       |
| ------------------------------------------------------------- | ---------- | ------------------------------------------------------------------------------------------------ |
| Timeline truncation at 100 events drops late label changes    | Low        | Counted into `timeline_truncated`; caption surfaces. Pagination is a follow-up.                  |
| Caller supplies labels the repo has never used                | Medium     | `missing_wip_labels` count + `gh`-style "did you mean …" at startup.                             |
| Concurrent labels produce surprising column choice            | Medium     | Caption: "most progress wins — rightmost in --wip-labels."                                       |
| Two GitHub Aging modes (review-cycle vs label) confuse users  | Medium     | `flow aging --help` and the chart caption name the mode in use.                                  |
| Merged-but-not-shipped PRs show as stuck WIP                  | Wanted     | Captioned: `merged_not_shipped: N`. This is the point.                                           |
| Closed-not-merged PR with stale WIP label leaks into WIP      | Was high   | **Fixed in §8b:** `ABANDONED_STATUS` at `closedAt` regardless of labels. Counted.                |
| Reopened PR loses its history                                 | Was high   | **Fixed in §8b:** lifecycle state machine consumes `ReopenedEvent`. Counted.                     |
| Renamed label breaks historical intervals                     | Medium     | `missing_wip_labels` surfaces it; users list both old and new names. We do not alias.            |
| Bot-authored PRs inflate Pre-WIP                              | Medium     | `bot_no_wip` count; offer `--exclude-bots` (already exists on `flow efficiency`).                |
| Same-instant event order is GraphQL's, not deterministic ours | Low        | Document. In practice GitHub's order is stable for the same response; cache makes it consistent. |
| User reads the chart and assumes `isDraft` matters in label mode | Medium  | Caption: "label mode does not consult `isDraft` / `reviewDecision`."                             |

## 12. Open questions

Leans are defaults, not commitments.

1. **Where does `Pre-WIP` go in the Aging chart?** Aging skips it
   entirely (per success criterion #1). CFD shows it as the bottom
   band. **Lean: keep this asymmetry — it matches what each chart is
   for.**
2. **Should `Departed` show on Aging?** No — aging by definition is
   for in-flight work. **Lean: exclude.**
3. **Should `flow efficiency` learn `--wip-labels` too?** Replace
   event clustering with label-interval math for active/wait
   accounting. **Lean: follow-up; keep this spec scoped.**
4. **CFD axis caption.** "WIP columns: shaping, in-progress,
   in-review" is enough; no need to caption Pre-WIP / Departed.
   **Lean: yes.**
5. **Discoverability** (per `feedback-issue-scope-dont-invent-flags`
   memory). This is genuine new capability — no existing flag does
   this. `flow aging --help` must clearly note that two modes exist
   (review-cycle by default, label mode when `--wip-labels` is
   supplied).

6. **Should the signal-quality block be opt-in (`--verbose`) or
   always-on?** The whole point of §8b is that the chart without
   these counts is a pretty lie. **Lean: always-on in HTML and JSON
   envelopes; in the text headline keep it tight to a single line
   that names the highest-priority issue (e.g. "WARNING: 9 abandoned
   PRs carry stale WIP labels — Aging excludes them, CFD shows them
   in Abandoned band").**

7. **Reopens that span the window boundary.** If a PR was reopened
   *before* the window starts, do we count the most recent reopen
   only, or all of them? **Lean: count every reopen whose timestamp
   is in the window — both for the `reopened` count and for
   interval emission. PRs whose entire reopen cycle is pre-window
   are summarized as "1 reopen historical, 0 in-window."**

## 13. Verification checklist

- [ ] Human reviews and approves §1.
- [ ] Open questions §12 each have a decision.
- [ ] Success criteria §2 are testable.
- [ ] Boundaries §10 accepted, especially the "Never" list.
- [ ] `DECISIONS.md` is updated to back-link this spec from §9
      and §10 (as part of the implementing PR, not before).
