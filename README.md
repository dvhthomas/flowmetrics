# flowmetrics

Vacanti-style flow metrics from **GitHub PR data or Jira issue data**:
portfolio flow efficiency, plus Monte Carlo forecasting for both
*"when will it be done?"* and *"how many items by this date?"*. Output
is renderable as agent-readable JSON, human-readable terminal text
(rich tables), or single-file HTML reports with embedded charts.

```
# GitHub (default)
uv run flow efficiency week --repo astral-sh/uv

# Jira (Apache ASF Jira works anonymously)
uv run flow efficiency week \
    --jira-url https://issues.apache.org/jira --jira-project BIGTOP \
    --start 2026-05-01 --stop 2026-05-10
```

## What it measures

- **Flow efficiency** — `active_time / cycle_time` across merged PRs in a
  date window. Portfolio-level (Vacanti's recipe) — never per-engineer.
- **When-done forecast** — given N items to complete and 30 days of
  recent throughput, simulate 10,000 futures and report 50/70/85/95%
  confidence completion dates.
- **How-many forecast** — given a target date, report the minimum item
  count we can commit to at each confidence level (percentiles read
  **backward**: higher confidence = fewer items).

The full math, assumptions, and limitations are documented in
[`docs/METRICS.md`](docs/METRICS.md) and [`docs/FORECAST.md`](docs/FORECAST.md).
Architectural decisions and known constraints (GitHub API caps, cache
strategy) are in [`docs/DECISIONS.md`](docs/DECISIONS.md). Terminology
follows Vacanti — see [`docs/GLOSSARY.md`](docs/GLOSSARY.md).

## Install

Requires Python 3.13 and [`uv`](https://docs.astral.sh/uv/). Credentials
come from `gh auth token` (run `gh auth login` once) or the
`GITHUB_TOKEN` env var.

```
uv sync
```

## Command usage

The CLI is `flow`, grouped into two top-level commands.

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
contaminated by Scrum. See [`docs/GLOSSARY.md`](docs/GLOSSARY.md).

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

## Output formats

Every command takes `--format text|json|html`. Default is `text`.

| Format | Audience | Output |
|--------|----------|--------|
| `text` (default) | Humans, terminal | `rich`-styled tables + key insights. No chart art — charts live in HTML. |
| `json` | Agents, scripts | Schema-versioned envelope. Includes raw data, chart data (so an agent can reason about charts it can't see), and a `cli_invocation` field for provenance. **Stderr + warnings are captured into the `logs` field** so an agent reading only stdout doesn't miss diagnostics. Errors emit a `flowmetrics.error.v1` envelope with `type`, `message`, `hint`, and an optional `command_to_fix`. |
| `html` | Archival | Single-file HTML report (datetime-stamped filename, embedded base64 PNG charts with 50/70/85/95 percentile lines, collapsible per-PR table, reproducible-command block at the top). |

### Agent example

```
uv run flow forecast when-done --repo astral-sh/uv --items 50 \
    --format json | jq '.percentiles'
```

The JSON envelope includes:

- `schema` — versioned identifier (e.g. `flowmetrics.forecast.when_done.v1`).
- `input` — echo of all parameters.
- `result` / `training` / `simulation` / `percentiles` — the raw data.
- `chart_data` — everything needed to reconstruct charts without seeing the image.
- `interpretation` — `headline`, `key_insight`, `next_actions`, `caveats`.
- `logs` — stderr + warnings captured during the run.
- `docs` — paths to the explainer docs in this repo.

## Sample output

<!-- BEGIN SAMPLES -->
*Last generated: 2026-05-12 18:10 UTC.*

7 public sources covering a spread of team archetypes (GitHub PR data and Apache Jira issue data). Every link below was
produced by running this tool live against the real GitHub API and is
regenerated every time `uv run python scripts/generate_samples.py` runs.

| Repo | Archetype | Efficiency | When-done | How-many |
|------|-----------|------------|-----------|----------|
| `astral-sh/uv` | Fast-moving Rust/Python tooling (GitHub) | [html](samples/astral-sh_uv/efficiency-week.html) · [text](samples/astral-sh_uv/efficiency-week.txt) · [json](samples/astral-sh_uv/efficiency-week.json) | [html](samples/astral-sh_uv/forecast-when-done.html) · [text](samples/astral-sh_uv/forecast-when-done.txt) · [json](samples/astral-sh_uv/forecast-when-done.json) | [html](samples/astral-sh_uv/forecast-how-many.html) · [text](samples/astral-sh_uv/forecast-how-many.txt) · [json](samples/astral-sh_uv/forecast-how-many.json) |
| `pytest-dev/pytest` | Mature Python framework with active maintenance (GitHub) | [html](samples/pytest-dev_pytest/efficiency-week.html) · [text](samples/pytest-dev_pytest/efficiency-week.txt) · [json](samples/pytest-dev_pytest/efficiency-week.json) | [html](samples/pytest-dev_pytest/forecast-when-done.html) · [text](samples/pytest-dev_pytest/forecast-when-done.txt) · [json](samples/pytest-dev_pytest/forecast-when-done.json) | [html](samples/pytest-dev_pytest/forecast-how-many.html) · [text](samples/pytest-dev_pytest/forecast-how-many.txt) · [json](samples/pytest-dev_pytest/forecast-how-many.json) |
| `huggingface/transformers` | ML library, mixed community + maintainer flow (GitHub) | [html](samples/huggingface_transformers/efficiency-week.html) · [text](samples/huggingface_transformers/efficiency-week.txt) · [json](samples/huggingface_transformers/efficiency-week.json) | [html](samples/huggingface_transformers/forecast-when-done.html) · [text](samples/huggingface_transformers/forecast-when-done.txt) · [json](samples/huggingface_transformers/forecast-when-done.json) | [html](samples/huggingface_transformers/forecast-how-many.html) · [text](samples/huggingface_transformers/forecast-how-many.txt) · [json](samples/huggingface_transformers/forecast-how-many.json) |
| `pre-commit/pre-commit` | Developer-tooling Python project (GitHub) | [html](samples/pre-commit_pre-commit/efficiency-week.html) · [text](samples/pre-commit_pre-commit/efficiency-week.txt) · [json](samples/pre-commit_pre-commit/efficiency-week.json) | [html](samples/pre-commit_pre-commit/forecast-when-done.html) · [text](samples/pre-commit_pre-commit/forecast-when-done.txt) · [json](samples/pre-commit_pre-commit/forecast-when-done.json) | [html](samples/pre-commit_pre-commit/forecast-how-many.html) · [text](samples/pre-commit_pre-commit/forecast-how-many.txt) · [json](samples/pre-commit_pre-commit/forecast-how-many.json) |
| `CalcMark/go-calcmark` | Custom request: Go computational-document tool (GitHub) | [html](samples/CalcMark_go-calcmark/efficiency-week.html) · [text](samples/CalcMark_go-calcmark/efficiency-week.txt) · [json](samples/CalcMark_go-calcmark/efficiency-week.json) | [html](samples/CalcMark_go-calcmark/forecast-when-done.html) · [text](samples/CalcMark_go-calcmark/forecast-when-done.txt) · [json](samples/CalcMark_go-calcmark/forecast-when-done.json) | [html](samples/CalcMark_go-calcmark/forecast-how-many.html) · [text](samples/CalcMark_go-calcmark/forecast-how-many.txt) · [json](samples/CalcMark_go-calcmark/forecast-how-many.json) |
| `ASF/CASSANDRA` | Apache Cassandra — active distributed-database project (Jira) | [html](samples/ASF_CASSANDRA/efficiency-week.html) · [text](samples/ASF_CASSANDRA/efficiency-week.txt) · [json](samples/ASF_CASSANDRA/efficiency-week.json) | [html](samples/ASF_CASSANDRA/forecast-when-done.html) · [text](samples/ASF_CASSANDRA/forecast-when-done.txt) · [json](samples/ASF_CASSANDRA/forecast-when-done.json) | [html](samples/ASF_CASSANDRA/forecast-how-many.html) · [text](samples/ASF_CASSANDRA/forecast-how-many.txt) · [json](samples/ASF_CASSANDRA/forecast-how-many.json) |
| `ASF/BIGTOP` | Apache Bigtop — smaller-team build/packaging project (Jira) | [html](samples/ASF_BIGTOP/efficiency-week.html) · [text](samples/ASF_BIGTOP/efficiency-week.txt) · [json](samples/ASF_BIGTOP/efficiency-week.json) | [html](samples/ASF_BIGTOP/forecast-when-done.html) · [text](samples/ASF_BIGTOP/forecast-when-done.txt) · [json](samples/ASF_BIGTOP/forecast-when-done.json) | [html](samples/ASF_BIGTOP/forecast-how-many.html) · [text](samples/ASF_BIGTOP/forecast-how-many.txt) · [json](samples/ASF_BIGTOP/forecast-how-many.json) |

Full overview: [samples/index.html](samples/index.html).
<!-- END SAMPLES -->

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

This script is the source of truth for the `samples/` directory and the
"Sample output" section above. Every run makes live GitHub calls
(cached on disk, so reruns are free), regenerates the per-repo sample
files in `samples/`, rewrites `samples/index.html`, and overwrites the
README's samples section. The pure helpers (repo config, index
template, README rewrite) are unit-tested by `tests/test_samples_helpers.py`.
