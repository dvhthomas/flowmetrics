# Spec: canonical work-item event stream

Status: **Draft, awaiting approval.** Reframed around the
schema-first thought experiment: what's the minimum table shape
that lets every flow metric drop out of it? Implementation work
(Issue+PR stitching included) becomes translation rules layered
on top.

## Objective

Today flowmetrics' canonical model is `WorkItem(...,
status_intervals: list[StatusInterval])` where each
`StatusInterval(start, end, status)` is a half-open window.
Sources (GitHub, Jira) build intervals from their own
source-specific events. Renderers consume the intervals.

Two problems:

1. **Redundant data.** Interval `end` is just the next interval's
   `start`. Reading code has to keep both in sync.
2. **Source semantics leak.** The translation logic is scattered
   across `sources/github.py`, `sources/github_labels.py`,
   `sources/jira.py`. Bringing in a new event source (Issues with
   labels, Project Boards, commit messages, …) means hunting
   through every renderer for assumptions.

This spec proposes the canonical model as **two tables**: work
items, and stage transitions. Sources translate their native
events into stage-transition rows. The metric layer reads only
those rows + a team-supplied workflow definition. Every flow
metric — Cycle Time, Throughput, WIP, CFD, Aging, Scatterplot,
Forecast — falls out of this minimum.

### Why this matters

The user's framing was a SQLite thought experiment: "if we pulled
data out into SQLite, what would the tables look like?" Once you
can answer that, the rest is mechanical:

- Sources become **translators** that produce rows.
- Metrics become **queries** over the rows + workflow def.
- "Issue+PR stitching" becomes **one translator rule** in the
  GitHub adapter: "when a PR with `closingIssuesReferences`
  merges, emit a `Done` transition on the linked Issue."

Same lens unifies every source we'll ever add.

## Storage: stateless first, persistence later

The schema below is **shape-of-data**, not storage. Today every
flowmetrics invocation is stateless — fetch source data into
memory, compute, render, exit. The thought experiment that
inspired this spec was "if we *did* persist into SQLite, what
would the tables look like?" — the answer is the same two
tables whether they live in RAM or on disk.

Phase 0–4 of this spec all stay **stateless and in-memory**.
Persistence (SQLite, parquet, anything) is a separate future spec
that would benefit *more* once the canonical schema is real,
because then any cache becomes a faithful record of the
canonical events instead of source-specific bytes.

So: nail the canonical schema and the metric-layer queries first.
Storage choice follows.

## The two tables

```
work_item                       stage_transition
─────────────────               ─────────────────
id          text PK             item_id     text  (FK)
title       text                entered_at  datetime
url         text                stage       text  (team-defined)
source      text                signal      text  (named constant)
```

That's it. Everything else is derivable.

**`work_item`**: bare identity. `id` is opaque (the source picks
the shape: `github:owner/repo:pr:42`, `jira:CASSANDRA-9430`,
etc., as long as it's globally unique). `title` is for display.
`url` is the drill-down link. `source` is the producing
adapter's tag for filtering.

**`stage_transition`**: one row per entry into a stage. There is
no `exited_at` column — the next transition's `entered_at` is the
implicit exit. An item with no transitions exists in the source
but never entered the workflow; an item with one transition
entered a stage and stayed there. Backward moves (re-opened,
regressed) are just additional rows; the current stage is the
most recent transition's `stage`.

**`signal`** documents *which named event* produced the
transition row — `github-label-added`, `github-pr-merged`,
`github-pr-closes-issue`, `jira-status-changed`, etc. The
constant *is* the audit trail. No string-matching on item IDs to
guess what happened.

### The third thing the metric layer reads (not a table)

A team-supplied **workflow definition**:

- `stages`: ordered list of stage names — flow direction
- `wip_set`: subset of `stages` the team agreed counts as
  Work-In-Progress

The workflow def doesn't live in the DB because it's a *team
configuration*, not data. Today it's `--workflow` or
`--wip-labels`; the spec keeps that surface.

## Flow metrics, expressed as queries over the schema

| Metric                       | Query shape                                                                                                                                            |
|------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------|
| **Cycle Time** (one item)    | `max(entered_at) − min(entered_at WHERE stage IN wip_set)` for the item, when its current stage is past the wip_set                                    |
| **Throughput** (per window)  | count of distinct `item_id` whose latest transition in `[t1, t2]` lands on a stage past the wip_set                                                    |
| **WIP** (snapshot)           | count of distinct `item_id` whose latest transition at-or-before `asof` lands on a stage in the wip_set                                                |
| **CFD** (over time)          | for each sample date, for each stage, count items whose latest transition at-or-before the sample date lands on that stage                             |
| **Aging** (in-flight, now)   | for each item with `current_stage ∈ wip_set`, `age = now − min(entered_at WHERE stage ∈ wip_set)`                                                       |
| **Scatterplot** (per item)   | for each completed item, plot `(latest entered_at, cycle_time)`                                                                                        |
| **Forecast** (when/how-many) | sample completed-items-per-day from the historical throughput series above                                                                             |

Every report is a fold over the same canonical stream. No
source-specific logic in the metric layer.

## Translation: how each source emits rows

Sources own the translation from their native events to
`stage_transition` rows. Each translation is one named signal.

### GitHub adapter

| Source event                                                                | Emitted row                                                                              |
|-----------------------------------------------------------------------------|------------------------------------------------------------------------------------------|
| PR opened                                                                   | `stage = "Open"`, `signal = "github-pr-created"`                                         |
| PR draft → ready-for-review                                                 | `stage = "Awaiting Review"`, `signal = "github-pr-ready-for-review"`                     |
| PR review requests changes                                                  | `stage = "Changes Requested"`, `signal = "github-pr-review-changes-requested"`           |
| PR review approves                                                          | `stage = "Approved"`, `signal = "github-pr-review-approved"`                             |
| PR merged                                                                   | `stage = "Merged"`, `signal = "github-pr-merged"`                                        |
| Issue created                                                               | `stage = "Open"` (or team's first stage), `signal = "github-issue-created"`              |
| Issue label added (label ∈ team's stage-mapping)                            | `stage = <team's stage for that label>`, `signal = "github-label-added"`                 |
| Issue closed without merge                                                  | `stage = "Closed (not planned)"`, `signal = "github-issue-closed"`                       |
| **PR with `closingIssuesReferences` merges** ← the Issue+PR stitching rule | on the linked Issue: `stage = "Merged"`, `signal = "github-pr-closes-issue"`             |

