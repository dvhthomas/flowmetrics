# Tasks: canonical work-item event stream

Status: **Draft, awaiting approval.** Per-task breakdown derived
from `docs/PLAN-issue-pr-stitching.md`. Each task is sized to a
single focused session (≤5 files), starts with a failing test,
and lists explicit acceptance + verification.

Companion to:
- `docs/SPEC-issue-pr-stitching.md` (what & why)
- `docs/PLAN-issue-pr-stitching.md` (phase ordering & risks)

## Cross-cutting rules (apply to every task)

Three constraints the user explicitly named, plus the standing
TDD discipline:

### 1. TDD with evidence

Every task starts by writing a failing test that asserts the
new behaviour. The test is committed (or visibly red in the
working tree) BEFORE the implementing code is written.
"Evidence" means a commit history of the form:

```
abc1234  add failing test: <task name>     ← red here
def5678  implement <task name>              ← green here
```

When the implementation and test land in the same commit, the
commit message MUST quote the failing-output stanza from the
pre-implementation `pytest -q` run. No exceptions.

The user has already rejected one TDD-violating change in this
session (memory:
[feedback_strict_tdd_test_first_always.md](../memory/)). The
rule stands.

### 2. Secure code

- **Never hardcode tokens, URLs with credentials, or repo
  identifiers in tests.** `GITHUB_TOKEN` and Jira credentials
  flow through env vars only. Tests use fixture cache, never
  live API.
- **GraphQL queries take user input as bound variables only,**
  never via `fmt`-style string interpolation. Pattern is
  already enforced (`client.graphql(query, variables)`); each
  new fetcher must follow it.
- **`_safe_json_for_script_tag()` is the only path** to embed
  JSON inside an HTML `<script>` tag. New chart specs must use
  it (commit `2327747` rationale).
- **No logging of raw API responses** above debug level (could
  leak issue titles / PR descriptions that are sometimes
  sensitive).

### 3. Safe API consumption — NOT chatty

Each task that adds or modifies an API-touching code path
includes:

- **Cache key derived from the full request**, identical
  across runs with same args (`FileCache.make_key(query,
  variables)`). Re-runs with the same window hit cache, not
  the network.
- **Pagination via cursor**, page size capped at 100 (GitHub
  GraphQL default; never request more than the API allows).
- **Single batched query per logical fetch**, not N+1. Loops
  that issue one query per item are bugs. The Issue fetcher
  follows the same time-window-batched pattern as the existing
  `fetch_prs_merged_in_window`.
- **Rate-limit aware retries** via the existing `GitHubClient`
  retry middleware. Never bypass it.
- **API-call-count budget** named in the task. Adding 2× the
  PR-side calls is the maximum acceptable increase. Tasks
  exceeding this must justify in the commit message.

Per-task budget cells below name the expected call counts.

### 4. High performance

- Pure-Python only. No NumPy. (Memory + spec constraint.)
- Batch CPython-level calls into stdlib C-implementations
  where they exist (`rng.choices`, `itertools.accumulate`,
  `bisect_left` — see the forecast perf work in commit
  `8c0877e` for the pattern).
- Profile before optimizing. Tasks that claim a perf
  improvement quote a before/after measurement in the commit
  message.

---

## Phase 0 — vocabulary rename (3 tasks, ~1 day)

Mechanical renames. Each task has the same shape: write a test
that exercises the NEW name (which fails because the old name is
still in use), then mechanical search-and-replace + run the
suite until green.

### Task 0.1: rename `WorkItem.merged_at` → `completed_at`

- **Subject**: rename the `merged_at` field on `WorkItem` and
  `FlowEfficiency` to `completed_at`. Update every caller.
- **TDD evidence**: new test in `tests/test_compute.py`
  asserts `WorkItem(...).completed_at` exists with the same
  value semantics as today's `merged_at` (i.e. the timestamp
  when the item exited the workflow).
- **Acceptance**: every occurrence of `merged_at` in `src/` and
  `tests/` is now `completed_at`, except inside `sources/`
  modules where it appears in source-specific contexts
  (e.g. GraphQL response key `mergedAt`).
