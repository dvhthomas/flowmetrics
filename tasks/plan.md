# Plans: Portability & Web Contract Builder

Two independent plans, each broken into vertical slices that ship value
on their own. Save artifacts as PRs that can be reviewed and merged in
slice order; nothing in one plan blocks the other.

Working assumptions (call out anything that's wrong):

- **Cross-platform scope** = macOS, Linux, Windows.
- **Containers** are included as an alternate path (one Dockerfile + one
  compose recipe + one GitHub Actions example), not the primary path.
- **Backup target** defaults to local-disk `rsync`/`restic` with one
  cloud (S3-compatible) example; not S3-only.
- **Auth for the web contract builder** rides on the existing
  `--password` (HTTP Basic via `--host 0.0.0.0`); localhost-bound use
  stays unauthenticated.
- **Stage discovery** in the builder uses a *probe materialise* — small
  bounded fetch that returns known stages from the source — with
  manual override always available.

---

## Plan A: Portability — automate, back up, restore on any OS

### Goal

A non-developer on macOS, Linux, or Windows can:

1. Install flowmetrics.
2. Drop a workflow YAML into `contracts/`.
3. Schedule a daily materialise via their OS's native scheduler **or** a
   container.
4. Back up the warehouse to disk or object storage.
5. Restore from that backup onto a fresh machine and serve unchanged.

### Why now

The materialise CLI is already cron-friendly (exit codes, atomic
writes, manifests). The gaps are surface-level: one Windows subprocess
bug, missing schedule templates for each OS, no backup story, and ops
docs scattered across HOWTO + SPEC. Closing them is mostly docs +
scripts + one platform-fix; ~1 week of focused work.

### Dependency graph

```
Slice A1 ──┐
           ▼
Slice A2 ──→ Slice A3 ──→ Slice A5
           ▼               (docs)
Slice A4 ──┘
```

`A1` (Windows + cross-platform fixes) unblocks anything that runs on
Windows. `A2` (scheduler templates) and `A4` (container) each ship a
working automation path on their own. `A3` (backup/restore) only needs
A1. `A5` consolidates docs once the recipes exist.

### Slices

#### A1 — Cross-platform compatibility fixes

**One PR. Unblocks Windows users.**

- Patch `app.py` browser-triggered backfill: use
  `start_new_session=True` on POSIX and
  `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` on Windows. Single
  branch on `os.name`. Add a unit test that exercises both branches via
  monkey-patched `os.name` (mock the Popen call; assert kwargs).
- Replace POSIX-only debugging hints in CLI error messages
  (`lsof -ti:PORT`, `kill …`) with a cross-platform block: POSIX hint
  on POSIX, Windows hint (`netstat -ano | findstr :PORT`,
  `taskkill /F /PID PID`) on Windows. Same `os.name` check.
- Document that `flow serve` and `flow materialise` both work on
  Windows by exercising the existing test suite under `windows-latest`
  in CI (add a Windows job to `.github/workflows/test.yml`; mark any
  CI-only-broken tests `@pytest.mark.skipif(os.name == 'nt')` with a
  TODO link to a follow-up issue).

**Files touched**: `src/flowmetrics/app.py`, `src/flowmetrics/cli.py`,
`src/flowmetrics/serve.py`, new `tests/test_cross_platform_subprocess.py`,
`.github/workflows/test.yml`.

**Acceptance**

- `flow serve` + `flow materialise` complete without error on
  `windows-latest` in CI.
- Browser-triggered backfill returns the expected JSON status
  transitions on Windows (mocked subprocess test).
- Port-busy error message branches by OS; both branches asserted by
  unit test.

**Verification**

- New unit tests pass locally (`uv run pytest tests/test_cross_platform_subprocess.py`).
- CI green on the new windows-latest matrix entry.
- Manual: run `flow serve` on a Windows VM, hit `/data-source`,
  trigger a backfill — status file transitions running → done.

#### A2 — Native scheduler recipes (cron / launchd / Task Scheduler)

**One PR. Ships a working scheduled-ingest on every supported OS.**

- Add `scripts/scheduling/` with three minimal, parameterised templates:
  - `linux-systemd/flowmetrics-materialise.service` +
    `flowmetrics-materialise.timer` (daily, 02:30 local).
  - `linux-cron/crontab.sample` (daily, same time; safe `PATH` + `cd`
    preamble).
  - `macos-launchd/com.flowmetrics.materialise.plist`
    (`StartCalendarInterval` daily 02:30; `StandardErrorPath`
    points at a log file).
  - `windows-task-scheduler/flowmetrics-materialise.xml` (importable
    via `schtasks /Create /XML`) + a one-page README on what to edit.
- Each template uses environment-variable substitution
  (`FLOWMETRICS_HOME`, `FLOWMETRICS_VENV`, `FLOWMETRICS_WORKFLOW`) so
  copying + setting two vars is the whole install.
- Add a "one liner" wrapper script per OS that materialises every YAML
  in `contracts/` so the scheduler only fires one command
  (`scripts/scheduling/materialise-all.sh` + `.ps1`). Wrapper iterates,
  logs successes/failures to a single JSON manifest per day, exits
  non-zero only if every contract failed (so monitoring alerts
  meaningfully).
- README per directory explaining install steps, log inspection, and
  how to dry-run.

**Files touched**: new `scripts/scheduling/**`, no source code.

**Acceptance**

- Each template runs successfully on its native OS using the demo
  `astral-uv-week` contract (manual verification documented in the
  PR description).
- Wrapper script writes a `_status/daily-{date}.json` with per-contract
  outcomes and exits 0 when at least one contract succeeded.
- Templates parameterised with the same three env vars; no
  hard-coded paths.

**Verification**

- Linux: install timer in a Docker container running systemd, advance
  the clock, confirm the run fired and produced fresh Parquet.
- macOS: load plist via `launchctl bootstrap gui/$UID`, trigger via
  `launchctl kickstart`, confirm log file populated.
- Windows: import XML via `schtasks /Create /XML`, trigger via
  `schtasks /Run`, confirm warehouse updated.

#### A3 — Backup & restore (`flow backup` / `flow restore`)

**One PR. Single command for warehouse portability.**

- Add `flow backup --data-dir DATA --output PATH [--include-cache]`.
  Output is a single timestamped `.tar.zst` (zstd: fast, well-supported).
  Contents: the entire warehouse (`work_items/`, `transitions/`,
  `runs/`), plus a `flowmetrics-backup.json` header with:
  - schema version (`flowmetrics.backup.v1`),
  - flowmetrics version,
  - DuckDB version (Parquet writer),
  - source contract names + sizes,
  - SHA-256 of every file for integrity.
  Cache is excluded by default (re-fetchable); `--include-cache` flips it.
- Add `flow restore --input PATH --data-dir DATA [--force]`. Verifies
  the header schema + checksums before extracting; refuses to overwrite
  a non-empty `--data-dir` unless `--force`.
- `flow backup --target s3://bucket/prefix` writes the same tarball
  via `boto3` when present, with `--profile NAME` honoring the AWS
  shared-credentials file. Use `botocore`'s standard env vars; never
  bake creds into config.
- Add scheduler templates in `scripts/scheduling/backup/` for each OS,
  running `flow backup` after the daily materialise.
- Unit tests: round-trip a tiny synthetic warehouse — backup → restore
  → DuckDB read returns identical rows. Test that restore refuses to
  overwrite non-empty dir without `--force`. Test that a corrupted
  tarball (mutated bytes) fails fast with a clear error.

**Files touched**: new `src/flowmetrics/backup.py`, `src/flowmetrics/cli.py`
(2 new commands), `tests/test_backup_restore.py`, scheduler templates.

**Acceptance**

- A backup of a 100MB warehouse + restore on a fresh `--data-dir`
  produces a byte-identical (per checksum) DuckDB read.
- S3 mode actually uploads when `boto3` is installed and credentials
  are available; fails with a clear "boto3 not installed" message
  otherwise.
- Corrupted tarball restore fails before extraction with an
  actionable error.

**Verification**

- `uv run pytest tests/test_backup_restore.py` — round-trip tests pass.
- Manual S3 round-trip against MinIO (documented in PR description).
- Manual full-disk recovery scenario: corrupt `data/`, restore from
  yesterday's tarball, serve, charts render unchanged.

#### A4 — Container path (Dockerfile + compose + GH Actions CronJob)

**One PR. Alternate automation path; ops-team-friendly.**

- `Dockerfile` (multi-stage: `uv sync --frozen` in builder, slim runtime
  with only the venv + the package). Single image runs either
  `flow materialise` or `flow serve` based on `CMD`. Image labels:
  `org.opencontainers.image.source`, `…version`.
- `compose.yml`: two services — `materialise` (one-shot, run via
  `docker compose run`) and `serve` (long-running, port 8000, bind-mounts
  `./contracts` and `./data`). Documented as the "I just want it
  running" path.
- `.github/workflows/materialise.yml`: scheduled GH Actions workflow
  that runs `flow materialise` against contracts committed in the repo
  (uses repo as the source of truth for YAML; warehouse persists to
  a GH cache / artifact). Demonstrates the cloud-native pattern.
- Test job in CI that builds the image and runs
  `docker run --rm flowmetrics flow --help` — catches missing dependencies
  in the slim base.

**Files touched**: new `Dockerfile`, new `compose.yml`,
new `.github/workflows/materialise.yml`, CI updates.

**Acceptance**

- Image is < 250 MB.
- `docker compose up serve` brings up the dashboard against a
  bind-mounted `data/` directory.
- The GH Actions workflow runs on a manual trigger end-to-end against a
  sample contract.

**Verification**

- `docker build .` + `docker run` health-checks in CI.
- Manual: `docker compose up serve`, open `http://localhost:8000`,
  verify a tile renders.
- Manual: `gh workflow run materialise.yml`, inspect logs + the
  saved artifact.

#### A5 — Ops guide on GitHub Pages

**One PR. Consolidates Slices A1–A4 into a coherent doc.**

- New `docs/OPERATIONS.md`, linked from the README's "Documentation"
  list. Sections:
  1. *Two automation paths* — native scheduler vs. container; picker
     based on the reader's situation (one server vs. fleet, prefer
     systemd vs. K8s).
  2. *Cross-platform install* — three short paths (macOS/Linux/Windows),
     each ending with "you should now be able to run `flow --help`".
  3. *Daily ingest* — one subsection per OS, each linking to the
     templates in `scripts/scheduling/`.
  4. *Backup & restore* — `flow backup` + `flow restore`, the on-disk
     layout, recovery scenarios (lost warehouse, lost machine,
     migrating between machines).
  5. *Containers* — Docker + compose + GH Actions, copy-pasteable.
  6. *Troubleshooting* — "warehouse won't read after upgrade",
     "stale lock file", "subprocess hangs", with the fix command.
