# SPEC: flowmetrics

> Project-wide spec. Hygiene + conventions. Current build focus
> referenced in §10. Feature-level design docs live under `docs/SPEC-*.md`.

---

## 1. Objective

flowmetrics computes Vacanti-style flow metrics — cycle time, flow
efficiency, CFD, aging, Monte Carlo forecasts — from GitHub PRs and
Jira issues. It exists to give engineering teams *honest* flow signals:
the canonical data model is faithful to the source, the math is
explicit, and the reports refuse to fabricate precision.

**Users.** Engineering managers, team leads, and engineers using flow
metrics at standup and weekly retrospectives. Secondary: LLM-driven
agents querying the JSON API for insights.

**Success.** A user can point flowmetrics at a real GitHub repo or
Jira project, run a single command, and get reports (HTML / text /
JSON) whose numbers are explainable item-by-item. Reports load
sub-second from cached data; the slowest operation is the initial
fetch.

**What success is *not*.** Per-engineer scoring. Numeric targets
("FE must be > 25%"). Comparison across teams under different
contracts. Real-time / minute-granular dashboards.

---

## 2. Tech stack

| Layer | Choice | Notes |
|---|---|---|
| Language | Python ≥ 3.13 | Required by pyproject |
| Package mgmt | `uv` | `uv.lock` is the source of truth |
| Web (planned) | FastAPI + uvicorn + HTMX + Jinja | See `docs/SPEC-warehouse-app.md` |
| Data (planned) | DuckDB embedded + Parquet | Local-disk, no external services |
| HTTP | `httpx` | Both ETL fetches and tests (via `MockTransport`) |
| Charts | Vega-Lite via CDN script tags | No npm build step |
| Templating | Jinja2 | Server-side HTML |
| Linter | `ruff` | Config in `pyproject.toml` |
| Type checker | `ty` (Astral) | `pyproject.toml` `[tool.ty.environment]` |
| Tests | `pytest` + `pytest-cov` | Strict TDD; see §6 |
| Browser tests | Playwright (Chromium) | Opt-in via `browser` marker |
| Process control | `click` for CLI | `APScheduler` for cron (when warehouse lands) |

Standard library is preferred where it suffices. New dependencies are
an "Ask First" decision (§7).

---

## 3. Commands

All commands assume the working directory is the repository root.

```
# Build / install
uv sync                              # install all dependencies including dev

# Test
uv run pytest                        # unit tests; excludes integration + browser
uv run pytest -m integration         # opt-in: live GitHub API tests (needs GH token)
uv run pytest -m browser             # opt-in: Playwright tests
uv run pytest tests/test_X.py -x     # single file, stop on first failure
uv run pytest -k "test_pattern"      # by name pattern
uv run pytest --cov=src/flowmetrics  # coverage report (informational only)

# Lint + format + types
uv run ruff check                    # lint
uv run ruff check --fix              # auto-fix
uv run ruff format                   # format
uv run ty check                      # type-check

# CLI (current)
uv run flow --help                   # entrypoint
uv run flow efficiency --repo OWNER/REPO --start DATE --stop DATE
uv run flow aging --repo OWNER/REPO ...
uv run flow scatterplot --repo OWNER/REPO ...
uv run flow cfd  --repo OWNER/REPO ...
uv run flow forecast-when-done ...
uv run flow forecast-how-many ...

# Samples
uv run python scripts/generate_samples.py            # regenerate samples
uv run python scripts/generate_samples.py --offline  # use cache only

# Pre-commit (planned — see §10)
uv run pre-commit install            # one-time setup
uv run pre-commit run --all-files    # run all hooks manually
```

---

## 4. Project structure