Stages are the team's strings; the adapter doesn't invent them.
What the table above shows as `"Awaiting Review"` etc. is just
what the *team* chose to call those stages — the adapter maps
the named signal to whatever string the team supplied in their
workflow config.

The Issue+PR stitching disappears as a special case. It's just
one more row of translation: "this PR-side event becomes a
transition on the Issue's stage stream." No `StitchedItem`
class, no `linked_item_ids` field. Two work items each with their
own transition rows, plus one signal documenting the cross-link.

### Jira adapter

| Source event                       | Emitted row                                                                |
|------------------------------------|----------------------------------------------------------------------------|
| Issue created                      | `stage = <team's first stage>`, `signal = "jira-issue-created"`            |
| Changelog status transition        | `stage = <new status>`, `signal = "jira-status-changed"`                   |
| Issue resolved                     | `stage = "Resolved"`, `signal = "jira-issue-resolved"`                     |

Same shape, different signal names. Jira's changelog is already
event-stream-shaped, so the translation is nearly identity for
the basics.

> **Note**: this spec's examples are GitHub-heavy because that's
> where we already see the most complexity (Issues vs PRs vs
> labels vs review events vs project boards). **Jira is likely
> just as nuanced** — workflow schemes per project, custom
> fields, sub-tasks linking to parents, Epic-Story-Subtask
> hierarchies. We haven't fully mapped Jira's strangeness into
> the canonical model yet, but the shape is the same: every Jira
> peculiarity becomes one more named signal in `signals.py` and
> one more row of translation in `sources/jira.py`. No changes
> to the canonical schema, no changes to the metric layer.

