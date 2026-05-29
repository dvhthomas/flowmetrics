# Spec: Wire step `matches` into materialisation

Status: **DRAFT — for review. Do not implement until approved.**

## Objective

Make the workflow **steps** a user defines in the contract builder
actually drive how source data is bucketed into stages — so the
dashboard's CFD, aging, cycle-time and throughput reflect the user's
workflow, not the source adapter's hard-coded vocabulary. Today the
steps/`matches` are **preview-only**; this spec closes that gap.

## Current state (verified)

- A step is `{name, wip, matches: [str]}` (`contract.py`). `matches`
  resolves to `effective_matches` = `matches` or `(name,)`.
- `matches` is consumed in exactly **one** place: the dry-run preview
  bucketer `source_probe.bucket_items_by_step` (called from the
  `_dry-run` endpoint, `app.py:891`). Matching there is:
  - **OR**, by plain **string equality** against an item's single
    `current_stage` (`source_probe.py:295` — `if stage in matches`);
  - **untyped** — a match string is compared to the stage text with no
    notion of "label" vs "status" vs "lifecycle event".
- The **real** materialise pipeline does **not** use `matches`. The
  source adapters emit their own `StageTransition(item_id, entered_at,
  stage, signal)` rows:
  - GitHub PR review lifecycle stages: `Draft / Awaiting Review /
    Changes Requested / Approved / Merged` (`sources/github.py:236`);
    label-mode uses the label name as the stage.
  - Jira: the raw status name (`sources/intervals.py:53`, `iv.status`).
  - `signal` is a named event constant (`signals.py`):
    `github-pr-created`, `github-pr-ready-for-review`,
    `github-pr-review-changes-requested`, `github-pr-review-approved`,
    `github-pr-merged`, `github-label-added/removed`,
    `jira-status-changed`, `jira-resolved`, …
- The only contract→dashboard influence is `Contract.states.wip` — the
  set of step **names** flagged WIP — used to filter aging/CFD WIP
  (`aging.py:60`, `app.py:1625/1894`). This silently works **only when
  step names equal the adapter's stage names**. The builder's chip
  vocabulary (`PR opened`, `Marked ready for review`, …) does **not**
  equal the adapter stages, so a UI-built workflow's WIP filter matches
  nothing.

### Answers to the two questions that prompted this

1. **AND across criteria?** No. `matches` is a flat OR list. There is no
   way to express "label A AND label B AND PR open".
2. **Per-platform typing?** No. `matches: - ready` means literally
   `current_stage == "ready"`; a label `ready`, a status `ready`, and a
   lifecycle event are indistinguishable.

## Key insight

Each transition already carries **both** axes the user is conflating:
- `signal` → the **named lifecycle event** ("PR merged", "status
  changed"), and
- `stage` → the **label / status text**.

So a *typed* matcher can target the right axis without new fetching:
`{signal: github-pr-merged}` vs `{label: "ready"}` vs `{status: "In
Progress"}`.

## Proposed design

### 1. Typed matchers (replaces flat string `matches`)

A step's matcher is one of:

| Kind     | Matches when…                                  | Source axis | Value space |
|----------|------------------------------------------------|-------------|-------------|
| `event`  | a transition `signal` == the event code        | `signal`    | **fixed code vocabulary** (below) |
| `label`  | a `github-label-added` stage == value          | `stage`     | repo's label names (source-defined) |
| `status` | a Jira status (`stage`) == value               | `stage`     | project's status names (source-defined) |
| `stage`  | raw adapter stage text == value (escape hatch) | `stage`     | free text |

#### Event codes — short, stable, typo-resistant

`event:` is the one axis with a *fixed, enumerable* set, so it gets a
short kebab-case code rather than a free-text display name. "PR marked
ready for review" is accurate but a typo magnet in hand-edited YAML;
`pr-ready` is not. Codes are scoped by the contract's `source` (already
known), so the `github-`/`jira-` prefix from `signals.py` is dropped.
The UI chip shows the friendly label; the stored/validated value is the
code. Codes map 1:1 to the existing `signals.py` constants — the single
source of truth.

GitHub (`source: github`):

| code               | label (UI)            | signal constant                        |
|--------------------|-----------------------|----------------------------------------|
| `pr-opened`        | PR opened             | `github-pr-created`                    |
| `pr-ready`         | Ready for review      | `github-pr-ready-for-review`           |
| `changes-requested`| Changes requested     | `github-pr-review-changes-requested`   |
| `approved`         | Review approved       | `github-pr-review-approved`            |
| `pr-merged`        | PR merged             | `github-pr-merged`                     |
| `issue-opened`     | Issue opened          | `github-issue-created`                 |
| `issue-closed`     | Issue closed          | `github-issue-closed`                  |

Jira (`source: jira`):

| code             | label (UI)      | signal constant         |
|------------------|-----------------|-------------------------|
| `created`        | Issue created   | `jira-issue-created`    |
| `status-changed` | Status changed  | `jira-status-changed`   |
| `resolved`       | Resolved        | `jira-resolved`         |

