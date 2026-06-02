---
title: flowmetrics docs
---

# flowmetrics documentation

Organised by [Diátaxis](https://diataxis.fr/): four kinds of doc for
four different needs. Pick the entry point that matches what you're
trying to do.

| If you want to… | Read | Type |
|----|----|----|
| Learn the tool end-to-end | [Tutorial](tutorial.md) | Tutorial |
| Solve one specific task | [How-to guides](howto/) | How-to |
| Look up a flag, file, or schema | [Reference](reference.md) | Reference |
| Understand why it's built this way | [Explanation](explain/) | Explanation |

## Tutorial — learn

- **[Tutorial — your first dashboard in 5 minutes](tutorial.md)** —
  install, configure a workflow, materialize, browse. One linear
  path; copy-pasteable.

## How-to guides — solve a problem

Task-specific recipes. Each page is self-contained — read just the
one you need.

- [Install on macOS, Linux, Windows](howto/install.md)
- [Add a workflow in the browser](howto/add-workflow-in-browser.md)
- [Write a workflow YAML by hand](howto/write-workflow-yaml.md)
- [Fetch data once](howto/fetch-data.md)
- [Schedule data fetches](howto/schedule-fetches.md)
- [Run the dashboard locally](howto/run-dashboard-locally.md)
- [Run as a persistent web server](howto/run-as-persistent-server.md)
- [Back up and restore](howto/backup-and-restore.md)
- [Deploy with Docker](howto/deploy-with-docker.md)
- [Extract metrics for agents](howto/extract-metrics-for-agents.md)
- [Upgrade](howto/upgrade.md)
- [Develop against a source checkout](howto/develop-from-source.md)
- [Troubleshooting](howto/troubleshooting.md)

[Full how-to index →](howto/)

## Reference — canonical facts

Dense, fact-oriented, no opinions.

- **[CLI, YAML, file layout, output envelopes](reference.md)** — every
  command, every flag, every file the warehouse touches, every JSON
  envelope schema.
- **[Glossary](glossary.md)** — terms; the terms we deliberately
  avoid (Scrum-contaminated "backlog" and "velocity").

## Explanation — understand the design

Discussion of trade-offs and the math behind the numbers.

- **[Monte Carlo forecasting](explain/forecasting.md)** — `flow
  forecast date` and `flow forecast throughput`, with worked
  examples.
- **[Architectural decisions and known constraints](explain/decisions.md)**
  — GitHub API caps, cache strategy, WIP-tracking source scope.
- **[GitHub label-driven CFD and Aging](explain/github-labels.md)** —
  design rationale + signal-quality contract.
- **[Screenshots](explain/screenshots.md)** — what the dashboard
  looks like against two real data sources.

[Full explanation index →](explain/)
