---
title: Schedule data fetches
---

# Schedule data fetches

> **Diátaxis: How-to.** Keep the warehouse fresh by running
> `flow materialize --all` on a schedule.

## macOS — built-in `flow materialize --bg`

Fastest path on macOS — `flow` writes the launchd plist for you
and bootstraps it. No template editing.

```bash
# Daily at 6 AM local time, every configured workflow.
flow materialize --all --bg --at 06:00 \
    --workflows-dir ~/flow/config \
    --data-dir       ~/flow/data
```

The command prints the plist path, the schedule, and where logs
land. Make sure your Mac's timezone matches what you mean:

```bash
sudo systemsetup -gettimezone   # e.g. America/Denver for Mountain
```

Single workflow (instead of `--all`):
```bash
flow materialize sk --bg --at 06:00 \
    --workflows-dir ~/flow/config \
    --data-dir       ~/flow/data
```

Verify the next firing:
```bash
launchctl print gui/$UID/com.flowmetrics.materialize | grep -A2 next
```

Test-fire now without waiting for 06:00:
```bash
launchctl kickstart -k gui/$UID/com.flowmetrics.materialize
tail -20 ~/flow/data/_status/materialize.out.log
```

Tear it down:
```bash
flow materialize --bg --stop
```

If you need calendar intervals fancier than "once per day at HH:MM"
(every-N-hours, specific weekdays), hand-edit the templated plist at
[`scripts/scheduling/macos-launchd/`](../../scripts/scheduling/macos-launchd/) instead.

## Other hosts — templated scheduler files

Linux, Windows, or operators wanting more knobs than `--bg`
exposes: pick the template that matches your host. Each directory
under `scripts/scheduling/` has a paste-ready template + a README
with install / verify / uninstall commands.

| Host | Template directory |
|----|----|
| macOS | [`scripts/scheduling/macos-launchd/`](../../scripts/scheduling/macos-launchd/) |
| Linux + systemd | [`scripts/scheduling/linux-systemd/`](../../scripts/scheduling/linux-systemd/) |
| Linux + cron | [`scripts/scheduling/linux-cron/`](../../scripts/scheduling/linux-cron/) |
| Windows | [`scripts/scheduling/windows-task-scheduler/`](../../scripts/scheduling/windows-task-scheduler/) |
| GitHub Actions (CI-hosted) | `.github/workflows/materialize.yml` |

All templates take the same three placeholders:

| Var | What |
|-----|------|
| `FLOWMETRICS_HOME` | Install root (holds `contracts/` and `data/`). |
| `FLOWMETRICS_FLOW` | Absolute path to the `flow` binary (`which flow`). |
| `FLOWMETRICS_USER` | Unix user the job runs as (systemd / cron only). |

## Read the manifest, not the exit code, to learn what failed

```bash
jq . $FLOWMETRICS_HOME/data/_status/daily-$(date -u +%F).json
```

The exit code only signals total failure (so a single bad YAML
doesn't page on-call). Per-workflow detail lives in `.results`.

## Next

- [Back up and restore](backup-and-restore.md) — wire the same
  scheduler to a rotated backup.
- [Run as a persistent web server](run-as-persistent-server.md) — so
  the dashboard is always up to read the freshened data.
