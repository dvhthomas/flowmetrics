# Plan: canonical work-item event stream

Status: **Draft, awaiting approval.** Maps the spec
(`docs/SPEC-issue-pr-stitching.md`) into reviewable phases with
risks, ordering, and verification checkpoints. No tasks yet —
tasks come in Phase 3 once you've green-lit this plan.

Companion to `docs/SPEC-issue-pr-stitching.md`; read that first.

## Components and dependencies

```
                                  ┌──────────────────────┐
                                  │ signals.py           │  ← Phase 1
                                  │   constants only     │
                                  └──────────┬───────────┘
                                             │
                       ┌─────────────────────┴─────────────────┐
                       │                                       │
                       ▼                                       ▼
        ┌──────────────────────┐                 ┌───────────────────────┐
        │ canonical.py         │  ← Phase 1     │ sources/intervals.py  │  ← Phase 1
        │   StageTransition    │                 │   bridge: rows →     │
        │   WorkflowDef        │                 │   today's intervals  │
        └──────────┬───────────┘                 └───────────┬───────────┘
                   │                                         │
                   │                                         │ (existing reports
                   │                                         │  keep working
                   │                                         │  unchanged)
                   │                                         ▼
                   │                          ┌─────────────────────────────────┐
                   │                          │ aging.py / cfd.py /             │
                   │                          │ scatterplot.py / forecast.py    │
                   │                          │ — read StatusInterval today     │
                   │                          └─────────────────────────────────┘
                   │
                   ▼
   ┌───────────────────────────────────┐
   │ sources/github_issues.py          │  ← Phase 2
   │   emit StageTransition rows       │
   └───────────────┬───────────────────┘
                   │
                   ▼
   ┌───────────────────────────────────┐
   │ sources/github_stitch.py          │  ← Phase 2
   │   emit github-pr-closes-issue     │
   │   transitions                     │
   └───────────────┬───────────────────┘
                   │
                   ▼
   ┌───────────────────────────────────┐
   │ aging.py + cli.py                 │  ← Phase 3
   │   --include-issues                │
   │   reads StageTransition directly  │
   └───────────────────────────────────┘
                   │
                   ▼
   ┌───────────────────────────────────┐
   │ cfd / scatterplot / efficiency /  │  ← Phase 4
   │ forecast: migrate one per ship    │
   │ Delete intervals.py bridge.       │
   └───────────────────────────────────┘
```

Vertical arrows = "depends on the previous box being landed."
The five phases are **strictly sequential** at the boundaries —
no phase parallelism. Inside a phase the work CAN often run in
parallel (e.g. Phase 0 renames are independent), but each phase
gates the next.

## Ordering rationale

The order is forced by **back-compat**, not preference:

- **Phase 0 first** because renaming `merged_at` →
  `completed_at` everywhere is mechanical and zero-risk, but it
  touches every file we're about to add to. Doing it after
  Phase 1 means re-renaming new files.
- **Phase 1 second** because the canonical types + bridge let
  every subsequent phase write to the new model while existing
  reports keep working through the bridge.
- **Phase 2 third** because Issue fetching + the closing-PR
  signal are net-new behaviour but don't yet need a consumer.
  The new sources produce `StageTransition` rows; the bridge
  flattens them into `StatusInterval`s that existing reports
  see as ordinary intervals.
- **Phase 3 fourth** because Aging is the first report that
  benefits from the new data path (stitched Issue+PR items).
  It also proves the canonical-types-end-to-end story on a real
  report.
- **Phase 4 last** because CFD / Scatterplot / Efficiency /
  Forecast are each independent migrations. They can ship in
  any order, but only after Phase 3 has proven the pattern.
  Once they all migrate, the bridge is deleted (becomes dead
  code).

## Risks and mitigations

### Phase 0: vocabulary rename

- **Risk:** JSON envelope is a "public" contract for agent
  consumers. A consumer reading `merged_at` will silently miss
  the field after the rename.
  - **Mitigation:** the user is the single consumer today and
    has explicitly accepted the contract break (spec open-Q 1).
    No transitional alias. Tests catch internal callers; the
    user catches downstream.

- **Risk:** test files reference `merged_at` everywhere; missing
  one breaks a downstream test in a confusing way.
  - **Mitigation:** mechanical search-and-replace across `src/`
    AND `tests/` in the same commit. Run full suite — any
    miss fails immediately, in the same commit, at the test
    that uses the stale name.

### Phase 1: signals + canonical types + bridge

- **Risk:** the bridge's "flatten StageTransition rows into
  StatusInterval list" function gets the boundary cases wrong —
  e.g. an item with one transition (no exit), or with backward
  moves. Bad bridge → every downstream report sees subtly wrong
  intervals.
  - **Mitigation:** golden-test the bridge against the existing
    sources' output. For each existing source-to-intervals
    code path, write a new test that runs the source AND the
    bridge separately and asserts identical `StatusInterval`
    lists. The bridge passes only when its output is bit-for-
    bit identical to today's behaviour. Strict TDD.

- **Risk:** signal-constants module grows into a god module
  (every source adds 10+ constants).
  - **Mitigation:** flat namespace, alphabetical by prefix,
    one constant per real-world event type. Three sub-prefixes
    today: `github-*`, `jira-*`, `flowmetrics-*` (the last for
    derived signals like cross-source linking). Cap at ~30
    constants by the end of Phase 4; revisit if it grows
    further.

### Phase 2: Issue fetching + closing-PR signal

