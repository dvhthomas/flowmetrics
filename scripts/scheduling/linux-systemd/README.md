# systemd

Two units:

| File | Job | Type |
|----|----|----|
| `flowmetrics-materialise.{service,timer}` | Daily ingest | Oneshot, fired by a timer |
| `flowmetrics-serve.service` | Persistent dashboard | Long-running, restarted on failure |

Use one or both. Together they give you a host that fetches data on
a schedule and always has the dashboard reachable across reboots.

## Persistent dashboard (serve)

```bash
# 1. Edit the unit: set FLOWMETRICS_HOME and FLOWMETRICS_FLOW to
#    absolute paths. FLOWMETRICS_FLOW is the `flow` binary
#    (`which flow` shows it; typically ~/.local/bin/flow after
#    `uv tool install`).
$EDITOR flowmetrics-serve.service

# 2a. User-level install (single-user host, no root needed) —
#     dashboard runs as your user, dies on logout unless
#     `loginctl enable-linger $USER` is set.
mkdir -p ~/.config/systemd/user
cp flowmetrics-serve.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now flowmetrics-serve.service
loginctl enable-linger $USER   # keep alive across logout

# 2b. System-wide install (server box) — runs under the user
#     declared by `User=` (add a `User=flowmetrics` line under
#     [Service] before copying).
sudo cp flowmetrics-serve.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now flowmetrics-serve.service

# 3. Verify.
systemctl --user status flowmetrics-serve   # or: sudo systemctl status …
curl -fsS http://127.0.0.1:8000/healthz
```

For non-loopback binds add `--password <…>` to the `ExecStart` (or
set `FLOW_PASSWORD` via `Environment=`) — `flow serve` refuses to
bind otherwise.

```bash
# Stop / uninstall (user install).
systemctl --user disable --now flowmetrics-serve.service
rm ~/.config/systemd/user/flowmetrics-serve.service
systemctl --user daemon-reload
```

## Daily ingest (materialise timer)

Pick the user that owns your install (e.g. `flowmetrics`).

## Install

```bash
# 1. Edit the .service file: set FLOWMETRICS_HOME + FLOWMETRICS_VENV
#    to match your install paths. The User= line is templated as %i so
#    you can do user@instance enabling, but for a single-host setup
#    you can hard-code it.

# 2. Copy the unit + timer in (system-wide install).
sudo cp flowmetrics-materialise.service /etc/systemd/system/
sudo cp flowmetrics-materialise.timer   /etc/systemd/system/
sudo systemctl daemon-reload

# 3. Enable + start the TIMER (not the service — the timer triggers it).
#    Replace `flowmetrics` with the user the job runs as.
sudo systemctl enable --now flowmetrics-materialise@flowmetrics.timer

# 4. (Optional) Fire it once manually to validate.
sudo systemctl start flowmetrics-materialise@flowmetrics.service
```

## Verify

```bash
# When is it next scheduled?
systemctl list-timers | grep flowmetrics

# What did the last run do?
journalctl -u flowmetrics-materialise@flowmetrics.service --since=today

# Or read the structured manifest:
cat $FLOWMETRICS_HOME/data/_status/daily-$(date -u +%F).json | jq .
```

## Adjust the schedule

`OnCalendar=*-*-* 02:30:00` in the `.timer` is daily at 02:30 local
time. To run every 6 hours instead:

```ini
OnCalendar=*-*-* 00,06,12,18:00
```

`systemd-analyze calendar 'YOUR EXPR'` previews the next firings.

## Uninstall

```bash
sudo systemctl disable --now flowmetrics-materialise@flowmetrics.timer
sudo rm /etc/systemd/system/flowmetrics-materialise.{service,timer}
sudo systemctl daemon-reload
```
