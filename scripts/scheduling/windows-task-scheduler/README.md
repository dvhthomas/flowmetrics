# Windows Task Scheduler

Daily ingest under the Windows Task Scheduler. The XML template runs
`flow materialise-all` once a day; one-line installer with `schtasks`.

## Install

```powershell
# 1. Open flowmetrics-materialise.xml in a text editor and replace:
#       <UserId>DOMAIN\username</UserId>
#       <Command>C:\flowmetrics\.venv\Scripts\flow.exe</Command>
#       <Arguments>... C:\flowmetrics\contracts ... C:\flowmetrics\data</Arguments>
#       <WorkingDirectory>C:\flowmetrics</WorkingDirectory>
#    with the values for your install. Save.

# 2. Register the task. Run from an elevated PowerShell or cmd.
schtasks /Create /XML flowmetrics-materialise.xml /TN flowmetrics-materialise

# 3. Fire it once to verify.
schtasks /Run /TN flowmetrics-materialise
```

## Verify

```powershell
# Detail (last run time, last result, next scheduled run):
schtasks /Query /TN flowmetrics-materialise /V /FO LIST

# Or read the structured manifest. UTC date — adjust if your TZ
# is many hours off.
Get-Content C:\flowmetrics\data\_status\daily-2026-05-26.json
```

`Last Result: 0` means success. Other codes:

- `1` — total failure (every workflow's materialise raised).
- `2` — usage / configuration error (look at the manifest's error
  fields).
- `0x80070005` — Access denied; the user account doesn't have
  read/write on `C:\flowmetrics`.

## Adjust the schedule

The `<StartBoundary>` tag's time-of-day is what matters; the date is
just a base. To fire every 4 hours instead of daily, replace
`<ScheduleByDay>` with `<Repetition>`:

```xml
<Repetition>
  <Interval>PT4H</Interval>
  <Duration>P1D</Duration>
</Repetition>
```

## Uninstall

```powershell
schtasks /Delete /TN flowmetrics-materialise /F
```

## Gotchas

- **Account**: the task runs under the `<UserId>` named in the XML. If
  the account is a Microsoft account, the `<UserId>` is the email; if
  local, it's just the username. Get the right one via
  `whoami` in PowerShell.
- **PATH**: `<Command>` must be an absolute path to `flow.exe`. The
  task runs without your shell's `PATH`, so `flow` alone won't work.
- **Network**: `RunOnlyIfNetworkAvailable=true` skips the run when
  there's no network. Disable if you want the task to attempt offline
  and surface the error.
