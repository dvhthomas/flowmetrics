---
title: Run as a persistent web server
---

# Run as a persistent web server

> **Diátaxis: How-to.** Dashboard that survives logout, reboots, and
> crashes. No Terminal tab to leave open.

| Host | Path |
|----|----|
| macOS | `flow serve --bg` (built-in, launchd) |
| Linux | `flow serve --bg` (built-in, systemd --user) |
| Windows | NSSM wrapper, see below |

## macOS + Linux — `flow serve --bg`

```bash
# Install + start. Idempotent: re-run to reload with new flags.
flow serve --bg \
    --workflows-dir ~/flow/contracts \
    --data-dir       ~/flow/data
# → http://127.0.0.1:8000/
#   logs:  ~/flow/data/_status/serve.{out,err}.log
#   stop:  flow serve --bg --stop
```

What it does:

- **macOS**: writes a LaunchAgent plist to
  `~/Library/LaunchAgents/com.flowmetrics.serve.plist`,
  `launchctl bootout` (in case it was already loaded), then
  `launchctl bootstrap`. `RunAtLoad=true` + `KeepAlive=true` so the
  agent starts at login and respawns after crashes.
- **Linux**: writes a user unit to
  `~/.config/systemd/user/flowmetrics-serve.service`, runs
  `systemctl --user daemon-reload`, then `enable` + `restart`.
  `Restart=on-failure` respawns the dashboard on crash. To survive
  logout, run `sudo loginctl enable-linger $USER` once — `flow
  serve --bg` prints the reminder.

Tear it down (same command, both OSes):

```bash
flow serve --bg --stop
```

Want to tweak the unit by hand? The templated paths under
[`scripts/scheduling/macos-launchd/`](../../scripts/scheduling/macos-launchd/)
and
[`scripts/scheduling/linux-systemd/`](../../scripts/scheduling/linux-systemd/)
ship the same shape. `flow serve --bg` writes equivalent files —
manual editing is just for tweaks the flag doesn't expose
(per-second restart, custom journald routing, etc.).

## Status + logs

```bash
# macOS
launchctl print gui/$UID/com.flowmetrics.serve | grep state
tail -F ~/flow/data/_status/serve.{out,err}.log

# Linux
systemctl --user status flowmetrics-serve
journalctl --user -u flowmetrics-serve -f
tail -F ~/flow/data/_status/serve.{out,err}.log
```

## Windows (Task Scheduler / NSSM)

Task Scheduler can launch `flow serve` at logon but won't supervise
it. For a true "service" use [NSSM](https://nssm.cc/):

```powershell
# As Administrator:
nssm install Flowmetrics "$env:USERPROFILE\.local\bin\flow.exe" ^
  serve --workflows-dir C:\flow\contracts --data-dir C:\flow\data
nssm set Flowmetrics AppDirectory C:\flow
nssm set Flowmetrics Start SERVICE_AUTO_START
Start-Service Flowmetrics
Start-Process http://127.0.0.1:8000
```

## Binding off localhost

For any of the above: edit the args to include `--host 0.0.0.0 --port
N --password <…>` (or set `FLOW_PASSWORD` via the unit's environment).
`flow serve` refuses to bind a non-loopback host without a password.

## Next

- [Schedule data fetches](schedule-fetches.md) — keep the warehouse
  fresh while the dashboard runs.
- [Back up and restore](backup-and-restore.md) — protect a
  long-running install.