```
src/flowmetrics/
  __init__.py             — `main` entry point
  canonical.py            — StageTransition / WorkflowDef / Stream dataclasses
  signals.py              — named signal constants (e.g. SIGNAL_GITHUB_PR_MERGED)
  stream.py               — Stream class: current_stage_at, in_flight_at
  stream_reports.py       — cfd_daily_counts, scatterplot_points, throughput, FE
  cfd.py                  — CFD computation (current; pre-stream path)
  compute.py              — flow-efficiency computation (status-duration + clustering)
  cluster.py              — event clustering for inferred active time
  aging.py                — WIP aging snapshot
  forecast.py             — Monte Carlo simulation
  percentiles.py          — empirical percentile math
  invariants.py           — runtime data invariants
  stale.py                — stale-item exclusion
  github_stitch.py        — Issue+PR cross-source stitching
  cli.py                  — Click commands
  report.py               — report dataclasses (Input/Output)
  interpretation.py       — headline / key_insight / next_actions generation
  service.py              — source adapter facade (GitHub/Jira)
  cache.py                — FileCache, cache-miss semantics
  serve.py                — `samples` HTTP server (legacy)
  sources/
    github.py             — PR_SEARCH_QUERY + lifecycle deriver
    github_issues.py      — Issue parser + closing-PR linkage
    github_labels.py      — label-driven WIP detection
    jira.py               — Jira changelog parser
    intervals.py          — bridge: WorkItem.status_intervals → StageTransition[]
  renderers/
    html_renderer.py
    text_renderer.py
    json_renderer.py
    vega_specs.py         — Vega-Lite JSON spec builders
    templates/            — Jinja2 templates for HTML reports

tests/
  test_*.py               — unit tests; ~670 currently
  fixtures/
    cache/                — pinned GitHub GraphQL response fixtures
    canonical/            — pinned canonical-data JSON fixtures
  conftest.py             — pytest fixtures + network guard

scripts/
  generate_samples.py     — produces samples/* across 8 demo repos
  screenshot_sample.sh    — Playwright-driven preview capture

docs/
  METRICS.md              — flow-efficiency calculation contract
  TUNING.md               — per-repo tuning guidance
  DECISIONS.md            — architectural trade-offs
  GLOSSARY.md             — Vacanti vocabulary
  FORECAST.md             — MCS methodology
  HOWTO.md                — user-facing usage
  SPEC-warehouse-app.md   — feature-level design for the warehouse + web app
  SPEC-github-labels.md   — label-driven workflow spec
  SPEC-issue-pr-stitching.md — Issue+PR linkage spec
  PLAN-*.md / TASKS-*.md  — execution plans (transient)

samples/
  <repo-slug>/            — generated report bundle per demo repo
  index.html              — landing page
  SAMPLES.md              — same content as index.html, github-readable

.cache/                   — local FileCache (gitignored)
reports/                  — local CLI output (gitignored)
```

---

## 5. Code style

The codebase favours **concrete clarity over abstraction**. A
representative slice:

```python
# src/flowmetrics/canonical.py
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class StageTransition:
    """One row in the canonical transitions table.

    `entered_at` is when the item entered `stage`. The exit is implicit
    in the NEXT transition for the same item; for the terminal stage,
    the exit equals the item's `completed_at`.
    """
    item_id: str
    entered_at: datetime
    stage: str
    signal: str
```

### Conventions

- **Dataclasses for data.** `@dataclass(frozen=True)` for value types.
  No ORM. No mutable shared state across modules.
- **Type hints required** on public functions. Internal helpers may
  elide them when obvious.
- **Comments are rare.** Only when the *why* is non-obvious: a hidden
  constraint, a subtle invariant, a workaround for a specific bug.
  Never explain *what* the code does — the code does that.
- **Module docstrings explain the module's purpose** in 2–4 sentences.
  Function docstrings explain non-obvious contracts (edge cases,
  invariants), not signatures.
- **No premature abstraction.** Three similar lines is better than a
  premature helper. Add the abstraction when the fourth case arrives
  and the pattern is clear.
- **No backwards-compatibility shims** for code we control. Delete
  unused code; don't leave `_legacy_` wrappers.
- **Errors at boundaries only.** Internal code trusts internal code.
  Validate at user-input and external-API boundaries.
