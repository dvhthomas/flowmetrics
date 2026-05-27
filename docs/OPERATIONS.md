# Operations guide

For running flowmetrics in a long-lived install — scheduled ingest,
backup + restore, and the two automation paths (native scheduler vs.
container). Pair this with [HOWTO.md](HOWTO.md), which covers
*getting-started*; OPERATIONS picks up where that ends.

## Two automation paths

```
                       ┌────────────────────────────────┐
                       │   How do you want to schedule? │
                       └─────────────┬──────────────────┘
              ┌──────────────────────┴────────────────────┐
        Native scheduler                              Container
   (cron / systemd / launchd / Task Scheduler)    (Docker compose / k8s)
              │                                          │
   one host, OS-integrated logging                fleet-friendly,
   no Docker daemon needed                        same image everywhere
```

**Native** is the default for a single host or a developer workstation —
fewer moving parts. **Container** wins once you're running on more than
one box or your team already standardises on Docker / Kubernetes.

Both paths run the same one-liner under the hood:

```
flow materialise-all --workflows-dir CONTRACTS --data-dir DATA
```

## Cross-platform install

flowmetrics runs on macOS, Linux, and Windows. The install path is the
same on all three:

```bash
# 1. Get the source.
git clone https://github.com/dvhthomas/flowmetrics
cd flowmetrics

# 2. uv (https://docs.astral.sh/uv/) handles Python + deps.
uv sync

# 3. Smoke-test.
uv run flow --help
```

Credentials for live source fetches:

- **GitHub** — `gh auth login` (uses `gh auth token`) or set
  `$GITHUB_TOKEN` directly.
- **Jira** — Atlassian Jira: any instance you have read access to.
  Apache's public Jira (`https://issues.apache.org/jira`) serves
  anonymous reads. Other Jira
  instances: configure in the workflow YAML (see [HOWTO](HOWTO.md)).

## Daily ingest

Copy a template, set three env vars, install the schedule. Each
directory under `scripts/scheduling/` has a README with the install /
verify / uninstall commands for its platform.

| OS / scheduler | Template directory |
|----|----|
| Linux + systemd | `scripts/scheduling/linux-systemd/` |
| Linux + cron | `scripts/scheduling/linux-cron/` |
| macOS + launchd | `scripts/scheduling/macos-launchd/` |
| Windows + Task Scheduler | `scripts/scheduling/windows-task-scheduler/` |

Common env vars across every template:

| Var | What |
|-----|------|
| `FLOWMETRICS_HOME` | Repo / install root. Holds `contracts/` and `data/`. |
| `FLOWMETRICS_VENV` | Python venv (`$FLOWMETRICS_HOME/.venv` for a `uv sync` install). |
| `FLOWMETRICS_USER` | Unix user the job runs as (systemd / cron only). |

The wrapper command `flow materialise-all` iterates every YAML in
`--workflows-dir` and writes a per-day JSON manifest at
`$FLOWMETRICS_HOME/data/_status/daily-<UTC-date>.json`. **Read the
manifest, not the exit code, to learn what failed** — the exit code
only signals total failure (so a single bad YAML doesn't page
on-call); per-workflow detail lives in `results`.

## Backup + restore

`flow backup` writes a single timestamped `.tar.gz` of the warehouse;
`flow restore` verifies and unpacks. The tarball carries a header with
a SHA-256 of every file, so a corrupted or tampered archive fails
before extraction can damage a half-restored warehouse.

```bash
# Snapshot under a daily-rotated location.
flow backup --data-dir data
#   → data/_backups/flowmetrics-<UTC-timestamp>.tar.gz

# Pick a specific output.
flow backup --data-dir data --output /backups/today.tar.gz

# Include the source-API cache (off by default — it's re-fetchable).
flow backup --data-dir data --include-cache

# Bring a warehouse back. Refuses to clobber non-empty targets
# unless --force.
flow restore --input /backups/today.tar.gz --data-dir fresh-data/
```

Scheduled backup wrappers live under `scripts/scheduling/backup/`:
POSIX (`backup-and-prune.sh`) and Windows (`backup-and-prune.ps1`)
both invoke `flow backup` and retain the 14 newest archives.

### Recovery scenarios

| Situation | Recovery |
|----|----|
| Local warehouse corrupted (DuckDB read errors) | `flow restore --force` from the most recent good `.tar.gz`. |
| Lost machine, new host | Restore the backup on the new box; symlink `contracts/` from your repo checkout. |
| Migrating between machines | `flow backup` on the old, copy the tarball, `flow restore` on the new. |
| Stale `_status/*.json` lock | Delete the file. The 10-minute auto-expiry covers normal crashes; manual delete is the eject button. |

### Off-host backups

`flow backup` writes one file. Whatever you already use (`rsync`,
`restic`, `aws s3 cp`, an external backup service) can ship that file
off-host — there's no flowmetrics plumbing required. Point your tool
at `$FLOWMETRICS_HOME/data/_backups/`.

## Containers

Two paths, both backed by the same Dockerfile:

### Local: Docker Compose

```bash
# Build + run the dashboard.
docker compose up serve
# → http://localhost:8000

# Materialise on demand.
docker compose --profile ingest run --rm materialise
```

`compose.yml` bind-mounts `./contracts` and `./data` so edits and
deletes round-trip to the host.

### Cloud: GitHub Actions

`.github/workflows/materialise.yml` runs `flow materialise-all` on a
daily cron schedule against the workflows committed to the repo, then
uploads `data/` as a build artifact (14-day retention). Use this when
the warehouse lives in CI rather than on a host you operate.

Adjust the `cron:` line in the workflow file to taste; manual triggers
via the Actions tab work too.

## Troubleshooting

### `port 8000 on 127.0.0.1 is already in use`

Either another flowmetrics is running, or another app is on the port.

- **POSIX**: `lsof -ti:8000` finds the process; `kill $(lsof -ti:8000)`
  frees it.
- **Windows**: `netstat -ano | findstr :8000` finds the PID;
  `taskkill /F /PID <PID>` frees it.

Or just pick a different port: `flow serve --port 8001`.

### `Address already in use` from Docker

The compose `serve` service binds to 8000 on the host. Stop other
instances: `docker compose down`, or change the published port in
`compose.yml` (`"127.0.0.1:8001:8000"`).

### Browser-triggered backfill never finishes

The status file at `data/_status/<workflow>.json` records progress.
Stale `running` records older than 10 minutes auto-expire on the next
poll; if you need to clear immediately, delete the file and retry.

### Parquet read errors after upgrade

DuckDB writes Parquet at its current version. A major DuckDB bump can
change the on-disk shape. Restore from the most recent good backup
into a fresh `--data-dir`, or re-run `flow materialise-all` against
your contracts (the source API holds the canonical data; the warehouse
is downstream).

### A workflow's data isn't updating

```bash
# Check the most recent manifest for that workflow's result.
jq '.results[] | select(.workflow == "your-workflow")' \
   data/_status/daily-$(date -u +%F).json

# Re-run just that workflow.
flow materialise your-workflow --workflows-dir contracts --data-dir data
```

## See also

- [HOWTO.md](HOWTO.md) — getting started: install, the dashboard, the
  ad-hoc CLI subcommands.
- [METRICS.md](METRICS.md) — what each chart computes, with
  references to the underlying framework.
- [DECISIONS.md](DECISIONS.md) — why we built it the way we did.
- `scripts/scheduling/` — copy-paste scheduler templates per OS.
