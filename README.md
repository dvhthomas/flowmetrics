# flowmetrics

A demo-quality tool for **flow metrics and Monte Carlo forecasting**,
against GitHub PR data or Apache Jira issue data. Built on the kanban
flow-metrics framework as laid out in Daniel Vacanti's
[*Actionable Agile Metrics*](https://leanpub.com/actionableagilemetrics)
and [*When Will It Be Done?*](https://leanpub.com/whenwillitbedone) —
assumptions are surfaced on the page itself, not buried in docs.

**Live site:** <https://dvhthomas.github.io/flowmetrics/> · 
**Browse live sample reports →** <https://dvhthomas.github.io/flowmetrics/samples/>

## What it looks like

A Cumulative Flow Diagram against Apache CASSANDRA's Jira changelog —
113 issues, seven workflow states stacked by the standard CFD properties:

[![Cumulative Flow Diagram for Apache CASSANDRA](samples/preview.png)](https://dvhthomas.github.io/flowmetrics/samples/ASF_CASSANDRA/cfd.html)

### **[Browse all seven sample reports →](https://dvhthomas.github.io/flowmetrics/samples/)**

Seven public sources — five GitHub repos (`astral-sh/uv`,
`pytest-dev/pytest`, `huggingface/transformers`, `pre-commit/pre-commit`,
`CalcMark/go-calcmark`) and two Apache Jira projects (`CASSANDRA`,
`BIGTOP`) — each rendered as HTML, plain text, and agent-readable JSON.

## Two ways to use it

**Interactive dashboard** — point at a workflow YAML, materialise into
a local Parquet warehouse, browse charts in your browser. Period picker
drives Throughput, Cycle Time, CFD, and Forecast; Aging WIP is pinned to
the latest data.

```
uv run flow materialise astral-uv-week --workflows-dir contracts/
uv run flow serve --workflows-dir contracts/
# → http://127.0.0.1:8000
```

**Ad-hoc reports** — one-shot CLI for terminal pipelines, static HTML
exports, or agent consumption (`--format json`).

```
$ uv run flow efficiency --repo astral-sh/uv
Portfolio flow efficiency for astral-sh/uv May 5 → May 11:
8.4% across 46 completed items.

$ uv run flow forecast when-done --repo astral-sh/uv --items 50 --format json \
    | jq '.summary.percentiles'
{"50": "2026-05-19", "70": "2026-05-21", "85": "2026-05-23", "95": "2026-05-26"}
```

See **[How to install and run](docs/HOWTO.md)** for the full walkthrough.

## What you get

- **Cycle Time** — scatterplot of completed items with empirical P50/P85/P95.
- **Throughput** — daily completion counts with an empirical P50/P85
  reference band (toggle: include weekends / weekdays only).
- **Cumulative Flow Diagram** — the six standard CFD properties laid out
  honestly: arrivals on top, departures on bottom, vertical distance =
  WIP, slope = arrival rate.
- **Aging WIP** — every in-flight item plotted by current workflow state
  × age (CD − SD + 1), with completed-item percentile lines as risk
  thresholds.
- **Forecasts** — Monte Carlo *when-done* (date for N items) and
  *how-many* (items by target date) at 50/70/85/95% confidence.
- **Flow efficiency** — Portfolio FE (`Σ active / Σ cycle`) across
  merged items in a window. System-level, never per-engineer.

## Why this exists

Most flow-metrics products implement the kanban-flow toolkit partially
or with subtle distortions — a "flow efficiency" that's actually mean
per-PR, a CFD that smooths over the vertical-distance-equals-WIP
property, an Aging chart with percentile lines drawn from arbitrary
windows. This project implements the math honestly, surfaces the
assumptions on the rendered page itself rather than hiding them in
docs, and links out to the canonical references where a number could
be misread. It's a learning artifact, not a product.

## Documentation

- **[How to install and run](docs/HOWTO.md)** — install, the dashboard
  workflow, ad-hoc CLI commands, output formats, testing.
- **[Operations](docs/OPERATIONS.md)** — scheduled ingest on every
  major OS, backup + restore, Docker + GH Actions, troubleshooting.
- **[Metrics](docs/METRICS.md)** — how cycle / active / wait time and
  flow efficiency are computed; the clustering algorithm; assumptions.
- **[Forecasting](docs/FORECAST.md)** — Monte Carlo when-done and
  how-many, with worked examples.
- **[Decisions](docs/DECISIONS.md)** — architectural trade-offs and
  known constraints (GitHub API caps, cache strategy, WIP-tracking
  source scope).
- **[Glossary](docs/GLOSSARY.md)** — terms and definitions; the terms
  we deliberately avoid (Scrum-contaminated "backlog" and "velocity");
  a concrete Portfolio-FE-vs-mean-FE worked example.