- Update `docs/HOWTO.md`: prune cron / backup content out (it's now in
  OPERATIONS); leave HOWTO as the *getting-started* doc.
- Update README's "Documentation" section to point at the new page.
- Verify Jekyll renders the new page and its internal links resolve.

**Files touched**: `docs/OPERATIONS.md`, `docs/HOWTO.md`, `README.md`.

**Acceptance**

- A new reader following only `docs/OPERATIONS.md` can stand up a
  scheduled materialise on each OS.
- Internal markdown links resolve on github.com AND on the published
  Pages site (`jekyll-relative-links` handles the rewrite — verify
  visually after publish).
- README "Documentation" lists the new doc.

**Verification**

- `bundle exec jekyll serve` locally; click every link in OPERATIONS.
- Push to main, wait for Pages build, click every link on the live
  site.

### Phase checkpoints (Plan A)

- **After A1**: Windows is on the support matrix. Pause for review
  before scheduling-template work — ensures the platform-fix touched
  the right places.
- **After A2 + A3**: Daily ingest + backup story exist as installable
  artifacts. Pause for review before container work — confirms native
  path is the priority.
- **After A4 + A5**: Both automation paths and docs are live. Final
  review before announcing.

---

## Plan B: Web-UI contract builder

### Goal

A user with the dashboard open can:

