# systemd timer

Daily ingest under a user-level systemd timer. Pick the user that owns
your install (e.g. `flowmetrics`).

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