- **Risk:** GraphQL payload size. Issues with long label
  histories (kubernetes/kubernetes-style: hundreds of label
  events per issue) blow up the GraphQL query cost.
  - **Mitigation:** time-window the fetch the same way PRs are
    today. `fetch_issues_with_labels(start, stop)` only
    captures label events whose timestamp falls in the window.
    Cap timeline-events per query at the existing limit (100,
    same as PRs). Paginated cursors handle the rest.

- **Risk:** `closingIssuesReferences` returns Issues from
  OTHER repos (cross-repo closes). We can't fetch label
  history for repos we don't have access to.
  - **Mitigation:** filter `closingIssuesReferences` to
    same-repo Issues only at the source layer. Cross-repo
    references are dropped silently (logged at debug). Out of
    scope for this spec.

- **Risk:** Issue IDs collide with PR IDs in the same repo
  (GitHub allocates `#N` from a single counter across issues
  AND prs, so they don't actually collide, but the GLOBALLY
  UNIQUE id format must encode the distinction).
  - **Mitigation:** the spec-mandated ID shape is
    `github:owner/repo:pr:42` vs `github:owner/repo:issue:42`.
    Same-numeric-N stays distinguishable. Tested.

### Phase 3: Aging consumes the new stream

- **Risk:** `compute_aging()` has a fixed function signature
  consumed by tests + the CLI. Changing it breaks back-compat.
  - **Mitigation:** make the new code path opt-in via the
    `--include-issues` flag. When set, CLI passes
    `StageTransition` rows; when unset, today's
    `compute_aging(items, *, asof, max_age_days)` path runs
    unchanged. The two paths coexist until Phase 4 retires the
    old one.

- **Risk:** the `--include-issues` opt-out default flips
  behaviour for existing users running `flow aging
  --wip-labels '...'`. They suddenly see Issues in their
  charts.
  - **Mitigation:** *intended* behaviour — the user explicitly
    chose opt-out as default. Document in the commit message
    and in CLI `--help`. Anyone who wants the old shape passes
    `--no-include-issues`.

### Phase 4: migrate the other reports

- **Risk:** four reports, each their own quirks. Easy to
  introduce regression in one when migrating another.
  - **Mitigation:** one report per commit, full suite green
    between each. Visual regen + spot-check screenshot at each
    step. The bridge stays alive through all of Phase 4; it's
    only deleted in the FINAL commit, after the last report
    migrates.

## Parallelization

Inside a single phase:

- **Phase 0:** all renames are independent. Can be one big
  commit or N small ones; suite catches misses either way.
- **Phase 1:** signals.py + canonical.py + the bridge can be
  developed in three separate files in any order, then wired
  together. Tests are independent.
- **Phase 2:** `github_issues.py` and `github_stitch.py` are
  separable, but the stitch tests need issues fetched, so
  build issues first.
- **Phase 3:** Aging migration is one path; no parallelism.
- **Phase 4:** the four remaining reports are independent of
  each other.

Across phases: **none.** The bridge from Phase 1 is the API
that Phases 2–4 build on; no point starting Phase 2 before
Phase 1's bridge is suite-green.

## Verification checkpoints

End of each phase, run:

```bash
uv run pytest -q                  # all tests green
uv run ruff check src/ tests/    # lint clean
```

Plus the phase-specific check:

| Phase | Phase-specific verification                                                                                          |
|-------|----------------------------------------------------------------------------------------------------------------------|
| 0     | `grep -rn 'merged_at\|pr_url' src/ tests/` returns zero matches outside source adapters that legitimately handle PR data internally. |
| 1     | New test: bridge flattens canonical rows into intervals byte-identical to today's source output, for every existing source-test fixture. |
| 2     | New tests: `test_github_issues.py` and `test_github_stitch.py` green. Manual: `flow aging --repo dvhthomas/kno --wip-labels '...'` runs without error. |
| 3     | New test: end-to-end aging fixture with one stitched Issue+PR produces an `AgingItem` whose age, current_state, and url match the rule in the spec. Manual: regen Cassandra aging — same numbers as before, since Jira path is untouched. Regen one GitHub repo aging with `--include-issues` and spot-check the chart. |
| 4     | (per report) regenerate all samples. Confirm no visual regression on the report being migrated. |

## Scope boundaries this plan honours

- **No new dependencies.** All work uses stdlib + existing
  deps (`click`, `httpx`, `jinja2`, `rich`).
- **No persistence.** Phases 0–4 stay in-memory. SQLite or
  similar is a future spec.
- **No new sources.** GitHub and Jira are the only adapters.
  Project Boards, commit messages, changelog scraping all
  remain out-of-scope.
- **No work-type slicing.** Documented as a future dimension
  in the spec; not built in any phase here.
- **Flow-metrics vocabulary only** in canonical types. Source-
  specific words (PR, Issue, merge, label, changelog,
  resolution, etc.) live inside source adapters and never leak
  out.

## What this plan does NOT cover

- Per-task breakdowns. Those live in Phase 3 of the
  spec-driven workflow, written once you green-light this
  plan.
- Timeline. The phases are sized (Phase 0 = 1 day, Phase 1 =
  2–3 days, Phase 2 = 2–3 days, Phase 3 = 2 days, Phase 4 =
  4–6 days spread across one report per commit). Total: ~2
  weeks of focused work. Not a calendar commitment.
- Documentation updates. Each phase commits doc updates for
  user-visible changes (new CLI flag, new fields in JSON
  envelope, etc.) but no separate "docs sprint."

## Plan-level approval question

Before I move to Phase 3 (per-task breakdowns), the human-
reviewable question on this plan is:

**Is the phase ordering right, and are the mitigations adequate
for the named risks?**

If yes, I write the Phase-3 task list next. If you want phases
restructured, risks expanded, or mitigations strengthened,
redirect now — task breakdowns are cheaper to throw away than
implementation.
