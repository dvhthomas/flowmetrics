# Scheduled ingest

Templates for running `flow materialise-all` once a day under your
OS's native scheduler. Pick the directory for your platform and follow
the README inside.

| OS | Directory | Scheduler |
|----|-----------|-----------|
| Linux (modern) | `linux-systemd/` | systemd `.service` + `.timer` |
| Linux (BSD-ish) | `linux-cron/` | crontab entry |
| macOS | `macos-launchd/` | launchd `.plist` |
| Windows | `windows-task-scheduler/` | Task Scheduler `.xml` |

Each template fires one command:

```
flow materialise-all --workflows-dir $FLOWMETRICS_HOME/contracts \
    --data-dir $FLOWMETRICS_HOME/data
```

That command iterates every YAML in the workflows directory, runs
`flow materialise` per workflow, and writes a JSON manifest to
`$FLOWMETRICS_HOME/data/_status/daily-<UTC-date>.json` summarising
the results. One failing workflow doesn't block the rest — the
manifest holds per-workflow status, the exit code only flags total
failure.

## Three env vars per template

All templates parameterise on the same three variables. Set them once
and the rest is copy-paste:

| Var | What |
|-----|------|
| `FLOWMETRICS_HOME` | Repo / install root. Holds `contracts/` and `data/`. |
| `FLOWMETRICS_VENV` | The Python venv to run from (`$FLOWMETRICS_HOME/.venv` is fine). |
| `FLOWMETRICS_USER` | (systemd / cron only) Unix user the job runs as. |

## Why one command, not "one per workflow"

Adding a new workflow means dropping a YAML in `contracts/`. The
scheduler doesn't change. Removing a workflow means deleting the YAML.
Still no scheduler change. This is the cron-team-of-one pattern —
contracts directory is the source of truth.

## See also

- [docs/HOWTO.md](../../docs/HOWTO.md) — scheduling, backup, restore,
  persistent server, Docker, troubleshooting.
- [docs/REFERENCE.md](../../docs/REFERENCE.md) — every `flow` flag
  with defaults.
- `flow materialise-all --help` — current flag list.