- **No emojis in code or comments.** Reports may render emoji-like
  symbols (arrows, en-dashes) intentionally; the linter allows them.
- **Naming.** `snake_case` for functions and locals; `PascalCase` for
  types; module-level `UPPER_SNAKE` for true constants. Names should
  read; avoid `mgr`, `util`, `helper`.

### Forbidden patterns

- `try: ... except Exception: pass` — never. If you don't know what
  could go wrong, find out before catching.
- Setting `__all__` to control imports unless there's a concrete
  consumer that needs it.
- Adding optional flags to "configure" behaviour the code can decide
  for itself (see memory entry: "Don't invent flags when the gap is
  discoverability").

---

## 6. Testing strategy

**Strict TDD is the rule.** Red → green → **refactor**, in that order.
The refactor step is not optional — it's how we stop rot.

```
1. Write a failing test that specifies the new behaviour.
2. Run it. Confirm it fails for the *right* reason.
3. Write the minimum code that makes it pass.
4. Run all tests. Confirm green.
5. REFACTOR. Clean up duplication, names, structure — both production
   code AND tests. Run the full suite again. This step is where you
   pay down the debt you just took on.
6. Commit.
```

### Why the refactor step is load-bearing

Without refactor, every red→green cycle leaves a small mess: a
slightly-too-specific function, a slightly-too-clever conditional, a
test that asserts more than it should. The mess compounds. The
refactor step is when you read what you just wrote and ask: is this
the shape I'd write if I were starting from scratch?

If you don't have time to refactor, you don't have time to ship the
feature. Open a follow-up task and downgrade scope; never skip
refactor to "land it now."

### The test pyramid: unit / component / e2e

Three categories, narrowest to widest scope. Each asserts at the
level appropriate to its concern. **A test at the wrong level is a
test you can't trust.**

| Type | Location | Marker | Scope | Asserts |
|---|---|---|---|---|
| **Unit** | `tests/test_*.py` | (default) | One function or dataclass, no IO | Pure logic. Edge cases. Math. Predicate parsing. Canonical-data invariants. |
| **Component** | `tests/test_*_component.py` | (default) | One layer in isolation (Jinja template + fake data, one FastAPI route with stubbed DB, one Vega-Lite spec builder) | The layer's contract with its callers — input → output shape. No browser. |
| **E2E** | `tests/test_*_e2e.py` | `@pytest.mark.e2e` | Playwright against the real running app with real Parquet + DuckDB | What the user actually sees and can do. Data values, axis labels, zoom/reset/filter interactions. |

`@pytest.mark.integration` is a cross-cutting marker for tests that
hit live external APIs (GitHub, Jira). It can apply to any of the
three levels but is opt-in by default (the unit test suite never
touches the network — `conftest.py` enforces this).

Pyramid shape: **many unit, fewer component, few e2e**. Each level is
slower and harder to write than the one above; investing in the
upper levels keeps the suite fast. But the bottom level (e2e) is the
*only* evidence for "the user can use this." Skipping it doesn't make
the bug go away; it makes the bug invisible.

### The credibility rule: passing tests must match reality

**If a test passes and the user-visible behaviour is broken, the
test was wrong — not the bug, the test.** Fix the test before
claiming the bug is fixed. A false-positive test is worse than no
test, because it actively trains us to stop checking.

This has happened in this project (the zoom regression earlier this
year: spec-shape tests confirmed the Vega-Lite JSON had
`bind:scales`, but the actual chart didn't zoom because
`view.fill: null` left no event-catcher rect). The right reaction
wasn't "the test was checking the wrong field"; it was "the test was
checking at the wrong *level*." The fix was promoting the assertion
to e2e: Playwright drives a real drag against a real rendered
chart and asserts the visible x-domain actually changed.

**Reporting rule.** When claiming a user-visible feature works, cite
evidence at the right level:

- Logic correctness → unit test name.
- Layer contract → component test name.
- **User-visible behaviour → e2e test name OR observed in browser
  with screenshot.** Never claim a UI feature works based only on
  unit/component evidence. Open the page; assert what the human sees.

**The anti-pattern this prevents.** Reporting "the chart renders
fine, tests pass" when the user can open the page and see it
doesn't. That destroys the trust contract: the user has to start
double-checking every claim, which turns every iteration into a loop
of "go satisfy expectations, not implementation." The pyramid + the
credibility rule together are the cheapest way to never enter that
loop.

### Practical consequence for slice acceptance

Each slice in §10 (warehouse-app build) lands a slice-shaped e2e
test in `tests/test_slice<N>_e2e.py`. The slice is not done until
that e2e test exists, asserts what the slice promised, and passes.
Unit and component tests fill in below; the e2e test is the
acceptance gate.

### Test quality bar

- One concept per test. If the test name needs `and`, split it.
- Names describe behaviour, not implementation:
  `test_completed_issue_records_terminal_resolved_interval` not
  `test_jira_parser_returns_5_items`.
- Tests use small, hand-built inputs OR named real fixtures —
  never large opaque blobs.
- Assertions specify *what* matters: prefer `assert "Resolved" in stages`
  over `assert len(stages) == 6`. The first survives intentional
  growth; the second doesn't.
- When a test breaks during refactor, fix the test if it was testing
  implementation detail; fix the code if the test was testing
  behaviour. The distinction matters.

### UI tests verify what the user sees, not what the code emits

This deserves its own rule because it's the most common failure mode
of front-end tests, and it's load-bearing on the whole "human can use
this" success criterion.

A test that asserts a `<div id="chart">` exists tells you the page
*tried* to render a chart. It tells you nothing about whether the
chart shows the right data, has correct axes, lets the user zoom, or
resets cleanly. The first time the code emits the div but Vega-Lite
silently fails to draw inside it, the test still passes — and the
user sees a blank rectangle.

**The rule.** UI tests must drive real interaction (Playwright) and
assert real rendered state:

```python
# BAD — tests the DOM, not the user's experience
assert '<div id="scatterplot">' in html
assert 'scatterplot.html' in href

# GOOD — drives interaction, asserts what the user can see
page.goto(report_url)
expect(page.locator(".vega-embed svg")).to_be_visible()
expect(page.locator(".vega-embed")).to_contain_text("Cycle time (days)")
# Specific data is rendered:
expect(page.locator("svg .mark-point").nth(0)).to_have_attribute(
    "aria-label", lambda v: "May 1" in v and "12.4 days" in v,
)
# Interactivity works:
page.locator(".vega-embed svg").drag_to(...)  # zoom via wheel + drag
expect(page.locator(".x-axis")).to_have_text_matching(r"May\s+0[3-7]")
page.dblclick(".vega-embed svg")  # reset
expect(page.locator(".x-axis")).to_have_text_matching(r"Apr|May")  # back to full range
```

**The promotion.** When fixing a regression that "the chart was empty
but the test passed," the right reaction is *not* "add a screenshot
diff." It is "the test should have asserted the data, the axis
labels, and the interactivity. Rewrite it that way."

**Generalises beyond charts.** Same rule for text reports:

```python
# BAD
assert "Flow Efficiency" in stdout
# GOOD
assert "3.8% flow efficiency across 43 completed items" in stdout
assert "median 1.8d, P85 13.3d" in stdout
```

Same rule for JSON responses:

```python
# BAD
assert response.status_code == 200
assert "headline" in response.json()
# GOOD — the headline is the contract; assert the human-readable string
assert response.json()["headline"].startswith("85% confident at least")
```

The bias is always toward: **what would a human checking this answer
verify?** Test for that.

### Coverage

No hard numeric floor. `pytest --cov` is available for investigation,
not as a gate. Coverage is a leading indicator that's easy to game;
TDD-with-refactor is the real signal.

---

## 7. Boundaries

### Always

- **TDD with refactor.** Red test → green code → refactor — in every
  change, including one-line ones. Memory entry: "Strict TDD: test
  first, always."
- **Run `uv run pytest` and `uv run ruff check` before any commit.**
  Pre-commit (§10) automates this; until then it's manual.
- **Match existing patterns.** Look at the surrounding module before
  introducing a new shape. Three identical patterns in the codebase
  is not coincidence; it's the convention.
- **Update tests when refactoring.** Tests are code too; the refactor
  step applies to them.
- **Validate at boundaries.** Source adapters (`sources/*`),
  user-facing CLI args, and HTTP endpoints are where input checks
  live. Internal helpers trust their callers.
- **Use the canonical model.** Anything operating on "work" should go
  through `StageTransition` / `WorkItem` / `Stream` — not raw GraphQL
  shapes or Jira JSON.
- **Convert relative dates to absolute when persisting.** Memory
  entries already specify this for memory storage; same rule for
  artifacts in the repo.

### Ask first

- **Adding a dependency to `pyproject.toml`.** Cost is real (more to
  audit, more to upgrade, more to break). Stdlib alternatives first.
- **Changing `PR_SEARCH_QUERY` or `JIRA_SEARCH_QUERY`.** Breaks the
  fixture cache; needs a migration (see commit history for the
  `state` field fix as the pattern).
- **Modifying canonical data shapes** (`StageTransition`, `WorkItem`,
  `Stream`). Every report depends on these; changes ripple.
- **Removing or skipping a failing test.** Investigate why it's
  failing; the test is usually right.
- **Adding a new CLI flag.** Memory entry: don't invent flags when
  the real gap is documentation/discoverability.
- **`git push`, opening or merging PRs, `git rebase`.** Default to
  asking; the user pushes their own commits in this repo.
- **Schema-breaking changes** to anything under `samples/`,
  `tests/fixtures/`, or (when it exists) the Parquet warehouse.
- **Touching `docs/SPEC-warehouse-app.md`** — that's the active
  design doc and changes there imply a decision.

### Never

- **Skip the refactor step in TDD.** Even when "it works."
- **Commit secrets.** `.env`, tokens, credentials, `.cache/` —
  gitignored for a reason. Never override.
- **`git commit --no-verify` / `--no-gpg-sign`** unless the user
  explicitly asks. Hook failures mean a real issue; fix the issue.
- **`git push --force` to `main`.** The branch is protected by
  convention.
- **`git reset --hard` / `git checkout .`** on unsaved work without
  explicit confirmation. The "this looks unrelated" file might be
  the user's in-flight change.
- **Delete tests because they're "noisy" or "flaky".** Flake means
  there's a real race; investigate.
- **Claim a user-visible feature works based only on unit or
  component test evidence.** Cite an e2e test name, or open the page
  and report what you actually saw. "The tests pass" is not a status
  report for UI work — it's an invitation for the user to verify and
  find you wrong. See §6 "credibility rule."
- **Add CI workflows** without explicit decision (current state per
  §10: pre-commit only, no GH Actions).
- **Add code without a corresponding test.** Even refactors need a
  green test suite at every step.
- **Introduce abstractions for hypothetical futures.** Add when the
  fourth caller arrives, not before.

---

## 8. Memory-encoded conventions

These are already saved as memory entries and act with full force in
this project. Listed here so the spec is self-contained:

| Topic | Rule |
|---|---|
| TDD | Red → green → refactor, always. Test-second changes are rejected. |
| WIP definition | Per-invocation via `--wip-labels` only; no YAML config; order = most progress wins. |
| Aging percentiles | Empirical, not Monte Carlo simulated. Say so explicitly in user-facing text. |
| Flag invention | When the gap is discoverability, fix docs/visibility first. Don't add new surface. |

---

## 9. Success criteria

**The primary acceptance test is: a human can use the artifact to
make a faster or safer decision than they could without it.** Not
coverage, not architectural purity, not "the layer is done."

For a code change to satisfy the spec, the following must all be
true:

1. **A human can see the resulting behaviour, and a test asserts
   what the human sees** — not what the code emits internally. For UI
   work: a Playwright test drives real interaction and asserts the
   rendered axes/data/zoom/reset. For CLI / API: tests assert the
   human-readable strings, not just status codes or response shape.
   See §6 "UI tests verify what the user sees" for the rule.
   "It compiles and the layer is correct" is not enough; neither is
   "the div exists."
2. A failing test was written first and exists in the diff.
3. The test fails before the production change is applied.
4. The full suite (`uv run pytest`) passes after.
5. `uv run ruff check` is clean.
6. `uv run ty check` is clean (or the type-check failure is explained
   in the commit message).
7. The refactor step happened — duplication is gone, names are clear,
   the diff reads like the shape you'd write from scratch.
8. The commit message names *why*, not just *what*.

Criterion #1 is the one most often violated by good engineers
building good infrastructure. The other criteria gate quality; #1
gates *relevance*.

---

## 10. Current build focus

The active feature is the warehouse-backed web app. See
`docs/SPEC-warehouse-app.md` for the v0 feature spec.

### Build principle: vertical slices, not bottom-up layers

Every milestone ends with **a working browser page a human can use
to make a decision**. The "perfect data layer first, UI on top
later" sequencing is explicitly rejected: it produces nothing
visible until late in the project and trains us to build
abstractions without consumers.

Each slice exercises the full stack at narrow scope (source → ETL →
Parquet → DuckDB → API → HTML), and widens from there.

### Slices (high-level)

Aligned to `docs/ideas/flowmetrics-single-operator-multi-instance.md`.

1. **`flow materialise` writes Parquet.** CLI command; external cron
   invokes it. No web yet.
2. **`flow serve` one chart with port + password gate.** Single
   dashboard page, one scatterplot, `--host` / `--port` flags.
   `--host != 127.0.0.1` requires `--password`.
3. **Window controls + aging section.** Same page, anchored sections.
4. **Team filter + forecast + CFD.** Metadata extraction in ETL;
   filter widget driven by `/api/internal/dimensions`.
5. **Jira source on the same UI.** Cross-source stitching works.
6. **Contract switcher + editor.** YAML textarea, validate, refresh
   button invokes `flow materialise` subprocess.
7. **MCP server.** `flow mcp --data-dir …` exposes tools, resources,
   prompts. Configured in Claude Desktop.
8. **Multi-instance + hardening.** `--data-dir`, `--contracts-dir`,
   CSRF, CSP, token-redaction, daily backup script.

Detailed acceptance criteria per slice live in
`docs/SPEC-warehouse-app.md` §15.

### Within-slice work order

1. Walk the slice as a user — describe the click path before coding.
2. Stub the page first — Jinja template with fake data.
3. Wire the runtime endpoint against fake/synthetic Parquet.
4. Wire the ETL to produce real Parquet.
5. Replace fakes with real data top-to-bottom.
6. Refactor (the TDD refactor step, applied to the slice).

This keeps the human-facing artifact in front of the technical work,
not behind it.

### Pre-commit hooks (planned for v1)

The CI-light approach for v1: pre-commit hooks instead of GitHub
Actions. Configure `.pre-commit-config.yaml` to run:

- `ruff check` (with `--fix`)
- `ruff format`
- `pytest` (default suite — fast, no network, no browser)
- `ty check`

Hooks block the commit on failure. No `--no-verify`. GH Actions can
come later if the project gains external contributors.

---

## 11. Open questions

Tracked as task items. The big ones blocking further detail:

- **OIDC IdP shape.** Generic OIDC vs ship configs for specific
  providers (Okta, Google, Azure AD, Authentik). See
  `SPEC-warehouse-app.md` §13.
- **First-class agent prompts.** Specific use cases to validate the
  JSON API shape against.
- **MCS scope cardinality cap.** When a high-cardinality
  `group_by initiative` is requested, pre-compute or compute-on-demand?

---

**End of v0.** Edits welcome — this doc is meant to be lived in, not
written once. If a rule here is being broken often, the rule is
probably wrong; fix the spec before adding exceptions.