1. Create a new contract end-to-end without touching the filesystem.
2. Pick a source (GitHub / Jira), have the source validate the
   repo/project for them.
3. Build the workflow's `wip` / `done` stages by probing the source for
   known states (with manual override) and drag-reordering.
4. Save the result; the YAML lands in `--workflows-dir` and the new
   contract shows up in the dashboard immediately.
5. Edit an existing contract through the same UI.

### Why now

The dashboard renders contracts but treats them as read-only file-
system fixtures. SPEC §15.6 already lays out a "contract switcher +
editor" as Slice 6; this plan operationalises it without the YAML
textarea (which is friction for non-developers).

### Dependency graph

```
B1 (read API) ──┐
                ├──→ B3 (new-contract wizard) ──┐
B2 (write API) ─┘                               ├──→ B5 (edit page)
                                                │
                B4 (stage probe) ───────────────┘
```

`B1` and `B2` are the API foundation. `B3` is the first user-facing
slice (new contract). `B4` is the helper that makes the stage step
worth a UI. `B5` rounds it out with edit.

### Slices

#### B1 — Contract read API

**One PR. Exposes today's YAML through an HTTP read endpoint.**

- New endpoint `GET /api/internal/contracts` → list of `{id, label,
  source}` for every YAML under `--workflows-dir`. JSON.
- New endpoint `GET /api/internal/contracts/{id}` → full contract
  payload: parsed dataclass fields + the raw YAML text + a
  `materialise_status` block ({last_run_at, status, items}).
- Both are unauthenticated when serving on `127.0.0.1`; gated by the
  existing HTTP Basic when serving off-localhost.
- Unit tests: list returns every YAML; detail returns parsed +
  raw + status; both return 404 for unknown IDs.

**Files touched**: `src/flowmetrics/app.py`, new
`src/flowmetrics/web/api/contracts.py` (route module), tests.

**Acceptance**

- `curl http://localhost:8000/api/internal/contracts` lists the demo
  contract.
- `curl …/api/internal/contracts/astral-uv-week` returns the parsed
  fields, raw YAML, and last-run summary.
- Off-localhost calls without basic auth return 401.

**Verification**

- New unit tests pass.
- Manual `curl` round-trip against the running dev server.

#### B2 — Contract write API + server-side validation

**One PR. Round-trip via API; CLI not required.**

- `PUT /api/internal/contracts/{id}` — body is `{yaml: STRING}`.
  Validates by routing through the existing `load_contract` parser
  (no duplication). On success: writes `{id}.yaml` to `--workflows-dir`
  atomically (`tmp` → `os.replace`) and returns the parsed payload.
- `DELETE /api/internal/contracts/{id}` — removes the YAML. Refuses
  if Parquet for that contract exists unless body has
  `{purge_data: true}`. Returns 409 on conflict with a `hint` pointing
  at the purge option.
- Validation surface: `POST /api/internal/contracts/_validate` —
  takes `{yaml: STRING}`, returns `{valid: bool, errors: [{line,
  column, message}]}` without touching disk. Used live by the editor.
