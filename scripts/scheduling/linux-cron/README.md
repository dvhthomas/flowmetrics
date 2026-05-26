# cron

Daily ingest under user-level or system-level cron. systemd timers are
preferred on modern Linux (better logging, fewer surprises around
`PATH`), but cron is still everywhere and dead simple.

## Install

### User-level

```bash
# Edit the sample (set FLOWMETRICS_HOME + FLOWMETRICS_VENV), then:
crontab -l > /tmp/current
cat /tmp/current crontab.sample > /tmp/new
crontab /tmp/new
crontab -l  # verify
```

### System-level

```bash
sudo cp crontab.sample /etc/cron.d/flowmetrics
# /etc/cron.d entries need a user column — edit the file and add the
# username after the schedule:
#   30 2 * * *  flowmetrics  cd ... && ...
sudo chmod 644 /etc/cron.d/flowmetrics
```

## Verify

```bash
# Next firing (Debian/Ubuntu — checks cron's view of the file):
sudo grep CRON /var/log/syslog | tail -20

# Or just wait for tomorrow and read the manifest:
cat $FLOWMETRICS_HOME/data/_status/daily-$(date -u +%F).json | jq .
```

## Gotchas

- **`PATH`**: cron's default `PATH` is `/usr/bin:/bin`. The sample sets
  a fuller one. If you put the `uv` binary somewhere unusual, add that
  directory.
- **`%` in commands**: cron treats `%` as a newline. If you ever add
  `date +%F`-style expansions, escape them: `\%F`.
- **Quiet failure mode**: cron only mails you when stderr is non-empty.
  The `2>&1 | logger -t flowmetrics` redirect routes everything to
  syslog instead; check `journalctl -t flowmetrics` (or
  `grep flowmetrics /var/log/syslog`) to read run output.
