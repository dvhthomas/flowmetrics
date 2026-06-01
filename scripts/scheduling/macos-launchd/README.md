# launchd (macOS)

Two units, both LaunchAgents (user-level — no root):

| Plist | Job | Trigger |
|----|----|----|
| `com.flowmetrics.materialise.plist` | Daily ingest | `StartCalendarInterval` |
| `com.flowmetrics.serve.plist` | Persistent dashboard | `RunAtLoad` + `KeepAlive` |

Use one or both. Together they give you a Mac that fetches data on a
schedule and always has the dashboard reachable at
http://127.0.0.1:8000 across logout, sleep, and reboot — no Terminal
tab to leave open.

## Persistent dashboard (serve)

```bash
# 1. Edit the plist: replace REPLACE_HOME and REPLACE_FLOW with
#    absolute paths. REPLACE_FLOW is the `flow` binary —
#    `which flow` will tell you (typically $HOME/.local/bin/flow
#    after `uv tool install`).
$EDITOR com.flowmetrics.serve.plist

# 2. Drop into the user LaunchAgents dir.
cp com.flowmetrics.serve.plist ~/Library/LaunchAgents/

# 3. Register + start.
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.flowmetrics.serve.plist

# 4. Verify.
launchctl print gui/$UID/com.flowmetrics.serve | grep state
open http://127.0.0.1:8000
```

The agent restarts on login, after a crash, and after a clean exit
(`KeepAlive=true`). To bind on a different port, edit the
`ProgramArguments` block. To bind on a non-loopback host, add
`--password <…>` to the args (or set `FLOW_PASSWORD` in
`EnvironmentVariables`) — `flow serve` refuses to bind otherwise.

```bash
# Stop / uninstall.
launchctl bootout gui/$UID/com.flowmetrics.serve
rm ~/Library/LaunchAgents/com.flowmetrics.serve.plist
```

## Daily ingest (materialise)

## Install

```bash
# 1. Edit the plist: replace REPLACE_HOME and REPLACE_VENV with your
#    paths. Use absolute paths, not ~.
$EDITOR com.flowmetrics.materialise.plist

# 2. Drop into the user LaunchAgents dir.
cp com.flowmetrics.materialise.plist ~/Library/LaunchAgents/

# 3. Register it with launchd.
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.flowmetrics.materialise.plist

# 4. (Optional) Fire it once to verify.
launchctl kickstart -k gui/$UID/com.flowmetrics.materialise
```

## Verify

```bash
# Is it registered?
launchctl list | grep flowmetrics

# Read the logs from the most recent run.
tail -50 $FLOWMETRICS_HOME/data/_status/launchd.out.log
tail -50 $FLOWMETRICS_HOME/data/_status/launchd.err.log

# Or the structured manifest:
cat $FLOWMETRICS_HOME/data/_status/daily-$(date -u +%F).json | jq .
```

## Adjust the schedule

`StartCalendarInterval` accepts `Hour` / `Minute` / `Weekday` /
`Day` / `Month`. To fire every six hours:

```xml
<key>StartCalendarInterval</key>
<array>
  <dict><key>Hour</key><integer>0</integer><key>Minute</key><integer>30</integer></dict>
  <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>30</integer></dict>
  <dict><key>Hour</key><integer>12</integer><key>Minute</key><integer>30</integer></dict>
  <dict><key>Hour</key><integer>18</integer><key>Minute</key><integer>30</integer></dict>
</array>
```

## Uninstall

```bash
launchctl bootout gui/$UID/com.flowmetrics.materialise
rm ~/Library/LaunchAgents/com.flowmetrics.materialise.plist
```

## Gotchas

- **`PATH`**: the plist sets an explicit `PATH` because launchd's
  default is bare. If `uv` lives elsewhere, add the directory.
- **Sleep / wake**: macOS doesn't wake a sleeping Mac to fire a
  scheduled job. The plist's `Persistent`-equivalent
  (`StartCalendarIntervalDoesNotFireWhenSleeping=false`) makes the job
  fire on next wake instead of skipping.
- **Permissions**: the venv directory must be readable by the user
  the LaunchAgent runs as (your user, by default).