- Auth: same posture as B1 + a CSRF check on write methods (use
  FastAPI's middleware pattern; tie the token to the session cookie).
- Tests cover: round-trip create → list → read; validation surface
  matches CLI errors; delete refused when warehouse non-empty;
  CSRF block.

**Files touched**: `src/flowmetrics/web/api/contracts.py` (extend),
`src/flowmetrics/contract.py` (factor out a `validate_yaml_text` that
returns structured errors with line numbers), tests.

**Acceptance**

- `curl -X PUT …/contracts/foo --data '{"yaml":"contract:\n  name:
  foo\n  source: github\n  repo: owner/repo"}'` creates the file and
  returns 200 with the parsed payload.
- Invalid YAML returns 422 with line numbers in the error array.
- Delete refuses when data exists; succeeds with `purge_data: true`.

**Verification**

- Unit tests for each path.
- Manual: drive the API end-to-end with `curl`; confirm `contracts/`
  reflects writes; confirm the dashboard's workflow picker shows the
  new contract on the next page load.

#### B3 — New-contract wizard (source picker + repo/project validator)

**First user-facing slice. Source-only; stages come in B4.**

- New page `/admin/contracts/new` (FastAPI route + Jinja template).
  Three steps in a single form, no navigation between them — all
  visible at once for fast scanning:
  1. Name (slug) + label (human display).
  2. Source picker (radio: GitHub | Jira) → conditional fields:
     GitHub `repo` (owner/name); Jira `jira_url` + `jira_project`.
  3. Date window (`start`, `stop`), optional.
- On blur of the repo/project field, fires `POST
  /api/internal/contracts/_probe-source` with `{source, repo} ` (or
  `{source, jira_url, jira_project}`). Returns
  `{ok: bool, label?: str, error?: str}`. Inline check/cross.
- "Save & open" button validates the form against
  `_validate`, then `PUT`s, then redirects to the new workflow's
  dashboard.
- Empty `states:` is allowed at save time — stages are added in B4 or
  inferred at chart-render time (existing behavior).
- Add a "+ New contract" button to the workflow switcher on `/`.

**Files touched**: new
`src/flowmetrics/web/templates/contracts_new.html.jinja`,
`src/flowmetrics/web/api/contracts.py` (add `_probe-source`),
`src/flowmetrics/sources/{github,jira}.py` (export a `validate_target`
helper), template updates on the home page, tests.

**Acceptance**

- Wizard at `/admin/contracts/new` renders without an existing
  contract.
- Source probe returns a green check for `astral-sh/uv` and a red X +
  message for `does-not/exist`.
- Save creates the YAML, redirects, and the new dashboard renders.

**Verification**

- Unit test for `_probe-source` (mock the source adapter).
- E2E (Playwright): fill in form for `astral-sh/uv`, save, assert
  redirect to `/workflows/{name}` and the page returns 200.

#### B4 — Stage builder via probe materialise

**One PR. Replaces "freeform stage names" with discovery + DnD.**

- After the source step passes in B3, the wizard reveals a "Stages"
  section.
- "Discover stages" button calls a new endpoint `POST
  /api/internal/contracts/_probe-stages`. Server runs a bounded
  materialise (`--since` = last 30 days, no `--status-file`) into a
  scratch dir, reads the resulting transitions to extract distinct
  stage names, deletes the scratch dir. Returns
  `{stages: [name], hint?: str}` (the hint surfaces things like
  "no PRs in the last 30 days; widen the window").
- The UI shows three buckets (Backlog / WIP / Done) with discovered
  stages as draggable chips. Empty buckets allowed. Order = render
  order. Free-form "+ Add stage" input for custom names.
- "Save" persists via the existing `PUT` (the wizard now writes the
  full `states:` block).
- Probe results cache for 15 minutes per source so the user can iterate
  without re-paying the API call.
- Tests: stage probe returns sensible results against a recorded
  cassette; cache hits don't re-fetch; UI saves what the user dragged.

**Files touched**:
`src/flowmetrics/web/api/contracts.py` (new endpoint),
`src/flowmetrics/web/templates/contracts_new.html.jinja` (stage UI),
small JS module for drag-and-drop (vanilla — no React), tests.

**Acceptance**

- Probe on `astral-sh/uv` returns the expected GitHub PR stages
  (`Draft`, `Awaiting Review`, `Changes Requested`, `Approved`,
  `Merged`).
- The wizard saves a contract whose `states:` block matches the
  user's drag order.
- Probe failure (e.g. invalid repo at probe time) surfaces inline,
  doesn't block save with manual entry.

**Verification**

- Unit tests for the probe endpoint with mocked source.
- Playwright E2E: discover stages, drag two between buckets, save,
  read the YAML, assert order.

#### B5 — Edit existing contract

**Final slice. Closes the loop.**

- New page `/admin/contracts/{id}/edit`. Reuses the wizard template
  with a different mode flag (`mode: edit`). Pre-fills every field
  from the `GET` payload.
- Re-running the stage probe on an existing contract gives the user a
  diff: "discovered stages match your current `states:` (no change
  needed)" or "stages now include `Awaiting Review` — add to WIP?".
- "Save" routes through the same `PUT` (idempotent overwrite).
  "Delete" routes through `DELETE` + the confirmation dialog
  (the destructive action prompts for the contract name).
- Add an "Edit" link to the data-source strip in `_base.html.jinja`
  next to the workflow name.

**Files touched**: `contracts_edit.html.jinja` (or reuse-with-mode-flag
on `contracts_new.html.jinja`), `app.py` (new route), template
updates, tests.

**Acceptance**

- Editing the demo contract's label persists and reflects in the
  breadcrumb on the next page load.
- Stage diff highlights additions vs removals.
- Delete with no warehouse → succeeds; with warehouse → blocked
  until confirmed.

**Verification**

- Unit + E2E tests as above.
- Manual: rename `astral-uv-week` to `astral-uv-30d`, confirm files
  and dashboard.

### Phase checkpoints (Plan B)

- **After B1 + B2**: API round-trip works without UI. Pause for review
  to confirm the validation surface and CSRF posture before any HTML.
- **After B3**: New contracts can be created end-to-end (without
  stages). Pause for UX review on the wizard flow.
- **After B4**: Stages discoverable; the "you don't have to know
  GitHub PR labels" pitch is real. Pause to validate against a non-
  GitHub source (Jira) before edit.
- **After B5**: Plan complete. Final review.

### Non-goals (explicit)

- Multi-tenant auth, RBAC, per-user contracts. The existing single
  `--password` is the auth boundary.
- A YAML textarea editor. The structured form covers every field
  in the schema today; a raw editor is friction we don't need.
- Contract templating / cloning. Add when there's a third use case.
- Webhook-triggered re-materialise. The cron path is enough until
  someone asks.

---

## Plan C: Server-managed contract builder (v2)

### Status

Plans A + B above are shipped. This plan supersedes the v1 wizard
(`/admin/contracts/new`) with a server-managed schema, richer UI
affordances (live source-vocab probing), and a proper archive
lifecycle.

