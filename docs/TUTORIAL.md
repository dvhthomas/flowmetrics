# Tutorial — your first dashboard in 5 minutes

This walks you from a clean machine to a working dashboard, all
through the browser. One linear path, copy-paste-able.

For task-specific recipes (scheduling, backup, persistent server,
Docker, scripting YAML by hand) see [HOWTO.md](HOWTO.md). For full
CLI + file-layout detail see [REFERENCE.md](REFERENCE.md).

## 1. Install `uv`

`uv` is a Python toolchain installer. It handles Python itself,
dependencies, and the install of flowmetrics as a global command.

| Platform | One-liner |
|----|----|
| macOS | `brew install uv` |
| Linux | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Windows (PowerShell) | `irm https://astral.sh/uv/install.ps1 \| iex` |

`uv --version` should now print something.

## 2. Install flowmetrics

```bash
uv tool install git+https://github.com/dvhthomas/flowmetrics
```

That puts a `flow` binary on your PATH (`~/.local/bin/flow` on
macOS/Linux). Confirm:

```bash
flow --version
# → flow 0.1.0   (or 0.1.0.devN+gSHA for an intermediate commit)
```

> Prefer a source checkout? `git clone` the repo, then `uv tool
> install --force .` from inside it. Either path ends with the same
> global `flow` command.

## 3. Sign in to GitHub (one minute)

`flow` reads PRs and issues via the GitHub API, which needs a token.
Easiest path:

```bash
gh auth login       # interactive — pick GitHub.com, HTTPS, browser
```

Or set `GITHUB_TOKEN` directly to a fine-grained PAT with **public
repo read** scope. Public Jira (e.g. Apache) needs no token.

## 4. Start the dashboard

```bash
flow serve
# → http://127.0.0.1:8000
```

That's it — `flow` creates the `contracts/` and `data/` directories
on demand. On macOS, swap `flow serve` for `flow serve --bg` to
install it as a persistent LaunchAgent that survives logout and
reboot ([details](HOWTO.md#run-as-a-persistent-web-server)).

## 5. Add a workflow in the browser

Open http://127.0.0.1:8000. The home page shows **No workflows yet**
and a **+ New workflow** button. Click it.

The wizard walks you through:

1. **Name + display label** — short slug for routing (`astral-uv`)
   and a friendlier label (`Astral uv`).
2. **Source** — pick GitHub or Jira.
3. **Repo / project** — `astral-sh/uv` for GitHub; `<jira_url>` +
   project key for Jira. The wizard probes the source to discover
   labels and stages so it can offer the right options at the next
   step.
4. **Stages** — for GitHub, choose between the default review cycle
   (Draft → Awaiting Review → Changes Requested → Approved) and
   label-driven mode (drag your PR labels into order, leftmost =
   earliest stage). For Jira, pick from the project's actual
   statuses.
5. **Window** — start + stop dates. Defaults are sensible; widen
   later if you want more history.

Hit **Save**. The wizard writes to `contracts.db` and bounces you to
the workflow's dashboard.

## 6. Fetch the data

The dashboard shows the right shape but empty cards until the
warehouse is populated. Click **Data source** (top-right).

You'll see a coverage map for your window. Click **Backfill** to
materialise the workflow. The page polls `flow materialise`'s
status file under the hood; the bar fills as work_items + transitions
land in `data/`. A few seconds for a small window.

## 7. Browse

Click the workflow name to return to the dashboard. You'll see:

- **Aging WIP** — every in-flight item by current state × age, pinned
  to the most recent materialise.
- **Cycle Time / Throughput / CFD / Forecast** — driven by the Period
  picker (top of the page).

Hover any dot for the underlying item + a link to its GitHub / Jira
page.

## Next steps

- **Schedule periodic fetches** so the warehouse stays fresh →
  [HOWTO § Schedule data fetches](HOWTO.md#schedule-data-fetches).
- **Run the dashboard as a persistent service** (login-independent
  on macOS, systemd on Linux) →
  [HOWTO § Run as a persistent web server](HOWTO.md#run-as-a-persistent-web-server).
- **Back up + restore** your warehouse and config →
  [HOWTO § Back up](HOWTO.md#back-up--restore).
- **Ad-hoc reports** for terminals / agents (`flow forecast`,
  `flow efficiency`, …) → [HOWTO § Ad-hoc CLI reports](HOWTO.md#ad-hoc-cli-reports).
- **Define workflows from YAML** (scripted / committable, instead of
  the wizard) → [HOWTO § Write a workflow YAML by hand](HOWTO.md#write-a-workflow-yaml-by-hand).
