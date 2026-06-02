# Scheduled backup

Run `flow backup` after the daily ingest, retain N days, ship offsite
on whatever cadence you trust.

The recipe is the same on every OS; pick the wrapper that matches
your scheduler.

## What "good enough" looks like

```
02:30  flow materialize --all   (data is fresh)
02:45  flow backup            (snapshot the fresh data)
02:50  retain last 14 days, delete older
03:00  optionally: rsync to off-host
```

## POSIX (Linux, macOS) — `backup-and-prune.sh`

```bash
#!/usr/bin/env bash
set -euo pipefail
: "${FLOWMETRICS_HOME:?set FLOWMETRICS_HOME}"
: "${FLOWMETRICS_VENV:?set FLOWMETRICS_VENV}"

cd "$FLOWMETRICS_HOME"
"$FLOWMETRICS_VENV/bin/flow" backup --data-dir data

# Retain 14 newest archives; delete the rest.
ls -1t data/_backups/flowmetrics-*.tar.gz \
  | tail -n +15 \
  | xargs -r rm --
```

Add to your `linux-systemd/` `.service`, `linux-cron/` crontab, or
`macos-launchd/` plist as a SECOND `ExecStart` / cron line / launchd
plist (or a wrapper shell script that chains them).

## Windows — `backup-and-prune.ps1`

```powershell
$ErrorActionPreference = "Stop"
$Home = $env:FLOWMETRICS_HOME
$Venv = $env:FLOWMETRICS_VENV
if (-not $Home -or -not $Venv) {
  throw "Set FLOWMETRICS_HOME and FLOWMETRICS_VENV."
}

Set-Location $Home
& "$Venv\Scripts\flow.exe" backup --data-dir data

# Retain 14 newest.
Get-ChildItem "$Home\data\_backups\flowmetrics-*.tar.gz" |
  Sort-Object LastWriteTime -Descending |
  Select-Object -Skip 14 |
  Remove-Item -Force
```

Add a second `<Exec>` action to your Task Scheduler XML pointing at
this script.

## Off-host

`flow backup` writes a single file. `rsync`, `scp`, `aws s3 cp`,
`restic backup`, or any other backup tool already handles archives;
point yours at `$FLOWMETRICS_HOME/data/_backups/`. No flowmetrics
plumbing required.
