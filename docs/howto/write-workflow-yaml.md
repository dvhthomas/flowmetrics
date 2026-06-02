---
title: Write a workflow YAML by hand
---

# Write a workflow YAML by hand

> **Diátaxis: How-to.** Scripted / committable workflow setup —
> skips the in-browser wizard.

The wizard's output IS YAML under the hood; hand-authoring just skips
the UI.

One YAML per workflow, in a directory you control (e.g.
`~/flow/contracts/`). On `flow serve` first-boot, any YAMLs in the
workflows-dir are imported into `workflows.db` and moved to
`migrated/` — so YAML-edits round-trip into the wizard.

## Minimal GitHub PR-review workflow

```yaml
workflow:
  name: astral-uv-week
  source: github
  repo: astral-sh/uv
  start: 2026-05-04
  stop:  2026-05-10
```

## Label-driven GitHub workflow

WIP is "anything carrying one of these labels". Order = most progress
wins (first match).

```yaml
workflow:
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

See [GitHub label-driven CFD and Aging](../explain/github-labels.md)
for the resolution rules (most-progress-wins, signal-quality
contract, merged-but-not-shipped).

## Atlassian Jira (anonymous public read)

```yaml
workflow:
  name: cassandra-month
  source: jira
  jira_url:     https://issues.apache.org/jira
  jira_project: CASSANDRA
  start: 2026-04-12
  stop:  2026-05-11
```

## Reference + starters

- Field reference + all valid combinations:
  [Reference § Workflow YAML](../reference.md#workflow-yaml).
- Copy-paste starters: [`samples/`](../../samples/).
