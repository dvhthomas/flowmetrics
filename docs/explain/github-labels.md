---
title: GitHub label-driven CFD and Aging
---

# GitHub label-driven CFD and Aging

> **Diátaxis: Explanation.** Design rationale for the `wip_labels`
> mode on GitHub. Why we let the caller name the WIP labels per
> workflow, how conflicts resolve, and what the chart caption must
> say so the reader can trust the picture.

> **Historical note.** Earlier drafts of this design used a
> per-invocation CLI flag (`--wip-labels "a,b,c"`). The shipped
> surface puts the same data on the workflow YAML's `wip_labels`
> field — same semantics (per-workflow, deterministic, no in-repo
> config discovery), different home. Most of this doc still reads
> "labels named in `wip_labels`" rather than "labels named on the
> CLI flag" to match the shipped surface.

Related: [Decisions §9 and §10](decisions.md),
[Glossary](../glossary.md), [Reference § Workflow YAML](../reference.md#workflow-yaml).

## 1. Objective

Make GitHub a first-class source for `flow metric cumulative` and
`flow metric aging` by **letting the caller name the labels that
constitute WIP**. Everything else is "not WIP" by exclusion. The user
supplies the WIP-label set per workflow; flowmetrics applies it.

The GitHub Timeline API exposes `LabeledEvent` and `UnlabeledEvent`
with ISO-8601 timestamps. A sequence of those events on a PR, filtered
to the user's WIP labels, materializes the same
`[StatusInterval(start, end, status)]` series the rest of the codebase
already consumes from Jira changelogs.

### What this unlocks

- **CFD** for GitHub PRs goes from two-state degenerate (arrivals /
  departures) to a real multi-band stack: one band per WIP label,
  in the order the caller named them, plus implicit arrivals
  (pre-WIP) and departures (no WIP label applied).
- **Aging** for GitHub PRs gains a label-driven mode that surfaces
  the team's *actual* workflow, alongside the existing review-cycle
  mode that uses `isDraft` + `reviewDecision`.

### Inputs — and only these inputs

Per workflow, on the YAML:

```yaml
workflow:
  name: kno-shaping
  source: github
  repo: dvhthomas/kno
  start: 2026-04-01
  stop:  2026-05-10
  wip_labels:
    - shaping
    - in-progress
    - in-review
```

That's the entire user-facing surface. The list is **ordered, with
most progress on the right**. Order drives column/band placement and
breaks ties when a PR concurrently holds more than one WIP label —
**most progress wins** (rightmost in the list).

Re-defining "WIP" is a one-edit, one-run change. The caller decides
per workflow. There is no in-repo config and no remembered state
between runs. Want to see what happens if `in-review` isn't counted
as WIP? Edit the YAML (or write a second workflow with a different
set). Want to add `blocked` to your WIP set just for one view? Add
it to a separate workflow's `wip_labels`.

Flowmetrics makes no other assumption about labels. It does not read
any in-repo config (`.github/labels.yml`, `project-label-sync.yml`,
GitHub Projects v2, etc.). It does not assume the target repo has any
particular convention, or any convention at all. Target repos may
have arbitrary label sets — the caller picks which subset counts as
WIP for the workflow they want right now.

### Out of scope

- **Per-repo config files.** Per-workflow only.
- **GitHub issues.** PRs only. (See [Decisions §10](decisions.md#10-for-github-only-pull-requests-count-as-work--issues-are-invisible).)
- **Auto-detection of WIP labels.** The caller names them; we don't
  guess.
- **Multiple WIP classes** (pre-wip / wip / done as separate
  categories). One class only: WIP, by name. Not-WIP, by exclusion.
- **Replacing the existing review-cycle Aging.** Label mode is opt-in
  via `wip_labels`; absent the field, behavior is unchanged.

## 2. Success criteria

1. `flow metric aging` against a workflow with `wip_labels: [a, b, c]`
   renders an Aging chart with one column per named label, in the
   order given, plotting only PRs whose **current** label set
   intersects `{a, b, c}`. PRs with no WIP labels currently applied
   are excluded from Aging.
2. `flow metric cumulative` against the same workflow renders a CFD
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
7. A workflow without `wip_labels` continues to produce the
   review-cycle Aging output as today. No regression to the
   zero-config path.

## 3. Code layout (as shipped)

```
src/flowmetrics/
  github.py            ← query + fetcher for labels
  github_labels.py     ← pure functions:
                          - parse wip_labels
                          - materialize StatusIntervals from LabelEvents
                          - tie-break concurrent labels
  cfd.py               ← consumes status_intervals
  aging.py             ← filters items whose current interval status is
                          the implicit pre-WIP / departures bucket
  compute.py           ← StatusInterval is the contract
```

## 4. Data model

The codebase's contract is `WorkItem.status_intervals: list[StatusInterval]`.
This design adds a new producer; consumers are effectively unchanged.

### Inputs (parsed from `wip_labels`)

```python
@dataclass(frozen=True)
class WipLabels:
    """Ordered, deduped list of label names the workflow has declared
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

@dataclass(frozen=True)
class LifecycleEvent:
    at: datetime
    kind: Literal["merged", "closed", "reopened"]
```

### Constants

```python
PRE_WIP_STATUS   = "Pre-WIP"     # arrivals band on CFD; excluded from Aging
DEPARTED_STATUS  = "Departed"    # merged-and-cleaned; excluded from Aging
ABANDONED_STATUS = "Abandoned"   # closed-not-merged; excluded from Aging
```

All three names are caption-only.

### Materialization

```python
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
    follows §5 precisely:

    - `active` set is mutated by label events whose label is in `wip`.
      Non-WIP labels are filtered out before the walk.
    - Lifecycle state (`OPEN` → `CLOSED` → `MERGED` with reopen
      reverting `CLOSED` → `OPEN`) gates the status mapping.
    - Status mapping is the table in §5: empty+OPEN → PRE_WIP,
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
    """True iff the item's current status is one of the workflow's
    WIP labels — i.e. NOT in `_NON_WIP`."""
```

## 5. Signal conflicts and deterministic resolution

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
- `column`: the rightmost-in-`wip_labels` member of `active`, or
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
   labels.** GitHub's `closedAt` without `mergedAt` means the PR was
   discarded — labels on it are stale by definition. Forcing it to
   Abandoned at `closedAt` prevents abandoned PRs leaking into WIP
   forever. If the PR is later reopened (`ReopenedEvent`), it
   re-enters `active`-based resolution at the reopen timestamp.

### Three new implicit columns / bands

| Column              | Visible in CFD | Visible in Aging |
| ------------------- | -------------- | ---------------- |
| `PRE_WIP_STATUS`    | Yes (arrivals) | No               |
| `DEPARTED_STATUS`   | Yes (departures, merged-and-cleaned) | No |
| `ABANDONED_STATUS`  | Yes (departures, closed-not-merged) | No |

`DEPARTED_STATUS` and `ABANDONED_STATUS` are separate so the CFD can
distinguish "merged work shipped" from "work abandoned." Some teams
care; conflating them would lose information cheaply recovered.

### Conflict matrix

| Conflict                                                   | Outcome                                                                                     |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| PR `mergedAt` with WIP label still applied                 | Stays in WIP column. Captioned: "merged-but-not-shipped: N PRs."                            |
| PR `closedAt`-not-merged with WIP label still applied      | Moves to `ABANDONED_STATUS` at `closedAt`. The label is ignored from that moment on.        |
| PR reopened (`ClosedEvent` then `ReopenedEvent`)           | Returns to label-driven resolution at the reopen timestamp. The closed gap appears in `ABANDONED_STATUS`. |
| Same-instant `add(a)` + `remove(b)`                        | Apply in GraphQL response order. Document that ordering is GitHub's, not ours.              |
| Label renamed mid-history (`in-progress` → `wip`)          | **User must list both names** in `wip_labels`. We warn at materialize if any name in `wip_labels` is absent from both the current `labels(first: 50)` snapshot of any PR in the window AND every historical timeline event. |
| `wip_labels` names a label not present in repo at all      | Warn at materialize with `gh`-style "did you mean …" suggestion using the repo's label list.    |
| Bot-authored PR with no labels                             | Default behavior: visible in `PRE_WIP_STATUS` band. The chart caption will say "N of M PRs are bot-authored and carry no WIP label." |
| `isDraft: true` PR carrying a WIP label                    | Label wins; draft state ignored. Caption explicitly says "label mode does not consult `isDraft`." |
| Timeline >100 events (truncation)                          | The fetcher counts these per-PR and the chart caption says "N PRs had truncated timelines (tail dropped)." Materializer behavior is correct given truncated input. |
| Search cap of 1000                                         | Existing warning from [Decisions §4](decisions.md#4-github-search-caps-results-at-1000) is reused. |

### Signal-quality contract

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
- **Not auto-resolve renamed labels.** GitHub's API keeps the
  historical name on past events. We do not heuristically alias.
- **Not infer state from `ReadyForReviewEvent` / `PullRequestReview`.**
- **Not collapse `DEPARTED_STATUS` and `ABANDONED_STATUS`.** Separate
  bands; user can read them.
- **Not drop PRs that hold no WIP labels.** They are visible in the
  Pre-WIP band — the size of that band is itself diagnostic.

## 6. Risks and mitigations

| Risk                                                          | Likelihood | Mitigation                                                                                       |
| ------------------------------------------------------------- | ---------- | ------------------------------------------------------------------------------------------------ |
| Timeline truncation at 100 events drops late label changes    | Low        | Counted into `timeline_truncated`; caption surfaces. Pagination is a follow-up.                  |
| Caller supplies labels the repo has never used                | Medium     | `missing_wip_labels` count + `gh`-style "did you mean …" at materialize.                          |
| Concurrent labels produce surprising column choice            | Medium     | Caption: "most progress wins — rightmost in wip_labels."                                          |
| Two GitHub Aging modes (review-cycle vs label) confuse users  | Medium     | `flow metric aging --help` and the chart caption name the mode in use.                            |
| Merged-but-not-shipped PRs show as stuck WIP                  | Wanted     | Captioned: `merged_not_shipped: N`. This is the point.                                            |
| Closed-not-merged PR with stale WIP label leaks into WIP      | Mitigated  | `ABANDONED_STATUS` at `closedAt` regardless of labels. Counted.                                   |
| Reopened PR loses its history                                 | Mitigated  | Lifecycle state machine consumes `ReopenedEvent`. Counted.                                       |
| Renamed label breaks historical intervals                     | Medium     | `missing_wip_labels` surfaces it; users list both old and new names. We do not alias.            |
| Same-instant event order is GraphQL's, not deterministic ours | Low        | Document. In practice GitHub's order is stable for the same response; cache makes it consistent. |
| User reads the chart and assumes `isDraft` matters in label mode | Medium  | Caption: "label mode does not consult `isDraft` / `reviewDecision`."                             |
