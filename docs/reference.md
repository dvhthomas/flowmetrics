---
title: Reference
---

# Reference

> **Diátaxis: Reference.** Fact-oriented. Every CLI command, every
> flag, every file the warehouse touches, every output schema. No
> tutorial framing, no opinions — for those see
> [Tutorial](tutorial.md) / [How-to guides](howto/) /
> [Explanation](explain/).

- [CLI](#cli)
- [Workflow YAML](#workflow-yaml)
- [Data directory layout](#data-directory-layout)
- [Workflows directory layout](#workflows-directory-layout)
- [Output envelopes](#output-envelopes)
- [Environment variables](#environment-variables)
- [Exit codes](#exit-codes)

## CLI

The single executable is `flow`. Run `flow --help` for the dispatcher
and `flow <command> --help` for a command's full surface. Defaults
are shown where they exist.

`flow --version` prints the installed version. Versions come from
`hatch-vcs`: tagged commits → plain PEP-440 (`0.1.0`); intermediate
commits → `0.1.0.devN+g<sha>` where `N` counts commits since the
last tag and `<sha>` is the short hash. The same value appears in
`uv tool list` and `pip show flowmetrics`.

### `flow serve`

Run the dashboard. Reads only the local Parquet warehouse — never
hits the source API during a request.

| Flag | Default | Notes |
|----|----|----|
| `--port N` | 8000 | TCP port. |
| `--host ADDR` | 127.0.0.1 | Any non-loopback value **requires** `--password`. |
| `--data-dir PATH` | `data` | Parquet warehouse root. |
| `--workflows-dir PATH` | `contracts` | YAMLs + `workflows.db` live here. |
| `--password TEXT` | — | HTTP Basic password. Also reads `$FLOW_PASSWORD`. |
| `--bg / --no-bg` | off | macOS + Linux. Install + start as a persistent native service. Idempotent — re-run to reload. |
| `--stop / --no-stop` | off | With `--bg`: stop + uninstall. Without `--bg`: error (the operator probably typed it wrong). |

Background install:

- **macOS**: writes `~/Library/LaunchAgents/com.flowmetrics.serve.plist`
  with `RunAtLoad=true` + `KeepAlive=true`.
- **Linux**: writes `~/.config/systemd/user/flowmetrics-serve.service`
  with `Restart=on-failure`. Survives logout only with
  `sudo loginctl enable-linger $USER`; the CLI prints the reminder.

Logs land at `<data-dir>/_status/serve.{out,err}.log` on both. Other
platforms raise a clear error pointing at the templated units under
`scripts/scheduling/`.

### `flow materialize NAME`

Fetch + canonicalise one workflow into Parquet. Exits 0 on success.

| Flag | Default | Notes |
|----|----|----|
| `--data-dir PATH` | `data` | Where `work_items/` + `transitions/` + `runs/` land. |
| `--workflows-dir PATH` | `contracts` | YAML lookup root. |
| `--cache-dir PATH` | `.cache/github` | Source-API response cache. |
| `--offline / --online` | online | Offline = cache-only; online = hit API on miss. |
| `--since YYYY-MM-DD` | YAML `start` | Override window start for this run. |
| `--until YYYY-MM-DD` | YAML `stop` | Override window stop for this run. |
| `--status-file PATH` | — | Opt-in JSON `running → done/failed` record for the Data Source page. |

### `flow materialize --all`

Iterate every configured workflow (DB rows + un-migrated YAMLs)
and materialize each. A single failing workflow doesn't block the
rest. Exit code is 0 when at least one workflow succeeded;
per-workflow detail lives in the manifest.

| Flag | Default |
|----|----|
| `--data-dir PATH` | `data` |
| `--workflows-dir PATH` | `contracts` |
| `--cache-dir PATH` | `.cache/github` |
| `--offline / --online` | online |
| `--manifest PATH` | `<data-dir>/_status/daily-<UTC-date>.json` |

### `flow materialize --bg`

Install / uninstall a scheduled materialize job. macOS only (Linux
support uses the templated systemd timer for now).

| Flag | Default | Notes |
|----|----|----|
| `--bg / --no-bg` | off | Install + activate a scheduled job. Requires `--at HH:MM`. |
| `--at HH:MM` | — | Local time the job fires daily (24-hour). |
| `--stop / --no-stop` | off | With `--bg`: uninstall the schedule. Without `--bg`: error. |

The scheduled command captures whatever `NAME | --all`,
`--workflows-dir`, and `--data-dir` you passed at install time;
re-run `flow materialize --bg --at HH:MM …` to reschedule with
different args (idempotent — reloads the plist). Writes a
LaunchAgent at `~/Library/LaunchAgents/com.flowmetrics.materialize.plist`
with `StartCalendarInterval` set to the chosen hour/minute; missed
firings (Mac asleep) run on next wake. Logs at
`<data-dir>/_status/materialize.{out,err}.log`.

### `flow backup`

Snapshot the warehouse (and optionally the config DB) into a single
timestamped `.tar.gz`.

| Flag | Default | Notes |
|----|----|----|
| `--data-dir PATH` | `data` | Warehouse to back up. |
| `--workflows-dir PATH` | — | If given, includes `workflows.db` via SQLite online-backup API. |
| `--output PATH` | `<data-dir>/_backups/flowmetrics-<UTC-timestamp>.tar.gz` | |
| `--include-cache / --no-include-cache` | off | Cache is re-fetchable. |

### `flow restore`

Verify + extract a `flow backup` tarball. SHA-256 of every payload
is verified **before** any byte is written.

| Flag | Default | Notes |
|----|----|----|
| `--input FILE` | required | The `.tar.gz`. |
| `--data-dir PATH` | required | Target for the warehouse. |
| `--workflows-dir PATH` | — | Target for `workflows.db`. Required if the archive carries config or `--config-only` is set. |
| `--force / --no-force` | off | Allow overwriting a non-empty target. |
| `--data-only / --no-data-only` | off | Skip config. |
| `--config-only / --no-config-only` | off | Skip data. |

`--data-only` and `--config-only` are mutually exclusive. Old
(data-only) archives restore with the default invocation; trying
`--config-only` against one is a hard error.

### `flow workflows list`

Read-only enumeration of configured workflows.

| Flag | Default | Notes |
|----|----|----|
| `--workflows-dir PATH` | `contracts` | DB + YAML lookup root. |
| `--data-dir PATH` | — | When given, adds a DATA column showing whether materialize has produced Parquet for each workflow. |
| `--all / --no-all` | off | Include archived rows. |

Output columns: `NAME`, `SOURCE` (`db` = wizard-managed in
`workflows.db`, `yaml` = un-migrated YAML file), `TARGET` (GitHub
`owner/repo` or `JIRA_PROJECT @ jira_url`). Archived rows carry a
`[archived]` suffix.

### `flow metric ...`

Text + JSON metric extraction for agents / headless humans. Every
subcommand needs exactly one of:

- `--workflow-name NAME` — look up a configured workflow in
  `<--workflows-dir>/workflows.db` (DB-first, YAML fallback).
- `--workflow-yaml PATH` — point at a YAML file directly (ad-hoc).

The workflow definition supplies the source (`--repo` equivalent)
AND the stage order — the CLI doesn't accept those inline.

| Subcommand | Purpose | Window flags |
|----|----|----|
| `flow metric throughput` | Daily completion counts in a window | `--start --stop` |
| `flow metric cumulative` | CFD points — state counts over time | `--start --stop` |
| `flow metric aging` | In-flight × state × age + percentiles | `--asof` (defaults to today) |
| `flow metric cycle-time` | Per-item cycle times + P50/P70/P85/P95 | `--start --stop` (defaults to last 30 days) |

### `flow forecast`

Monte Carlo forecasts. Same workflow-pointing pattern as `metric`.
Subcommands name what the answer is.

| Subcommand | Answer | Required |
|----|----|----|
| `flow forecast date` | A date — when will N items be done? | `--items` |
| `flow forecast throughput` | An item count — how many items by a target date? | `--target-date` |

Common flags (apply to `metric` + `forecast`):

| Flag | Notes |
|----|----|
| `--workflow-name NAME` | Stored workflow lookup. |
| `--workflow-yaml PATH` | YAML file path (for ad-hoc queries). |
| `--workflows-dir PATH` | Default `contracts`. Where `--workflow-name` looks. |
| `--cache-dir PATH` | Default `.cache/github`. |
| `--offline / --online` | Cache-only vs. hit-API-on-miss. |
| `--format text\|json` | text=humans (default), json=agents. |

Forecast-only:

| Flag | Notes |
|----|----|
| `--history-start / --history-end` | Training window (UTC). Defaults to a 30-day window ending yesterday. |
| `--runs N` | Monte Carlo iterations. Default 10 000. |
| `--seed N` | Reproducible RNG. |
| `--start-date YYYY-MM-DD` | Forecast horizon start (`forecast date` only). |

The math: [Monte Carlo forecasting](explain/forecasting.md).

## Workflow YAML

One file per workflow, in `--workflows-dir`. The file name is
arbitrary; the `workflow.name` field is the identifier. The first-boot
migration imports YAMLs into `workflows.db` and moves the file to
`migrated/`, so a YAML edit + restart round-trips through the wizard.

```yaml
workflow:
  name: <unique-slug>        # required — used as the workflow id
  source: github | jira      # required
  start: YYYY-MM-DD          # required — window start (UTC)
  stop:  YYYY-MM-DD          # required — window stop (UTC)

  # GitHub
  repo: owner/name           # required when source: github
  wip_labels: [a, b, c]      # optional — PR-label-driven WIP, ordered

  # Jira
  jira_url: https://…        # required when source: jira
  jira_project: KEY          # required when source: jira
```

> **Legacy.** The top-level key was `contract:` in older versions and
> still parses for back-compat. New YAMLs should use `workflow:`.

### WIP rules

- Default (GitHub PR review cycle): `Draft → Awaiting Review →
  Changes Requested → Approved`, derived from `isDraft` +
  `reviewDecision`.
- `wip_labels`: anything carrying one of these labels is in flight.
  Order matters — leftmost = earliest stage, rightmost = most
  progress. First-match wins. See
  [GitHub label-driven CFD and Aging](explain/github-labels.md) for
  the resolution rules.

Worked starters: [`samples/`](../samples/).

## Data directory layout

Everything `flow materialize` writes lands under `--data-dir`:

```
DATA_DIR/
├── work_items/
│   └── contract_id=<name>/year=YYYY/month=MM/day=DD/items-<run>.parquet
├── transitions/
│   └── contract_id=<name>/year=YYYY/month=MM/day=DD/transitions-<run>.parquet
├── runs/
│   └── <contract>/run_id=<…>/manifest.json   # per-run audit trail
├── _status/
│   ├── daily-<UTC-date>.json                 # materialize --all manifest
│   ├── <workflow>.json                       # browser-backfill status
│   ├── launchd.out.log / launchd.err.log     # launchd templates
│   └── serve.out.log  / serve.err.log        # serve unit logs
└── _backups/
    └── flowmetrics-<UTC-timestamp>.tar.gz    # `flow backup` output
```

The `_`-prefixed directories carry meta (manifests, logs, backups) —
`flow backup` skips them.

## Workflows directory layout

```
WORKFLOWS_DIR/
├── *.yaml             # one workflow YAML per file
└── workflows.db       # SQLite — server-managed contract store
```

`workflows.db` is created/managed by `flow serve` (the in-browser
contract editor); the YAMLs are the canonical text source. Both are
included in a `flow backup --workflows-dir …` snapshot.

> **Legacy filename.** Older installs may carry `contracts.db`. It is
> auto-renamed to `workflows.db` on first open.

## Output envelopes

`--format json` always emits a single top-level object with a
versioned `schema` field. Schemas in current use:

| Schema | Source command |
|----|----|
| `flowmetrics.metric.throughput.v1` | `flow metric throughput` |
| `flowmetrics.metric.cumulative.v1` | `flow metric cumulative` |
| `flowmetrics.metric.aging.v1` | `flow metric aging` |
| `flowmetrics.metric.cycle_time.v1` | `flow metric cycle-time` |
| `flowmetrics.forecast.when_done.v1` | `flow forecast date` |
| `flowmetrics.forecast.how_many.v1` | `flow forecast throughput` |
| `flowmetrics.materialize_all.v1` | `flow materialize --all` daily manifest |
| `flowmetrics.backup.v1` | header inside a `flow backup` tarball |
| `flowmetrics.error.v1` | any command on failure |

Common fields across success envelopes:

- `schema` — versioned identifier.
- `input` — every CLI flag the run was invoked with.
- `result` / `summary` / `percentiles` — the answer.
- `training` / `simulation` — provenance for forecast envelopes.
- `chart_data` — enough to reconstruct the chart without the source.
- `interpretation` — `headline`, `key_insight`, `next_actions`,
  `caveats`.
- `logs` — captured stderr + warnings.
- `reproducer` — the exact command to re-run.

Error envelope (`flowmetrics.error.v1`):

```json
{
  "schema": "flowmetrics.error.v1",
  "error": "<one-line message>",
  "hint": "<remediation>",
  "command_to_fix": "<copy-pasteable command>"
}
```

## Environment variables

| Var | Read by | Purpose |
|-----|---------|---------|
| `GITHUB_TOKEN` | All GitHub-touching commands | Direct API token. Overrides `gh auth token`. |
| `FLOW_PASSWORD` | `flow serve` | HTTP Basic password. Required when binding to a non-loopback host. |

Scheduler templates (under `scripts/scheduling/`) use these
additional names — none are read by `flow` itself, only by the
wrapper scripts:

| Var | Used by | Purpose |
|-----|---------|---------|
| `FLOWMETRICS_HOME` | All templates | Install root (holds `contracts/` and `data/`). |
| `FLOWMETRICS_FLOW` | launchd / systemd-serve templates | Absolute path to the `flow` binary. |
| `FLOWMETRICS_VENV` | Legacy templates | Path to a `.venv` (pre-`uv tool install` layout). |
| `FLOWMETRICS_USER` | systemd / cron templates | Unix user the job runs as. |

## Exit codes

| Command | 0 | non-zero |
|----|----|----|
| `flow materialize NAME` | success | any failure |
| `flow materialize --all` | ≥1 workflow succeeded | every workflow failed |
| `flow backup` / `flow restore` | success | malformed archive, checksum mismatch, dirty target without `--force` |
| `flow serve` | clean shutdown (SIGTERM/SIGINT) | bind failure, missing config |
| `flow metric *` / `flow forecast *` | success | source-API failure, invalid input |
| `flow workflows list` | success | bad `--workflows-dir` |

For schedulers wired to alert on non-zero: `materialize --all` is
intentionally lenient — read `_status/daily-<UTC-date>.json` for
per-workflow detail.

## See also

- [Tutorial](tutorial.md) — linear walkthrough from zero to dashboard.
- [How-to guides](howto/) — task-specific recipes.
- [Monte Carlo forecasting](explain/forecasting.md) — what the
  forecast numbers mean.
- [Glossary](glossary.md) — terms; the terms we avoid.
- [Architectural decisions](explain/decisions.md) — trade-offs.