### Goal

A user with the dashboard open can:

1. Build a workflow contract through a structured, schema-validated
   form — no YAML textarea, no guessing field names.
2. Define an **ordered list of steps**, each with `name` + `wip: bool`,
   reorder them, and toggle their WIP flag inline.
3. Get **live suggestions from the source** as they build: the actual
   labels in the GitHub repo, the actual statuses in the Jira project,
   and a curated list of source-native lifecycle events ("PR opened",
   "Ready for review", "PR closed", "Issue resolved", …) so they
   don't have to invent step names.
4. Save → server stores the contract; export YAML as a downloadable
   file or copy it to the clipboard when needed (sharing, version
   control, ad-hoc CLI runs).
5. **Archive** (soft-delete) a contract instead of erasing it, and
   **restore** from the archive view.

### Working assumptions (revised after feedback)

- **Storage is server-managed; the DB row carries the YAML as text.**
  A SQLite database at `<workflows-dir>/contracts.db` is the source
  of truth. Each row is `(id, yaml, archived_at, archived_reason,
  created_at, updated_at)` — the contract body is one `yaml TEXT`
  column. The "convenience" is that the server owns the lifecycle
  (CRUD, archive, audit timestamps) without scattering files; the
  shape stays YAML-shaped so import/export is a no-op and the
  Pydantic schema is still the validator. `flow serve` and
  `flow materialise(-all)` both read from the DB. The DB sits
  *alongside* the materialise warehouse (the existing `--data-dir`),
  not inside it.
- **First-start migration.** On first server boot (or via
  `flow contracts migrate`), any `*.yaml` / `*.yml` files in the
  workflows dir are imported as rows and moved to
  `<workflows-dir>/migrated/` so the user has a rollback. This
  preserves the demo contracts in samples/ as a one-shot seed.
- **Schema change**: the per-step canonical shape changes from
  `states: {backlog: [...], wip: [...], done: [...]}` to
  `steps: [{name, wip}]` — an ordered list with a per-step boolean.
  The first non-WIP block before the first WIP step renders as
  **"Ready"** in the UI (not "Backlog" — see vocabulary note); the
  trailing non-WIP block renders as "Done".
- **Vocabulary: "Ready", not "Backlog"** in user-facing copy. "Backlog"
  implies an unbounded pile of old, un-triaged work; "Ready" means
  *items committed to be worked next*. The change cascades through
  the builder UI, the chart explanations, and the glossary.
- **Pydantic** replaces the hand-rolled validator. The parser is
  tolerant of the OLD shape during YAML import (read both shapes,
  always store / write the new one).
- **Single backend per contract.** Source stays contract-wide
  (`github` xor `jira`). Steps are source-agnostic strings; the
  contract's source field decides which probe endpoint runs.

### Dependency graph

```
C1 (DB + schema) ──→ C2 (archive) ────────────────────────→ C7 (archive page)
                ├──→ C3 (steps editor) ──┐
                ├──→ C4 (source vocab) ──┤
                │                        ├──→ C6 (export YAML)
                └──→ C5 (dry-run preview)┘
```

`C1` is foundational. `C2` enables archive lifecycle via the API.
`C3` is the first user-facing slice (new builder page). `C4` adds
smart UI affordances on top of the builder. `C5` is the dry-run
preview — fetches a capped sample and renders items per step so
the user can see whether their definition actually matches real
data. `C6` is the export-to-YAML capability. `C7` is the
archived-contracts page.

### Slices

#### C1 — SQLite store + Pydantic schema + YAML import

**Foundation. No UI change in this slice.**

- New `src/flowmetrics/contracts_db.py` with a thin SQLite store at
  `<workflows-dir>/contracts.db`. **Storage is yaml-in-a-column**
  — the DB owns lifecycle metadata, the YAML owns shape:
  ```sql
  CREATE TABLE contracts (
    id TEXT PRIMARY KEY,
    yaml TEXT NOT NULL,             -- canonical contract body
    archived_at TEXT,               -- NULL = live; ISO when archived
    archived_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  );
  CREATE INDEX contracts_archived_at_idx ON contracts(archived_at);
  ```
  No per-field columns. Listing parses the YAML to surface `source`
  / `label` (and caches the parsed view in memory keyed on
  `updated_at`); writes go through Pydantic validation and
  `emit_canonical_yaml`. Adding a future field to the Contract
  Pydantic model means zero DB migration.
- New Pydantic models in `src/flowmetrics/contract.py`:
  ```python
  class Step(BaseModel):
      name: str
      wip: bool = False
  class Contract(BaseModel):
      id: str
      source: Literal["github", "jira"]
      repo: str | None = None
      jira_url: str | None = None
      jira_project: str | None = None
      start: date | None = None
      stop: date | None = None
      label: str | None = None
      steps: list[Step] = []
  ```
  Replaces today's dataclass `Contract` + `WorkflowStates`. The
  existing `name` field gets renamed `id` in the canonical model
  (still serialises as `name:` in YAML for back-compat); call sites
  read `.id`.