## Future dimension: slicing by work type (NOT in this spec)

Often called *classes of service* or *work-item types* — the same
metric (cycle time, throughput, CFD, …) computed separately per
category. Common splits:

- **Team-typed work**: bug / feature / keep-the-lights-on /
  refactor. Different cycle-time distributions per type — a bug
  fix typically ships faster than a feature.
- **Classes of service**: Standard / Expedite / Fixed-Date /
  Intangible. Used to drive WIP policy.

**Not building this in any of phases 0–4.** But the schema must
not block it. The clean addition is one new column on the
`work_item` table:

```
work_item
─────────────────
id          text PK
title       text
url         text
source      text
types       text[]   ← future; team-defined tags, multi-valued
```

Or, if we ever normalize to a real DB, a many-to-many side
table:

```
work_item_type
─────────────────
item_id     text  (FK)
type        text  (team-defined)
```

The translation rule per source would be the team's choice:

- GitHub: labels matching a configured prefix (e.g.
  `type:bug`, `type:feature`). The adapter strips the prefix
  and writes the rest into `work_item.types`.
- Jira: the native Issue Type field maps directly.

The metric layer would gain one optional filter argument
(`types: set[str] | None = None`) on every metric query. None
= include all items; non-None = restrict the population. The
arithmetic is unchanged; only the row-filter at the top of
each query changes.

Worth keeping in mind during Phase 1 design: don't bake
single-valued type assumptions into the `WorkItem` dataclass or
the query helpers. A `types: tuple[str, ...] = ()` field added
later should be a pure addition, never a breaking change.

## Phased plan

### Phase 0 — vocabulary rename (no behaviour change)

The existing source-specific names need cleanup before the new
model lands. Mechanical search-and-replace, suite-verified.

| File                              | Old name        | New name        |
|-----------------------------------|-----------------|-----------------|
| `compute.py`, `WorkItem`          | `merged_at`     | `completed_at`  |
| `compute.py`, `FlowEfficiency`    | `merged_at`     | `completed_at`  |
| `report.py`, `ScatterplotPoint`   | `pr_url`        | `url`           |
| `aging.py`, `AgingItem`           | `pr_url`        | `url`           |
| `sources/github.py`               | `_pr_url()`     | `_item_url()`   |

JSON schema: ship clean. No `.v1` → `.v2` bump; single user
today.

### Phase 1 — Named-signal constants + `StageTransition` type

```
src/flowmetrics/signals.py
    Module of named constants (one per recognized event).
    Mirrors gh-velocity's `model.Signal*` pattern.

src/flowmetrics/canonical.py
    @dataclass(frozen=True) StageTransition
    @dataclass(frozen=True) WorkflowDef  (stages, wip_set)
    Pure types. No I/O, no source coupling.

src/flowmetrics/sources/intervals.py
    Helper: given a list of StageTransition rows for one item,
    derive `status_intervals` (today's StatusInterval shape) so
    the existing reports keep working unchanged. Bridge module —
    deleted once everything reads StageTransition natively.

tests/test_canonical.py
    Pure-logic tests for the type + bridge.
tests/test_signals.py
    Trivial — every signal name we emit is in `signals.py`.
```