Validation rejects an `event:` code outside this set with a message that
lists the valid codes — so a hand-edited typo fails loudly at save/parse,
not silently at materialise.

YAML shape (each matcher is a typed mapping; no bare strings):

```yaml
steps:
  - name: In Review
    wip: true
    matches:
      - event: pr-ready              # fixed code, not "PR marked ready…"
      - event: changes-requested
      - label: needs-review          # source-native label name
  - name: Done
    wip: false
    matches:
      - event: pr-merged
```

- **Within a step: OR** — the item enters the step on *any* matching
  transition. (Matches today's mental model.)
- **No cross-criteria AND in v1.** "Label A AND label B AND open" needs
  point-in-time snapshot state, which only the label-mode snapshot has
  (not the transition stream). Deferred to its own spec; OR-of-typed-
  matchers covers the common cases.

### 2. A remap layer at materialise time

Add a pure function that relabels adapter transitions to the user's
step names before they're written to the warehouse:

```
remap_transitions(raw: list[StageTransition], steps) -> list[StageTransition]
# each raw transition whose (signal|stage) matches a step's matcher
# is rewritten stage=<step.name>; a transition matching no step is
# relabelled stage="_unmatched" (surfaced as a coverage-gap bucket).
```

Result: the warehouse `stage` column holds the user's step names, so
`Contract.states.wip` (step names) lines up automatically and every
downstream metric reflects the user's workflow. Contracts with **no
steps** skip remapping → today's adapter-native behavior (backward
compatible).

### 3. Builder + dry-run alignment

- The builder chips already separate "Labels in the repo" from
  "Lifecycle events" — wire those to emit typed matchers instead of bare
  strings: a lifecycle chip emits `event: <code>` (chip shows the
  friendly label, stores the code), a label/status chip emits
  `label:`/`status: <source-native value>`. Because chips are clicked,
  the user never types a code; the code only matters for hand-edited
  YAML and validation messages.
- `bucket_items_by_step` (dry-run) and `remap_transitions` (materialise)
  must share the **same** matcher-evaluation function so the preview is
  faithful to what materialise will do. (Today they diverge — that's the
  root of the confusion.)

## Boundaries

- **Always:** keep a no-steps contract working exactly as today
  (adapter-native stages); share one matcher evaluator between
  preview and materialise.
- **Ask first:** any change to the settled decisions above (remap
  timing, `_unmatched` handling, OR-only, no-migration clean break).
- **Never:** silently drop transitions (unmatched ones go to
  `_unmatched`, never the floor).

## Decisions (settled)

All open questions are resolved — ready to implement.

1. **Event-code vocabulary:** short kebab codes (`pr-ready`,
   `changes-requested`, `pr-merged`, …) scoped by source, mapped 1:1 to
   `signals.py`. The UI chip shows the friendly label.
2. **Remap timing:** **at materialise.** Each transition's `stage` is
   rewritten to the matching step name during backfill, so the warehouse
   stores step names and every downstream metric + `states.wip` works
   unchanged. Editing steps requires a re-backfill to take effect.
3. **Unmatched transitions:** **surface as `_unmatched`** — relabel
   strays to a single `_unmatched` stage so the dashboard shows a
   coverage-gap bucket (parity with the dry-run preview). Nothing is
   silently dropped.
4. **AND logic:** **OR-only in v1.** A step matches on *any* of its
   matchers. Cross-criteria AND ("open AND label:blocked") needs
   point-in-time snapshot state and is deferred to its own spec.
5. **Migration:** **none — clean break.** Nobody uses this yet, so there
   is no translator/upgrade code. The few existing test contracts are
   rewritten by hand to the typed schema as part of the work, and
   **bare-string `matches` support is dropped**: a matcher must be a
   typed mapping (`{event|label|status|stage: value}`). A step with an
   *empty* `matches` still falls back to `stage: <step.name>` (the
   existing `effective_matches` behavior), so simple "stage named like
   the step" workflows need no matchers at all.

## Success criteria

- A UI-built GitHub PR workflow ("Open / In Review / Merged" with event
  matchers) materialises so the CFD/aging/throughput bucket by those
  three steps, and `states.wip` highlights "In Review".
- The dry-run preview and the materialised result agree item-for-item
  on bucketing (shared evaluator).
- A no-steps contract is byte-for-byte unchanged.
- New unit tests: typed-matcher evaluation (label/status/event/stage),
  `remap_transitions` (incl. unmatched handling), preview/materialise
  parity; one e2e: build a typed workflow → backfill → dashboard buckets
  by the user's steps.

## Test strategy

- Unit: matcher evaluator + `remap_transitions` (pure, table-driven).
- Component: dry-run preview uses the shared evaluator.
- e2e (offline fixture): build "Open/In Review/Merged" → materialise →
  assert warehouse stages are the step names and WIP filter is "In
  Review".
