# How to install and run flowmetrics

## Install

Requires Python 3.13 and [`uv`](https://docs.astral.sh/uv/). Credentials
come from `gh auth token` (run `gh auth login` once) or the
`GITHUB_TOKEN` env var. Anonymous Jira reads work against
`https://issues.apache.org/jira` without credentials.

```
uv sync
```

## Commands

The CLI is `flow`, grouped into five subcommands.

### Flow efficiency

```
# This week (Monday to Sunday)
uv run flow efficiency week --repo astral-sh/uv

# A specific window
uv run flow efficiency week --repo astral-sh/uv \
    --start 2026-05-04 --stop 2026-05-10
```

### Forecast — "when will it be done?"

We say `--items`, not `--backlog`: Vacanti flags "backlog" as
contaminated by Scrum. See [Glossary](GLOSSARY.md).

```
# 50 items to complete, 30 days of training data (defaults).
# Training window defaults: history ends yesterday-UTC, starts 29 days earlier.
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
# Columns come from isDraft + reviewDecision.
uv run flow aging --repo astral-sh/uv \
    --workflow "Draft,Awaiting Review,Changes Requested,Approved"

# GitHub — label-driven mode.
# You name the labels that count as WIP, in order with most progress
# on the right. PR state is materialized from LabeledEvent /
# UnlabeledEvent timestamps; PRs not currently in a WIP column are
# excluded. See docs/SPEC-github-labels.md for the resolution rules.
uv run flow aging --repo dvhthomas/kno \
    --wip-labels "shaping,in-progress,in-review"

# Jira
uv run flow aging --jira-url https://issues.apache.org/jira --jira-project BIGTOP \
    --workflow "Open,In Progress,Patch Available"
```

## Output formats

Every command takes `--format text|json|html`. Default is `text`.

| Format | Audience | Output |
|--------|----------|--------|
| `text` (default) | Humans, terminal | One-line headline by default; `--verbose` adds `rich`-styled tables and the full interpretation. No chart art — charts live in HTML. |
| `json` | Agents, scripts | Schema-versioned envelope. Includes raw data, chart data (so an agent can reason about charts it can't see), and a `cli_invocation` field for provenance. **Stderr + warnings are captured into the `logs` field** so an agent reading only stdout doesn't miss diagnostics. Errors emit a `flowmetrics.error.v1` envelope with `type`, `message`, `hint`, and an optional `command_to_fix`. |
| `html` | Archival | Single-file HTML report (datetime-stamped filename, embedded base64 PNG charts with 50/70/85/95 percentile lines, collapsible per-PR table, reproducible-command block at the top). |

### Agent example

```
uv run flow forecast when-done --repo astral-sh/uv --items 50 \
    --format json | jq '.summary'
```

The JSON envelope includes:

- `schema` — versioned identifier (e.g. `flowmetrics.forecast.when_done.v1`).
- `input` — echo of all parameters.
- `result` / `training` / `simulation` / `percentiles` — the raw data.
- `chart_data` — everything needed to reconstruct charts without seeing the image.
- `interpretation` — `headline`, `key_insight`, `next_actions`, `caveats`.
- `logs` — stderr + warnings captured during the run.
- `docs` — paths to the explainer docs in this repo.

## Testing

Unit tests run by default and never hit the network — a `conftest.py`
fixture asserts on any attempt. Run them as many times as you like:

```
uv run pytest
```

Integration tests are opt-in (require `gh auth login` or
`$GITHUB_TOKEN`) and make real GraphQL calls:

```
uv run pytest -m integration
```

Lint and type check (Astral stack):

```
uv run ruff check
uv run ty check src
```

## Generating fresh samples

```
uv run python scripts/generate_samples.py
```

This script is the source of truth for the `samples/` directory.
Every run makes live GitHub and Jira calls (cached on disk, so reruns
are free), regenerates the per-repo sample files in `samples/`, and
rewrites `samples/index.html`. The pure helpers (repo config, index
template) are unit-tested by `tests/test_samples_helpers.py`.

## Local Pages preview

The published site uses Jekyll via GitHub Pages
(`_config.yml` at repo root). To preview locally requires Ruby and
Bundler:

```
bundle install
bundle exec jekyll serve
```

In practice, the Pages build is fast enough that pushing to `main` and
waiting for the workflow is the simpler iteration loop.
