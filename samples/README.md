# Sample workflow YAMLs

Copy-paste starters. Drop one into your `contracts/` directory, edit
the fields, then `flow materialise <name>` followed by `flow serve`.

| File | Source | Pattern |
|------|--------|---------|
| [github-pr-review-cycle.yaml](github-pr-review-cycle.yaml) | GitHub | PR review cycle (`Draft → Awaiting Review → Changes Requested → Approved → Merged`). The default for a typical OSS or corporate repo. |
| [github-pr-labels.yaml](github-pr-labels.yaml) | GitHub | Label-driven workflow — useful when the team uses PR labels (`in-review`, `qa-pending`, etc.) as stage markers. |
| [github-with-explicit-states.yaml](github-with-explicit-states.yaml) | GitHub | Same as PR review cycle but with an explicit `states:` block pinning the order — gives reproducible chart bands when the data is sparse. |
| [jira-bigtop.yaml](jira-bigtop.yaml) | Jira | Apache's public Jira instance, BIGTOP project (anonymous reads). Small project, all six standard workflow states. |
| [jira-cassandra.yaml](jira-cassandra.yaml) | Jira | Apache's public Jira instance, CASSANDRA project. Larger project with richer workflow (`Patch Available`, `Resolved`, `Closed`). |

## What the fields mean

Every contract takes these keys:

| Key | Required | What |
|-----|----------|------|
| `name` | yes | Routing slug. Lowercase + dashes. Must match the YAML filename stem. |
| `label` | no | Human-friendly display name shown in the dashboard's breadcrumb. Falls back to `name`. |
| `source` | yes | `github` or `jira`. |
| `repo` | github only | `owner/name`, e.g. `astral-sh/uv`. |
| `jira_url` + `jira_project` | jira only | The Jira base URL and the project key. |
| `start` / `stop` | recommended | YYYY-MM-DD window. Materialise refuses to run without explicit dates today (a later slice will relax this). |
| `states` | no | Three-category classification of workflow states. When omitted, stages are inferred from data via pairwise precedence. |

## See also

- [Tutorial](../docs/TUTORIAL.md) — install → materialise → serve in
  one linear walkthrough.
- [How-to guides](../docs/HOWTO.md) — scheduling, backup, persistent
  server, Docker.
- [Reference](../docs/REFERENCE.md) — every CLI flag and the full
  workflow YAML schema.
