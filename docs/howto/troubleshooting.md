---
title: Troubleshooting
---

# Troubleshooting

> **Diátaxis: How-to.** Common failure modes and what to do about
> them. Each section is one symptom → one fix.

## `port 8000 on 127.0.0.1 is already in use`

Either another flowmetrics is running, or another app is on the port.

- **POSIX**: `lsof -ti:8000` finds the process; `kill $(lsof -ti:8000)`
  frees it.
- **Windows**: `netstat -ano | findstr :8000` finds the PID;
  `taskkill /F /PID <PID>` frees it.

Or just pick a different port: `flow serve --port 8001`.

## `Address already in use` from Docker

The compose `serve` service binds to 8000 on the host. Stop other
instances (`docker compose down`) or change the published port in
`compose.yml` (`"127.0.0.1:8001:8000"`).

## Browser-triggered backfill never finishes

The status file at `data/_status/<workflow>.json` records progress.
Stale `running` records older than 10 minutes auto-expire on the next
poll; if you need to clear immediately, delete the file and retry.

## A workflow's data isn't updating

```bash
# Check the most recent manifest for that workflow's result.
jq '.results[] | select(.workflow == "your-workflow")' \
   data/_status/daily-$(date -u +%F).json

# Re-run just that workflow.
flow materialize your-workflow --workflows-dir contracts --data-dir data
```

## Parquet read errors after upgrade

DuckDB writes Parquet at its current version. A major bump can change
the on-disk shape. Restore from the most recent good backup into a
fresh `--data-dir`, or re-run `flow materialize --all` against your
contracts.

## `flow` not on PATH after install

`uv tool install` prints the directory it dropped binaries into.
Usually `~/.local/bin` (macOS/Linux) or `%USERPROFILE%\.local\bin\`
(Windows). Add that to your shell's PATH, or run `uv tool update-shell`
to have `uv` do it for you.
