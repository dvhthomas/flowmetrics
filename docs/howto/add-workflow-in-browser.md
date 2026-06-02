---
title: Add a workflow in the browser
---

# Add a workflow in the browser

> **Diátaxis: How-to.** The simplest path to a configured workflow.
> No YAML editing.

Start the dashboard, click **+ New workflow**, fill in the wizard.

```bash
flow serve            # or `flow serve --bg` on macOS / Linux
# → http://127.0.0.1:8000
```

The wizard probes your source (GitHub repo or Jira project) to
auto-suggest labels / statuses, lets you pick stages, and writes to
`<workflows-dir>/workflows.db`. Walkthrough with screenshots:
[Tutorial § 5](../tutorial.md#5-add-a-workflow-in-the-browser).

Once saved, hit **Data source** → **Backfill** to materialize.

## Editing later

Re-open the wizard from the workflow dashboard's gear icon. Edits
round-trip through `workflows.db` — same precedence applies as for
hand-authored YAML (DB row wins when both exist for the same name).

## Next

- [Fetch data once](fetch-data.md)
- [Schedule data fetches](schedule-fetches.md)
- [Write a workflow YAML by hand](write-workflow-yaml.md) (for
  scripted / committable setups)
