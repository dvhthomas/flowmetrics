# Todo

Live checklist for the two plans in [plan.md](plan.md). Tick boxes as
each PR lands. Both plans now shipped end-to-end.

## Plan A — Portability ✅

### Slice A1: Cross-platform compatibility fixes ✅ (commit 6000b9e)

- [x] Branch `os.name` in `app.py` browser-backfill subprocess.
- [x] Add `tests/test_cross_platform_subprocess.py`.
- [x] OS-branch the `lsof`/`kill` hints in serve port-busy and
      samples-serve.
- [x] Add multi-OS CI matrix in `.github/workflows/test.yml`
      (ubuntu / macos / windows).
- [x] Cleared the 33 pre-existing ruff lints so CI starts green.

### Slice A2: Native scheduler recipes ✅ (commit bf6c858)

- [x] `flow materialise-all` wrapper command + 6 unit tests.
- [x] linux-systemd `.service` + `.timer` + README.
- [x] linux-cron `crontab.sample` + README.
- [x] macos-launchd `.plist` + README.
- [x] windows-task-scheduler `.xml` + README.
- [x] Top-level `scripts/scheduling/README.md` indexes them.

### Slice A3: `flow backup` / `flow restore` ✅ (commit 6f0db7e)

- [x] `src/flowmetrics/backup.py` — `.tar.gz` + header + SHA-256.
- [x] `flow backup` CLI command.
- [x] `flow restore` CLI command (verifies checksums before
      writing).
- [x] +10 tests: round-trip, refuse-without-force, corrupted
      tarball, tampered payload, cache exclude/include, default
      dated output.
- [x] `scripts/scheduling/backup/` wrappers (POSIX + PowerShell).

### Slice A4: Container path ✅ (commit 68e4726)

- [x] `Dockerfile` (multi-stage, non-root, slim).
- [x] `compose.yml` (serve + materialise services).
- [x] `.github/workflows/materialise.yml` (scheduled GH Actions
      ingest with artifact upload).
- [x] CI `docker` job builds + smokes `flow --help` inside the
      container.

### Slice A5: Ops guide ✅ (commit afc1143)

- [x] `docs/OPERATIONS.md` consolidating A1–A4.
- [x] README "Documentation" lists the new page.

## Plan B — Web-UI contract builder ✅

### Slice B1: Contract read API ✅ (commit 1322780)

- [x] `GET /api/internal/contracts` — list.
- [x] `GET /api/internal/contracts/{id}` — full payload (parsed +
      raw YAML + materialise status block).
- [x] Auth respects localhost-vs-network posture.
- [x] +9 unit tests; `_available_contracts` now picks up `.yml`
      alongside `.yaml`.

### Slice B2: Contract write API + validation ✅ (commit 6d38b63)

- [x] `POST /api/internal/contracts/_validate` — structured
      `{valid, errors[{message, line?, column?}]}`.
- [x] `PUT /api/internal/contracts/{id}` — atomic write.
- [x] `DELETE /api/internal/contracts/{id}` — refuses when
      Parquet exists; `?purge_data=true` wipes alongside.
- [x] CSRF guard (`X-Requested-With: fetch`) on every write.
- [x] +13 unit tests.

### Slice B3: New-contract wizard ✅ (commit eeed5ef)

- [x] Route + template `/admin/contracts/new`.
- [x] Source picker with conditional fields.
- [x] `POST /api/internal/contracts/_probe-source` (injectable
      callable; production uses httpx to HEAD the GitHub repo or
      GET the Jira project).
- [x] Save flow: validate → PUT → redirect.
- [x] "+ New workflow" CTA on `/`.
- [x] +6 unit tests; visually verified in the browser.

### Slice B4: Stage builder via probe materialise ✅ (commit 254fe39)

- [x] `POST /api/internal/contracts/_probe-stages` with 15-min
      per-target cache + `?force=true` bust.
- [x] Stages fieldset with three click-to-move buckets +
      "+ Add custom stage" free-form input.
- [x] Save persists the full `states:` block via the existing
      PUT.
- [x] +7 unit tests.

### Slice B5: Edit existing contract ✅ (commit 2b7397d)

- [x] Route + template `/admin/contracts/{id}/edit` (reuses
      wizard with `mode=edit`).
- [x] Hydrates every field from GET on load; locks the id.
- [x] Delete affordance with name-typing prompt + warehouse
      purge confirmation.
- [x] "edit" link in the data-source strip on every dashboard.
- [x] +5 unit tests.

## Plan C — Server-managed contract builder (v2) ✅ SHIPPED

All slices on `main`: C1 `660aedf`, C2 `7b8a44e`, C3 `4136ae0`,
C4 `b8ae57a`, C5 `9b7a2e6`, C6+C7 `c0830d2`. 1199 tests green.

### Slice C1: SQLite store + Pydantic schema + YAML import ✅

- [x] New `src/flowmetrics/contracts_db.py` (single `contracts`
      table at `<workflows-dir>/contracts.db`; **yaml-in-a-column**
      schema — `id, yaml, archived_at, archived_reason,
      created_at, updated_at`).
- [x] Rewrite `src/flowmetrics/contract.py` with Pydantic models;
      `name` → `id` in the canonical type (YAML key stays `name:`).
- [x] `Contract.states` `@property` compatibility shim so every CFD
      / Aging / charts call site stays green.
- [x] `parse_contract_text` reads BOTH old `states:` and new
      `steps:` YAML shapes. `emit_canonical_yaml(contract)` writes
      the new shape.
