---
title: Back up and restore
---

# Back up and restore

> **Diátaxis: How-to.** Take a snapshot of the warehouse + config,
> and put it back later.

The backup is a single timestamped `.tar.gz` carrying:

- the data warehouse (Parquet + manifests under `--data-dir`);
- optionally the config DB (`<workflows-dir>/workflows.db`),
  snapshotted via SQLite's online backup API so a running server
  can't tear it;
- a `flowmetrics-backup.json` header with a SHA-256 per file.

Restore verifies every checksum **before** writing anything, so a
corrupted or tampered archive fails before it can damage a
half-restored install.

## Back up

```bash
# Data warehouse only.
flow backup --data-dir DATA_DIR
#   → DATA_DIR/_backups/flowmetrics-<UTC-timestamp>.tar.gz

# Data warehouse + config DB (workflows.db).
flow backup \
    --data-dir       DATA_DIR \
    --workflows-dir  CONTRACTS_DIR \
    --output         /backups/today.tar.gz

# Include the source-API response cache (off by default — it's
# re-fetchable from the source).
flow backup --data-dir DATA_DIR --include-cache
```

## Restore

```bash
# Default — restore both data and config (if the archive carries them).
flow restore \
    --input          /backups/today.tar.gz \
    --data-dir       FRESH_DATA \
    --workflows-dir  FRESH_CONTRACTS

# Roll back the warehouse only, leave workflows.db untouched.
flow restore --input today.tar.gz --data-dir FRESH_DATA --data-only

# Roll back workflows.db only, leave the warehouse untouched.
flow restore --input today.tar.gz --workflows-dir FRESH_CONTRACTS --config-only

# Overwrite a non-empty target (otherwise restore bails to protect
# work-in-progress).
flow restore --input today.tar.gz --data-dir EXISTING --force
```

`--data-only` and `--config-only` are mutually exclusive. An old
data-only backup restores fine with the default invocation; trying
`--config-only` against one is a hard error (the archive has nothing
to give).

## Scheduled rotation

`scripts/scheduling/backup/backup-and-prune.sh` (POSIX) and
`backup-and-prune.ps1` (Windows) wrap `flow backup` and retain the
14 newest archives. Wire them into the same scheduler you use for
materialize — see [Schedule data fetches](schedule-fetches.md).

## Off-host backups

`flow backup` writes one file. Whatever you already use (`rsync`,
`restic`, `aws s3 cp`, an external backup service) can ship that
file off-host — there's no flowmetrics plumbing required. Point your
tool at `$FLOWMETRICS_HOME/data/_backups/`.

## Recovery scenarios

| Situation | Recovery |
|----|----|
| Warehouse corrupted (DuckDB read errors) | `flow restore --data-only --force` from the most recent good backup. |
| Bad config edit clobbered `workflows.db` | `flow restore --config-only --force` from the most recent backup. |
| Lost machine, fresh host | Install `flow` (see [Install](install.md)), then `flow restore` the most recent backup. |
| Stale `_status/*.json` lock | Delete the file. The 10-minute auto-expiry covers normal crashes; manual delete is the eject button. |
