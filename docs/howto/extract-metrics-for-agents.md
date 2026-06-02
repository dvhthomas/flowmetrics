---
title: Extract metrics for agents
---

# Extract metrics for agents

> **Diátaxis: How-to.** Text + JSON metric extraction for terminals,
> pipelines, and agents. No charts — for those, use the web UI.

The CLI is intentionally graphics-free. **For charts, use the web UI
(`flow serve`)**.

Every metric command takes either `--workflow-name NAME` (look up a
configured workflow in the store) or `--workflow-yaml PATH` (point at
a YAML file directly). The workflow definition supplies the source
(GitHub repo or Jira project) AND the stage order — you never repeat
those inline.

## Metrics

```bash
# Daily completion counts.
flow metric throughput \
    --workflow-name astral-uv \
    --workflows-dir ~/flow/contracts \
    --start 2026-05-04 --stop 2026-05-10

# Cumulative Flow Diagram data (state counts over time).
flow metric cumulative \
    --workflow-name astral-uv \
    --workflows-dir ~/flow/contracts \
    --start 2026-05-04 --stop 2026-05-10

# In-flight items × current state × age + percentile thresholds.
flow metric aging \
    --workflow-name astral-uv \
    --workflows-dir ~/flow/contracts

# Per-item cycle times + P50/P70/P85/P95.
flow metric cycle-time \
    --workflow-name astral-uv \
    --workflows-dir ~/flow/contracts \
    --start 2026-05-04 --stop 2026-05-10
```

## Ad-hoc against an un-stored YAML

For a workflow that isn't in your store, use `--workflow-yaml`
instead:

```bash
flow metric cycle-time \
    --workflow-yaml ./my-demo-workflow.yaml \
    --start 2026-05-04 --stop 2026-05-10
```

## Monte Carlo forecasts

Same `--workflow-name` / `--workflow-yaml` pattern:

```bash
# Date forecast: when will 50 items be done?
flow forecast date --workflow-name astral-uv --items 50

# Throughput forecast: how many items by 2026-06-30?
flow forecast throughput --workflow-name astral-uv --target-date 2026-06-30
```

`flow forecast date` names what the answer is — a date.
`flow forecast throughput` likewise — an item-count rate.

Background on the math: [Monte Carlo
forecasting](../explain/forecasting.md).

## Output format

Every command takes `--format text|json` (default `text`).

```bash
flow metric cycle-time \
    --workflow-name astral-uv \
    --workflows-dir ~/flow/contracts \
    --start 2026-05-04 --stop 2026-05-10 \
    --format json \
    | jq '.percentiles_days'
```

## JSON envelopes

Every JSON envelope carries a versioned `schema` field:

- `flowmetrics.metric.throughput.v1`
- `flowmetrics.metric.cumulative.v1`
- `flowmetrics.metric.aging.v1`
- `flowmetrics.metric.cycle_time.v1`
- `flowmetrics.forecast.when_done.v1` (`flow forecast date`)
- `flowmetrics.forecast.how_many.v1` (`flow forecast throughput`)

Plus `input` (every flag), `summary` (key numbers), the raw data,
and a one-line `headline`. Field-by-field detail:
[Reference § Output envelopes](../reference.md#output-envelopes).

## Next

- [Reference § CLI](../reference.md#cli) — every flag.
- [Monte Carlo forecasting](../explain/forecasting.md) — what the
  forecast numbers mean and how to read them.
