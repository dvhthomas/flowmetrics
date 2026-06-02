---
title: Fetch data once
---

# Fetch data once

> **Diátaxis: How-to.** Single-shot materialize — the building block
> for everything else.

## See what's configured

```bash
flow workflows list --workflows-dir CONTRACTS_DIR
# NAME           SOURCE  TARGET
# astral-uv      db      astral-sh/uv
# kno-shaping    db      dvhthomas/kno
```

`SOURCE` shows where each workflow came from: `db` for wizard-managed
rows in `workflows.db`, `yaml` for un-migrated YAML files in the
workflows-dir. When both exist for the same name, the DB row wins —
same precedence the materialize commands use.

Add `--data-dir DATA_DIR` to extend the table with a `DATA` column
showing whether materialize has produced Parquet for each workflow.

## Materialize a single workflow

```bash
flow materialize <workflow-name> \
    --workflows-dir CONTRACTS_DIR \
    --data-dir       DATA_DIR
```

What this does:

- Hits the source API (cached on disk under `.cache/github/` or
  `.cache/jira/`).
- Canonicalises events to a single transitions table.
- Writes Parquet under `DATA_DIR/work_items/` and
  `DATA_DIR/transitions/`, plus a run manifest under
  `DATA_DIR/runs/<workflow>/run_id=<…>/manifest.json`.
- Exits 0 on success, non-zero on failure. Cron-friendly.

## Materialize every configured workflow

What the schedulers below run:

```bash
flow materialize --all \
    --workflows-dir CONTRACTS_DIR \
    --data-dir       DATA_DIR
```

A single failing workflow doesn't block the others; the per-day
manifest at `DATA_DIR/_status/daily-<UTC-date>.json` records what
ran, what passed, and what failed. The exit code only signals total
failure (no workflow succeeded) so cron-style monitoring doesn't
page on a single bad workflow.

## Window overrides

`--since YYYY-MM-DD` / `--until YYYY-MM-DD` (single-workflow form
only) override the YAML's `start` / `stop` for this run.

## Offline / online

`--offline` reads only the on-disk cache; `--online` (default) hits
the API on a cache miss. See [Explanation § Decisions](../explain/decisions.md#5-the-cache-is-unconditional-and-never-expires)
for why the cache never expires.

## Next

- [Schedule data fetches](schedule-fetches.md) — wire this into cron
  / launchd / systemd.
- [Run the dashboard locally](run-dashboard-locally.md) — browse the
  results.
