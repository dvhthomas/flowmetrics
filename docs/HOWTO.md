# How-to guides

Task-specific recipes. Each section is self-contained — read just the
one you need.

If you're starting from scratch, do the [tutorial](TUTORIAL.md) first.
For canonical detail on every flag, file, and schema, see
[REFERENCE.md](REFERENCE.md).

- [Install on macOS, Linux, Windows](#install)
- [Add a workflow in the browser](#add-a-workflow-in-the-browser)
- [Write a workflow YAML by hand](#write-a-workflow-yaml-by-hand)
- [Fetch data once](#fetch-data-once)
- [Schedule data fetches](#schedule-data-fetches)
- [Run the dashboard locally](#run-the-dashboard-locally)
- [Run as a persistent web server](#run-as-a-persistent-web-server)
- [Back up & restore](#back-up--restore)
- [Deploy with Docker](#deploy-with-docker)
- [Run ad-hoc CLI reports](#ad-hoc-cli-reports)
- [Output for agents (JSON)](#output-for-agents-json)
- [Upgrade](#upgrade)
- [Develop against a source checkout](#develop-against-a-source-checkout)
- [Troubleshooting](#troubleshooting)

## Install

Same path on every OS: install `uv`, then `uv tool install`.

```bash
# macOS
brew install uv

# Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
irm https://astral.sh/uv/install.ps1 | iex
```

```bash
# Install flowmetrics as a global tool (isolated env, exposes `flow`).
uv tool install git+https://github.com/dvhthomas/flowmetrics

# Verify.
flow --help
which flow                          # macOS/Linux
where flow                          # Windows
```

`flow` lands in `~/.local/bin/` on macOS/Linux and
`%USERPROFILE%\.local\bin\` on Windows. If those aren't on your PATH,
`uv tool install` prints the line to add.

**Credentials:**

- **GitHub** — `gh auth login` (uses `gh auth token`) or set
  `$GITHUB_TOKEN` directly. A fine-grained PAT with **public repo
  read** is enough for public repos.
- **Atlassian Jira** — public instances (e.g. Apache's
  `https://issues.apache.org/jira`) need no credentials. Private
  instances: see [REFERENCE § Workflow YAML](REFERENCE.md#workflow-yaml).

## Add a workflow in the browser

The simplest path. Start the dashboard, click **+ New workflow**, fill
in the wizard.

```bash
flow serve            # or `flow serve --bg` on macOS for persistent
# → http://127.0.0.1:8000
```

The wizard probes your source (GitHub repo or Jira project) to
auto-suggest labels / statuses, lets you pick stages, and writes to
`<workflows-dir>/contracts.db`. Walkthrough with screenshots:
[TUTORIAL § 5](TUTORIAL.md#5-add-a-workflow-in-the-browser).

Once saved, hit **Data source** → **Backfill** to materialise.

## Write a workflow YAML by hand

For scripted setups, code review, or committing workflows to version
control. The wizard's output IS YAML under the hood; hand-authoring
just skips the UI.

One YAML per workflow, in a directory you control (e.g.
`~/flow/contracts/`). On `flow serve` first-boot, any YAMLs in the
workflows-dir are imported into `contracts.db` and moved to
`migrated/` — so YAML-edits round-trip into the wizard.

```yaml
# Minimal GitHub PR-review workflow.
contract:
  name: astral-uv-week
  source: github
  repo: astral-sh/uv
  start: 2026-05-04
  stop:  2026-05-10
```

```yaml
# Label-driven GitHub workflow — WIP is "anything carrying one of
# these labels". Order = most progress wins (first match).
contract:
  name: kno-shaping
  source: github
  repo: dvhthomas/kno
  start: 2026-04-01
  stop:  2026-05-10
  wip_labels:
    - shaping
    - in-progress
    - in-review
```

```yaml
# Atlassian Jira (anonymous public read).
contract:
  name: cassandra-month
  source: jira
  jira_url:     https://issues.apache.org/jira
  jira_project: CASSANDRA
  start: 2026-04-12
  stop:  2026-05-11
```

Field reference + all valid combinations:
[REFERENCE § Workflow YAML](REFERENCE.md#workflow-yaml). Copy-paste
starters: [`samples/`](../samples/).

## Fetch data once

```bash
flow materialise <workflow-name> \
    --workflows-dir CONTRACTS_DIR \
    --data-dir       DATA_DIR
```

- Hits the source API (cached on disk under
  `.cache/github/` or `.cache/jira/`).
- Canonicalises events to a single transitions table.
- Writes Parquet under `DATA_DIR/work_items/` and
  `DATA_DIR/transitions/`, plus a run manifest under
  `DATA_DIR/runs/<workflow>/run_id=<…>/manifest.json`.
- Exits 0 on success, non-zero on failure. Cron-friendly.

Fetch every workflow YAML in a directory in one go (this is what the
schedulers below run):

```bash
flow materialise-all \
    --workflows-dir CONTRACTS_DIR \
    --data-dir       DATA_DIR
```

Writes a per-day manifest at
`DATA_DIR/_status/daily-<UTC-date>.json` with per-workflow results.
A single failing YAML doesn't block the rest.

## Schedule data fetches

Pick the scheduler that matches your host. Each directory under
`scripts/scheduling/` has a paste-ready template + a README with
install / verify / uninstall commands.

| Host | Template directory |
|----|----|
| macOS | [`scripts/scheduling/macos-launchd/`](../scripts/scheduling/macos-launchd/) |
| Linux + systemd | [`scripts/scheduling/linux-systemd/`](../scripts/scheduling/linux-systemd/) |
| Linux + cron | [`scripts/scheduling/linux-cron/`](../scripts/scheduling/linux-cron/) |
| Windows | [`scripts/scheduling/windows-task-scheduler/`](../scripts/scheduling/windows-task-scheduler/) |
| GitHub Actions (CI-hosted) | `.github/workflows/materialise.yml` |

All templates take the same three placeholders:

| Var | What |
|-----|------|
| `FLOWMETRICS_HOME` | Install root (holds `contracts/` and `data/`). |
| `FLOWMETRICS_FLOW` | Absolute path to the `flow` binary (`which flow`). |
| `FLOWMETRICS_USER` | Unix user the job runs as (systemd / cron only). |

**Read the manifest, not the exit code, to learn what failed:**

```bash
jq . $FLOWMETRICS_HOME/data/_status/daily-$(date -u +%F).json
```

The exit code only signals total failure (so a single bad YAML
doesn't page on-call). Per-workflow detail lives in `.results`.

## Run the dashboard locally

```bash
flow serve \
    --workflows-dir CONTRACTS_DIR \
    --data-dir       DATA_DIR
# → http://127.0.0.1:8000
```

- Reads only from the local Parquet warehouse. Never hits GitHub /
  Jira during a request.
- `--port N` picks an alternate port.
- `--host 0.0.0.0` binds publicly and **requires** `--password <…>`
  (or `$FLOW_PASSWORD`). HTTP Basic auth is then enforced.

## Run as a persistent web server

For a dashboard that survives logout, reboots, and crashes — no
Terminal tab to leave open.

| Host | Path |
|----|----|
| macOS | `flow serve --bg` (built-in) |
| Linux (systemd) | [`flowmetrics-serve.service`](../scripts/scheduling/linux-systemd/flowmetrics-serve.service) |
| Windows | NSSM wrapper, see below |

### macOS — `flow serve --bg`

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

- Writes a LaunchAgent plist to `~/Library/LaunchAgents/com.flowmetrics.serve.plist`
  with the flags you pass (resolved to absolute paths).
- `launchctl bootout` (best-effort, in case it was already loaded)
  then `launchctl bootstrap` it. Starts immediately.
- `RunAtLoad=true` + `KeepAlive=true`: launchd restarts the agent on
  login, crash, or clean exit. Survives logout and reboot.

Tear it down:

```bash
flow serve --bg --stop
```

Want the plist by hand? The templated path under
[`scripts/scheduling/macos-launchd/`](../scripts/scheduling/macos-launchd/)
ships the same shape — useful when you need to tweak schedule keys
the `--bg` flag doesn't expose.

### Linux (systemd — user unit)

```bash
cd scripts/scheduling/linux-systemd

$EDITOR flowmetrics-serve.service
mkdir -p ~/.config/systemd/user
cp flowmetrics-serve.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now flowmetrics-serve.service
loginctl enable-linger $USER          # survive logout
curl -fsS http://127.0.0.1:8000/healthz
```

Status: `systemctl --user status flowmetrics-serve`. Logs:
`journalctl --user -u flowmetrics-serve -f`.

### Windows (Task Scheduler / NSSM)

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

### Binding off localhost

For any of the above: edit the args to include `--host 0.0.0.0 --port
N --password <…>` (or set `FLOW_PASSWORD` via the unit's environment).
`flow serve` refuses to bind a non-loopback host without a password.

## Back up & restore

The backup is a single timestamped `.tar.gz` carrying:

- the data warehouse (Parquet + manifests under `--data-dir`);
- optionally the config DB (`<workflows-dir>/contracts.db`),
  snapshotted via SQLite's online backup API so a running server
  can't tear it;
- a `flowmetrics-backup.json` header with a SHA-256 per file.

Restore verifies every checksum **before** writing anything, so a
corrupted or tampered archive fails before it can damage a
half-restored install.

### Back up

```bash
# Data warehouse only.
flow backup --data-dir DATA_DIR
#   → DATA_DIR/_backups/flowmetrics-<UTC-timestamp>.tar.gz

# Data warehouse + config DB (contracts.db).
flow backup \
    --data-dir       DATA_DIR \
    --workflows-dir  CONTRACTS_DIR \
    --output         /backups/today.tar.gz

# Include the source-API response cache (off by default — it's
# re-fetchable from the source).
flow backup --data-dir DATA_DIR --include-cache
```

### Restore

```bash
# Default — restore both data and config (if the archive carries them).
flow restore \
    --input          /backups/today.tar.gz \
    --data-dir       FRESH_DATA \
    --workflows-dir  FRESH_CONTRACTS

# Roll back the warehouse only, leave contracts.db untouched.
flow restore --input today.tar.gz --data-dir FRESH_DATA --data-only

# Roll back contracts.db only, leave the warehouse untouched.
flow restore --input today.tar.gz --workflows-dir FRESH_CONTRACTS --config-only

# Overwrite a non-empty target (otherwise restore bails to protect
# work-in-progress).
flow restore --input today.tar.gz --data-dir EXISTING --force
```

`--data-only` and `--config-only` are mutually exclusive. An old
data-only backup restores fine with the default invocation; trying
`--config-only` against one is a hard error (the archive has nothing
to give).

### Scheduled rotation

`scripts/scheduling/backup/backup-and-prune.sh` (POSIX) and
`backup-and-prune.ps1` (Windows) wrap `flow backup` and retain the
14 newest archives. Wire them into the same scheduler you use for
materialise (above).

### Off-host backups

`flow backup` writes one file. Whatever you already use (`rsync`,
`restic`, `aws s3 cp`, an external backup service) can ship that
file off-host — there's no flowmetrics plumbing required. Point your
tool at `$FLOWMETRICS_HOME/data/_backups/`.

### Recovery scenarios

| Situation | Recovery |
|----|----|
| Warehouse corrupted (DuckDB read errors) | `flow restore --data-only --force` from the most recent good backup. |
| Bad config edit clobbered `contracts.db` | `flow restore --config-only --force` from the most recent backup. |
| Lost machine, fresh host | Install `flow` (above), then `flow restore` the most recent backup. |
| Stale `_status/*.json` lock | Delete the file. The 10-minute auto-expiry covers normal crashes; manual delete is the eject button. |

## Deploy with Docker

```bash
docker compose up serve
# → http://localhost:8000

docker compose --profile ingest run --rm materialise
```

`compose.yml` bind-mounts `./contracts` and `./data` so edits and
deletes round-trip to the host. Both services use the same image
built from `Dockerfile`.

For CI-hosted ingest (no host to operate), see
`.github/workflows/materialise.yml` — runs `flow materialise-all` on
a cron schedule and uploads `data/` as a build artifact.

## Ad-hoc CLI reports

The same metrics as one-shot commands — for terminals, pipelines,
static HTML exports, and agent consumption. No warehouse required;
these hit the source API directly.

```bash
# Flow efficiency for this week.
flow efficiency --repo astral-sh/uv

# Forecast when 50 items will be done.
flow forecast when-done --repo astral-sh/uv --items 50

# How many items will be done by 2026-06-30.
flow forecast how-many --repo astral-sh/uv --target-date 2026-06-30

# Cumulative Flow Diagram.
flow cfd --repo astral-sh/uv --start 2026-04-12 --stop 2026-05-11 \
    --workflow "Open,Merged"

# Aging WIP — label-driven mode.
flow aging --repo dvhthomas/kno --wip-labels "shaping,in-progress,in-review"
```

Every command takes `--format text|json|html` (default `text`). See
[REFERENCE § CLI](REFERENCE.md#cli).

## Output for agents (JSON)

```bash
flow forecast when-done --repo astral-sh/uv --items 50 --format json \
    | jq '.summary.percentiles'
```

JSON includes a schema URI (`flowmetrics.forecast.when_done.v1` etc.),
raw input, raw result, training window, simulation parameters, chart
data, captured stderr, and a one-line reproducer. Errors emit a
`flowmetrics.error.v1` envelope with `hint` and `command_to_fix`.

Field-by-field detail: [REFERENCE § Output envelopes](REFERENCE.md#output-envelopes).

## Upgrade

```bash
# Re-fetch latest main and reinstall.
uv tool upgrade flowmetrics

# Confirm the new version landed.
flow --version
# → flow 0.1.0                   (tagged release)
# → flow 0.1.0.dev3+ge8a2cd1     (intermediate commit)

# Source checkout?
git pull && uv sync --reinstall-package flowmetrics
```

Versions are derived from git via `hatch-vcs`: tagged commits render
as plain PEP-440 (`0.1.0`), intermediate commits as
`0.1.0.devN+g<sha>` where `N` counts commits since the last tag and
`<sha>` is the short commit hash. The CI build, your local checkout,
and a `uv tool install` all converge on the same version for the
same git state.

If a running `flow serve --bg` is on an old build, re-run
`flow serve --bg` (idempotent) to reload the LaunchAgent against
the upgraded binary.

After upgrade, if Parquet read errors crop up (a major DuckDB bump
can change on-disk shape), restore from the most recent good backup
into a fresh `--data-dir` or re-run `flow materialise-all` — the
warehouse is downstream of the source API, never the source of truth.

## Develop against a source checkout

```bash
git clone https://github.com/dvhthomas/flowmetrics
cd flowmetrics
uv sync                            # creates .venv/

# Run from the checkout.
uv run flow --help
uv run pytest                      # unit suite (no network)
uv run pytest -m integration       # opt-in, needs gh auth
uv run ruff check
uv run ty check src
```

To use this checkout as your global tool while you iterate:

```bash
uv tool install --force --editable .
```

`--editable` keeps the global `flow` pointed at your source tree so
edits take effect without a re-install.

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
instances (`docker compose down`) or change the published port in
`compose.yml` (`"127.0.0.1:8001:8000"`).

### Browser-triggered backfill never finishes

The status file at `data/_status/<workflow>.json` records progress.
Stale `running` records older than 10 minutes auto-expire on the next
poll; if you need to clear immediately, delete the file and retry.

### A workflow's data isn't updating

```bash
# Check the most recent manifest for that workflow's result.
jq '.results[] | select(.workflow == "your-workflow")' \
   data/_status/daily-$(date -u +%F).json

# Re-run just that workflow.
flow materialise your-workflow --workflows-dir contracts --data-dir data
```

### Parquet read errors after upgrade

DuckDB writes Parquet at its current version. A major bump can change
the on-disk shape. Restore from the most recent good backup into a
fresh `--data-dir`, or re-run `flow materialise-all` against your
contracts.

### `flow` not on PATH after install

`uv tool install` prints the directory it dropped binaries into.
Usually `~/.local/bin` (macOS/Linux) or `%USERPROFILE%\.local\bin\`
(Windows). Add that to your shell's PATH, or run `uv tool update-shell`
to have `uv` do it for you.

## See also

- [TUTORIAL.md](TUTORIAL.md) — linear walkthrough from zero to dashboard.
- [REFERENCE.md](REFERENCE.md) — every command, flag, file, schema.
- [METRICS.md](METRICS.md) — how each chart is computed.
- [DECISIONS.md](DECISIONS.md) — why we built it this way.
- [`scripts/scheduling/`](../scripts/scheduling/) — paste-ready
  scheduler templates per OS.