- **Verify**: `grep -rn 'merged_at' src/ tests/` returns zero
  matches outside `sources/`. Full suite green.
- **Files**:
  - `src/flowmetrics/compute.py`
  - `src/flowmetrics/aging.py`
  - `src/flowmetrics/cfd.py`
  - `src/flowmetrics/sources/github.py`, `sources/jira.py`
    (mapping layer only)
  - tests touched downstream (`test_compute.py`,
    `test_aging.py`, etc.)
- **API budget**: zero. No new fetch paths.

### Task 0.2: rename `pr_url` → `url` on report-level dataclasses

- **Subject**: rename `AgingItem.pr_url` and
  `ScatterplotPoint.pr_url` to `url`. Update all consumers
  (renderers, templates, JSON).
- **TDD evidence**: new test asserts
  `AgingItem(item_id=..., url='https://…').url == 'https://…'`.
- **Acceptance**: `pr_url` removed from canonical types and
  every consumer. Templates reference `.url`. JSON envelope
  emits `url` (back-compat break — accepted per spec).
- **Verify**: `grep -rn 'pr_url' src/ tests/` returns zero
  matches. Full suite green. Visual: regen one sample, confirm
  hover tooltip + click-through still work.
- **Files**:
  - `src/flowmetrics/aging.py`
  - `src/flowmetrics/report.py`
  - `src/flowmetrics/renderers/json_renderer.py`
  - `src/flowmetrics/renderers/templates/aging.html.jinja`
  - `src/flowmetrics/renderers/templates/scatterplot.html.jinja`
- **API budget**: zero.

### Task 0.3: rename `_pr_url()` → `_item_url()` in GitHub source

- **Subject**: rename the helper that constructs canonical URLs
  from `(repo, number)`.
- **TDD evidence**: existing tests assert
  `WorkItem.url == 'https://github.com/.../pull/N'`; renaming
  the helper without updating callers will fail those.
- **Acceptance**: helper renamed; every call site updated.
- **Verify**: `grep -rn '_pr_url' src/` returns zero matches.
  Full suite green.
- **Files**:
  - `src/flowmetrics/sources/github.py`
- **API budget**: zero.

---

## Phase 1 — signals, canonical types, bridge (5 tasks, ~2–3 days)

### Task 1.1: signals.py — named-event constants

- **Subject**: create `src/flowmetrics/signals.py` exporting
  named constants for every recognized source event.
- **TDD evidence**: `tests/test_signals.py` asserts each named
  constant exists, is a string, and is unique. Test exists
  before the module body.
- **Acceptance**: ~15 constants exported per the spec's initial
  set (`SIGNAL_GITHUB_ISSUE_CREATED`,
  `SIGNAL_GITHUB_PR_CLOSES_ISSUE`, `SIGNAL_JIRA_RESOLVED`,
  etc.).
- **Verify**: `uv run pytest tests/test_signals.py -q` green.
- **Files**:
  - `src/flowmetrics/signals.py`
  - `tests/test_signals.py`
- **API budget**: zero.

### Task 1.2: canonical.py — `StageTransition` + `WorkflowDef`

- **Subject**: pure dataclasses for the canonical model.
  `StageTransition(item_id, entered_at, stage, signal)` and
  `WorkflowDef(stages, wip_set)`.
- **TDD evidence**: tests assert frozen-dataclass behaviour,
  construction with required fields, validation
  (`wip_set ⊆ stages`).
- **Acceptance**: types exist, are frozen, validate inputs.
  No I/O, no source coupling.
- **Verify**: `uv run pytest tests/test_canonical.py -q` green.
- **Files**:
  - `src/flowmetrics/canonical.py`
  - `tests/test_canonical.py`
- **API budget**: zero.

### Task 1.3: sources/intervals.py — bridge: rows → today's intervals