Sources keep emitting `StatusInterval` for back-compat. New
internal pipeline: `source → StageTransition rows → bridge →
StatusInterval` (today's reports). Once the bridge is proven the
reports start consuming `StageTransition` directly and the
bridge goes away.

### Phase 2 — Issue fetching + the closing-PR signal

```
src/flowmetrics/sources/github_issues.py
    GraphQL fetch for Issues + their label timeline events.
    Emits StageTransition rows (NOT StatusInterval).

src/flowmetrics/sources/github_stitch.py
    Reads PR `closingIssuesReferences` from the existing PR
    fetch, and FOR EACH such reference, emits a transition on
    the Issue's stream with `signal = "github-pr-closes-issue"`.
    No new dataclass. Just rows added to the existing stream.

tests/test_github_issues.py
tests/test_github_stitch.py
```

### Phase 3 — Aging consumes the new stream

```
src/flowmetrics/cli.py
    `flow aging --include-issues` flag — opt-in for now.
    Reads StageTransition rows directly (bridge stops applying
    for Aging's path).

src/flowmetrics/aging.py
    `compute_aging()` accepts StageTransition rows + WorkflowDef.

tests/test_aging_with_issues.py
    End-to-end fixture test.
```

### Phase 4+ — port the other reports off the bridge

CFD, Scatterplot, Efficiency, Forecast each migrate to consume
`StageTransition` rows directly. One report per ship. Bridge
deleted when the last one migrates.

## Vocabulary discipline

Kanban-flow framing throughout. Source-specific words (PR, Issue,
merge, label, changelog, status, resolution) live inside the
source adapters and never leak into the shared types
(`WorkItem`, `StageTransition`, `WorkflowDef`).

Translation table:

| Canonical                | GitHub source                                  | Jira source            |
|--------------------------|------------------------------------------------|------------------------|
| `WorkItem.id`            | `github:owner/repo:pr:42` / `…:issue:7`        | `jira:CASSANDRA-9430`  |
| `WorkItem.title`         | PR/Issue title                                 | Issue summary          |
| `WorkItem.url`           | `/pull/N` or `/issues/N`                       | `/browse/KEY`          |
| `StageTransition.stage`  | team's stage name (mapped from label / event)  | team's stage name      |
| `StageTransition.signal` | `github-*` constant                            | `jira-*` constant      |

## Inspiration: gh-velocity's patterns

(`../gh-velocity`, Go.) Two patterns directly informed this spec:

1. **Named signal constants on every event** (`SignalIssueCreated`,
   `SignalLabelAdded`, `SignalPRMerged`, …). The constant is the
   documented "rule" — anyone reading the data knows exactly
   which source event produced the row.

2. **Translation strategies live in source adapters, not the
   metric layer.** Their `internal/strategy/` has one file per
   linker (`prlink.go`, `commitref.go`, `changelog.go`). Same
   principle here: the canonical layer reads rows; the source
   adapter knows how to build them.

What we are NOT doing: porting gh-velocity, depending on it,
rewriting flowmetrics in Go. The patterns are the inspiration;
the implementation stays Python and stays focused on the
kanban-flow metric set + Jira-equal-citizen support.

## Boundaries

### Always do

- Test first, every behavioural change.
- Run the full suite + ruff before any commit.
- Use the kanban-flow vocabulary in the canonical types — never
  invent stage names, never prescribe what counts as WIP, never let
  source-specific words leak into the shared layer.
- Preserve existing CLI surface. New flags are additive.

### Ask first

- Adding a new dependency.
- Extending the canonical schema (third table, new field).
- Deleting `StatusInterval` (the bridge).
- Adding sample repos.

### Never do

- Reimplement gh-velocity's full strategy permutation system.
- Pattern-match on `item_id` to guess source type — the source
  is the authority (commit `737884c` and stays).
- Make the new schema mandatory before back-compat is proven.
  The bridge stays until every report has migrated.

## Open Questions

All resolved.

1. ~~JSON schema versioning~~ — ship clean. No `.v1` → `.v2`.
2. ~~`closingIssuesReferences` direction~~ — PR side, signal
   `github-pr-closes-issue`.
3. ~~Item ID format~~ — **globally unique**, shape
   `github:owner/repo:pr:42` / `github:owner/repo:issue:7` /
   `jira:PROJECT-KEY`. Today's bare `#42` becomes the
   display-only `WorkItem.short_id` for renderers; the canonical
   `id` is the globally unique form.
4. ~~Stuck Issues~~ — counted as in-flight Aging when WIP-labelled.
5. ~~Default for `--include-issues`~~ — **opt-out** (Issues
   included by default when `--wip-labels` is set; user passes
   `--no-include-issues` to suppress). The flag exists so older
   scripts can pin behaviour, but the new default IS the
   expected one.
6. ~~Multi-phase appetite~~ — confirmed, all phases approved.
