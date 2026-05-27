# How to install and run flowmetrics

## Install

Requires Python 3.13 and [`uv`](https://docs.astral.sh/uv/). Credentials
come from `gh auth token` (run `gh auth login` once) or the
`GITHUB_TOKEN` env var. Atlassian Jira reads use the instance's own
auth; Apache's public instance at `https://issues.apache.org/jira`
serves anonymous reads without credentials.

```
uv sync
```

## Interactive dashboard

The primary workflow. Define a *workflow YAML*, materialise it into a
local Parquet warehouse, and browse the dashboard in your browser.

### 1. Write a workflow YAML

One YAML per workflow, in a directory you control. Minimal GitHub
example (`contracts/astral-uv-week.yaml`):

```yaml
contract:
  name: astral-uv-week
  source: github
  repo: astral-sh/uv
  start: 2026-05-04
  stop: 2026-05-10
```

Jira works the same with `source: jira`, `jira_url:`, and
`jira_project:`. Label-driven GitHub PR stitching uses a `wip_labels:`
list — see [docs/SPEC-github-labels.md](SPEC-github-labels.md) for the
resolution rules.

### 2. Materialise the warehouse

```
uv run flow materialise astral-uv-week \
    --workflows-dir contracts/ \
    --data-dir data/
```

Fetches from the source API (cached on disk), canonicalises, and writes
Parquet under `data/`. Cron-friendly — exits 0 on success and writes a
JSON status file when `--status-file` is set.

### 3. Serve the dashboard

```
uv run flow serve --workflows-dir contracts/ --data-dir data/
# → http://127.0.0.1:8000
```

`--port` picks an alternate port if 8000 is busy. `--host 0.0.0.0`
binds publicly and requires `--password` (or `$FLOW_PASSWORD`).

The dashboard splits into two sections:

- **Current state** — Aging WIP, pinned to the most recent materialise
  date. The Period picker doesn't apply.
- **Time slice** — Throughput, Cycle Time, Cumulative Flow, and Forecast,
  all driven by the Period picker (last 7/14/30/90 days, last
  week/2-weeks, all time, or custom). Throughput shows an empirical
  P50/P85 reference band with an "Include weekends / Weekdays only"
  toggle.

The **Data source** page (top-right link) shows coverage and lets you
backfill date ranges from the browser.

## Ad-hoc CLI reports

The same metrics as one-shot commands — useful for terminals,
pipelines, static HTML exports, and agent consumption.

### Flow efficiency

```
# This week (Monday to Sunday)
uv run flow efficiency --repo astral-sh/uv

# A specific window
uv run flow efficiency --repo astral-sh/uv \
    --start 2026-05-04 --stop 2026-05-10
```

### Forecast — "when will it be done?"

We say `--items`, not `--backlog` — the word is Scrum-loaded. See
[Glossary](GLOSSARY.md).

```
uv run flow forecast when-done --repo astral-sh/uv --items 50

# Explicit training window + deterministic seed
uv run flow forecast when-done --repo astral-sh/uv \
    --items 50 \
    --history-start 2026-04-11 --history-end 2026-05-10 \
    --start-date 2026-05-11 \
    --runs 10000 --seed 42
```

### Forecast — "how many items by this date?"

```
uv run flow forecast how-many --repo astral-sh/uv \
    --target-date 2026-06-30
```

### Cumulative Flow Diagram

```
uv run flow cfd --repo astral-sh/uv \
    --start 2026-04-12 --stop 2026-05-11 \
    --workflow "Open,Merged"

# Jira — richer workflow
uv run flow cfd --jira-url https://issues.apache.org/jira --jira-project BIGTOP \
    --start 2026-04-12 --stop 2026-05-11 \
    --workflow "Open,In Progress,Patch Available,Resolved"
```

### Aging Work In Progress

```
# GitHub — review-cycle mode (default).
uv run flow aging --repo astral-sh/uv \
    --workflow "Draft,Awaiting Review,Changes Requested,Approved"

# GitHub — label-driven mode.
uv run flow aging --repo dvhthomas/kno \
    --wip-labels "shaping,in-progress,in-review"

# Jira
uv run flow aging --jira-url https://issues.apache.org/jira --jira-project BIGTOP \
    --workflow "Open,In Progress,Patch Available"
```

## Output formats

Every CLI command takes `--format text|json|html`. Default is `text`.

| Format | Audience | Output |
|--------|----------|--------|
| `text` (default) | Humans, terminal | One-line headline; `--verbose` adds tables and interpretation. |
| `json` | Agents, scripts | Schema-versioned envelope: raw data, chart data, captured stderr, reproducer command. Errors emit a `flowmetrics.error.v1` envelope with `hint` and `command_to_fix`. |
| `html` | Archival | Single-file HTML report — embedded charts with 50/70/85/95 percentile lines, collapsible per-item table, reproducer at the top. |

### Agent example

```
uv run flow forecast when-done --repo astral-sh/uv --items 50 \
    --format json | jq '.summary'
```

The JSON envelope includes:

- `schema` — versioned identifier (e.g. `flowmetrics.forecast.when_done.v1`).
- `input` / `result` / `training` / `simulation` / `percentiles` — raw data.
- `chart_data` — everything needed to reconstruct charts.
- `interpretation` — `headline`, `key_insight`, `next_actions`, `caveats`.
- `logs` — stderr + warnings captured during the run.

## Testing

Unit tests run by default and never hit the network — a `conftest.py`
fixture asserts on any attempt.

```
uv run pytest
```

Integration tests are opt-in (require `gh auth login` or `$GITHUB_TOKEN`):

```
uv run pytest -m integration
```

Lint and type check:

```
uv run ruff check
uv run ty check src
```

## Generating fresh samples

```
uv run python scripts/generate_samples.py
```

Source of truth for the `samples/` directory. Every run makes live
GitHub and Jira calls (cached on disk), regenerates the per-repo sample
files, and rewrites `samples/index.html`.

## Local Pages preview

The published site uses Jekyll via GitHub Pages (`_config.yml` at repo
root). To preview locally:

```
bundle install
bundle exec jekyll serve
```

In practice, pushing to `main` and waiting for the Pages workflow is the
simpler iteration loop.