- **Subject**: pure function that takes
  `list[StageTransition]` for one item plus the team's
  `WorkflowDef`, returns the equivalent `list[StatusInterval]`
  (today's shape). This is the back-compat bridge.
- **TDD evidence**: tests assert that a hand-built sequence
  of transitions flattens to the expected intervals, including
  the tricky cases: single transition (open-ended interval),
  backward moves, identical timestamps.
- **Acceptance**: function exists; covers single-transition,
  multi-transition, backward-move cases.
- **Verify**: `uv run pytest tests/test_intervals_bridge.py
  -q` green.
- **Files**:
  - `src/flowmetrics/sources/intervals.py`
  - `tests/test_intervals_bridge.py`
- **API budget**: zero.

### Task 1.4: golden-test the bridge against existing source output

- **Subject**: for each existing source-test fixture in
  `tests/test_github*.py` and `tests/test_jira_source.py`,
  write a paired test that:
    1. Runs the source, captures its `StatusInterval`s.
    2. Translates the same fixture to `StageTransition`s by
       hand.
    3. Asserts the bridge produces byte-identical
       `StatusInterval`s.
- **TDD evidence**: tests written and red. The bridge from
  Task 1.3 is the only code that should be needed to make
  them green; no source changes yet.
- **Acceptance**: ≥3 golden tests, one per significant source
  path (`fetch_prs_merged_in_window`, `fetch_open_prs`,
  Jira `_paginated_fetch`).
- **Verify**: tests green.
- **Files**:
  - `tests/test_bridge_golden.py` (new)
- **API budget**: zero.

### Task 1.5: thread Jira source through the bridge as proof-of-concept

- **Subject**: refactor `JiraSource._paginated_fetch` to
  build `StageTransition` rows internally, then pass them
  through the bridge. External API of `fetch_completed_in_window`
  and `fetch_in_flight` is UNCHANGED.
- **TDD evidence**: existing Jira tests stay green after the
  internal refactor (no behaviour change). New test asserts
  the source emits `StageTransition` rows with the right
  `signal` constants when introspected.
- **Acceptance**: Jira source uses bridge internally; outputs
  unchanged.
- **Verify**: existing Jira suite green. New introspection
  test green.
- **Files**:
  - `src/flowmetrics/sources/jira.py`
  - `tests/test_jira_source.py` (new test only)
- **API budget**: zero (refactor only).

---

## Phase 2 — Issue fetching + closing-PR signal (4 tasks, ~2–3 days)

### Task 2.1: GraphQL query for Issues + label timelines

- **Subject**: define `ISSUE_SEARCH_QUERY` in
  `sources/github_issues.py`. Pulls issues from a date window
  WITH their `timelineItems(itemTypes: [LABELED_EVENT,
  UNLABELED_EVENT, CLOSED_EVENT])` and
  `closedByPullRequestsReferences`.
- **TDD evidence**: test asserts the query string contains
  the expected GraphQL fields. Then test asserts that a
  fixture response parses into the new
  `_issue_node_to_transitions(node, repo)` function correctly.
- **Acceptance**: query string built; parser exists.
- **Verify**: `uv run pytest tests/test_github_issues.py -q`
  green. Query passes GraphQL validation (manually test once
  against live API, log call cost).
- **Files**:
  - `src/flowmetrics/sources/github_issues.py`
  - `tests/test_github_issues.py`
- **API budget**: zero in tests (fixture only); 1 paginated
  query in production per
  `fetch_issues_with_labels(start, stop)` call.

### Task 2.2: `fetch_issues_with_labels(start, stop)` with caching + pagination

- **Subject**: the actual fetcher. Same shape as
  `fetch_prs_merged_in_window` — search by date window,
  paginate cursor, cache the full response.
- **TDD evidence**: test asserts that a cache-seeded fixture
  is read without a network call, returns the expected
  `list[WorkItem]` with `signal=SIGNAL_GITHUB_LABEL_ADDED` on
  every interval derived from a label event.
- **Acceptance**: fetcher exists; uses bound GraphQL
  variables; paginates; honours `read_only=True` for offline
  cache mode.
- **Verify**: cache-backed test green. Manual: one live call
  against `dvhthomas/kno`, response cached, second call
  reads cache.
- **Files**:
  - `src/flowmetrics/sources/github_issues.py`
  - `tests/test_github_issues.py`
- **API budget**: 1 paginated query per call; per-page cost
  ≤ existing PR-fetch cost (≤ ~5 GraphQL rate-limit points
  per page based on the timeline-event sub-selection).
- **Safety**: no string-formatted query — bound variables
  only. Cache key includes the full variable map.

### Task 2.3: `github_stitch.py` — pure stitching logic

- **Subject**: pure function `stitch(items, *, closes)` that
  takes a list of `WorkItem`s and a `closes: Mapping[str, str]`
  (closer-id → closed-id from
  `closingIssuesReferences`/`closedByPullRequestsReferences`)
  and emits a transition with
  `signal=SIGNAL_GITHUB_PR_CLOSES_ISSUE` on the closed item.
- **TDD evidence**: pure-logic tests in
  `tests/test_github_stitch.py` — Issue alone, PR alone,
  Issue + one PR, Issue + multiple PRs, PR with no parent.
- **Acceptance**: function returns the union of the input
  items with stitching transitions added; never duplicates
  items; passes through unmodified items unchanged.
- **Verify**: `uv run pytest tests/test_github_stitch.py -q`
  green.
- **Files**:
  - `src/flowmetrics/sources/github_stitch.py`
  - `tests/test_github_stitch.py`
- **API budget**: zero (pure function).

### Task 2.4: integrate Issues + stitching into `_GitHubSourceAdapter`

- **Subject**: new keyword `include_issues: bool = True` on
  `fetch_in_flight()` (matches the opt-out default). When
  set, fetch issues alongside PRs, stitch, return the unified
  list.
- **TDD evidence**: cache-backed integration test runs both
  fetchers against fixtures, asserts the stitched output
  includes the new signal on the linked Issue.
- **Acceptance**: adapter emits stitched items; back-compat
  test (without the new flag) returns today's PR-only output.
- **Verify**: integration test green. Existing GitHub source
  tests untouched.
- **Files**:
  - `src/flowmetrics/service.py`
  - `tests/test_service_github_issues.py` (new)
- **API budget**: when `include_issues=True`, 2× the PR-only
  call count (issues alongside PRs). Issues and PRs are
  independent paginated queries; no N+1.

---

## Phase 3 — Aging consumes the stream (4 tasks, ~2 days)

### Task 3.1: `--include-issues` / `--no-include-issues` CLI flags

- **Subject**: add the boolean flag to `flow metric aging`. Default
  `True` per spec; user can pass `--no-include-issues` to
  suppress.
- **TDD evidence**: CLI integration test using `click`'s
  `CliRunner` asserts that the flag is recognized, default is
  True, opt-out works.
- **Acceptance**: flag plumbed through to the adapter's
  `fetch_in_flight(include_issues=...)` call.
- **Verify**: CLI test green.
- **Files**:
  - `src/flowmetrics/cli.py`
  - `tests/test_cli_aging_issues.py` (new)
- **API budget**: zero (just plumbing).

### Task 3.2: `compute_aging()` reads `StageTransition` rows

- **Subject**: refactor `compute_aging()` to accept either
  today's `list[WorkItem]` (back-compat) OR a list of
  `(WorkItem, list[StageTransition])` tuples. Phase 4 retires
  the first variant.
- **TDD evidence**: new test asserts a stitched
  Issue+PR pair, fed as transitions, produces an `AgingItem`
  whose `age_days` measures from `min(first WIP-label-on-Issue,
  PR.completed_at)` — matching the spec's success criterion.
- **Acceptance**: `compute_aging()` handles both shapes.
- **Verify**: new test green; existing aging tests untouched.
- **Files**:
  - `src/flowmetrics/aging.py`
  - `tests/test_aging.py`
- **API budget**: zero.

### Task 3.3: end-to-end aging test with stitched Issue+PR fixture

- **Subject**: fixture-driven test that runs the full CLI
  `flow metric aging --repo X --wip-labels Y --include-issues
  --offline` against an in-memory cache containing one Issue
  + one closing PR.
- **TDD evidence**: test asserts the final HTML / JSON
  envelope carries a single stitched `AgingItem` with the
  expected fields.
- **Acceptance**: test green; no live API calls.
- **Verify**: `uv run pytest
  tests/test_aging_with_issues_e2e.py -q` green.
- **Files**:
  - `tests/test_aging_with_issues_e2e.py`
  - `tests/fixtures/cache/aging_issue_pr_pair.json` (cache)
- **API budget**: zero (offline-only test).

### Task 3.4: regen samples + visual spot-check

- **Subject**: regenerate the existing aging samples; confirm
  no regression on existing repos (Jira unchanged, GitHub
  repos with `--wip-labels` now include Issues by default).
- **TDD evidence**: not strictly TDD — this is a verification
  task. The "tests" are the visual screenshots and the
  numerical comparison.
- **Acceptance**: Cassandra aging numbers unchanged (Jira path
  untouched). GitHub aging samples may show new Issue items;
  spot-check one chart visually.
- **Verify**: regen, eyeball, commit if visually correct.
- **Files**: no code changes; sample outputs updated.
- **API budget**: full sample-generation run (~20 API calls).

---

## Phase 4 — port the remaining reports (5 tasks, ~4–6 days)

Each task migrates one report off `StatusInterval` and onto
`StageTransition` directly. The bridge stays alive until the
final task.

### Task 4.1: CFD reads `StageTransition` directly

- **Subject**: refactor `build_cfd()` to accept transitions
  + `WorkflowDef` instead of items-with-intervals.
- **TDD evidence**: new test asserts a hand-built transition
  set produces the expected per-state band widths at each
  sample date.
- **Verify**: all CFD tests green; regen Cassandra CFD,
  compare numbers (identical expected, since Jira path is
  unchanged).
- **Files**: `src/flowmetrics/cfd.py`, `tests/test_cfd.py`.
- **API budget**: zero.

### Task 4.2: Scatterplot reads `StageTransition` directly

- **Subject**: same shape as 4.1 but for the scatterplot
  data path.
- **TDD evidence**: per-item cycle-time computation now
  derives from transitions (entered-WIP → exited-WIP).
- **Files**: `src/flowmetrics/cli.py` (scatterplot build()),
  `tests/test_vega_specs.py`.
- **API budget**: zero.

### Task 4.3: Efficiency reads `StageTransition` directly

- **Subject**: `compute_pr_flow` was the most
  GitHub-PR-specific function in the codebase. Reframe it as
  "given transitions for one item, compute active-time vs
  cycle-time."
- **TDD evidence**: existing per-PR flow tests pass with the
  new transition-based input.
- **Files**: `src/flowmetrics/compute.py`,
  `tests/test_compute.py`.
- **API budget**: zero.

### Task 4.4: Forecast reads `StageTransition` directly

- **Subject**: forecast samples daily throughput. Today's
  path counts merged PRs per day; the new path counts items
  whose latest transition lands on a stage past the wip_set
  per day.
- **TDD evidence**: convergence test (analytic median
  recovery at 10K runs) still passes against the new
  source-data path.
- **Files**: `src/flowmetrics/throughput.py`,
  `src/flowmetrics/service.py`.
- **API budget**: zero.

### Task 4.5: delete `sources/intervals.py` bridge

- **Subject**: with every report migrated, the bridge is
  dead code. Delete it.
- **TDD evidence**: `grep -rn 'from .*intervals import' src/`
  returns zero matches before this task starts. Test suite
  green before and after the deletion.
- **Acceptance**: file deleted; suite green.
- **Verify**: full suite + ruff.
- **Files**:
  - `src/flowmetrics/sources/intervals.py` (deleted)
  - any callers (should be none by this point)
- **API budget**: zero.

---

## Verification per phase boundary

```bash
uv run pytest -q                  # all green
uv run ruff check src/ tests/    # clean
```

Plus the phase-specific check from
`docs/PLAN-issue-pr-stitching.md`.

## Approval question

**Are the per-task acceptance criteria + verification commands
specific enough that you (or another implementer) can run the
task without ambiguity?**

If yes, I move to Phase 4 (Implement) starting with Task 0.1
— writing the failing test first.

If you want any task reshaped (smaller scope, different
boundary, more explicit perf budget), redirect now. Tasks are
still cheap to throw away at this stage.
