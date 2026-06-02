---
title: Explanation
---

# Explanation

> **Diátaxis: Explanation.** Discussion of design and trade-offs.
> Read these to understand *why* — not to learn how to use the tool.

## Math

- **[Monte Carlo forecasting](forecasting.md)** — `flow forecast date`
  and `flow forecast throughput`. The simulator, the percentile
  flip, what we assume, and how to read the output.

## Architecture and trade-offs

- **[Architectural decisions and known constraints](decisions.md)** —
  GitHub API caps, cache strategy, WIP-tracking source scope, "why
  PRs only on GitHub".
- **[GitHub label-driven CFD and Aging](github-labels.md)** — design
  spec for the `wip_labels` mode. Resolution rules, signal-quality
  contract, why merged-but-not-shipped is a wanted signal.

## Visual reference

- **[Screenshots](screenshots.md)** — what the dashboard looks like
  against two real data sources.

## See also

- [Tutorial](../tutorial.md) — learn the tool.
- [How-to guides](../howto/) — solve a specific task.
- [Reference](../reference.md) — canonical facts.