- **Compatibility shim**: `Contract.states` `@property` synthesises
  the old `WorkflowStates(backlog, wip, done)` object so every CFD /
  Aging / charts/* call site stays green. `backlog` here is the
  non-WIP prefix; the property's name preserves the existing API
  even as the UI vocab moves to "Ready".
- **YAML I/O** stays as a util — `parse_contract_text(text)` reads
  both old and new shapes; `emit_canonical_yaml(contract)` writes
  the new shape. Both live in `contract.py`, both used by the
  import/export endpoints later.
- **Migration on first server start**: `flow serve` calls
  `contracts_db.ensure_initialized(workflows_dir)`:
  1. Create the DB if missing.
  2. Scan `<workflows-dir>/*.yaml,yml` (top-level only).
  3. For each YAML, parse → upsert into the DB.
  4. Move the processed YAML to `<workflows-dir>/migrated/`
     (preserves rollback path; never deletes).
  5. Echo a one-line summary to stderr.
- Same migration runs from `flow contracts migrate` as a manual
  trigger for cron-style installs.
- `flow materialise(-all)` reads from the DB. Removed: directory
  iteration. Added: an optional `--from-yaml PATH` flag for one-off
  contracts not yet in the DB (used by the test suite + ad-hoc).

**Files touched**: new `src/flowmetrics/contracts_db.py`,
`src/flowmetrics/contract.py` (rewrite), `src/flowmetrics/cli.py`
(materialise paths), `src/flowmetrics/app.py` (CRUD endpoints now
hit the DB).

**Acceptance**

- A fresh `<workflows-dir>/` with three YAMLs (the demo set) →
  start `flow serve` → DB now holds three rows; YAMLs are under
  `migrated/`; `GET /api/internal/contracts` returns the three.
- The full existing test suite passes — the test helpers (which
  inject YAMLs into a temp workflows-dir) now exercise the import
  path implicitly. Tests that need a contract pre-seeded use the
  PUT API as today.
- `flow materialise apache-cassandra-week` succeeds against a DB
  freshly seeded from the existing demo YAML.

**Verification**

- `uv run pytest -q` — every test green.
- Manual: archive the existing `/tmp/slice2-view/contracts/`,
  re-start `flow serve`, watch the YAMLs migrate to the DB. Open
  the dashboard, click each workflow, charts render identically.

#### C2 — Archive / restore lifecycle (server endpoints)

**One PR. No UI surface; API + materialise behaviour only.**

- `POST /api/internal/contracts/{id}/archive` — sets
  `archived_at = NOW()`. Idempotent. Refuses if Parquet exists for
  this contract unless body has `{"force": true}` (the warehouse is
  fine to keep; the archive doesn't touch data).
- `POST /api/internal/contracts/{id}/restore` — sets
  `archived_at = NULL`. Refuses if a live contract with the same id
  exists (would shadow).
- `GET /api/internal/contracts` keeps `WHERE archived_at IS NULL`;
  `?include_archived=true` opens it up. Detail endpoint surfaces
  `archived: true|false` on every response.
- `DELETE /api/internal/contracts/{id}` becomes **hard delete** —
  requires `archived_at IS NOT NULL`. Returns 409 with the hint
  "archive this contract first" when called on a live row.
- `flow materialise(-all)` adds `WHERE archived_at IS NULL` to its
  fetch. Archived contracts are invisible to the cron path.
- Audit: archive sets `archived_at` from server clock. Optional
  reason in the request body lands in a new `archived_reason TEXT`
  column (NULL when omitted).

**Files touched**: `src/flowmetrics/contracts_db.py` (add the
queries), `src/flowmetrics/app.py` (endpoints), tests
(`test_contracts_archive.py`).

**Acceptance**

- curl-driven: create → archive → list excludes it → list with
  `?include_archived=true` includes it with `archived: true` →
  restore → list includes it → archive again → hard-delete →
  list excludes it permanently.
- `flow materialise-all` skips archived contracts (verified by
  archiving one of the demo contracts, running the daily
  wrapper, asserting the manifest has only the others).

**Verification**

- `tests/test_contracts_archive.py` covers every path above.
- Manual: archive `apache-cassandra-week` via curl, confirm the
  dashboard's home page drops it; restore, confirm it returns.

#### C3 — Steps editor (replaces v1 wizard)

**First user-facing slice. Vocabulary refresh + editor rewrite.**

- New `/admin/contracts/new` (overwrites the v1 wizard). Same
  template hosts `/admin/contracts/{id}/edit` via the
  `wizard_mode` flag.
- Identity + Source fieldsets stay (B3 carried them forward).
- The **Stages** fieldset (three buckets, B4) is REPLACED with a
  **Steps** editor:
  - Ordered list rendered as rows. Each row carries a
    text input (step name), a WIP checkbox, ↑ / ↓ reorder buttons,
    and a delete (×) button.
  - "+ Add step" at the bottom appends an empty row.
  - Each row also shows an **auto-derived category badge** —
    "READY" for non-WIP rows before the first WIP, "WIP" for
    `wip: true` rows, "DONE" for non-WIP rows after the last WIP.
    The badge updates as the user toggles WIP / reorders. The badge
    is read-only — categories are *consequences* of the WIP flags
    + step order, not a separate input.
- Vocabulary cascade: every "Backlog" in user-facing strings
  becomes "Ready". CHART explanations under aging /
  cycle-time / cfd detail pages get the same update. Glossary
  doc gains a Ready ↔ Backlog disambiguation note.
- Save flow: client serialises the rows into a `steps:` list,
  PUTs to `/api/internal/contracts/{id}` (DB write, not YAML).
  Same redirect contract.
- Delete-confirmation prompt on the edit page now calls
  `/archive` first (NOT hard-delete); confirmation copy is
  softer ("archive" instead of "delete").

**Files touched**: rewrite `contracts_new.html.jinja`,
update `cycle_time_detail.html.jinja` +
`aging_detail.html.jinja` + `cfd_detail.html.jinja` (vocabulary
update), `docs/GLOSSARY.md` (Ready ↔ Backlog note),
`tests/test_contract_wizard.py` + `test_stage_builder.py`
(update assertions).

**Acceptance**

- Wizard renders with the Steps editor (not the three buckets).
- Reorder: ↑ / ↓ buttons swap adjacent rows; category badges
  recompute.
- Save writes a contract whose `steps:` list matches the row
  order. Reload pre-fills with the same order.
- No "Backlog" anywhere in the user-facing surface; "Ready"
  appears as the auto-derived badge.
- Delete button shows "Archive" copy; confirmation routes through
  `/archive`.

**Verification**

- Updated `tests/test_contract_wizard.py`.
- Playwright E2E: build a 5-step workflow, mark 2 rows WIP in the
  middle, save, assert the dashboard renders with the new shape.
- Manual: confirm "Ready" appears (not "Backlog") on the
  builder + on the cycle-time / aging / CFD detail pages.

#### C4 — Source vocab probe (live labels / statuses / lifecycle events)

**Single concentrated UX slice. Makes the builder feel built-for-the-source.**

- Replaces the existing `_probe-stages` endpoint with a richer
  `POST /api/internal/contracts/_probe-source-vocab`. Body:
  ```json
  { "source": "github", "repo": "astral-sh/uv" }
  // or
  { "source": "jira", "jira_url": "...", "jira_project": "BIGTOP" }
  ```
  Response:
  ```json
  {
    "labels": [...]           // GitHub: repo labels; Jira: project statuses
    "lifecycle_events": [...] // hard-coded curated list per source
    "warehouse_stages": [...] // names already observed in this workflow
  }
  ```
  - **GitHub `labels`** — `GET /repos/{owner}/{repo}/labels` (~50
    rate-limit cost; cached 15 min per repo).
  - **GitHub `lifecycle_events`** — curated constant:
    `["PR opened", "Marked ready for review", "Changes requested",
     "Review approved", "PR merged", "PR closed without merge",
     "Issue opened", "Issue closed"]`.
  - **Jira `labels`** — `GET /rest/api/3/project/{key}/statuses` →
    distinct status names (often 5–15 per project).
  - **Jira `lifecycle_events`** — curated constant:
    `["Issue created", "Assigned", "Resolved", "Reopened",
     "Closed"]`.
  - **`warehouse_stages`** — existing `_probe-stages` behaviour
    (distinct stage names already in the materialised transitions).
- UI affordance in the builder:
  - "Suggestions" panel under the Steps editor.
  - Three small subsections: **Used in your warehouse**, **Labels
    in the source**, **Standard lifecycle events**.
  - Each item is a clickable chip → appends a new step with the
    chip's name + a sensible WIP default (warehouse stages =
    `wip: true`; lifecycle "opened"/"created" = `wip: false`;
    lifecycle "closed"/"merged"/"resolved" = `wip: false`;
    intermediate review chips = `wip: true`).
- Cache: same 15-min TTL pattern as the existing
  `_probe-stages_cache`, keyed on `(source, repo / jira_url+project)`.
- Fallback: if the source probe fails (no network, 404 repo,
  rate-limited), the panel surfaces the failure inline and still
  shows the warehouse + lifecycle subsections so the user keeps
  flowing.

**Files touched**: `src/flowmetrics/app.py` (replace
`_probe-stages` with `_probe-source-vocab`),
`contracts_new.html.jinja` (Suggestions panel + chip handlers),
`tests/test_stage_builder.py` → renamed/extended.

**Acceptance**

- Probe against `astral-sh/uv` returns 50+ real repo labels in
  `labels` and the 8 curated lifecycle events.
- Probe against the Apache CASSANDRA project returns the 7
  project statuses.
- A click on any chip appends the corresponding step row with the
  right name + a defensible WIP default.
- Probe failure surfaces a single-line inline error in the panel
  while keeping the warehouse + lifecycle sections functional.

**Verification**

- Unit tests with mocked source-API responses.
- Manual: open the builder, probe the demo workflows, click a
  GitHub label chip, see it appear as a step row; save.

#### C5 — Dry-run preview (capped sample, per-step table)

**One PR. The "does my definition actually return data?" affordance.**

- New endpoint `POST /api/internal/contracts/_dry-run`. Body:
  the in-progress contract payload (same shape as `_validate`) + a
  `{since: "YYYY-MM-DD"}` cap-start date and an optional
  `items_cap: int` (default 200). Source target comes from the
  contract body — same as `_probe-source-vocab`.
- Server fetches **up to the smaller of** 200 items OR a 30-day
  window from `since`. Streams items in time order; stops at the
  first cap to bite.
- The fetch is **non-persisting**: items go through the existing
  Source adapter but bypass the materialise → Parquet path. They
  live in a process-local cache only.
- Response shape:
  ```json
  {
    "fetched_at": "2026-05-27T18:00:00Z",
    "expires_at": "2026-05-27T18:05:00Z",
    "stopped_by": "items_cap" | "time_window",
    "items_fetched": 187,
    "window": {"from": "2026-04-27", "to": "2026-05-27"},
    "per_step": [
      {"step_name": "Draft", "wip": true, "count": 23, "items": [...]},
      {"step_name": "Awaiting Review", "wip": true, "count": 41, ...},
      ...,
      {"step_name": "_unmatched", "count": 4, "items": [...]}
    ]
  }
  ```
  Each `items` array is the same row shape `work_items_table.py`
  consumes today — that component renders the result.
- Cache: in-process dict keyed on
  `(source_target_hash, since, items_cap, steps_signature)`,
  5-minute TTL. The `expires_at` in the response surfaces when the
  cache rolls. `?force=true` busts.
- Builder UI: under the Steps editor, a "Preview against live
  source" disclosure section.
  - "From" date input (default: 30 days ago).
  - "Dry run" button → spinner → table per step using the existing
    `work_items_table.html.jinja` partial.
  - Empty steps render as
    "no items matched this step in the sample window — name might
    not match the source's actual state."
  - `_unmatched` bucket surfaces items whose current state didn't
    map to any of the user's steps — a powerful "your workflow is
    missing a step" signal.
- The preview is intentionally per-step ONLY; no other charts
  (CFD, cycle-time) render here. The point is "does my definition
  match real data?", not "what does the dashboard look like?".

**Files touched**: new
`src/flowmetrics/contract_preview.py` (the bounded fetch +
in-process cache), `src/flowmetrics/app.py` (endpoint),
`contracts_new.html.jinja` (Preview panel + table rendering),
`tests/test_contract_preview.py`.

**Acceptance**

- Dry-run against `astral-sh/uv` with `since = today - 30d` and
  a 5-step definition returns counts > 0 in at least one WIP
  step.
- The `_unmatched` bucket lists items whose current state isn't
  named in the contract — proves the "warn me about gaps"
  behaviour.
- Repeating the same call within 5 min returns the same payload
  with no network hit (cache).
- `?force=true` busts the cache and re-fetches.

**Verification**

- Unit tests with mocked source-API responses; assert cap
  semantics (stop at 200 even when 30d window has more; stop at
  30d when 200 items would need more).
- Playwright E2E: build a 3-step workflow against the demo
  GitHub source, click Dry run, see counts per step.
- Manual: rename one step to a typo (`"Awating Review"`),
  re-run, watch its bucket go empty AND its items appear under
  `_unmatched`.

#### C6 — Export YAML

**One PR. Sharing affordance on the edit page.**

- `GET /api/internal/contracts/{id}/yaml` returns the row's `yaml`
  column with `Content-Type: application/x-yaml` and
  `Content-Disposition: attachment; filename="<id>.yaml"`.
- Edit page gets a "Download YAML" link + a "Copy YAML" button
  (the latter fetches the body and writes to
  `navigator.clipboard`).
- Both work on archived contracts via `?include_archived=true`
  so an ops team can re-seed a deleted contract from their
  ad-hoc export.

**Files touched**: `src/flowmetrics/app.py` (endpoint),
`contracts_new.html.jinja` (two buttons + handler),
`tests/test_contracts_api.py`.

**Acceptance**

- Downloaded YAML is byte-identical to the row's `yaml` column.
- Downloaded YAML, fed to `flow materialise --from-yaml
  /path/to/file.yaml`, succeeds.
- Copy button populates the clipboard with the same text.

#### C7 — Archived contracts page

**Final slice. Closes the lifecycle.**

- `/admin/contracts/archive` lists archived rows (id, label,
  archived_at, archive reason, source).
- Per-row actions:
  - **Restore** → POST `/restore`, refresh list.
  - **Export YAML** (link to C6 endpoint with
    `?include_archived=true`).
  - **Hard delete** → DELETE `/{id}`; confirmation prompt
    explicit about permanence + spelling out the data implications.
- Subtle link from the home page when n > 0: "View archived
  (n)" — doesn't render at all when there are no archived rows.

**Files touched**: new `contracts_archive.html.jinja`,
route in `app.py`, link on `home.html.jinja`, tests.

**Acceptance**

- Archive a contract → home grows the "View archived (1)" link.
- Archive page shows it; restore brings it back.
- Hard-delete removes the row permanently; cannot be recovered.

**Verification**

- Playwright E2E: full lifecycle — create → archive → restore →
  archive → hard-delete → assert gone everywhere (home, archive
  page, materialise-all manifest).

### Phase checkpoints (Plan C)

- **After C1**: schema + storage migration solid; existing tests
  green. Pause for review BEFORE any UI work — the data model is
  the foundation everything else builds on.
- **After C2**: archive lifecycle works through curl. Pause for
  review of the API surface before the UI lands.
- **After C3**: new builder shipped with the "Ready" vocab; old
  wizard gone. Pause for a UX review.
- **After C4**: source vocab makes the builder feel native. Pause
  to validate the chip catalogue against a real Jira instance
  (not just GitHub).
- **After C5**: dry-run answers "did I get my workflow right?"
  before the user commits. Pause to confirm the per-step table
  reads honestly against both source kinds.
- **After C6 + C7**: lifecycle complete. Final review.

### Non-goals (explicit)

- **No multi-source contracts.** Source stays contract-wide.
- **No per-revision audit history.** Single `updated_at` + a
  single archive timestamp is enough; full revision tracking is a
  future ask.
- **No drag-and-drop reorder.** ↑/↓ buttons are equivalently
  testable, accessible by default, and don't pull in a DnD lib.
- **No DB migrations framework.** The schema is small enough that
  a single `CREATE TABLE IF NOT EXISTS` + future column adds via
  ALTER TABLE (gated on PRAGMA `user_version`) is sufficient.
- **No "import multiple YAMLs at once" UI.** Import-from-YAML
  stays as the first-boot migration + the per-contract
  `flow materialise --from-yaml` path.

---

## How to use this plan

1. Pick one plan to start with (A is lower-risk, ships docs +
   automation that everything else benefits from).
2. Work one slice at a time. Each slice is a self-contained PR with
   acceptance + verification in its description.
3. At each phase checkpoint, pause for human review; do not advance.
4. If the plan goes stale (something underneath changes, scope
   shifts), update this file first, then keep going.

The accompanying [tasks/todo.md](todo.md) is the live checklist —
tick boxes there as each slice lands.