- [x] `flow serve` first-boot migration: scan workflows dir → import
      YAMLs → move to `migrated/` → echo summary. Same path
      available via `flow contracts migrate`.
- [x] `flow materialise(-all)` read from the DB. New
      `--from-yaml PATH` flag for one-off ad-hoc contracts.
- [x] CRUD endpoints in `app.py` switched to the DB.
- [x] Existing test suite green (after the few helpers that inject
      YAMLs are switched to use the PUT API or a small DB seeder).
- **Checkpoint:** schema + storage review before any UI work.

### Slice C2: Archive / restore lifecycle (server endpoints)

- [x] `archived_at` + `archived_reason` columns on `contracts`.
- [x] `POST /api/internal/contracts/{id}/archive` — sets
      `archived_at`. Optional reason in body.
- [x] `POST /api/internal/contracts/{id}/restore` — clears
      `archived_at`; 409 if a live id collides.
- [x] List endpoint excludes archived by default;
      `?include_archived=true` includes with an `archived: true` flag.
- [x] `DELETE` becomes hard delete; refuses unless already archived.
- [x] `flow materialise(-all)` skips archived rows.
- [x] `tests/test_contracts_archive.py` covers the full lifecycle.
- **Checkpoint:** API curl flow works before any UI.

### Slice C3: Steps editor + "Ready" vocab cascade

- [x] Rewrite `/admin/contracts/new` (and the `mode=edit` reuse) to
      replace the three-bucket stage builder with an ordered Steps
      editor.
- [x] Per-step row: name input + WIP checkbox + ↑ / ↓ reorder + ×
      delete + auto-derived READY / WIP / DONE badge.
      "+ Add step" appends an empty row.
- [x] Vocabulary cascade: every "Backlog" → "Ready" in user-facing
      copy (builder UI + cycle-time / aging / cfd detail pages +
      GLOSSARY.md Ready ↔ Backlog note).
- [x] Edit-page Delete button now archives (not hard-deletes);
      copy reads "Archive". Confirmation routes through `/archive`.
- [x] Update / replace `tests/test_contract_wizard.py` +
      `test_stage_builder.py` for the new editor.
- [x] Playwright E2E: build a 5-step / 2-WIP workflow end-to-end;
      assert the READY / WIP / DONE badges recompute on toggle +
      reorder.
- **Checkpoint:** UX review.

### Slice C4: Source vocab probe (labels / statuses / lifecycle)

- [x] Replace `_probe-stages` with
      `POST /api/internal/contracts/_probe-source-vocab` returning
      `{labels, lifecycle_events, warehouse_stages}`.
- [x] GitHub probe: repo labels via
      `/repos/{owner}/{repo}/labels` + curated lifecycle events
      (`PR opened`, `Marked ready for review`, …).
- [x] Jira probe: project statuses via
      `/rest/api/3/project/{key}/statuses` + curated lifecycle
      events (`Issue created`, `Resolved`, …).
- [x] 15-minute per-target cache; `?force=true` busts.
- [x] Builder UI: "Suggestions" panel under the Steps editor with
      three subsections (Warehouse / Labels / Standard events).
      Click any chip → append a new step with a sensible WIP
      default.
- [x] Fallback when the probe fails: inline error in the panel;
      the warehouse + lifecycle sections still work.
- [x] Tests with mocked source-API responses.
- **Checkpoint:** validate against a real Jira instance.

### Slice C5: Dry-run preview (capped sample, per-step table)

- [x] New `src/flowmetrics/contract_preview.py` with a
      bounded-fetch helper: stream items from the source in time
      order, stop at the smaller of `items_cap` (default 200) or
      a 30-day window from `since`.
- [x] `POST /api/internal/contracts/_dry-run` — takes the
      in-progress contract payload + `{since, items_cap}`,
      returns `{fetched_at, expires_at, stopped_by, items_fetched,
      window, per_step: [{step_name, wip, count, items}, …,
      {step_name: "_unmatched", …}]}`.
- [x] In-process cache keyed on
      `(source_target, since, items_cap, steps_signature)`;
      5-min TTL; `?force=true` busts.
- [x] Builder UI "Preview against live source" disclosure: From
      date input + Dry-run button → per-step table using the
      existing `work_items_table.html.jinja` partial. Empty
      steps surface a "name might not match" hint;
      `_unmatched` bucket surfaces gap-in-workflow items.
- [x] No persistence — items never enter the warehouse.
- [x] Unit tests for cap semantics (200 cap, 30d cap, both at once).
- [x] Playwright E2E: build a 3-step workflow, click Dry run,
      see per-step counts; rename a step to a typo, see items
      shift into `_unmatched`.
- **Checkpoint:** validate against a real Jira instance.

### Slice C6: Export YAML

- [x] `GET /api/internal/contracts/{id}/yaml` returns the row's
      `yaml` column with `application/x-yaml` + download
      `Content-Disposition`. Works on archived rows via
      `?include_archived=true`.
- [x] "Download YAML" link + "Copy YAML" button on the edit page.
- [x] Downloaded YAML → `flow materialise --from-yaml PATH`
      succeeds (round-trip test).

### Slice C7: Archived contracts page

- [x] `/admin/contracts/archive` lists archived rows with date,
      reason, source + per-row actions (Restore, Export YAML,
      Hard delete).
- [x] Drawer-style link on `/` ("View archived (n)") that only
      renders when n > 0.
- [x] Playwright E2E: full lifecycle — create → archive →
      restore → archive → hard-delete → assert gone in home,
      archive page, AND `materialise-all` manifest.
- **Checkpoint:** Plan C complete.
